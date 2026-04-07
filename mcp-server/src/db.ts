import pg from 'pg'
import { readFileSync } from 'fs'
import { parse } from 'yaml'
import { resolve } from 'path'

const configPath = resolve(import.meta.dirname, '../../config/devbrain.yaml')
const config = parse(readFileSync(configPath, 'utf-8'))

const pool = new pg.Pool({
  host: config.database.host,
  port: config.database.port,
  user: config.database.user,
  password: config.database.password,
  database: config.database.database,
  max: 10,
})

export { pool, config }

export async function query<T extends pg.QueryResultRow = Record<string, unknown>>(
  text: string,
  params?: unknown[],
): Promise<pg.QueryResult<T>> {
  return pool.query<T>(text, params)
}
