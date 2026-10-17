// Hybrid retrieval helpers: RRF fusion, config resolution, keyword SQL.
// No DB, no network.
//
// Run: npm test   (node --import tsx --test src/*.test.ts)
import { describe, test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import {
  SEARCH_DEFAULTS,
  collapseNearDuplicates,
  keywordParams,
  keywordSql,
  matchedBy,
  nearDupSql,
  resolveSearchConfig,
  rrfFuse,
} from './hybrid.js'

describe('rrfFuse', () => {
  test('a key in both lists beats keys found by one list', () => {
    const fused = rrfFuse([['a', 'b', 'c'], ['c', 'd']], [1, 1], 60, 10)
    assert.deepEqual(fused, ['c', 'a', 'b', 'd'])
  })

  test('weight 0 for the keyword list keeps vector order', () => {
    const fused = rrfFuse([['a', 'b', 'c'], ['c', 'z']], [1, 0], 60, 3)
    assert.deepEqual(fused, ['a', 'b', 'c'])
  })

  test('equal fused scores fall to the tiebreak (cosine), then first seen', () => {
    // a: vector rank 1; x: keyword rank 1 → same score at equal weights.
    const cos = new Map([['a', 0.5], ['x', 0.9]])
    assert.deepEqual(rrfFuse([['a'], ['x']], [1, 1], 60, 2, cos), ['x', 'a'])
    assert.deepEqual(rrfFuse([['a'], ['x']], [1, 1], 60, 2), ['a', 'x'])
  })

  test('a key repeated within one list counts once, at its best rank', () => {
    const fused = rrfFuse([['a', 'b', 'a']], [1], 60, 10)
    assert.deepEqual(fused, ['a', 'b'])
  })

  test('limit slices the fused order', () => {
    assert.equal(rrfFuse([['a', 'b', 'c']], [1], 60, 2).length, 2)
  })

  test('matches the Python audit mirror on a fixed case', () => {
    // Same inputs as test_rrf_fuse_matches_mcp in ingest/tests/test_recall_audit.py.
    const fused = rrfFuse(
      [['m1', 'm2', 'm3', 'm4'], ['m4', 'm9', 'm1']],
      [1, 1],
      60,
      5,
      new Map([['m9', 0.7], ['m2', 0.8]]),
    )
    assert.deepEqual(fused, ['m1', 'm4', 'm2', 'm9', 'm3'])
  })
})

describe('matchedBy', () => {
  test('labels the channel(s) that returned a key', () => {
    const v = new Set(['a', 'b'])
    const k = new Set(['b', 'c'])
    assert.equal(matchedBy('a', v, k), 'vector')
    assert.equal(matchedBy('b', v, k), 'both')
    assert.equal(matchedBy('c', v, k), 'keyword')
  })
})

describe('resolveSearchConfig', () => {
  test('defaults when nothing is configured', () => {
    assert.deepEqual(resolveSearchConfig(undefined, {}), SEARCH_DEFAULTS)
  })

  test('yaml overrides defaults', () => {
    const cfg = resolveSearchConfig(
      { hybrid: false, rrf_k: 30, keyword_weight: 0.5, candidates: 80, keyword_terms: 'all', keyword_timeout_ms: 400 },
      {},
    )
    assert.equal(cfg.hybrid, false)
    assert.equal(cfg.rrfK, 30)
    assert.equal(cfg.keywordWeight, 0.5)
    assert.equal(cfg.candidates, 80)
    assert.equal(cfg.keywordIdentifiersOnly, false)
    assert.equal(cfg.keywordTimeoutMs, 400)
  })

  test('env overrides yaml', () => {
    const cfg = resolveSearchConfig(
      { hybrid: true, rrf_k: 30, keyword_weight: 0.5, keyword_terms: 'all' },
      { DEVBRAIN_HYBRID_SEARCH: 'off', DEVBRAIN_RRF_K: '90', DEVBRAIN_KEYWORD_WEIGHT: '2', DEVBRAIN_KEYWORD_TERMS: 'identifiers' },
    )
    assert.equal(cfg.hybrid, false)
    assert.equal(cfg.rrfK, 90)
    assert.equal(cfg.keywordWeight, 2)
    assert.equal(cfg.keywordIdentifiersOnly, true)
  })

  test('accepts on/off, true/false, 1/0 for the flag', () => {
    for (const [v, want] of [['on', true], ['1', true], ['true', true], ['off', false], ['0', false], ['false', false]] as const) {
      assert.equal(resolveSearchConfig({}, { DEVBRAIN_HYBRID_SEARCH: v }).hybrid, want, v)
    }
  })

  test('invalid values fall back instead of failing a search', () => {
    const cfg = resolveSearchConfig(
      { rrf_k: -1, candidates: 0, keyword_max_df: 5, keyword_terms: 'some' },
      { DEVBRAIN_HYBRID_SEARCH: 'maybe', DEVBRAIN_KEYWORD_WEIGHT: 'x' },
    )
    assert.equal(cfg.hybrid, SEARCH_DEFAULTS.hybrid)
    assert.equal(cfg.rrfK, SEARCH_DEFAULTS.rrfK)
    assert.equal(cfg.candidates, SEARCH_DEFAULTS.candidates)
    assert.equal(cfg.keywordMaxDf, SEARCH_DEFAULTS.keywordMaxDf)
    assert.equal(cfg.keywordWeight, SEARCH_DEFAULTS.keywordWeight)
    assert.equal(cfg.keywordIdentifiersOnly, SEARCH_DEFAULTS.keywordIdentifiersOnly)
  })
})

describe('keyword SQL', () => {
  const sql = keywordSql()

  test('uses exactly the placeholders keywordParams supplies', () => {
    const params = keywordParams('q', '[0]', { projectId: null, kinds: null, chunkSourceTypes: null }, SEARCH_DEFAULTS)
    const used = new Set([...sql.matchAll(/\$(\d+)/g)].map((m) => Number(m[1])))
    assert.deepEqual([...used].sort((a, b) => a - b), params.map((_, i) => i + 1))
  })

  test('has no percent sign (the Python audit runs it through psycopg2)', () => {
    assert.ok(!sql.includes('%'))
  })

  test('is gated on the FTS index and matches its expression', () => {
    assert.match(sql, /to_regclass\('devbrain\.idx_memory_fts_simple'\) AND indisvalid/)
    const migration = readFileSync(
      resolve(import.meta.dirname, '../../migrations/047_memory_fts_index.sql'),
      'utf-8',
    )
    const expr = "to_tsvector('simple'::regconfig, coalesce(title, '') || ' ' || content)"
    assert.ok(migration.includes(expr), 'migration builds the expression the SQL searches')
    // The planner only uses an expression index for the same expression.
    assert.ok(sql.includes(expr.replace('title', 'm.title').replace('content)', 'm.content)')))
  })

  test('applies the archived filter deep_search uses', () => {
    assert.match(sql, /m\.archived_at IS NULL/)
    assert.match(sql, /m\.embedding IS NOT NULL/)
  })
})

describe('deep_search wiring', () => {
  const src = readFileSync(resolve(import.meta.dirname, 'index.ts'), 'utf-8')
  const start = src.indexOf("'deep_search',")
  const end = src.indexOf('server.tool(', start)
  const body = src.slice(start, end)

  test('keyword channel runs under a statement timeout and falls back on error', () => {
    assert.match(body, /queryWithTimeout\(\s*keywordSql\(\)/)
    assert.match(body, /keyword channel skipped/)
  })

  test('fuses with the configured RRF weight and k', () => {
    assert.match(body, /rrfFuse\([\s\S]*searchCfg\.keywordWeight[\s\S]*searchCfg\.rrfK/)
  })
})

describe('collapseNearDuplicates', () => {
  const pairs: Array<[string, string]> = [['a', 'c'], ['a', 'e'], ['b', 'd']]

  test('copies join the best-ranked representative they match', () => {
    const groups = collapseNearDuplicates(['a', 'b', 'c', 'd', 'e', 'f'], pairs, 10)
    assert.deepEqual(groups, [
      { rep: 'a', similar: ['c', 'e'] },
      { rep: 'b', similar: ['d'] },
      { rep: 'f', similar: [] },
    ])
  })

  test('limit counts groups, and copies beyond it are still attached', () => {
    const groups = collapseNearDuplicates(['a', 'b', 'c', 'd', 'e', 'f'], pairs, 2)
    assert.deepEqual(groups.map((g) => g.rep), ['a', 'b'])
    assert.deepEqual(groups[0].similar, ['c', 'e'])
  })

  test('no chaining: similarity is to the representative only', () => {
    // c ~ a, d ~ c, but d is not similar to a → d opens its own group.
    const groups = collapseNearDuplicates(['a', 'c', 'd'], [['a', 'c'], ['c', 'd']], 10)
    assert.deepEqual(groups, [{ rep: 'a', similar: ['c'] }, { rep: 'd', similar: [] }])
  })

  test('newest representative keeps the slot and lists the others', () => {
    const created = new Map([['a', 1], ['c', 3], ['e', 2], ['b', 5], ['d', 4]])
    const groups = collapseNearDuplicates(['a', 'b', 'c', 'd', 'e'], pairs, 10, created)
    assert.deepEqual(groups, [
      { rep: 'c', similar: ['a', 'e'] },
      { rep: 'b', similar: ['d'] },
    ])
  })

  test('matches the Python audit mirror on a fixed case', () => {
    // Same inputs as test_collapse_matches_mcp in ingest/tests/test_recall_audit.py.
    const groups = collapseNearDuplicates(
      ['m1', 'm2', 'm3', 'm4', 'm5', 'm6'],
      [['m1', 'm3'], ['m2', 'm3'], ['m4', 'm6'], ['m5', 'm1']],
      3,
      new Map([['m1', 10], ['m3', 30], ['m5', 20], ['m4', 5], ['m6', 5]]),
    )
    assert.deepEqual(groups, [
      { rep: 'm3', similar: ['m1', 'm5'] },
      { rep: 'm2', similar: [] },
      { rep: 'm4', similar: ['m6'] },
    ])
  })
})

describe('dedup config', () => {
  test('defaults', () => {
    const cfg = resolveSearchConfig({}, {})
    assert.equal(cfg.dedup, true)
    assert.equal(cfg.dedupThreshold, 0.93)
    assert.equal(cfg.dedupRepresentative, 'newest')
  })

  test('env > yaml, invalid falls back', () => {
    const cfg = resolveSearchConfig(
      { dedup: true, dedup_threshold: 0.95, dedup_representative: 'best', dedup_timeout_ms: 200 },
      { DEVBRAIN_DEDUP_SEARCH: 'off', DEVBRAIN_DEDUP_THRESHOLD: '0.92' },
    )
    assert.equal(cfg.dedup, false)
    assert.equal(cfg.dedupThreshold, 0.92)
    assert.equal(cfg.dedupRepresentative, 'best')
    assert.equal(cfg.dedupTimeoutMs, 200)
    const bad = resolveSearchConfig({ dedup_threshold: 3, dedup_representative: 'oldest' }, {})
    assert.equal(bad.dedupThreshold, SEARCH_DEFAULTS.dedupThreshold)
    assert.equal(bad.dedupRepresentative, SEARCH_DEFAULTS.dedupRepresentative)
  })
})

describe('near-duplicate SQL', () => {
  const sql = nearDupSql()
  test('takes ids and threshold only, no percent sign', () => {
    const used = new Set([...sql.matchAll(/\$(\d+)/g)].map((m) => Number(m[1])))
    assert.deepEqual([...used].sort(), [1, 2])
    assert.ok(!sql.includes('%'))
  })
  test('returns the columns deep_search reads', () => {
    for (const col of ['a_id', 'b_id', 'a_created', 'b_created']) assert.match(sql, new RegExp(`AS ${col}\\b`))
  })
  test('deep_search runs it under a timeout and falls back', () => {
    const src = readFileSync(resolve(import.meta.dirname, 'index.ts'), 'utf-8')
    assert.match(src, /queryWithTimeout<[\s\S]*?>\(nearDupSql\(\)/)
    assert.match(src, /near-duplicate collapse skipped/)
  })
})

describe('recency neighbor strategy', () => {
  test('defaults to hnsw with a bounded scan', () => {
    const cfg = resolveSearchConfig({}, {})
    assert.equal(cfg.recencyStrategy, 'hnsw')
    assert.equal(cfg.recencyMaxScanTuples, 2000)
    assert.equal(cfg.recencyTimeoutMs, 2000)
  })

  test('env > yaml, invalid falls back', () => {
    const cfg = resolveSearchConfig(
      { recency_strategy: 'hnsw', recency_max_scan_tuples: 5000, recency_timeout_ms: 800 },
      { DEVBRAIN_RECENCY_STRATEGY: 'exact' },
    )
    assert.equal(cfg.recencyStrategy, 'exact')
    assert.equal(cfg.recencyMaxScanTuples, 5000)
    assert.equal(cfg.recencyTimeoutMs, 800)
    const bad = resolveSearchConfig({ recency_strategy: 'fast', recency_max_scan_tuples: 5 }, {})
    assert.equal(bad.recencyStrategy, 'hnsw')
    assert.equal(bad.recencyMaxScanTuples, 2000)
  })

  for (const file of ['recency_neighbors.sql', 'earliest_on_topic.sql']) {
    test(`${file} takes $1..$4 and has no percent sign`, () => {
      const sql = readFileSync(resolve(import.meta.dirname, '../sql', file), 'utf-8')
      const used = new Set([...sql.matchAll(/\$(\d+)/g)].map((m) => Number(m[1])))
      assert.deepEqual([...used].sort(), [1, 2, 3, 4])
      assert.ok(!sql.includes('%'))
      assert.match(sql, /ORDER BY m\.embedding <=> p\.embedding/)
    })
  }

  test('recency.ts runs the shared SQL under the hnsw planner settings', () => {
    const src = readFileSync(resolve(import.meta.dirname, 'recency.ts'), 'utf-8')
    assert.match(src, /sqlFile\('recency_neighbors\.sql'\)/)
    assert.match(src, /sqlFile\('earliest_on_topic\.sql'\)/)
    assert.match(src, /enable_sort: 'off'/)
    assert.match(src, /'hnsw\.max_scan_tuples': String\(strategy\.maxScanTuples\)/)
    assert.ok(!/const sql = `\s*WITH primaries/.test(src), 'no inline copy of the neighbor SQL left')
  })

  test('deep_search passes the configured strategy to both annotations', () => {
    const src = readFileSync(resolve(import.meta.dirname, 'index.ts'), 'utf-8')
    assert.match(src, /expandRecencyNeighbors\(memoryBackedIds, \{[\s\S]*?\}, neighborStrategy\)/)
    assert.match(src, /findEarliestOnTopic\(memoryBackedIds, undefined, neighborStrategy\)/)
  })
})
