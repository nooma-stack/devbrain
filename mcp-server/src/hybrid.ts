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
 * DEVBRAIN_KEYWORD_WEIGHT, DEVBRAIN_KEYWORD_TERMS (identifiers|all).
 * yaml: search.{hybrid, rrf_k, keyword_weight, candidates, keyword_max_df,
 * keyword_row_budget, keyword_max_terms, keyword_terms, keyword_timeout_ms}.
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
  }
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
