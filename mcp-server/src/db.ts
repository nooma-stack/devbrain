import pg from 'pg'
import { readFileSync } from 'fs'
import { parse } from 'yaml'
import { resolve } from 'path'

// Raw config (including database.password) stays module-private. Expose
// narrow accessors below so consumers get only the fields they need and
// credentials don't propagate into every file that happens to need a
// chunking limit or Ollama URL.
// DEVBRAIN_CONFIG env var (documented in .env.example) overrides the
// default location. Without this honored, tests can't point the MCP at
// a controlled config and the documented escape hatch is a no-op.
const configPath = process.env.DEVBRAIN_CONFIG ||
  resolve(import.meta.dirname, '../../config/devbrain.yaml')
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
    // HNSW search depth for every vector ORDER BY ... LIMIT in this
    // process (deep_search, recency neighbors, supersedes walks). The
    // pgvector default (40) measurably drops true top-10 members on this
    // corpus — a rank-#3 breadcrumb was reproducibly absent from top-10
    // until ef_search 200 (2026-08-27; 19ms vs 992ms for an exact scan,
    // so the recall headroom is nearly free). Applied per-connection:
    // SET is session-scoped and pool connections are long-lived.
    _pool.on('connect', (client) => {
      client.query("SET hnsw.ef_search = 200").catch(() => {})
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
  try {
    return await getPool().query<T>(text, params)
  } catch (err) {
    // Rewrap connection-time failures with an actionable hint so the
    // user sees "DB unreachable, start Docker" rather than a bare
    // "ECONNREFUSED 127.0.0.1:5433" that doesn't say what to do.
    throw _friendlyDbError(err)
  }
}

// ─── Connection diagnostics ──────────────────────────────────────────────────


/**
 * Map low-level pg / network errors to a single actionable message.
 * The original error is preserved as `.cause`.
 *
 * Recognised low-level codes:
 *   * `ECONNREFUSED`  — port not listening (Docker stopped, container down)
 *   * `ETIMEDOUT`     — packet dropped (firewall, host down)
 *   * `ENOTFOUND`     — DNS / hostname unresolvable
 *   * `28P01`         — pg auth: wrong password
 *   * `3D000`         — pg auth: database does not exist
 */
function _friendlyDbError(err: unknown): Error {
  if (!(err instanceof Error)) {
    return new Error(String(err))
  }
  // pg's code shape: ECONNREFUSED for network, SQLSTATE strings for auth.
  const code = (err as NodeJS.ErrnoException & { code?: string }).code
  const host = _config.database.host
  const port = _config.database.port
  let hint = err.message
  switch (code) {
    case 'ECONNREFUSED':
    case 'ETIMEDOUT':
      hint =
        `DevBrain MCP: cannot reach Postgres at ${host}:${port} (${code}). ` +
        `If you're on the laptop, start Docker Desktop and run ` +
        `\`cd /Users/$(whoami)/devbrain && docker compose up -d\` to bring ` +
        `the DB up. If you're on Mac Studio, check \`docker ps\` for the ` +
        `\`devbrain-db\` container.`
      break
    case 'ENOTFOUND':
      hint =
        `DevBrain MCP: hostname ${host} does not resolve. Check the ` +
        `database.host setting in config/devbrain.yaml.`
      break
    case '28P01':
      hint =
        `DevBrain MCP: Postgres rejected the credentials for user ` +
        `${_config.database.user}. Check the password in your .env.`
      break
    case '3D000':
      hint =
        `DevBrain MCP: database ${_config.database.database} does not exist ` +
        `on ${host}:${port}. Check the database.database setting in config/devbrain.yaml.`
      break
    default:
      // Other errors flow through unchanged.
      return err
  }
  const wrapped = new Error(hint)
  ;(wrapped as Error & { cause?: unknown }).cause = err
  return wrapped
}

/**
 * Probe the DB pool with a fast `SELECT 1`. Used at MCP server startup
 * to fail fast (and loudly) if the DB is unreachable, rather than letting
 * the first tool call hang or return an opaque error.
 *
 * `timeoutMs` bounds the probe; matches the pool's connectionTimeoutMillis
 * by default.
 *
 * Throws the same friendly-wrapped error that `query()` would, so the
 * caller can log it and exit.
 */
export async function waitForDb(timeoutMs: number = 5000): Promise<void> {
  const pool = getPool()
  // We need a hard wall-clock cap. The pool's connectionTimeoutMillis
  // bounds individual connection-acquire attempts but not the full
  // pool.query() retry behavior, so use Promise.race.
  let timeoutId: NodeJS.Timeout | null = null
  const timeout = new Promise<never>((_, reject) => {
    timeoutId = setTimeout(
      () => reject(new Error(`DB probe timed out after ${timeoutMs}ms`)),
      timeoutMs,
    )
  })
  try {
    await Promise.race([pool.query('SELECT 1'), timeout])
  } catch (err) {
    throw _friendlyDbError(err)
  } finally {
    if (timeoutId) clearTimeout(timeoutId)
  }
}

// Test-only: lets the test suite swap the config without touching the
// module-private singleton. Not exposed to runtime callers.
export function _testOnlyOverrideConfig(overrides: {
  host?: string
  port?: number
  user?: string
  database?: string
}): void {
  Object.assign(_config.database, overrides)
  // Force pool re-init on next getPool() call so the override takes effect.
  if (_pool) {
    _pool.end().catch(() => {})
    _pool = null
  }
}
