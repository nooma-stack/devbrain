// Relation-check mode + spawn-argument tests. No DB, no Python, no Ollama.
//
// Run: npm test   (node --import tsx --test src/*.test.ts)
import { after, afterEach, describe, test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const dir = mkdtempSync(join(tmpdir(), 'devbrain-relation-test-'))
const cfgPath = join(dir, 'devbrain.yaml')
writeFileSync(cfgPath, 'write_path:\n  relation_check:\n    mode: shadow\n')
process.env.DEVBRAIN_CONFIG = cfgPath

const { normalizeMode, relateArgs, relationCheckMode, triggerRelationCheckInBackground } =
  await import('./relation.js')

after(() => rmSync(dir, { recursive: true, force: true }))
afterEach(() => {
  delete process.env.DEVBRAIN_RELATION_CHECK
})

describe('normalizeMode', () => {
  test('accepts the three modes, case-insensitive', () => {
    assert.equal(normalizeMode('off'), 'off')
    assert.equal(normalizeMode('SHADOW'), 'shadow')
    assert.equal(normalizeMode(' on '), 'on')
  })
  test('YAML booleans map to on/off', () => {
    assert.equal(normalizeMode(true), 'on')
    assert.equal(normalizeMode(false), 'off')
  })
  test('anything else is off (safe default)', () => {
    assert.equal(normalizeMode(undefined), 'off')
    assert.equal(normalizeMode(null), 'off')
    assert.equal(normalizeMode('yes'), 'off')
    assert.equal(normalizeMode(''), 'off')
  })
})

describe('relationCheckMode', () => {
  test('reads yaml write_path.relation_check.mode', () => {
    assert.equal(relationCheckMode(), 'shadow')
  })
  test('env overrides yaml', () => {
    process.env.DEVBRAIN_RELATION_CHECK = 'off'
    assert.equal(relationCheckMode(), 'off')
    process.env.DEVBRAIN_RELATION_CHECK = 'on'
    assert.equal(relationCheckMode(), 'on')
  })
})

describe('relateArgs', () => {
  test('store: one memory plus a small project sweep', () => {
    assert.deepEqual(
      relateArgs({ memoryId: 'abc', projectSlug: 'p1', source: 'mcp:store', sweepMax: 5 }),
      ['cognify-relate', '--source=mcp:store', '--memory-id=abc', '--sweep', '--project=p1', '--max-checks=5'],
    )
  })
  test('sweep only (end_session lesson candidates)', () => {
    assert.deepEqual(
      relateArgs({ projectSlug: 'p1', source: 'mcp:end_session', sweepMax: 8 }),
      ['cognify-relate', '--source=mcp:end_session', '--sweep', '--project=p1', '--max-checks=8'],
    )
  })
  test('no sweep when sweepMax is 0', () => {
    assert.deepEqual(
      relateArgs({ memoryId: 'abc', projectSlug: 'p1', source: 'mcp:store', sweepMax: 0 }),
      ['cognify-relate', '--source=mcp:store', '--memory-id=abc'],
    )
  })
})

describe('triggerRelationCheckInBackground', () => {
  test('does not spawn when the check is off', () => {
    process.env.DEVBRAIN_RELATION_CHECK = 'off'
    assert.equal(
      triggerRelationCheckInBackground({ memoryId: 'abc', projectSlug: 'p1', source: 'mcp:store' }),
      false,
    )
  })
})
