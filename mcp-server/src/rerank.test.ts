// Reranker client + rerank helpers. No network: fetch is injected.
//
// Run: npm test   (node --import tsx --test src/*.test.ts)
import { beforeEach, describe, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { SEARCH_DEFAULTS, applyRerank, isLoopbackUrl, rerankDocText, resolveSearchConfig } from './hybrid.js'
import { _resetRerankBreaker, rerankScores } from './rerank.js'

const okFetch = (scores: unknown) =>
  (async () => new Response(JSON.stringify({ scores }), { status: 200 })) as unknown as typeof fetch

describe('rerankScores', () => {
  beforeEach(() => _resetRerankBreaker())

  test('returns the service scores in document order', async () => {
    let sent: unknown
    const f = (async (_u: unknown, init?: { body?: unknown }) => {
      sent = JSON.parse(String(init?.body))
      return new Response(JSON.stringify({ scores: [0.2, 0.9] }), { status: 200 })
    }) as unknown as typeof fetch
    const r = await rerankScores('http://127.0.0.1:18600', 'q', ['a', 'b'], 500, f)
    assert.deepEqual(r?.scores, [0.2, 0.9])
    assert.deepEqual(sent, { query: 'q', documents: ['a', 'b'] })
  })

  test('refuses non-loopback URLs without calling fetch', async () => {
    let called = false
    const f = (async () => { called = true; return new Response('{}') }) as unknown as typeof fetch
    assert.equal(await rerankScores('http://10.0.0.5:18600', 'q', ['a'], 500, f), null)
    assert.equal(called, false)
  })

  test('a refused connection opens the breaker at once', async () => {
    const refused = (async () => {
      throw Object.assign(new TypeError('fetch failed'), { cause: { code: 'ECONNREFUSED' } })
    }) as unknown as typeof fetch
    assert.equal(await rerankScores('http://127.0.0.1:1', 'q', ['a'], 500, refused), null)
    // Breaker open: even a good service is skipped until it closes.
    assert.equal(await rerankScores('http://127.0.0.1:1', 'q', ['a'], 500, okFetch([1])), null)
    _resetRerankBreaker()
    assert.deepEqual((await rerankScores('http://127.0.0.1:1', 'q', ['a'], 500, okFetch([1])))?.scores, [1])
  })

  test('bad responses open the breaker only after three in a row', async () => {
    const http500 = (async () => new Response('{}', { status: 500 })) as unknown as typeof fetch
    assert.equal(await rerankScores('http://127.0.0.1:1', 'q', ['a', 'b'], 500, okFetch([1])), null) // malformed
    assert.equal(await rerankScores('http://127.0.0.1:1', 'q', ['a'], 500, http500), null)
    // Two failures: still trying.
    assert.deepEqual((await rerankScores('http://127.0.0.1:1', 'q', ['a'], 500, okFetch([2])))?.scores, [2])
    for (let i = 0; i < 3; i++) await rerankScores('http://127.0.0.1:1', 'q', ['a'], 500, http500)
    assert.equal(await rerankScores('http://127.0.0.1:1', 'q', ['a'], 500, okFetch([3])), null)
  })

  test('a timeout returns null', async () => {
    // AbortSignal.timeout's timer is unref'd: keep a ref'd timer alive so the
    // test runner does not see an empty event loop before the abort fires.
    const slow = ((_u: unknown, init?: { signal?: AbortSignal }) =>
      new Promise((_res, rej) => {
        const keepAlive = setTimeout(() => rej(new Error('late')), 5000)
        init?.signal?.addEventListener('abort', () => {
          clearTimeout(keepAlive)
          rej(new Error('aborted'))
        })
      })) as unknown as typeof fetch
    assert.equal(await rerankScores('http://127.0.0.1:1', 'q', ['a'], 20, slow), null)
  })
})

describe('applyRerank', () => {
  test('reorders the head by score and keeps the tail', () => {
    const scores = new Map([['a', 0.1], ['b', 0.9], ['c', 0.5]])
    assert.deepEqual(applyRerank(['a', 'b', 'c', 'd', 'e'], scores, 3, 'rerank'), ['b', 'c', 'a', 'd', 'e'])
  })

  test('unscored head keys (codebase rows) follow the scored ones', () => {
    const scores = new Map([['a', 0.1], ['c', 0.5]])
    assert.deepEqual(applyRerank(['a', 'codebase:x', 'c', 'd'], scores, 3, 'rerank'), ['c', 'a', 'codebase:x', 'd'])
  })

  test('rrf blend fuses reranker order with the incoming order', () => {
    // Same inputs as test_apply_rerank_matches_mcp in ingest/tests/test_recall_audit.py.
    const scores = new Map([['m1', 0.1], ['m2', 0.8], ['m3', 0.9], ['m4', 0.2]])
    assert.deepEqual(applyRerank(['m1', 'm2', 'm3', 'm4', 'm5'], scores, 4, 'rrf'), ['m3', 'm2', 'm1', 'm4', 'm5'])
    assert.deepEqual(applyRerank(['m1', 'm2', 'm3', 'm4', 'm5'], scores, 4, 'rerank'), ['m3', 'm2', 'm4', 'm1', 'm5'])
  })
})

describe('rerankDocText', () => {
  test('prepends the title unless the content starts with it', () => {
    assert.equal(rerankDocText('Use HNSW', 'Because recall.'), 'Use HNSW\nBecause recall.')
    assert.equal(rerankDocText('Use HNSW', 'Use HNSW\nBecause recall.'), 'Use HNSW\nBecause recall.')
    assert.equal(rerankDocText(null, 'body'), 'body')
    assert.equal(rerankDocText('t', 'x'.repeat(5000)).length, 2000)
  })
})

describe('rerank config', () => {
  test('defaults', () => {
    const cfg = resolveSearchConfig({}, {})
    assert.equal(cfg.rerank, true)
    assert.equal(cfg.rerankUrl, 'http://127.0.0.1:18600')
    assert.equal(cfg.rerankCandidates, 30)
    assert.equal(cfg.rerankBlend, 'rrf')
  })

  test('env > yaml; non-loopback URLs are ignored', () => {
    const cfg = resolveSearchConfig(
      { rerank: true, rerank_url: 'http://localhost:18601/', rerank_candidates: 20, rerank_blend: 'rerank' },
      { DEVBRAIN_RERANK: 'off' },
    )
    assert.equal(cfg.rerank, false)
    assert.equal(cfg.rerankUrl, 'http://localhost:18601')
    assert.equal(cfg.rerankCandidates, 20)
    assert.equal(cfg.rerankBlend, 'rerank')
    const remote = resolveSearchConfig({ rerank_url: 'http://example.com:18600' }, { DEVBRAIN_RERANK_URL: 'http://192.168.0.9:1' })
    assert.equal(remote.rerankUrl, SEARCH_DEFAULTS.rerankUrl)
  })

  test('isLoopbackUrl', () => {
    for (const u of ['http://127.0.0.1:18600', 'http://localhost:1', 'http://[::1]:18600', 'https://127.1.2.3']) {
      assert.ok(isLoopbackUrl(u), u)
    }
    for (const u of ['http://0.0.0.0:1', 'http://10.0.0.1', 'ftp://127.0.0.1', 'http://127.0.0.1.example.com', 'nope']) {
      assert.ok(!isLoopbackUrl(u), u)
    }
  })
})

describe('deep_search wiring', () => {
  const src = readFileSync(resolve(import.meta.dirname, 'index.ts'), 'utf-8')
  test('reranks before the near-duplicate collapse', () => {
    const r = src.indexOf('rerankScores(searchCfg.rerankUrl')
    const d = src.indexOf('nearDupSql()')
    assert.ok(r > 0 && d > r)
  })
  test('memory and keyword queries return the title the reranker scores', () => {
    assert.match(src, /m\.kind as memory_kind,\s*m\.title,/)
    const kw = readFileSync(resolve(import.meta.dirname, '../sql/keyword_search.sql'), 'utf-8')
    assert.match(kw, /memory_id, memory_kind, title, content,/)
  })
})

describe('weak-match flag', () => {
  test('group strength prefers reranker scores, falls back to cosine', async () => {
    const { groupStrength } = await import('./hybrid.js')
    const rr = new Map([['b', 0.9]])
    const cos = new Map([['a', 0.5], ['b', 0.4], ['c', 0.7]])
    assert.deepEqual(groupStrength(['a', 'b'], rr, cos), { score: 0.9, basis: 'rerank' })
    assert.deepEqual(groupStrength(['a', 'c'], rr, cos), { score: 0.7, basis: 'cosine' })
    assert.deepEqual(groupStrength(['a', 'c'], null, cos), { score: 0.7, basis: 'cosine' })
    assert.equal(groupStrength(['z'], null, cos), undefined)
  })

  test('response strength uses the reranker scale when any group has it', async () => {
    const { responseStrength } = await import('./hybrid.js')
    assert.deepEqual(
      responseStrength([{ score: 0.6, basis: 'cosine' }, { score: 0.85, basis: 'rerank' }, undefined]),
      { score: 0.85, basis: 'rerank' },
    )
    assert.deepEqual(responseStrength([{ score: 0.6, basis: 'cosine' }]), { score: 0.6, basis: 'cosine' })
    assert.equal(responseStrength([]), undefined)
  })

  test('isWeak applies the basis threshold; nothing returned is weak', async () => {
    const { isWeak } = await import('./hybrid.js')
    const cfg = resolveSearchConfig({}, {})
    assert.equal(isWeak({ score: 0.87, basis: 'rerank' }, cfg), true)
    assert.equal(isWeak({ score: 0.95, basis: 'rerank' }, cfg), false)
    assert.equal(isWeak({ score: 0.40, basis: 'cosine' }, cfg), true)
    assert.equal(isWeak({ score: 0.60, basis: 'cosine' }, cfg), false)
    assert.equal(isWeak(undefined, cfg), true)
  })

  test('config: defaults, env switch, yaml thresholds', () => {
    const d = resolveSearchConfig({}, {})
    assert.equal(d.weakMatch, true)
    assert.equal(d.weakMatchRerankThreshold, 0.88)
    assert.equal(d.weakMatchCosineThreshold, 0.41)
    const c = resolveSearchConfig({ weak_match_rerank_threshold: 0.8, weak_match_cosine_threshold: 0.5 }, { DEVBRAIN_WEAK_MATCH: 'off' })
    assert.equal(c.weakMatch, false)
    assert.equal(c.weakMatchRerankThreshold, 0.8)
    assert.equal(c.weakMatchCosineThreshold, 0.5)
  })

  test('deep_search puts weak_match and the note ahead of the results', () => {
    const src = readFileSync(resolve(import.meta.dirname, 'index.ts'), 'utf-8')
    const w = src.indexOf('responseBody.weak_match = responseWeak')
    const r = src.indexOf('responseBody.results = results')
    assert.ok(w > 0 && r > w)
    assert.match(src, /if \(searchCfg\.weakMatch && isWeak\(strength, searchCfg\)\) r\.weak_match = true/)
  })
})
