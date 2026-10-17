// deep_search hybrid retrieval: a Postgres full-text keyword channel fused
// with the vector channel by weighted Reciprocal Rank Fusion.
//
// Pure helpers only (no DB, no config file) so they unit-test in isolation.
// db.ts resolves the live config through resolveSearchConfig(); index.ts
// runs KEYWORD_SQL and fuses with rrfFuse(). ingest/recall_audit mirrors
// rrfFuse and reads the same SQL file, so audit numbers match what ships.

import { readFileSync } from 'fs'
import { resolve } from 'path'

export interface SearchConfig {
  /** Run the keyword channel and fuse it with the vector channel. */
  hybrid: boolean
  /** RRF constant k in w / (k + rank). */
  rrfK: number
  /** RRF weight of the keyword list (the vector list has weight 1). */
  keywordWeight: number
  /** Rows fetched per channel before fusion (vector fetch = max(limit, this)). */
  candidates: number
  /** Query terms in more than this fraction of rows are dropped. */
  keywordMaxDf: number
  /** Max summed document count of the kept terms (bounds the matched set). */
  keywordRowBudget: number
  /** Max query terms kept. */
  keywordMaxTerms: number
  /** Only identifier-like tokens (snake_case, file.ext, CONSTANT, ...) feed the keyword channel. */
  keywordIdentifiersOnly: boolean
  /** statement_timeout for the keyword query; on timeout deep_search falls back to vector-only. */
  keywordTimeoutMs: number
  /** Collapse near-duplicate results (cosine >= dedupThreshold) into one slot. */
  dedup: boolean
  /** Cosine similarity at or above which two results count as the same memory. */
  dedupThreshold: number
  /** Which copy represents a group: the best-ranked one or the newest one. */
  dedupRepresentative: 'best' | 'newest'
  /** statement_timeout for the pair query; on timeout results are returned un-collapsed. */
  dedupTimeoutMs: number
  /**
   * How recency_neighbors / earliest_on_topic find same-topic rows:
   * 'hnsw' (HNSW index, bounded scan) or 'exact' (the planner's exact scan,
   * seconds per search on a large project).
   */
  recencyStrategy: 'hnsw' | 'exact'
  /** hnsw.max_scan_tuples for the 'hnsw' recency strategy. */
  recencyMaxScanTuples: number
  /** statement_timeout for the 'hnsw' recency queries; on timeout the annotation is left empty. */
  recencyTimeoutMs: number
  /** Rescore the head of the ranked pool with the local cross-encoder service (rerank/). */
  rerank: boolean
  /** Loopback URL of the reranker service. Non-loopback URLs are refused (memory text may hold PHI). */
  rerankUrl: string
  /** How many top candidates the reranker rescores. */
  rerankCandidates: number
  /** 'rrf': fuse reranker order with the incoming order; 'rerank': reranker order alone. */
  rerankBlend: 'rrf' | 'rerank'
  /** Request timeout; on timeout or error the search keeps the un-reranked order. */
  rerankTimeoutMs: number
  /** Flag responses / results whose best score says DevBrain probably lacks the answer. */
  weakMatch: boolean
  /** Weak below this reranker score (0..1). */
  weakMatchRerankThreshold: number
  /** Weak below this cosine similarity when no reranker score is available. */
  weakMatchCosineThreshold: number
  /** Wrap retrieved memory text in <memory>…</memory> with a notice (untrusted.ts). */
  untrustedWrap: boolean
}

// Measured 2026-10-17 with ingest/recall_audit on the Nooma corpus (see the
// PR that added this file). ingest/recall_audit/audit.py HybridParams must
// carry the same values (a test checks).
export const SEARCH_DEFAULTS: SearchConfig = {
  hybrid: true,
  rrfK: 60,
  keywordWeight: 1.0,
  candidates: 50,
  keywordMaxDf: 0.002,
  keywordRowBudget: 3000,
  keywordMaxTerms: 12,
  keywordIdentifiersOnly: true,
  keywordTimeoutMs: 1000,
  dedup: true,
  dedupThreshold: 0.93,
  dedupRepresentative: 'newest',
  dedupTimeoutMs: 500,
  recencyStrategy: 'hnsw',
  recencyMaxScanTuples: 2000,
  recencyTimeoutMs: 2000,
  rerank: true,
  rerankUrl: 'http://127.0.0.1:18600',
  rerankCandidates: 30,
  rerankBlend: 'rrf',
  rerankTimeoutMs: 1200,
  // ingest/recall_audit/weak.py, 2026-10-17 (76 gold + 50 absent-topic
  // queries x 2 scopes): rerank < 0.88 → precision 0.92, recall 0.94;
  // cosine < 0.41 → precision 0.94, recall 0.34 (fallback only).
  weakMatch: true,
  weakMatchRerankThreshold: 0.88,
  weakMatchCosineThreshold: 0.41,
  untrustedWrap: true,
}

type Env = Record<string, string | undefined>

function parseBool(v: unknown): boolean | undefined {
  if (typeof v === 'boolean') return v
  if (v === undefined || v === null) return undefined
  const s = String(v).trim().toLowerCase()
  if (['1', 'true', 'on', 'yes'].includes(s)) return true
  if (['0', 'false', 'off', 'no', ''].includes(s)) return false
  return undefined
}

function parseNum(v: unknown, ok: (n: number) => boolean): number | undefined {
  if (v === undefined || v === null || v === '') return undefined
  const n = Number(v)
  return Number.isFinite(n) && ok(n) ? n : undefined
}

function parseTerms(v: unknown): boolean | undefined {
  if (v === undefined || v === null) return undefined
  const s = String(v).trim().toLowerCase()
  if (s === 'identifiers') return true
  if (s === 'all') return false
  return undefined
}

/**
 * Resolve the search config: env > yaml `search:` section > SEARCH_DEFAULTS.
 * Invalid values fall back to the next source rather than failing a search.
 *
 * Env: DEVBRAIN_HYBRID_SEARCH (on/off), DEVBRAIN_RRF_K,
 * DEVBRAIN_KEYWORD_WEIGHT, DEVBRAIN_KEYWORD_TERMS (identifiers|all),
 * DEVBRAIN_DEDUP_SEARCH (on/off), DEVBRAIN_DEDUP_THRESHOLD,
 * DEVBRAIN_RECENCY_STRATEGY (hnsw|exact), DEVBRAIN_RERANK (on/off),
 * DEVBRAIN_RERANK_URL, DEVBRAIN_WEAK_MATCH (on/off), DEVBRAIN_UNTRUSTED_WRAP (on/off).
 * yaml: search.{hybrid, rrf_k, keyword_weight, candidates, keyword_max_df,
 * keyword_row_budget, keyword_max_terms, keyword_terms, keyword_timeout_ms,
 * dedup, dedup_threshold, dedup_representative, dedup_timeout_ms,
 * recency_strategy, recency_max_scan_tuples, recency_timeout_ms, rerank,
 * rerank_url, rerank_candidates, rerank_blend, rerank_timeout_ms, weak_match,
 * weak_match_rerank_threshold, weak_match_cosine_threshold, untrusted_wrap}.
 */
export function resolveSearchConfig(yaml: unknown, env: Env = process.env): SearchConfig {
  const y = (yaml && typeof yaml === 'object' ? yaml : {}) as Record<string, unknown>
  const d = SEARCH_DEFAULTS
  const pos = (n: number) => n > 0
  const pick = <T>(...vals: Array<T | undefined>): T => vals.find((v) => v !== undefined) as T
  return {
    hybrid: pick(parseBool(env.DEVBRAIN_HYBRID_SEARCH), parseBool(y.hybrid), d.hybrid),
    rrfK: pick(parseNum(env.DEVBRAIN_RRF_K, pos), parseNum(y.rrf_k, pos), d.rrfK),
    keywordWeight: pick(
      parseNum(env.DEVBRAIN_KEYWORD_WEIGHT, (n) => n >= 0),
      parseNum(y.keyword_weight, (n) => n >= 0),
      d.keywordWeight,
    ),
    candidates: Math.round(pick(parseNum(y.candidates, (n) => n >= 1 && n <= 500), d.candidates)),
    keywordMaxDf: pick(parseNum(y.keyword_max_df, (n) => n > 0 && n <= 1), d.keywordMaxDf),
    keywordRowBudget: pick(parseNum(y.keyword_row_budget, pos), d.keywordRowBudget),
    keywordMaxTerms: Math.round(pick(parseNum(y.keyword_max_terms, (n) => n >= 1 && n <= 64), d.keywordMaxTerms)),
    keywordIdentifiersOnly: pick(
      parseTerms(env.DEVBRAIN_KEYWORD_TERMS),
      parseTerms(y.keyword_terms),
      d.keywordIdentifiersOnly,
    ),
    keywordTimeoutMs: Math.round(pick(parseNum(y.keyword_timeout_ms, (n) => n >= 10), d.keywordTimeoutMs)),
    dedup: pick(parseBool(env.DEVBRAIN_DEDUP_SEARCH), parseBool(y.dedup), d.dedup),
    dedupThreshold: pick(
      parseNum(env.DEVBRAIN_DEDUP_THRESHOLD, (n) => n > 0 && n <= 1),
      parseNum(y.dedup_threshold, (n) => n > 0 && n <= 1),
      d.dedupThreshold,
    ),
    dedupRepresentative: pick(parseRepresentative(y.dedup_representative), d.dedupRepresentative),
    dedupTimeoutMs: Math.round(pick(parseNum(y.dedup_timeout_ms, (n) => n >= 10), d.dedupTimeoutMs)),
    recencyStrategy: pick(
      parseRecencyStrategy(env.DEVBRAIN_RECENCY_STRATEGY),
      parseRecencyStrategy(y.recency_strategy),
      d.recencyStrategy,
    ),
    recencyMaxScanTuples: Math.round(
      pick(parseNum(y.recency_max_scan_tuples, (n) => n >= 100 && n <= 1_000_000), d.recencyMaxScanTuples),
    ),
    recencyTimeoutMs: Math.round(pick(parseNum(y.recency_timeout_ms, (n) => n >= 10), d.recencyTimeoutMs)),
    rerank: pick(parseBool(env.DEVBRAIN_RERANK), parseBool(y.rerank), d.rerank),
    rerankUrl: pick(parseUrl(env.DEVBRAIN_RERANK_URL), parseUrl(y.rerank_url), d.rerankUrl),
    rerankCandidates: Math.round(pick(parseNum(y.rerank_candidates, (n) => n >= 2 && n <= 100), d.rerankCandidates)),
    rerankBlend: pick(parseBlend(y.rerank_blend), d.rerankBlend),
    rerankTimeoutMs: Math.round(pick(parseNum(y.rerank_timeout_ms, (n) => n >= 10), d.rerankTimeoutMs)),
    weakMatch: pick(parseBool(env.DEVBRAIN_WEAK_MATCH), parseBool(y.weak_match), d.weakMatch),
    weakMatchRerankThreshold: pick(
      parseNum(y.weak_match_rerank_threshold, (n) => n >= 0 && n <= 1),
      d.weakMatchRerankThreshold,
    ),
    weakMatchCosineThreshold: pick(
      parseNum(y.weak_match_cosine_threshold, (n) => n >= -1 && n <= 1),
      d.weakMatchCosineThreshold,
    ),
    untrustedWrap: pick(parseBool(env.DEVBRAIN_UNTRUSTED_WRAP), parseBool(y.untrusted_wrap), d.untrustedWrap),
  }
}

function parseUrl(v: unknown): string | undefined {
  if (v === undefined || v === null || String(v).trim() === '') return undefined
  try {
    return isLoopbackUrl(String(v).trim()) ? String(v).trim().replace(/\/+$/, '') : undefined
  } catch {
    return undefined
  }
}

function parseBlend(v: unknown): 'rrf' | 'rerank' | undefined {
  const s = v === undefined || v === null ? '' : String(v).trim().toLowerCase()
  return s === 'rrf' || s === 'rerank' ? s : undefined
}

/** http(s) URL whose host is a loopback address (127.0.0.0/8, ::1, localhost). */
export function isLoopbackUrl(url: string): boolean {
  let u: URL
  try {
    u = new URL(url)
  } catch {
    return false
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return false
  const h = u.hostname.replace(/^\[|\]$/g, '')
  return h === 'localhost' || h === '::1' || /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(h)
}

function parseRecencyStrategy(v: unknown): 'hnsw' | 'exact' | undefined {
  const s = v === undefined || v === null ? '' : String(v).trim().toLowerCase()
  return s === 'hnsw' || s === 'exact' ? s : undefined
}

function parseRepresentative(v: unknown): 'best' | 'newest' | undefined {
  const s = v === undefined || v === null ? '' : String(v).trim().toLowerCase()
  return s === 'best' || s === 'newest' ? s : undefined
}

/**
 * Weighted Reciprocal Rank Fusion: score(d) = sum over lists of w / (k + rank),
 * ranks 1-based. A key repeated inside one list counts once (its best rank).
 * Equal fused scores go to the higher tiebreak value (cosine), then to the
 * key seen first. Mirror of rrf_fuse in ingest/recall_audit/audit.py.
 */
export function rrfFuse(
  lists: string[][],
  weights: number[],
  k: number,
  limit: number,
  tiebreak: Map<string, number> = new Map(),
): string[] {
  const scores = new Map<string, number>()
  const firstSeen = new Map<string, number>()
  lists.forEach((ranked, li) => {
    const w = weights[li] ?? 1
    const seen = new Set<string>()
    ranked.forEach((key, i) => {
      if (seen.has(key)) return
      seen.add(key)
      scores.set(key, (scores.get(key) ?? 0) + w / (k + i + 1))
      if (!firstSeen.has(key)) firstSeen.set(key, firstSeen.size)
    })
  })
  return [...scores.keys()]
    .sort((a, b) =>
      (scores.get(b)! - scores.get(a)!) ||
      ((tiebreak.get(b) ?? 0) - (tiebreak.get(a) ?? 0)) ||
      (firstSeen.get(a)! - firstSeen.get(b)!))
    .slice(0, limit)
}

/** Which channels returned each fused key: 'vector', 'keyword' or 'both'. */
export function matchedBy(key: string, vectorKeys: Set<string>, keywordKeys: Set<string>): string {
  const v = vectorKeys.has(key)
  const kw = keywordKeys.has(key)
  return v && kw ? 'both' : kw ? 'keyword' : 'vector'
}

/** Path of the keyword SQL (works from src/ under tsx and from dist/). */
export const KEYWORD_SQL_PATH = resolve(import.meta.dirname, '../sql/keyword_search.sql')

let _keywordSql: string | null = null

/** The keyword channel's SQL text ($1..$10, see the file header). */
export function keywordSql(): string {
  if (_keywordSql === null) _keywordSql = readFileSync(KEYWORD_SQL_PATH, 'utf-8')
  return _keywordSql
}

export interface KeywordFilters {
  projectId: string | null
  kinds: string[] | null
  chunkSourceTypes: string[] | null
}

/** Positional parameters for keywordSql(), in $1..$10 order. */
export function keywordParams(
  searchQuery: string,
  vectorStr: string,
  filters: KeywordFilters,
  cfg: SearchConfig,
): unknown[] {
  return [
    searchQuery,
    cfg.keywordMaxDf,
    cfg.keywordRowBudget,
    cfg.candidates,
    vectorStr,
    filters.projectId,
    filters.kinds,
    filters.chunkSourceTypes,
    cfg.keywordMaxTerms,
    cfg.keywordIdentifiersOnly,
  ]
}

// ─── Near-duplicate collapse ────────────────────────────────────────────────

/** Path of the near-duplicate pair SQL ($1 ids, $2 threshold). */
export const NEAR_DUP_SQL_PATH = resolve(import.meta.dirname, '../sql/near_duplicates.sql')

let _nearDupSql: string | null = null

export function nearDupSql(): string {
  if (_nearDupSql === null) _nearDupSql = readFileSync(NEAR_DUP_SQL_PATH, 'utf-8')
  return _nearDupSql
}

export interface DupGroup {
  /** Representative key shown in the result slot. */
  rep: string
  /** Other copies (cosine >= threshold to the best-ranked member), in rank order. */
  similar: string[]
}

/**
 * Greedy near-duplicate collapse in rank order. Mirror of
 * collapse_near_duplicates in ingest/recall_audit/audit.py.
 *
 * Walk the ranked pool; a key listed in `pairs` with an existing group's
 * (best-ranked) representative joins the first such group, otherwise it
 * opens a new group while fewer than `limit` exist. The walk continues past
 * `limit` so every copy in the pool is attached. No chaining: similarity is
 * always to the group's best-ranked member.
 *
 * With `created` (key -> created_at epoch seconds), each group's shown
 * representative is then swapped for its newest member; the group keeps
 * the slot its best-ranked member earned.
 */
export function collapseNearDuplicates(
  ranked: string[],
  pairs: Array<[string, string]>,
  limit: number,
  created?: Map<string, number>,
): DupGroup[] {
  const adj = new Map<string, Set<string>>()
  for (const [a, b] of pairs) {
    if (!adj.has(a)) adj.set(a, new Set())
    if (!adj.has(b)) adj.set(b, new Set())
    adj.get(a)!.add(b)
    adj.get(b)!.add(a)
  }
  const groups: DupGroup[] = []
  const seen = new Set<string>()
  for (const key of ranked) {
    if (seen.has(key)) continue
    seen.add(key)
    const near = adj.get(key)
    const home = near ? groups.find((g) => near.has(g.rep)) : undefined
    if (home) home.similar.push(key)
    else if (groups.length < limit) groups.push({ rep: key, similar: [] })
  }
  if (created) {
    const at = (k: string) => created.get(k) ?? Number.NEGATIVE_INFINITY
    for (const g of groups) {
      let newest = g.rep
      for (const m of g.similar) if (at(m) > at(newest)) newest = m
      if (newest !== g.rep) {
        g.similar = [g.rep, ...g.similar.filter((m) => m !== newest)]
        g.rep = newest
      }
    }
  }
  return groups
}

// ─── Reranking ──────────────────────────────────────────────────────────────

/** Characters of memory text sent per candidate (mirror of RERANK_DOC_CHARS in the audit). */
export const RERANK_DOC_CHARS = 2000

/**
 * Text the reranker scores for one memory. cognify atoms keep the title out
 * of the content, so prepend it unless the content already starts with it.
 * Mirror of rerank_doc_text in ingest/recall_audit/audit.py.
 */
export function rerankDocText(title: string | null | undefined, content: string): string {
  const t = (title ?? '').trim()
  const c = content ?? ''
  const text = !t || c.trimStart().startsWith(t) ? c : `${t}\n${c}`
  return text.slice(0, RERANK_DOC_CHARS)
}

/**
 * Reorder the first `n` pool keys by reranker score; the rest keep their
 * order. Keys without a score (codebase rows) follow the scored ones. With
 * blend 'rrf', the reranker order is RRF-fused with the incoming order
 * inside the head. Mirror of apply_rerank in ingest/recall_audit/audit.py.
 */
export function applyRerank(
  pool: string[],
  scores: Map<string, number>,
  n: number,
  blend: 'rrf' | 'rerank' = 'rrf',
  k = 60,
): string[] {
  const head = pool.slice(0, n)
  const scored = head.filter((key) => scores.has(key))
  const unscored = head.filter((key) => !scores.has(key))
  // Array.prototype.sort is stable: equal scores keep the incoming order.
  let byScore = [...scored].sort((a, b) => scores.get(b)! - scores.get(a)!)
  if (blend === 'rrf') byScore = rrfFuse([byScore, scored], [1, 1], k, scored.length)
  return [...byScore, ...unscored, ...pool.slice(n)]
}

// ─── Weak-match flag ────────────────────────────────────────────────────────

export type MatchBasis = 'rerank' | 'cosine'

export interface MatchStrength {
  /** Best score for this result / response. */
  score: number
  basis: MatchBasis
}

/**
 * Strength of one result group: the highest reranker score among its
 * members when any was reranked, else the highest cosine. Mirror of the
 * per-group max in ingest/recall_audit/weak.py.
 */
export function groupStrength(
  members: string[],
  rerank: Map<string, number> | null,
  cosine: Map<string, number>,
): MatchStrength | undefined {
  const rr = rerank ? members.map((k) => rerank.get(k)).filter((v): v is number => v !== undefined) : []
  if (rr.length > 0) return { score: Math.max(...rr), basis: 'rerank' }
  const cc = members.map((k) => cosine.get(k)).filter((v): v is number => v !== undefined)
  return cc.length > 0 ? { score: Math.max(...cc), basis: 'cosine' } : undefined
}

/**
 * Response strength: reranker basis when the reranker scored any returned
 * group (its scores are on one scale), else cosine.
 */
export function responseStrength(groups: Array<MatchStrength | undefined>): MatchStrength | undefined {
  const rr = groups.filter((g): g is MatchStrength => g?.basis === 'rerank')
  const pool = rr.length > 0 ? rr : groups.filter((g): g is MatchStrength => g !== undefined)
  if (pool.length === 0) return undefined
  return { score: Math.max(...pool.map((g) => g.score)), basis: pool[0].basis }
}

/** True when the strength is below its basis' threshold (no strength = weak). */
export function isWeak(strength: MatchStrength | undefined, cfg: SearchConfig): boolean {
  if (!strength) return true
  const t = strength.basis === 'rerank' ? cfg.weakMatchRerankThreshold : cfg.weakMatchCosineThreshold
  return strength.score < t
}

export const WEAK_MATCH_NOTE =
  'Weak match: no result clears the relevance threshold, so DevBrain probably has no memory that answers this; treat these results as loosely related, not as the answer.'
