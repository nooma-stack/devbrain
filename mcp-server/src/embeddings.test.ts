// Query-vs-document embedding prefix tests. No DB, no Ollama: fetch is
// stubbed and the config points at a throwaway yaml.
//
// Run: npm test   (node --import tsx --test src/*.test.ts)
import { after, before, beforeEach, describe, test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

const dir = mkdtempSync(join(tmpdir(), 'devbrain-embed-test-'))
const cfgPath = join(dir, 'devbrain.yaml')
writeFileSync(
  cfgPath,
  [
    'database: {host: localhost, port: 1, user: x, password: x, database: x}',
    'embedding: {url: "http://ollama.invalid", model: snowflake-arctic-embed2}',
    'summarization: {url: "http://ollama.invalid", model: x}',
    'chunking: {max_tokens: 400, overlap_tokens: 80}',
    '',
  ].join('\n'),
)
process.env.DEVBRAIN_CONFIG = cfgPath

type EmbeddingsModule = typeof import('./embeddings.js')
let mod: EmbeddingsModule
let sent: unknown[] = []
const realFetch = globalThis.fetch

before(async () => {
  globalThis.fetch = (async (_url: unknown, init?: { body?: unknown }) => {
    const body = JSON.parse(String(init?.body))
    sent.push(body.input)
    const n = Array.isArray(body.input) ? body.input.length : 1
    return new Response(JSON.stringify({ embeddings: Array.from({ length: n }, () => [0.1, 0.2]) }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }) as typeof fetch
  mod = await import('./embeddings.js')
})

after(() => {
  globalThis.fetch = realFetch
  rmSync(dir, { recursive: true, force: true })
})

beforeEach(() => {
  sent = []
  delete process.env.DEVBRAIN_QUERY_PREFIX
})

describe('query embeddings', () => {
  test('embedQuery prefixes the query with "query: " by default for arctic-embed', async () => {
    await mod.embedQuery('where is the hnsw index built')
    assert.deepEqual(sent, ['query: where is the hnsw index built'])
  })

  test('DEVBRAIN_QUERY_PREFIX overrides the prefix', async () => {
    process.env.DEVBRAIN_QUERY_PREFIX = 'search_query: '
    await mod.embedQuery('x')
    assert.deepEqual(sent, ['search_query: x'])
  })

  test('empty DEVBRAIN_QUERY_PREFIX disables the prefix', async () => {
    process.env.DEVBRAIN_QUERY_PREFIX = ''
    await mod.embedQuery('raw query')
    assert.deepEqual(sent, ['raw query'])
  })
})

describe('document embeddings', () => {
  test('embed sends the document text unchanged', async () => {
    await mod.embed('Decision title\nbody')
    assert.deepEqual(sent, ['Decision title\nbody'])
  })

  test('embedBatch sends documents unchanged', async () => {
    await mod.embedBatch(['a', 'b'])
    assert.deepEqual(sent, [['a', 'b']])
  })

  test('documents stay bare even when a query prefix is configured', async () => {
    process.env.DEVBRAIN_QUERY_PREFIX = 'query: '
    await mod.embed('doc')
    assert.deepEqual(sent, ['doc'])
  })
})

describe('call sites', () => {
  // Guards against a write path (store / breadcrumb / end_session) being
  // switched to embedQuery, or deep_search being switched back to embed.
  const src = readFileSync(resolve(import.meta.dirname, 'index.ts'), 'utf-8')

  test('deep_search is the only embedQuery caller', () => {
    const calls = src.match(/\bembedQuery\(/g) ?? []
    assert.equal(calls.length, 1)
    const deepSearchStart = src.indexOf("'deep_search',")
    const nextTool = src.indexOf('server.tool(', deepSearchStart)
    const at = src.indexOf('embedQuery(')
    assert.ok(at > deepSearchStart && at < nextTool, 'embedQuery call is inside deep_search')
  })
})
