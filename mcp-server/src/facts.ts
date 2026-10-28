// `facts`: walk the facts graph (entities + typed relations, migration 050).
//
// The cognify `entities` pass (factory/cognify/entities.py) extracts things
// in the world (hosts, repos, services, accounts, domains, folders, people,
// orgs) and the facts between them, each edge with an exact quote of its
// source memory. This module answers a question from that graph with no
// model call: entity names and aliases found in the text as whole words
// seed a breadth-first walk over live facts, up to `hops`, ranked by
// nearness to the seeds (devbrain.facts_walk in SQL; one round trip), then
// the descriptions of the entities in the returned triples are added as
// notes. Deterministic and fast (single-digit ms on the Studio dataset).
//
// Design follows Glitch Cat Club's graph-memory-starter (MIT).
//
// `recallFacts` is the reusable entry point (the `recall` tool calls it);
// `registerFactsTool` adds the MCP `facts` tool.

import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { z } from 'zod'
import { getSearchConfig, query } from './db.js'
import { untrustedText, type UntrustedText } from './untrusted.js'

/**
 * One fact: subject --[predicate]--> object. subject/predicate/object are the
 * names recall.ts's factsLine reads.
 */
export interface FactTriple {
  subject: string
  subjectType: string
  predicate: string
  object: string
  objectType: string
  /** devbrain.memory id the newest statement of this fact came from. */
  sourceRef: string | null
  /** Exact span of that memory stating the fact (recorded data, untrusted). */
  quote: string
  /** created_at of that memory, ISO 8601. */
  observedAt: string
  /** Hops from the nearest seed (0 = the fact touches a seed). */
  depth: number
  projectId: string
  relationId: string
}

export interface FactNote {
  name: string
  type: string
  description: string
}

export interface FactsResult {
  triples: FactTriple[]
  notes: FactNote[]
  ms: number
}

export interface FactsOptions {
  /** Walk depth, 0-6. Default 3. */
  hops?: number
  /** Triples returned, 1-50. Default 8. */
  topK?: number
}

type Rows = { rows: Array<Record<string, unknown>> }
type QueryFn = (text: string, params?: unknown[]) => Promise<Rows>
/** A pg Pool / Client, anything with `query(text, params)`, or that function. */
export type FactsDb = { query: QueryFn } | QueryFn

/** Longest text used for seeding (a prompt can be long; names are short). */
export const MAX_SEED_TEXT = 8000

const WALK_SQL = `SELECT relation_id::text, source_id::text, source_name, source_type,
       predicate, target_id::text, target_name, target_type, project_id::text,
       source_ref::text, quote, observed_at, depth
  FROM devbrain.facts_walk($1::uuid, $2::text, $3::int, $4::int)`

const NOTES_SQL = `SELECT name, type, description FROM devbrain.entities
 WHERE id = ANY($1::uuid[]) AND description <> ''
 ORDER BY name, type`

function clampInt(v: unknown, lo: number, hi: number, dflt: number): number {
  const n = typeof v === 'number' && Number.isFinite(v) ? Math.trunc(v) : dflt
  return Math.min(hi, Math.max(lo, n))
}

function runner(db: FactsDb): QueryFn {
  if (typeof db === 'function') return db
  return (text, params) => db.query(text, params) as Promise<Rows>
}

function iso(v: unknown): string {
  if (v instanceof Date) return v.toISOString()
  return String(v ?? '')
}

/**
 * Facts for `text`: seed entities by name/alias (whole words, any case),
 * walk live relations up to `hops`, return the `topK` nearest triples and
 * the notes (descriptions) of the entities they mention. `projectId` null
 * walks every project. No model call.
 */
export async function recallFacts(
  db: FactsDb,
  projectId: string | null,
  text: string,
  opts: FactsOptions = { hops: 3, topK: 8 },
): Promise<FactsResult> {
  const t0 = performance.now()
  const q = runner(db)
  const hops = clampInt(opts.hops, 0, 6, 3)
  const topK = clampInt(opts.topK, 1, 50, 8)
  const seedText = String(text ?? '').slice(0, MAX_SEED_TEXT)
  if (!seedText.trim()) return { triples: [], notes: [], ms: performance.now() - t0 }

  const walk = await q(WALK_SQL, [projectId, seedText, hops, topK])
  const triples: FactTriple[] = walk.rows.map((r) => ({
    subject: String(r.source_name),
    subjectType: String(r.source_type),
    predicate: String(r.predicate),
    object: String(r.target_name),
    objectType: String(r.target_type),
    sourceRef: r.source_ref == null ? null : String(r.source_ref),
    quote: String(r.quote ?? ''),
    observedAt: iso(r.observed_at),
    depth: Number(r.depth ?? 0),
    projectId: String(r.project_id),
    relationId: String(r.relation_id),
  }))
  if (triples.length === 0) return { triples, notes: [], ms: performance.now() - t0 }

  const ids = [...new Set(walk.rows.flatMap((r) => [String(r.source_id), String(r.target_id)]))]
  const noteRows = await q(NOTES_SQL, [ids])
  const notes: FactNote[] = noteRows.rows.map((r) => ({
    name: String(r.name),
    type: String(r.type),
    description: String(r.description),
  }))
  return { triples, notes, ms: performance.now() - t0 }
}

export const NO_FACTS = 'no facts match'

/**
 * Tool text: one `A --[predicate]--> B (source)` line per triple and a
 * "where:" block of notes. Entity names are neutralized and descriptions
 * wrapped as untrusted memory text (untrusted.ts).
 */
export function formatFacts(result: FactsResult, u: UntrustedText, scope = 'all projects'): string {
  if (result.triples.length === 0) return NO_FACTS
  const lines = result.triples.map((t) => {
    const day = t.observedAt.slice(0, 10)
    const src = t.sourceRef ? `memory ${t.sourceRef}${day ? `, ${day}` : ''}` : day || 'source unknown'
    return `${u.title(t.subject)} --[${t.predicate}]--> ${u.title(t.object)} (${src})`
  })
  const head = `facts: ${result.triples.length} recalled in ${result.ms.toFixed(0)} ms (${scope})`
  let out = (u.notice ? `${u.notice}\n` : '') + `${head}\n\n${lines.join('\n')}`
  if (result.notes.length > 0) {
    out += '\n\nwhere:\n' + result.notes
      .map((n) => `  ${u.title(n.name)} (${n.type}): ${u.body(n.description)}`)
      .join('\n')
  }
  return out
}

export const FACTS_DESCRIPTION =
  'Look up facts about things in the world (hosts, repos, services, accounts, domains, folders, ' +
  'people, orgs) and how they connect: what runs where, what deploys to which host, which account ' +
  'or brain goes with a folder, who owns what. Names in the query seed a walk over a typed facts ' +
  'graph extracted from memories; each fact cites the memory it came from. Deterministic, no model ' +
  'call. Use for chained "which X goes with Y" questions; use deep_search for how/why.'

export function registerFactsTool(
  server: McpServer,
  opts: { resolveProjectId: (slug: string) => Promise<string | null>; defaultProject?: string | null },
): void {
  server.tool(
    'facts',
    FACTS_DESCRIPTION,
    {
      query: z.string().min(1).describe('Question or text naming the things to look up, e.g. "what runs on the SOHO VPS?"'),
      project: z.string().optional().describe('Project slug to limit facts to. Defaults to the current project (DEVBRAIN_PROJECT).'),
      cross_project: z.boolean().optional().default(false).describe('Use facts from every project instead of one.'),
      hops: z.number().int().min(1).max(6).optional().default(3).describe('How many links to follow from the named things. Default 3.'),
    },
    async ({ query: text, project, cross_project, hops }) => {
      // Like deep_search: one project by default, every project only on request.
      let projectId: string | null = null
      const slug = cross_project ? undefined : (project ?? opts.defaultProject)
      if (!cross_project) {
        if (!slug) {
          return { content: [{ type: 'text' as const, text: 'No project: pass `project`, or `cross_project: true` for every project.' }] }
        }
        projectId = await opts.resolveProjectId(slug)
        if (!projectId) {
          return { content: [{ type: 'text' as const, text: `Project "${slug}" not found.` }] }
        }
      }
      const result = await recallFacts({ query }, projectId, text, { hops: hops ?? 3, topK: 8 })
      const u = untrustedText(getSearchConfig().untrustedWrap)
      return {
        content: [{ type: 'text' as const, text: formatFacts(result, u, slug ? `project ${slug}` : 'all projects') }],
      }
    },
  )
}
