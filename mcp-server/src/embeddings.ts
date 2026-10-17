import { getEmbeddingConfig, getQueryPrefix } from './db.js'

const { url: OLLAMA_URL, model: EMBED_MODEL } = getEmbeddingConfig()

// Cap each Ollama request so a stalled embedding can't hang the tool call
// indefinitely (deep_search/store/breadcrumb all await an embed). On timeout
// fetch rejects with an AbortError, which propagates as a normal failure.
const EMBED_TIMEOUT_MS = Number(process.env.DEVBRAIN_EMBED_TIMEOUT_MS ?? 30000)

// ─── Documents vs queries ────────────────────────────────────────────────────
//
// snowflake-arctic-embed2 is an asymmetric retrieval model: search QUERIES
// are embedded with a "query: " prefix, stored DOCUMENTS are embedded bare.
//
//   embed() / embedBatch()  — DOCUMENTS. Every write path (store, breadcrumb,
//                             end_session, ingest, reembed) uses these. They
//                             must never carry the prefix: the corpus was
//                             embedded bare, and prefixing writes would
//                             silently shift stored vectors away from it.
//   embedQuery()            — QUERIES (deep_search). Prepends getQueryPrefix()
//                             (DEVBRAIN_QUERY_PREFIX > yaml
//                             embedding.query_prefix > model default; "" off).
//
// ingest/embeddings.py keeps the same split for Python callers.

/** The exact text sent to the model for a search query. */
export function queryEmbedText(text: string): string {
  return `${getQueryPrefix()}${text}`
}

/** Embed a DOCUMENT (no query prefix). */
export async function embed(text: string): Promise<number[]> {
  const response = await fetch(`${OLLAMA_URL}/api/embed`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model: EMBED_MODEL, input: text }),
    signal: AbortSignal.timeout(EMBED_TIMEOUT_MS),
  })

  if (!response.ok) {
    throw new Error(`Ollama embedding failed: ${response.status} ${await response.text()}`)
  }

  const data = (await response.json()) as { embeddings: number[][] }
  return data.embeddings[0]
}

/** Embed a search QUERY (with the model's query prefix). */
export async function embedQuery(text: string): Promise<number[]> {
  return embed(queryEmbedText(text))
}

/** Embed DOCUMENTS in one request (no query prefix). */
export async function embedBatch(texts: string[]): Promise<number[][]> {
  const response = await fetch(`${OLLAMA_URL}/api/embed`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model: EMBED_MODEL, input: texts }),
    signal: AbortSignal.timeout(EMBED_TIMEOUT_MS),
  })

  if (!response.ok) {
    throw new Error(`Ollama batch embedding failed: ${response.status} ${await response.text()}`)
  }

  const data = (await response.json()) as { embeddings: number[][] }
  return data.embeddings
}

export function toSqlVector(embedding: number[]): string {
  return `[${embedding.join(',')}]`
}
