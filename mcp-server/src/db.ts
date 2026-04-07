import pg from 'pg'
import { readFileSync } from 'fs'
import { parse } from 'yaml'
import { resolve } from 'path'

const configPath = resolve(import.meta.dirname, '../../config/devbrain.yaml')
const config = parse(readFileSync(configPath, 'utf-8'))

// Lazy pool initialization — don't connect until first query
let _pool: pg.Pool | null = null

function getPool(): pg.Pool {
  if (!_pool) {
    _pool = new pg.Pool({
      host: config.database.host,
      port: config.database.port,
      user: config.database.user,
      password: config.database.password,
      database: config.database.database,
      max: 5,
      connectionTimeoutMillis: 5000,
      idleTimeoutMillis: 30000,
    })
  }
  return _pool
}

export { config }

export async function query<T extends pg.QueryResultRow = Record<string, unknown>>(
  text: string,
  params?: unknown[],
): Promise<pg.QueryResult<T>> {
  return getPool().query<T>(text, params)
}
