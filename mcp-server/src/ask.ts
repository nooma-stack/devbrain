// `ask`: a typed question in front of deep_search's retrieval pipeline.
//
// Retrieval only and deterministic: no model writes the answer. The question
// kind picks which memory kinds are searched and how they are ranked, the
// supersedes / refined_by graph is followed so the CURRENT version is cited,
// and the response is the top 1-3 memories, delimited as untrusted data,
// with their dates, the edge notes and the weak-match flag.
//
// Reuses deep_search's building blocks (hybrid.ts, rerank.ts, recency.ts,
// the shared SQL files) without changing them; deep_search itself is not
// touched. Flag: env DEVBRAIN_ASK / yaml ask.enabled (default on).

import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { readFileSync } from 'fs'
import { resolve } from 'path'
import { parse } from 'yaml'
import { z } from 'zod'
import { getSearchConfig, query, queryWithTimeout } from './db.js'
import { embedQuery, toSqlVector } from './embeddings.js'
import {
  applyRerank,
  collapseNearDuplicates,
  groupStrength,
  isWeak,
  keywordParams,
  keywordSql,
  nearDupSql,
  rerankDocText,
  responseStrength,
  rrfFuse,
  type MatchStrength,
  type SearchConfig,
  WEAK_MATCH_NOTE,
} from './hybrid.js'
import { rerankScores } from './rerank.js'
import { screeningLabel, untrustedText } from './untrusted.js'

export const ASK_KINDS = ['decision', 'status', 'howto', 'why', 'who_when', 'issue'] as const
export type AskKind = (typeof ASK_KINDS)[number]

export interface KindProfile {
  /** memory.kind values searched. */
  kinds: string[]
  /** Preference per memory kind, 0..1 (1 = the kind this question wants). */
  prefer: Record<string, number>
  /** Score added for prefer = 1 (scaled by prefer). Relevance is 0..1. */
  priorBonus: number
  /** Score added for a memory written now, decaying with recencyHalfLifeDays. */
  recencyBonus: number
  recencyHalfLifeDays: number
  /** issue: extra bonus for rows that record a cause and a fix. */
  rootCauseBonus?: number
}

// Hand-set, then checked on the recall_audit gold set (see the PR). Answers
// cite distilled memories, so raw transcript chunks are not searched.
// Bonuses are small next to relevance (a reranker score, or cosine when the
// reranker is unavailable): they reorder near-ties toward the kind the
// question asks for and, for status, toward recent memories; they do not
// promote an unrelated memory over a relevant one.
export const PROFILES: Record<AskKind, KindProfile> = {
  decision: {
    kinds: ['decision', 'pattern', 'issue', 'session_summary'],
    prefer: { decision: 1, pattern: 0.5, issue: 0.3, session_summary: 0.3 },
    priorBonus: 0.05,
    recencyBonus: 0.02,
    recencyHalfLifeDays: 180,
  },
  why: {
    kinds: ['decision', 'pattern', 'issue', 'session_summary'],
    prefer: { decision: 1, session_summary: 0.6, pattern: 0.5, issue: 0.3 },
    priorBonus: 0.05,
    recencyBonus: 0.02,
    recencyHalfLifeDays: 180,
  },
  howto: {
    kinds: ['pattern', 'decision', 'issue', 'session_summary'],
    prefer: { pattern: 1, decision: 0.6, issue: 0.3, session_summary: 0.3 },
    priorBonus: 0.05,
    recencyBonus: 0.02,
    recencyHalfLifeDays: 180,
  },
  issue: {
    kinds: ['issue', 'pattern', 'decision', 'session_summary'],
    prefer: { issue: 1, pattern: 0.5, session_summary: 0.5, decision: 0.3 },
    priorBonus: 0.05,
    recencyBonus: 0.02,
    recencyHalfLifeDays: 180,
    rootCauseBonus: 0.02,
  },
  status: {
    kinds: ['session_summary', 'session_breadcrumb', 'decision', 'issue', 'pattern'],
    prefer: { session_summary: 1, session_breadcrumb: 1, decision: 0.3, issue: 0.3, pattern: 0.3 },
    priorBonus: 0.05,
    recencyBonus: 0.1,
    recencyHalfLifeDays: 21,
  },
  who_when: {
    kinds: ['session_summary', 'session_breadcrumb', 'decision', 'issue', 'pattern'],
    prefer: { session_summary: 1, session_breadcrumb: 0.7, decision: 0.6, issue: 0.4, pattern: 0.3 },
    priorBonus: 0.05,
    recencyBonus: 0.03,
    recencyHalfLifeDays: 90,
  },
}

// ─── Flag ───────────────────────────────────────────────────────────────────

let cachedYamlAsk: boolean | null = null

/** env DEVBRAIN_ASK > yaml ask.enabled > on. */
export function askEnabled(): boolean {
  const off = (v: unknown) => v === false || /^(off|0|false|no)$/i.test(String(v ?? '').trim())
  const env = process.env.DEVBRAIN_ASK
  if (env !== undefined) return !off(env)
  if (cachedYamlAsk === null) {
    try {
      const configPath =
        process.env.DEVBRAIN_CONFIG || resolve(import.meta.dirname, '../../config/devbrain.yaml')
      const v = (parse(readFileSync(configPath, 'utf-8')) ?? {})?.ask?.enabled
      cachedYamlAsk = v === undefined || v === null ? true : !off(v)
    } catch {
      cachedYamlAsk = true
    }
  }
  return cachedYamlAsk
}

// ─── Pure ranking helpers (unit-tested) ─────────────────────────────────────

export interface PoolRow {
  key: string
  kind: string
  createdAt: number
  hasRootCause: boolean
}

/** Kind + recency + root-cause bonus for one row. */
export function bonus(row: PoolRow | undefined, profile: KindProfile, nowSec: number): number {
  if (!row) return 0
  const ageDays = Math.max(0, (nowSec - row.createdAt) / 86400)
  return (
    profile.priorBonus * (profile.prefer[row.kind] ?? 0) +
    profile.recencyBonus * Math.pow(0.5, ageDays / profile.recencyHalfLifeDays) +
    (row.hasRootCause ? profile.rootCauseBonus ?? 0 : 0)
  )
}

/**
 * Final order: relevance + bonus. Rows the reranker scored are ordered among
 * themselves (reranker scale) and stay ahead of unscored rows, which are
 * ordered by cosine + bonus; scales are never mixed. Stable on ties.
 */
export function finalOrder(
  pool: string[],
  rows: Map<string, PoolRow>,
  profile: KindProfile,
  relevance: { rerank: Map<string, number> | null; cosine: Map<string, number> },
  nowSec: number,
): string[] {
  const scored = (keys: string[], rel: Map<string, number>) =>
    keys
      .map((k, i) => ({ k, i, s: (rel.get(k) ?? 0) + bonus(rows.get(k), profile, nowSec) }))
      .sort((a, b) => b.s - a.s || a.i - b.i)
      .map((x) => x.k)
  const rr = relevance.rerank
  if (!rr) return scored(pool, relevance.cosine)
  return [
    ...scored(pool.filter((k) => rr.has(k)), rr),
    ...scored(pool.filter((k) => !rr.has(k)), relevance.cosine),
  ]
}

// An issue row "records a cause and a fix" when its text says so.
const ROOT_CAUSE_RE = /\b(root cause|caused by|because|fix(ed)?|fix applied|resolved|workaround)\b/i

// ─── Untrusted-data delimiting ──────────────────────────────────────────────
//
// Same wrapper as deep_search (untrusted.ts, #229): <memory>…</memory> with
// wrapper-like text neutralized, the shared notice, and the screening label.

const MAX_ANSWER_CHARS = 1500

export function clipAnswer(text: string): string {
  return text.length > MAX_ANSWER_CHARS ? text.slice(0, MAX_ANSWER_CHARS) + ' […]' : text
}

// ─── Retrieval ──────────────────────────────────────────────────────────────

export interface AskArgs {
  question: string
  kind: AskKind
  projectId: string | null
  limit?: number
}

interface MemRow {
  id: string
  kind: string
  title: string | null
  content: string
  project: string
  created_at: Date
  screen_flags: string[] | null
}

export interface AskAnswer {
  memory_id: string
  kind: string
  title: string | null
  project: string
  date: string
  relevance?: number
  cosine?: number
  screen_flags?: string[]
  /** Older versions this answer replaces (it was reached through them). */
  replaces?: Array<{ memory_id: string; date: string; relation: 'supersedes' | 'refines' }>
  /** Near-identical copies folded into this answer. */
  similar_ids?: string[]
  /** Label when prompt-injection screening flagged the row. */
  screening?: string
  /** Memory text, wrapped as untrusted data (<memory>…</memory>). */
  content: string
}

export interface AskResult {
  question: string
  kind: AskKind
  answers: AskAnswer[]
  weak_match: boolean
  note?: string
  edge_notes: string[]
  /** Notice that wrapped memory text is data, not instructions. */
  notice?: string
  retrieval: { candidates: number; reranked: boolean; ms: number }
}

const VECTOR_SQL = `
  SELECT m.id::text AS memory_id, 1 - (m.embedding <=> $1::vector) AS score
  FROM devbrain.memory m
  WHERE m.embedding IS NOT NULL AND m.archived_at IS NULL
    AND m.kind = ANY($3::text[])
    AND ($4::uuid IS NULL OR m.project_id = $4::uuid)
  ORDER BY m.embedding <=> $1::vector
  LIMIT $2
`

let screenColumn: Promise<boolean> | null = null
function hasScreenColumn(q: QueryFn): Promise<boolean> {
  if (q !== query) {
    return q(`SELECT 1 AS ok FROM information_schema.columns WHERE column_name = 'screen_flags'`)
      .then((r) => r.rows.length > 0)
  }
  screenColumn ??= query<{ ok: number }>(
    `SELECT 1 AS ok FROM information_schema.columns WHERE table_schema = 'devbrain'
       AND table_name = 'memory' AND column_name = 'screen_flags'`,
  )
    .then((r) => r.rows.length > 0)
    .catch(() => false)
  return screenColumn
}

async function loadRows(ids: string[], q: QueryFn = query): Promise<Map<string, MemRow>> {
  if (ids.length === 0) return new Map()
  const flags = (await hasScreenColumn(q)) ? 'm.screen_flags' : 'NULL::text[]'
  const r = await q(
    `SELECT m.id::text AS id, m.kind, m.title, m.content, p.slug AS project, m.created_at,
            ${flags} AS screen_flags
       FROM devbrain.memory m JOIN devbrain.projects p ON p.id = m.project_id
      WHERE m.id = ANY($1::uuid[])`,
    [ids],
  )
  return new Map((r.rows as MemRow[]).map((row) => [row.id, row]))
}

/**
 * For each id, the newest live version reachable over supersedes
 * (new -supersedes-> old) and refined_by (old -refined_by-> new) edges, with
 * the chain of versions it replaces. Ids with no newer version are absent.
 */
export async function currentVersions(
  ids: string[],
  q: QueryFn = query,
): Promise<Map<string, { current: string; chain: Array<{ id: string; relation: 'supersedes' | 'refines' }> }>> {
  const out = new Map<string, { current: string; chain: Array<{ id: string; relation: 'supersedes' | 'refines' }> }>()
  if (ids.length === 0) return out
  const r = await q(
    `WITH RECURSIVE nxt AS (
       SELECT md.to_memory_id AS old_id, md.from_memory_id AS new_id, 'supersedes'::text AS relation
         FROM devbrain.memory_dependencies md WHERE md.edge_type = 'supersedes'
       UNION ALL
       SELECT md.from_memory_id, md.to_memory_id, 'refines'
         FROM devbrain.memory_dependencies md WHERE md.edge_type = 'refined_by'
     ),
     walk AS (
       SELECT s.id AS start_id, n.new_id AS node, s.id AS prev, n.relation, 1 AS depth
         FROM unnest($1::uuid[]) AS s(id)
         JOIN nxt n ON n.old_id = s.id
         JOIN devbrain.memory m ON m.id = n.new_id AND m.archived_at IS NULL
       UNION
       SELECT w.start_id, n.new_id, w.node, n.relation, w.depth + 1
         FROM walk w
         JOIN nxt n ON n.old_id = w.node
         JOIN devbrain.memory m ON m.id = n.new_id AND m.archived_at IS NULL
        WHERE w.depth < 10
     )
     SELECT start_id::text, node::text, prev::text, relation, depth FROM walk
     ORDER BY start_id, depth`,
    [ids],
  )
  type Step = { start_id: string; node: string; prev: string; relation: string; depth: number }
  const byStart = new Map<string, Step[]>()
  for (const row of r.rows as Step[]) {
    if (!byStart.has(row.start_id)) byStart.set(row.start_id, [])
    byStart.get(row.start_id)!.push(row)
  }
  for (const [start, rows] of byStart) {
    // Deepest node wins; ties broken by id for determinism.
    const maxDepth = Math.max(...rows.map((x) => x.depth))
    const deepest = rows.filter((x) => x.depth === maxDepth).map((x) => x.node).sort()[0]
    const chain: Array<{ id: string; relation: 'supersedes' | 'refines' }> = []
    let node = deepest
    for (let d = maxDepth; d >= 1; d--) {
      const step = rows.find((x) => x.depth === d && x.node === node)
      if (!step) break
      chain.push({ id: step.prev, relation: step.relation === 'refines' ? 'refines' : 'supersedes' })
      node = step.prev
    }
    out.set(start, { current: deepest, chain })
  }
  return out
}

function isoDate(d: Date | string | null | undefined): string {
  if (!d) return ''
  const t = d instanceof Date ? d : new Date(d)
  return Number.isNaN(t.getTime()) ? '' : t.toISOString().slice(0, 10)
}

type QueryFn = (text: string, params?: unknown[]) => Promise<{ rows: any[] }> // eslint-disable-line @typescript-eslint/no-explicit-any
type TimedQueryFn = (text: string, params: unknown[], timeoutMs: number) => Promise<{ rows: any[] }> // eslint-disable-line @typescript-eslint/no-explicit-any

export interface AskDeps {
  embed?: (q: string) => Promise<number[]>
  rerank?: typeof rerankScores
  searchCfg?: SearchConfig
  /** Test hook: fixed "now" (epoch seconds) for the recency bonus. */
  nowSec?: number
  /** Test hooks: replace the DB calls. */
  query?: QueryFn
  queryWithTimeout?: TimedQueryFn
}

export async function ask(args: AskArgs, deps: AskDeps = {}): Promise<AskResult> {
  const t0 = performance.now()
  const q: QueryFn = deps.query ?? query
  const qt: TimedQueryFn = deps.queryWithTimeout ?? queryWithTimeout
  const profile = PROFILES[args.kind]
  const cfg = deps.searchCfg ?? getSearchConfig()
  const limit = Math.max(1, Math.min(3, args.limit ?? 3))
  const vec = toSqlVector(await (deps.embed ?? embedQuery)(args.question))
  const depth = Math.max(cfg.candidates, 30)

  // 1. Vector + keyword channels over the profile's kinds, fused (RRF).
  const vector = await q(VECTOR_SQL, [
    vec, depth, profile.kinds, args.projectId,
  ])
  const cosine = new Map<string, number>(vector.rows.map((r) => [String(r.memory_id), Number(r.score)]))
  let pool: string[] = vector.rows.map((r) => String(r.memory_id))
  if (cfg.hybrid) {
    try {
      const kw = await qt(
        keywordSql(),
        keywordParams(args.question, vec, { projectId: args.projectId, kinds: profile.kinds, chunkSourceTypes: null }, cfg),
        cfg.keywordTimeoutMs,
      )
      const kwKeys: string[] = kw.rows.map((r) => String(r.memory_id))
      for (const r of kw.rows) if (!cosine.has(String(r.memory_id))) cosine.set(String(r.memory_id), Number(r.score))
      if (kwKeys.length > 0) {
        pool = rrfFuse([pool, kwKeys], [1, cfg.keywordWeight], cfg.rrfK, cosine.size, cosine)
      }
    } catch {
      // keyword channel is optional (timeout / missing index): vector order stands
    }
  }
  const rows = await loadRows(pool, q)
  pool = pool.filter((k) => rows.has(k))

  // 2. Local cross-encoder rerank of the head, as deep_search does.
  let rerankByKey: Map<string, number> | null = null
  if (cfg.rerank && pool.length > 0) {
    const head = pool.slice(0, cfg.rerankCandidates)
    const res = await (deps.rerank ?? rerankScores)(
      cfg.rerankUrl,
      args.question,
      head.map((k) => rerankDocText(rows.get(k)!.title, rows.get(k)!.content)),
      cfg.rerankTimeoutMs,
    )
    if (res) {
      rerankByKey = new Map(head.map((k, i) => [k, res.scores[i]]))
      pool = applyRerank(pool, rerankByKey, head.length, cfg.rerankBlend, cfg.rrfK)
    }
  }

  // 3. Kind prior + recency.
  const poolRows = new Map<string, PoolRow>(
    pool.map((k) => {
      const r = rows.get(k)!
      return [k, {
        key: k,
        kind: r.kind,
        createdAt: new Date(r.created_at).getTime() / 1000,
        hasRootCause: ROOT_CAUSE_RE.test(r.content),
      }]
    }),
  )
  const ranked = finalOrder(pool, poolRows, profile, { rerank: rerankByKey, cosine }, deps.nowSec ?? Date.now() / 1000)

  // 4. Near-duplicate collapse over the head (newest copy shown).
  const head = ranked.slice(0, 30)
  let groups = head.map((k) => ({ rep: k, similar: [] as string[] }))
  if (cfg.dedup && head.length > 1) {
    try {
      const pairs = await qt(nearDupSql(), [head, cfg.dedupThreshold], cfg.dedupTimeoutMs)
      const created = new Map([...poolRows].map(([k, r]) => [k, r.createdAt]))
      groups = collapseNearDuplicates(
        head,
        pairs.rows.map((p) => [String(p.a_id), String(p.b_id)] as [string, string]),
        head.length,
        created,
      )
    } catch {
      // un-collapsed on timeout
    }
  }

  // 5. Current version over supersedes / refined_by edges.
  const versions = await currentVersions(groups.map((g) => g.rep), q)
  const extraIds = [...versions.values()].flatMap((v) => [v.current, ...v.chain.map((c) => c.id)])
  const extraRows = await loadRows(extraIds.filter((id) => !rows.has(id)), q)
  for (const [k, v] of extraRows) rows.set(k, v)

  const wrap = untrustedText(cfg.untrustedWrap)
  const answers: AskAnswer[] = []
  const edgeNotes: string[] = []
  const used = new Set<string>()
  const strengths: Array<MatchStrength | undefined> = []
  for (const g of groups) {
    if (answers.length >= limit) break
    const v = versions.get(g.rep)
    const id = v?.current ?? g.rep
    if (used.has(id)) continue
    const row = rows.get(id)
    if (!row) continue
    used.add(id)
    for (const m of [g.rep, ...g.similar]) used.add(m)
    const members = [g.rep, ...g.similar]
    const strength = groupStrength(members, rerankByKey, cosine)
    strengths.push(strength)
    const a: AskAnswer = {
      memory_id: id,
      kind: row.kind,
      title: row.title === null ? null : wrap.title(row.title),
      project: row.project,
      date: isoDate(row.created_at),
      content: wrap.body(clipAnswer(row.content)),
    }
    if (rerankByKey?.has(g.rep)) a.relevance = Number(rerankByKey.get(g.rep)!.toFixed(4))
    if (cosine.has(g.rep)) a.cosine = Number(cosine.get(g.rep)!.toFixed(4))
    if (row.screen_flags?.length) {
      a.screen_flags = row.screen_flags
      a.screening = screeningLabel(row.screen_flags)
    }
    if (g.similar.length) a.similar_ids = g.similar
    if (v) {
      a.replaces = v.chain.map((c) => ({
        memory_id: c.id,
        date: isoDate(rows.get(c.id)?.created_at),
        relation: c.relation,
      }))
      const oldest = v.chain[v.chain.length - 1]
      edgeNotes.push(
        `${id} (${a.date}) is the current version: it ${v.chain[0].relation === 'refines' ? 'refines' : 'supersedes'} ` +
          v.chain.map((c) => `${c.id} (${isoDate(rows.get(c.id)?.created_at)})`).join(' ← ') +
          (oldest.id === g.rep ? '; the search matched the older version.' : '.'),
      )
    }
    answers.push(a)
  }

  const weak = cfg.weakMatch && isWeak(responseStrength(strengths), cfg)
  const out: AskResult = {
    question: args.question,
    kind: args.kind,
    answers,
    weak_match: weak,
    edge_notes: edgeNotes,
    notice: wrap.notice,
    retrieval: { candidates: pool.length, reranked: rerankByKey !== null, ms: Math.round(performance.now() - t0) },
  }
  if (weak) out.note = WEAK_MATCH_NOTE
  return out
}

// ─── MCP registration ───────────────────────────────────────────────────────

export const ASK_DESCRIPTION =
  'Answer a typed question from DevBrain memory (retrieval only, no generated text). ' +
  'kind: decision | why | howto | issue | status | who_when. Returns the top 1-3 memories ' +
  '(current versions, following supersedes / refined_by edges, with what they replaced), ' +
  'their dates, and weak_match when nothing answers. Memory text is wrapped as untrusted data.'

export function registerAskTool(
  server: McpServer,
  opts: { resolveProjectId: (slug: string) => Promise<string | null>; defaultProject: string | null },
): boolean {
  if (!askEnabled()) return false
  server.tool(
    'ask',
    ASK_DESCRIPTION,
    {
      question: z.string().describe('The question, in plain words'),
      kind: z.enum(ASK_KINDS).describe(
        'decision: what was decided; why: the reason behind something; howto: how to do X; ' +
          'issue: a bug, its cause and fix; status: where something stands now; who_when: who did X or when',
      ),
      project: z.string().optional().describe('Project slug (defaults to the current project)'),
      cross_project: z.boolean().optional().default(false).describe('Search all projects'),
      limit: z.number().min(1).max(3).optional().default(3).describe('Answers to return (1-3)'),
    },
    async ({ question, kind, project, cross_project, limit }) => {
      let projectId: string | null = null
      if (!cross_project) {
        const slug = project ?? opts.defaultProject
        if (slug) {
          projectId = await opts.resolveProjectId(slug)
          if (!projectId) {
            return { content: [{ type: 'text', text: `Project "${slug}" not found.` }] }
          }
        }
      }
      const result = await ask({ question, kind, projectId, limit })
      return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] }
    },
  )
  return true
}
