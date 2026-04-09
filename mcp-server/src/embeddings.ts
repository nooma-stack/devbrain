import { config } from './db.js'

const OLLAMA_URL = config.embedding.url
const EMBED_MODEL = config.embedding.model

// mxbai-embed-large has a 512 token context window (~4 chars/token).
const MAX_EMBED_CHARS = 512 * 4

function truncate(text: string): string {
  return text.length <= MAX_EMBED_CHARS ? text : text.slice(0, MAX_EMBED_CHARS)
}

export async function embed(text: string): Promise<number[]> {
  const response = await fetch(`${OLLAMA_URL}/api/embed`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model: EMBED_MODEL, input: truncate(text) }),
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
    body: JSON.stringify({ model: EMBED_MODEL, input: texts.map(truncate) }),
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
