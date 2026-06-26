import { getEmbeddingConfig } from './db.js'

const { url: OLLAMA_URL, model: EMBED_MODEL } = getEmbeddingConfig()

// Cap each Ollama request so a stalled embedding can't hang the tool call
// indefinitely (deep_search/store/breadcrumb all await an embed). On timeout
// fetch rejects with an AbortError, which propagates as a normal failure.
const EMBED_TIMEOUT_MS = Number(process.env.DEVBRAIN_EMBED_TIMEOUT_MS ?? 30000)

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
