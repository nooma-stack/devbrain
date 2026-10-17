// Write-time relation check — MCP side.
//
// The check itself (nearest same-project atoms → local model → same /
// refines / contradicts / new → edges, archive, reinforce) lives in
// factory/cognify/relate.py. The store tool can't wait for it: the local
// model is often cold (a 4–14 s load) and a larger local model can be
// slower than the ~2 s store budget. So the MCP
// writes first, answers, and spawns `devbrain cognify-relate` detached for
// the new row. Python owns the decision logic; this file only knows the
// mode and how to spawn.

import { spawn } from 'child_process'
import { readFileSync } from 'fs'
import { resolve } from 'path'
import { parse } from 'yaml'

export type RelationMode = 'off' | 'shadow' | 'on'

const REPO_ROOT = resolve(import.meta.dirname, '../..')
const PYTHON = resolve(REPO_ROOT, '.venv/bin/python')
const CLI = resolve(REPO_ROOT, 'factory/cli.py')

/** Normalize a yaml/env value. YAML 1.1 reads bare `off`/`on` as booleans. */
export function normalizeMode(v: unknown): RelationMode {
  if (v === false) return 'off'
  if (v === true) return 'on'
  const s = String(v ?? '').trim().toLowerCase()
  return s === 'shadow' || s === 'on' ? s : 'off'
}

let cachedYamlMode: RelationMode | null = null

function yamlMode(): RelationMode {
  if (cachedYamlMode !== null) return cachedYamlMode
  try {
    const configPath = process.env.DEVBRAIN_CONFIG || resolve(REPO_ROOT, 'config/devbrain.yaml')
    const cfg = parse(readFileSync(configPath, 'utf-8')) ?? {}
    cachedYamlMode = normalizeMode(cfg?.write_path?.relation_check?.mode)
  } catch {
    cachedYamlMode = 'off'
  }
  return cachedYamlMode
}

/** env DEVBRAIN_RELATION_CHECK > yaml write_path.relation_check.mode > off. */
export function relationCheckMode(): RelationMode {
  const env = process.env.DEVBRAIN_RELATION_CHECK
  if (env !== undefined) return normalizeMode(env)
  return yamlMode()
}

/**
 * The sentence the store reply appends once a check has been spawned. It
 * must match what the check will actually do: shadow mode only logs.
 */
export function relationStoreNote(mode: RelationMode, memoryId: string): string {
  const ref = `memory ${memoryId.slice(0, 8)}`
  if (mode === 'on') {
    return ` Duplicate check queued: if this is already known it will be merged into the existing memory (${ref}).`
  }
  if (mode === 'shadow') {
    return ` Duplicate check logged (shadow mode, ${ref}); nothing will change.`
  }
  return ''
}

/** CLI args for one async check (exported for tests). */
export function relateArgs(opts: {
  memoryId?: string | null
  projectSlug: string
  source: string
  sweepMax?: number
}): string[] {
  const args = ['cognify-relate', `--source=${opts.source}`]
  if (opts.memoryId) args.push(`--memory-id=${opts.memoryId}`)
  if (opts.sweepMax && opts.sweepMax > 0) {
    args.push('--sweep', `--project=${opts.projectSlug}`, `--max-checks=${opts.sweepMax}`)
  }
  return args
}

/**
 * Spawn the relation check in the background. Detached + stdio 'ignore' so
 * the MCP's JSON-RPC stdio is never touched and the tool call returns at
 * once. Returns false when the check is off or the spawn failed — the write
 * has already happened either way.
 */
export function triggerRelationCheckInBackground(opts: {
  memoryId?: string | null
  projectSlug: string
  source: string
  sweepMax?: number
}): boolean {
  if (relationCheckMode() === 'off') return false
  try {
    const child = spawn(PYTHON, [CLI, ...relateArgs(opts)], {
      cwd: resolve(REPO_ROOT, 'factory'),
      env: { ...process.env },
      detached: true,
      stdio: 'ignore',
    })
    child.on('error', () => {})
    child.unref()
    return true
  } catch {
    return false
  }
}
