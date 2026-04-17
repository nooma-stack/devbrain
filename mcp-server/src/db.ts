import pg from 'pg'
import { readFileSync } from 'fs'
import { parse } from 'yaml'
import { resolve } from 'path'

// Raw config (including database.password) stays module-private. Expose
// narrow accessors below so consumers get only the fields they need and
// credentials don't propagate into every file that happens to need a
// chunking limit or Ollama URL.
const configPath = resolve(import.meta.dirname, '../../config/devbrain.yaml')
const _config = parse(readFileSync(configPath, 'utf-8'))

let _pool: pg.Pool | null = null

function getPool(): pg.Pool {
  if (!_pool) {
    _pool = new pg.Pool({
      host: _config.database.host,
      port: _config.database.port,
      user: _config.database.user,
      password: _config.database.password,
      database: _config.database.database,
      max: 5,
      connectionTimeoutMillis: 5000,
      idleTimeoutMillis: 30000,
    })
  }
  return _pool
}

export interface ChunkingConfig {
  max_tokens: number
  overlap_tokens: number
}

export interface OllamaConfig {
  url: string
  model: string
}

export function getChunkingConfig(): ChunkingConfig {
  return {
    max_tokens: _config.chunking.max_tokens,
    overlap_tokens: _config.chunking.overlap_tokens,
  }
}

export function getEmbeddingConfig(): OllamaConfig {
  return {
    url: _config.embedding.url,
    model: _config.embedding.model,
  }
}

export function getSummarizationConfig(): OllamaConfig {
  return {
    url: _config.summarization.url,
    model: _config.summarization.model,
  }
}

export async function query<T extends pg.QueryResultRow = Record<string, unknown>>(
  text: string,
  params?: unknown[],
): Promise<pg.QueryResult<T>> {
  return getPool().query<T>(text, params)
}
