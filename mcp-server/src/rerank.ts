// Client for the local reranker service (rerank/server.py).
//
// deep_search sends the query and the texts of its top candidates and gets
// one relevance score per text. The service only listens on loopback and
// the client refuses non-loopback URLs (hybrid.ts isLoopbackUrl): memory
// text, which on BrightBrain may contain PHI, must never leave the machine.
//
// Failure is always soft: any error or timeout returns null and deep_search
// keeps the un-reranked order. A refused connection (service not running)
// opens a breaker for BREAKER_MS, so a stopped service costs one refused
// connection, not a request per search. Timeouts and bad responses open it
// only after BREAKER_AFTER in a row: one slow call (GPU busy) should not
// switch reranking off for the next searches.

import { isLoopbackUrl } from './hybrid.js'

const BREAKER_MS = 30_000
const BREAKER_AFTER = 3
let _openUntil = 0
let _failures = 0

export interface RerankResult {
  scores: number[]
  ms: number
}

/** Test hook: close the breaker. */
export function _resetRerankBreaker(): void {
  _openUntil = 0
  _failures = 0
}

function isConnectionRefused(err: unknown): boolean {
  const e = err as { code?: string; cause?: { code?: string } }
  return e?.code === 'ECONNREFUSED' || e?.cause?.code === 'ECONNREFUSED'
}

export async function rerankScores(
  url: string,
  query: string,
  documents: string[],
  timeoutMs: number,
  fetchImpl: typeof fetch = fetch,
): Promise<RerankResult | null> {
  if (documents.length === 0) return { scores: [], ms: 0 }
  if (!isLoopbackUrl(url)) {
    console.error('[deep_search] rerank skipped: rerank_url is not a loopback address')
    return null
  }
  if (Date.now() < _openUntil) return null
  const t0 = performance.now()
  try {
    const res = await fetchImpl(`${url.replace(/\/+$/, '')}/rerank`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, documents }),
      signal: AbortSignal.timeout(timeoutMs),
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const body = (await res.json()) as { scores?: unknown }
    const scores = body.scores
    if (!Array.isArray(scores) || scores.length !== documents.length || !scores.every((s) => typeof s === 'number' && Number.isFinite(s))) {
      throw new Error('malformed response')
    }
    _failures = 0
    return { scores: scores as number[], ms: performance.now() - t0 }
  } catch (err) {
    _failures++
    const msg = err instanceof Error ? err.message : String(err)
    if (isConnectionRefused(err) || _failures >= BREAKER_AFTER) {
      _openUntil = Date.now() + BREAKER_MS
      _failures = 0
      console.error(`[deep_search] rerank off for ${BREAKER_MS / 1000}s: ${msg}`)
    } else {
      console.error(`[deep_search] rerank skipped for this search: ${msg}`)
    }
    return null
  }
}
