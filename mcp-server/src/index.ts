#!/usr/bin/env node

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { spawn, spawnSync } from 'child_process'
import { existsSync, writeFileSync, unlinkSync } from 'fs'
import { homedir, tmpdir } from 'os'
import { join, resolve } from 'path'
import { z } from 'zod'
import { query } from './db.js'
import { embed, toSqlVector } from './embeddings.js'
import { recordMemory } from './memory.js'
import { summarizeSession } from './summarize.js'

// Factory orchestrator runner path
const FACTORY_RUNNER = resolve(import.meta.dirname, '../../factory/run.py')

const server = new McpServer({
  name: 'devbrain',
  version: '0.1.0',
})

// Default project from environment
const DEFAULT_PROJECT = process.env.DEVBRAIN_PROJECT ?? null

// ─── Helper: resolve project_id from slug ────────────────────────────────────

async function resolveProjectId(slug: string): Promise<string | null> {
  const result = await query<{ id: string }>(
    'SELECT id FROM devbrain.projects WHERE slug = $1',
    [slug],
  )
  return result.rows[0]?.id ?? null
}

// ─── Prompts (auto-injected context) ─────────────────────────────────────────

server.prompt(
  'startup',
  'Auto-injected context for every DevBrain session',
  {},
  async () => ({
    messages: [
      {
        role: 'user',
        content: {
          type: 'text',
          text: `You have access to DevBrain — a shared persistent memory and dev factory.

ALWAYS:
- Call get_project_context at the start of any work session
- Call deep_search before assuming anything about the project
- Call store when you make architecture decisions, discover patterns, or fix bugs
- Call end_session before ending any work session

DevBrain remembers across sessions and across different AI tools. What you store now will be available in future sessions, even with different models or apps.`,
        },
      },
    ],
  }),
)

// ─── Tool: get_project_context ───────────────────────────────────────────────

server.tool(
  'get_project_context',
  'Get current project context from DevBrain. Returns recent decisions, active factory jobs, known issues, and relevant patterns. Call this at the start of any work session.',
  {
    project: z.string().optional().describe('Project slug (defaults to DEVBRAIN_PROJECT env var)'),
  },
  async ({ project }) => {
    const slug = project ?? DEFAULT_PROJECT
    if (!slug) {
      return { content: [{ type: 'text', text: 'No project specified. Set DEVBRAIN_PROJECT env var or pass project parameter.' }] }
    }

    const projectId = await resolveProjectId(slug)
    if (!projectId) {
      return { content: [{ type: 'text', text: `Project "${slug}" not found in DevBrain.` }] }
    }

    // P2.d.i: decisions/issues/patterns now read from devbrain.memory.
    // The legacy tables (decisions/issues/patterns) carry richer
    // schemas (status, rationale, category, fix_applied, name) that
    // the unified memory table does not — return null for unmapped
    // fields per spec, preserving the response shape so downstream
    // consumers don't crash on missing keys. P2.d.ii will drop the
    // legacy tables once a soak period confirms no readers regress.
    const [projectInfo, decisions, issues, patterns, activeJobs, inactiveJobs, lockCount] = await Promise.all([
      query('SELECT name, description, root_path, constraints, tech_stack FROM devbrain.projects WHERE id = $1', [projectId]),
      query(
        `SELECT title, content AS decision, NULL AS rationale, created_at
         FROM devbrain.memory
         WHERE project_id = $1 AND kind = 'decision' AND archived_at IS NULL
         ORDER BY created_at DESC LIMIT 5`,
        [projectId],
      ),
      query(
        `SELECT title, NULL AS category, content AS description,
                NULL AS fix_applied, created_at
         FROM devbrain.memory
         WHERE project_id = $1 AND kind = 'issue' AND archived_at IS NULL
         ORDER BY created_at DESC LIMIT 5`,
        [projectId],
      ),
      query(
        `SELECT title AS name, NULL AS category, content AS description
         FROM devbrain.memory
         WHERE project_id = $1 AND kind = 'pattern' AND archived_at IS NULL
         ORDER BY created_at DESC LIMIT 5`,
        [projectId],
      ),
      query('SELECT title, status, current_phase, branch_name FROM devbrain.factory_jobs WHERE project_id = $1 AND status NOT IN (\'approved\', \'rejected\', \'deployed\', \'failed\') AND archived_at IS NULL ORDER BY created_at DESC LIMIT 5', [projectId]),
      query('SELECT title, status, current_phase, branch_name, error_count, archived_at FROM devbrain.factory_jobs WHERE project_id = $1 AND status IN (\'deployed\', \'failed\', \'approved\', \'rejected\') AND (archived_at IS NULL OR archived_at > now() - interval \'24 hours\') ORDER BY updated_at DESC LIMIT 5', [projectId]),
      query(`SELECT COUNT(*) as count FROM devbrain.file_locks
             WHERE project_id = $1 AND expires_at > now()`, [projectId]),
    ])

    const ctx = {
      project: projectInfo.rows[0] ?? null,
      recent_decisions: decisions.rows,
      recent_issues: issues.rows,
      relevant_patterns: patterns.rows,
      active_factory_jobs: activeJobs.rows,
      recent_completed_jobs: inactiveJobs.rows,
      active_file_locks: Number(lockCount.rows[0]?.count ?? 0),
    }

    return {
      content: [{
        type: 'text',
        text: JSON.stringify(ctx, null, 2),
      }],
    }
  },
)

// ─── Tool: deep_search ───────────────────────────────────────────────────────

server.tool(
  'deep_search',
  'Search DevBrain for relevant context. Call this FIRST before starting any work. Returns embedded chunks with source references. Use depth="auto" to automatically fetch raw context when the query asks for specific details.',
  {
    query: z.string().describe('Natural language search query'),
    project: z.string().optional().describe('Project slug (defaults to current project, omit for cross-project)'),
    cross_project: z.boolean().optional().default(false).describe('Search all projects'),
    source_types: z.array(z.string()).optional().describe('Filter by: session, decision, pattern, issue, codebase'),
    depth: z.enum(['summary', 'full', 'auto']).optional().default('auto').describe('summary=chunks only, full=chunks+raw context, auto=smart drill-down'),
    limit: z.number().optional().default(10).describe('Max results'),
  },
  async ({ query: searchQuery, project, cross_project, source_types, depth, limit }) => {
    const queryEmbedding = await embed(searchQuery)
    const vectorStr = toSqlVector(queryEmbedding)

    // P2.d.i read-path switch.
    //
    // Reads now come from devbrain.memory. Each chunk-kind memory row
    // is the dual-write of a legacy devbrain.chunks row (provenance_id
    // points back to chunks.id), so we LEFT JOIN to recover the legacy
    // chunk's source_type/source_id/line range — needed both to mimic
    // the pre-switch response shape AND to drive the raw_sessions
    // drill-down (raw_content was not migrated to memory).
    //
    // The codebase source_type does not live in devbrain.memory because
    // codebase_index ingest was never wired through memory_writer (see
    // P2.e). When the caller asks for source_types including 'codebase'
    // (or omits the filter), we run a second query against legacy
    // chunks WHERE source_type='codebase' and merge the candidate sets
    // before re-ranking on score.
    //
    // source_types maps to memory.kind:
    //   'session'   → kind='chunk' AND chunks.source_type='session', plus kind='session_summary'
    //   'decision'  → kind='decision'
    //   'pattern'   → kind='pattern'
    //   'issue'     → kind='issue'
    //   'codebase'  → legacy fallback only (see above)

    let projectId: string | null = null
    if (!cross_project) {
      const slug = project ?? DEFAULT_PROJECT
      if (slug) {
        projectId = await resolveProjectId(slug)
      }
    }

    // Build the kind-filter list from the user-supplied source_types.
    // 'session' covers BOTH chunk-kind memory rows whose underlying
    // legacy chunk has source_type='session' AND raw session_summary
    // memory rows; we OR those two together at the SQL level.
    let kindFilter: string[] | null = null
    let includeCodebase = true
    let restrictChunkSourceTypes: string[] | null = null
    if (source_types && source_types.length > 0) {
      includeCodebase = source_types.includes('codebase')
      const kinds = new Set<string>()
      const chunkSourceTypes = new Set<string>()
      for (const st of source_types) {
        if (st === 'session') {
          kinds.add('chunk')
          kinds.add('session_summary')
          chunkSourceTypes.add('session')
        } else if (st === 'decision' || st === 'pattern' || st === 'issue') {
          kinds.add(st)
        }
        // 'codebase' is handled by the legacy fallback below.
      }
      if (kinds.size > 0) {
        kindFilter = [...kinds]
        // Only apply chunks.source_type filter when the caller's filter
        // included 'session' but NOT a wildcard catch-all, so that
        // chunk-kind rows from non-session legacy sources don't leak.
        restrictChunkSourceTypes = chunkSourceTypes.size > 0 ? [...chunkSourceTypes] : null
      } else if (!includeCodebase) {
        // Caller filtered to source_types we have no mapping for —
        // return empty rather than a wide-open scan.
        return {
          content: [{
            type: 'text',
            text: JSON.stringify({ results: [], hint: 'No matching source_types.' }, null, 2),
          }],
        }
      }
    }

    type Candidate = {
      chunk_id: string
      content: string
      score: number
      source_type: string
      source_id: string | null
      source_line_start: number | null
      source_line_end: number | null
      project: string
      memory_id: string
      memory_kind: string
    }

    const candidates: Candidate[] = []

    // 1. Memory query — runs unless the caller filtered ONLY to codebase.
    const runMemoryQuery = kindFilter !== null || (source_types ?? []).length === 0
    if (runMemoryQuery) {
      const memoryParams: unknown[] = [vectorStr, limit]
      let memoryWhere = ''
      let pIdx = 3
      if (projectId) {
        memoryWhere += ` AND m.project_id = $${pIdx}`
        memoryParams.push(projectId)
        pIdx++
      }
      if (kindFilter) {
        memoryWhere += ` AND m.kind = ANY($${pIdx})`
        memoryParams.push(kindFilter)
        pIdx++
      }
      if (restrictChunkSourceTypes) {
        // chunk-kind rows must match the requested chunks.source_type;
        // non-chunk kinds (decision/pattern/issue/session_summary) pass
        // through this filter unaffected.
        memoryWhere += ` AND (m.kind <> 'chunk' OR c.source_type = ANY($${pIdx}))`
        memoryParams.push(restrictChunkSourceTypes)
        pIdx++
      }

      const memorySql = `
        SELECT
          m.id as memory_id,
          m.kind as memory_kind,
          m.content,
          1 - (m.embedding <=> $1::vector) as score,
          c.id as legacy_chunk_id,
          c.source_type as chunk_source_type,
          c.source_id as chunk_source_id,
          c.source_line_start as chunk_line_start,
          c.source_line_end as chunk_line_end,
          p.slug as project
        FROM devbrain.memory m
        JOIN devbrain.projects p ON m.project_id = p.id
        LEFT JOIN devbrain.chunks c
          ON m.kind = 'chunk' AND c.id = m.provenance_id
        WHERE m.embedding IS NOT NULL
          AND m.archived_at IS NULL
          ${memoryWhere}
        ORDER BY m.embedding <=> $1::vector
        LIMIT $2
      `

      const memoryResult = await query(memorySql, memoryParams)

      // Drift detector: empty memory + non-empty legacy is a signal that
      // the dual-write fell behind. Logged for operators; does NOT
      // trigger a fallback (the legacy fallback is for codebase only).
      if (memoryResult.rows.length === 0 && projectId) {
        const legacyCount = await query<{ count: string }>(
          `SELECT COUNT(*)::text as count FROM devbrain.chunks
           WHERE project_id = $1 AND embedding IS NOT NULL`,
          [projectId],
        )
        if (Number(legacyCount.rows[0]?.count ?? 0) > 0) {
          console.error(
            `[deep_search] WARNING: dual-write drift — devbrain.memory empty for project ${projectId} but legacy devbrain.chunks has rows. Run backfill-memory.`,
          )
        }
      }

      for (const row of memoryResult.rows) {
        const isChunk = row.memory_kind === 'chunk'
        const chunkId = isChunk && row.legacy_chunk_id ? String(row.legacy_chunk_id) : String(row.memory_id)
        const sourceType = isChunk && row.chunk_source_type
          ? String(row.chunk_source_type)
          : String(row.memory_kind)
        candidates.push({
          chunk_id: chunkId,
          content: String(row.content),
          score: Number(row.score),
          source_type: sourceType,
          source_id: row.chunk_source_id != null ? String(row.chunk_source_id) : null,
          source_line_start: row.chunk_line_start != null ? Number(row.chunk_line_start) : null,
          source_line_end: row.chunk_line_end != null ? Number(row.chunk_line_end) : null,
          project: String(row.project),
          memory_id: String(row.memory_id),
          memory_kind: String(row.memory_kind),
        })
      }
    }

    // 2. Codebase fallback — legacy chunks WHERE source_type='codebase'.
    // codebase_index ingest never landed in P2.b's dual-write, so this
    // remains a legacy-only read until P2.e consolidates it.
    if (includeCodebase) {
      const codebaseParams: unknown[] = [vectorStr, limit]
      let codebaseWhere = ''
      if (projectId) {
        codebaseWhere += ` AND c.project_id = $3`
        codebaseParams.push(projectId)
      }

      const codebaseSql = `
        SELECT
          c.id as chunk_id,
          c.content,
          1 - (c.embedding <=> $1::vector) as score,
          c.source_type,
          c.source_id,
          c.source_line_start,
          c.source_line_end,
          p.slug as project
        FROM devbrain.chunks c
        JOIN devbrain.projects p ON c.project_id = p.id
        WHERE c.embedding IS NOT NULL
          AND c.source_type = 'codebase'
          ${codebaseWhere}
        ORDER BY c.embedding <=> $1::vector
        LIMIT $2
      `
      const codebaseResult = await query(codebaseSql, codebaseParams)
      for (const row of codebaseResult.rows) {
        candidates.push({
          chunk_id: String(row.chunk_id),
          content: String(row.content),
          score: Number(row.score),
          source_type: String(row.source_type),
          source_id: row.source_id != null ? String(row.source_id) : null,
          source_line_start: row.source_line_start != null ? Number(row.source_line_start) : null,
          source_line_end: row.source_line_end != null ? Number(row.source_line_end) : null,
          project: String(row.project),
          memory_id: '',
          memory_kind: 'codebase',
        })
      }
    }

    // Re-rank merged candidates by descending score, then slice.
    candidates.sort((a, b) => b.score - a.score)
    const top = candidates.slice(0, limit)

    const results = await Promise.all(
      top.map(async (cand) => {
        const r: Record<string, unknown> = {
          chunk_id: cand.chunk_id,
          content: cand.content,
          score: Number(cand.score.toFixed(4)),
          source_type: cand.source_type,
          source_ref: cand.source_id
            ? `${cand.source_type}_${cand.source_id.slice(0, 8)}:${cand.source_line_start ?? '?'}-${cand.source_line_end ?? '?'}`
            : null,
          project: cand.project,
          has_full_context: cand.source_type === 'session' && cand.source_id != null,
        }

        // Auto drill-down: fetch raw context for top results if depth is
        // full or auto with high scores. Drill-down still uses the
        // legacy raw_sessions table — raw_content was not migrated to
        // memory in P2.b/c.
        if (
          (depth === 'full' || (depth === 'auto' && cand.score > 0.6)) &&
          cand.source_type === 'session' &&
          cand.source_id
        ) {
          const rawResult = await query(
            'SELECT raw_content FROM devbrain.raw_sessions WHERE id = $1',
            [cand.source_id],
          )
          if (rawResult.rows[0]) {
            const raw = rawResult.rows[0].raw_content as string
            const lines = raw.split('\n')
            const start = Math.max(0, (cand.source_line_start ?? 0) - 25)
            const end = Math.min(lines.length, (cand.source_line_end ?? lines.length) + 25)
            r.full_context = lines.slice(start, end).join('\n')
          }
        }

        return r
      }),
    )

    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          results,
          hint: results.some((r) => r.has_full_context)
            ? 'Call get_source_context(chunk_id) for full raw transcript around any result.'
            : 'No raw session context available for these results.',
        }, null, 2),
      }],
    }
  },
)

// ─── Tool: get_source_context ────────────────────────────────────────────────
//
// P2.d.i note: this tool intentionally still reads from devbrain.chunks
// + devbrain.raw_sessions. raw_content was NOT migrated to
// devbrain.memory in P2.b/c (the unified table holds the chunk's
// embedded text but not the full transcript), so the drill-down here
// has no memory-side equivalent. deep_search returns the legacy
// chunks.id for chunk-kind hits precisely so this tool keeps working
// after the read-path switch.

server.tool(
  'get_source_context',
  'Get the full raw transcript context around a search result. Use when a deep_search result has relevant info but you need more detail.',
  {
    chunk_id: z.string().describe('Chunk ID from a deep_search result'),
    window_lines: z.number().optional().default(50).describe('Lines of context around the chunk'),
  },
  async ({ chunk_id, window_lines }) => {
    const chunkResult = await query(
      'SELECT source_id, source_line_start, source_line_end FROM devbrain.chunks WHERE id = $1',
      [chunk_id],
    )

    if (chunkResult.rows.length === 0) {
      return { content: [{ type: 'text', text: `Chunk ${chunk_id} not found.` }] }
    }

    const chunk = chunkResult.rows[0]
    if (!chunk.source_id) {
      return { content: [{ type: 'text', text: 'This chunk has no linked raw source.' }] }
    }

    const rawResult = await query(
      'SELECT raw_content, source_app, started_at, summary FROM devbrain.raw_sessions WHERE id = $1',
      [chunk.source_id],
    )

    if (rawResult.rows.length === 0) {
      return { content: [{ type: 'text', text: 'Raw session not found.' }] }
    }

    const raw = rawResult.rows[0]
    const lines = (raw.raw_content as string).split('\n')
    const start = Math.max(0, (chunk.source_line_start as number ?? 0) - window_lines)
    const end = Math.min(lines.length, (chunk.source_line_end as number ?? lines.length) + window_lines)

    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          source_app: raw.source_app,
          session_date: raw.started_at,
          session_summary: raw.summary,
          context_lines: `${start + 1}-${end}`,
          total_lines: lines.length,
          content: lines.slice(start, end).join('\n'),
        }, null, 2),
      }],
    }
  },
)

// ─── Tool: store ─────────────────────────────────────────────────────────────

server.tool(
  'store',
  'Store something worth remembering in DevBrain. Call this when you make a decision, discover a pattern, fix a bug, or learn something important.',
  {
    type: z.enum(['decision', 'pattern', 'issue', 'note']).describe('Type of memory to store'),
    project: z.string().describe('Project slug'),
    title: z.string().describe('Brief title'),
    content: z.string().describe('Full content/description'),
    category: z.string().optional().describe('Category (e.g., "auth", "performance", "hipaa")'),
    tags: z.array(z.string()).optional(),
    rationale: z.string().optional().describe('Why this decision was made (for decisions)'),
    alternatives: z.array(z.string()).optional().describe('Alternatives considered (for decisions)'),
    root_cause: z.string().optional().describe('Root cause (for issues)'),
    fix_applied: z.string().optional().describe('Fix applied (for issues)'),
    prevention: z.string().optional().describe('How to prevent recurrence (for issues)'),
    example_code: z.string().optional().describe('Example code (for patterns)'),
  },
  async ({ type, project, title, content, category, tags, rationale, alternatives, root_cause, fix_applied, prevention, example_code }) => {
    const projectId = await resolveProjectId(project)
    if (!projectId) {
      return { content: [{ type: 'text', text: `Project "${project}" not found.` }] }
    }

    let recordId: string
    const embedding = await embed(`${title}\n${content}`)
    const vectorStr = toSqlVector(embedding)

    if (type === 'decision') {
      const result = await query<{ id: string }>(
        `INSERT INTO devbrain.decisions (project_id, title, context, decision, rationale, alternatives, constraints)
         VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id`,
        [projectId, title, content, content, rationale ?? '', JSON.stringify(alternatives ?? []), JSON.stringify([])],
      )
      recordId = result.rows[0].id
    } else if (type === 'pattern') {
      const result = await query<{ id: string }>(
        `INSERT INTO devbrain.patterns (project_id, name, category, description, example_code, tags)
         VALUES ($1, $2, $3, $4, $5, $6) RETURNING id`,
        [projectId, title, category ?? '', content, example_code ?? '', JSON.stringify(tags ?? [])],
      )
      recordId = result.rows[0].id
    } else if (type === 'issue') {
      const result = await query<{ id: string }>(
        `INSERT INTO devbrain.issues (project_id, title, category, description, root_cause, fix_applied, prevention)
         VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id`,
        [projectId, title, category ?? '', content, root_cause ?? '', fix_applied ?? '', prevention ?? ''],
      )
      recordId = result.rows[0].id
    } else {
      // Generic note stored as a chunk. P2.b does NOT dual-write notes:
      // 'note' isn't in the devbrain.memory.kind CHECK constraint, and
      // notes already land in chunks where P2.c backfill will pick them up.
      const result = await query<{ id: string }>(
        `INSERT INTO devbrain.chunks (project_id, source_type, content, embedding, metadata)
         VALUES ($1, 'note', $2, $3::vector, $4) RETURNING id`,
        [projectId, `${title}\n\n${content}`, vectorStr, JSON.stringify({ tags: tags ?? [], category: category ?? '' })],
      )
      recordId = result.rows[0].id
      return { content: [{ type: 'text', text: `Stored note "${title}" (${recordId.slice(0, 8)}).` }] }
    }

    // P2.b dual-write: decision/pattern/issue → devbrain.memory.
    // Reuses the embedding computed above; provenance_id is the legacy
    // row's UUID so the partial unique index dedupes concurrent retries.
    // We dual-write here (the canonical legacy row) — NOT the
    // auxiliary chunk insert below — so each store() lands exactly one
    // memory row per logical entity.
    await recordMemory({
      projectId,
      kind: type,
      title,
      content: `${title}\n\n${content}`,
      embeddingSql: vectorStr,
      provenanceId: recordId,
    })

    // Also create an embedded chunk for the record. Kept for legacy
    // search compatibility; no dual-write from here (would duplicate
    // the memory row created above).
    await query(
      `INSERT INTO devbrain.chunks (project_id, source_type, source_id, content, embedding)
       VALUES ($1, $2, $3, $4, $5::vector)`,
      [projectId, type, recordId, `${title}\n\n${content}`, vectorStr],
    )

    return { content: [{ type: 'text', text: `Stored ${type} "${title}" (${recordId.slice(0, 8)}).` }] }
  },
)

// ─── Tool: end_session ───────────────────────────────────────────────────────

server.tool(
  'end_session',
  'Call before ending any work session. Summarizes what was done and stores it in DevBrain for future reference.',
  {
    project: z.string().describe('Project slug'),
    summary: z.string().describe('What was accomplished in this session'),
    decisions_made: z.array(z.string()).optional(),
    files_changed: z.array(z.string()).optional(),
    issues_found: z.array(z.string()).optional(),
    next_steps: z.array(z.string()).optional(),
  },
  async ({ project, summary, decisions_made, files_changed, issues_found, next_steps }) => {
    const projectId = await resolveProjectId(project)
    if (!projectId) {
      return { content: [{ type: 'text', text: `Project "${project}" not found.` }] }
    }

    const fullContent = [
      `Session Summary: ${summary}`,
      decisions_made?.length ? `\nDecisions: ${decisions_made.join('; ')}` : '',
      files_changed?.length ? `\nFiles changed: ${files_changed.join(', ')}` : '',
      issues_found?.length ? `\nIssues: ${issues_found.join('; ')}` : '',
      next_steps?.length ? `\nNext steps: ${next_steps.join('; ')}` : '',
    ].filter(Boolean).join('\n')

    const embedding = await embed(fullContent)
    const vectorStr = toSqlVector(embedding)

    const chunkResult = await query<{ id: string }>(
      `INSERT INTO devbrain.chunks (project_id, source_type, content, embedding, metadata)
       VALUES ($1, 'session_summary', $2, $3::vector, $4)
       RETURNING id`,
      [projectId, fullContent, vectorStr, JSON.stringify({
        decisions_made: decisions_made ?? [],
        files_changed: files_changed ?? [],
        issues_found: issues_found ?? [],
        next_steps: next_steps ?? [],
        timestamp: new Date().toISOString(),
      })],
    )

    // P2.b dual-write: session_summary → devbrain.memory. The MCP
    // end_session tool doesn't write to raw_sessions in the current
    // code path, so we anchor provenance_id to the chunks row we just
    // created (deduplicates against concurrent end_session retries).
    await recordMemory({
      projectId,
      kind: 'session_summary',
      content: fullContent,
      embeddingSql: vectorStr,
      provenanceId: chunkResult.rows[0]?.id ?? null,
    })

    return { content: [{ type: 'text', text: `Session summary stored for project "${project}".` }] }
  },
)

// ─── Tool: health_check ──────────────────────────────────────────────────────
//
// P2.d.i observability surface for the dual-write soak period. Reports
// row counts in devbrain.memory (broken down by kind) alongside the
// matching legacy tables (chunks/decisions/patterns/issues + raw_sessions).
// Operators compare the two columns to detect dual-write drift before
// P2.d.ii drops the legacy tables. Optional `project` filter scopes
// counts to a single project; omit for cluster-wide totals.

server.tool(
  'health_check',
  'Report DevBrain storage row counts in devbrain.memory (by kind) alongside legacy tables. Use to verify dual-write parity during the P2 soak period.',
  {
    project: z.string().optional().describe('Project slug to scope counts (omit for all projects).'),
  },
  async ({ project }) => {
    const slug = project ?? null
    let projectId: string | null = null
    if (slug) {
      projectId = await resolveProjectId(slug)
      if (!projectId) {
        return { content: [{ type: 'text', text: `Project "${slug}" not found.` }] }
      }
    }

    // Build per-table count SQL with an optional project filter. We
    // run the queries in parallel — none of them mutate state.
    const memoryFilter = projectId ? 'AND project_id = $1' : ''
    const legacyFilter = projectId ? 'WHERE project_id = $1' : ''
    const params: unknown[] = projectId ? [projectId] : []

    const [
      memChunks,
      memDecisions,
      memPatterns,
      memIssues,
      memSessionSummaries,
      legChunks,
      legDecisions,
      legPatterns,
      legIssues,
      legRawSessions,
    ] = await Promise.all([
      query<{ count: string }>(`SELECT COUNT(*)::text as count FROM devbrain.memory WHERE kind = 'chunk' ${memoryFilter}`, params),
      query<{ count: string }>(`SELECT COUNT(*)::text as count FROM devbrain.memory WHERE kind = 'decision' ${memoryFilter}`, params),
      query<{ count: string }>(`SELECT COUNT(*)::text as count FROM devbrain.memory WHERE kind = 'pattern' ${memoryFilter}`, params),
      query<{ count: string }>(`SELECT COUNT(*)::text as count FROM devbrain.memory WHERE kind = 'issue' ${memoryFilter}`, params),
      query<{ count: string }>(`SELECT COUNT(*)::text as count FROM devbrain.memory WHERE kind = 'session_summary' ${memoryFilter}`, params),
      query<{ count: string }>(`SELECT COUNT(*)::text as count FROM devbrain.chunks ${legacyFilter}`, params),
      query<{ count: string }>(`SELECT COUNT(*)::text as count FROM devbrain.decisions ${legacyFilter}`, params),
      query<{ count: string }>(`SELECT COUNT(*)::text as count FROM devbrain.patterns ${legacyFilter}`, params),
      query<{ count: string }>(`SELECT COUNT(*)::text as count FROM devbrain.issues ${legacyFilter}`, params),
      query<{ count: string }>(`SELECT COUNT(*)::text as count FROM devbrain.raw_sessions ${legacyFilter}`, params),
    ])

    const num = (r: { rows: { count: string }[] }) => Number(r.rows[0]?.count ?? 0)

    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          project: slug,
          storage: {
            memory: {
              chunks: num(memChunks),
              decisions: num(memDecisions),
              patterns: num(memPatterns),
              issues: num(memIssues),
              session_summaries: num(memSessionSummaries),
            },
            legacy: {
              chunks: num(legChunks),
              decisions: num(legDecisions),
              patterns: num(legPatterns),
              issues: num(legIssues),
              raw_sessions: num(legRawSessions),
            },
          },
        }, null, 2),
      }],
    }
  },
)

// ─── Tool: list_projects ─────────────────────────────────────────────────────

server.tool(
  'list_projects',
  'List all projects registered in DevBrain.',
  {},
  async () => {
    const result = await query(
      'SELECT slug, name, description, root_path FROM devbrain.projects ORDER BY name',
    )
    return { content: [{ type: 'text', text: JSON.stringify(result.rows, null, 2) }] }
  },
)

// ─── Tool: factory_plan ──────────────────────────────────────────────────────

// Branch-name validation.
//
// A user-supplied branch string eventually reaches `git checkout <name>` and
// `git push -u origin <name>` inside the orchestrator. Without validation
// two concrete attacks work:
//
// 1. Leading "-" → git parses as flag.
//    `branch: "--help"` turns `git checkout -- help` effectively no-op;
//    worse, `branch: "--receive-pack=<cmd>"` on a later push invokes the
//    attacker's command on the remote (genuine RCE on servers that honor
//    the flag). Valid git refnames never begin with `-`.
//
// 2. Refspec form → bypasses main/master guard.
//    `branch: "feature:refs/heads/main"` is a valid git refspec. A naive
//    `name.toLowerCase() in {"main","master"}` check doesn't match, but
//    `git push origin feature:refs/heads/main` happily pushes onto main.
//    Safe refnames don't contain `:`.
//
// The regex below matches plain git refnames only: starts with an
// alphanumeric or underscore (so "-" and "." are excluded up front),
// then safe chars only. This is stricter than git's own `check-ref-format`
// but deliberately so — we'd rather reject an exotic-but-valid name than
// accept any of the attack shapes above.
const SAFE_BRANCH_RE = /^[A-Za-z0-9_][A-Za-z0-9_./-]{0,254}$/
const branchSchema = z
  .string()
  .trim()
  .min(1, 'branch must not be empty or whitespace-only')
  .max(255)
  .regex(
    SAFE_BRANCH_RE,
    'branch has unsafe characters — only [A-Za-z0-9_./-] allowed, cannot start with "-" or "."',
  )
  .refine(
    (v) => v.toLowerCase() !== 'main' && v.toLowerCase() !== 'master',
    { message: 'branch must not be main or master — factory operates on feature branches only' },
  )
  .optional()
  .describe(
    'Optional existing branch to continue work on. If unset, factory creates factory/<id>/<slug>. Refuses main/master and unsafe refnames synchronously; falls back to auto-create with a warning if the (validated) branch does not exist.',
  )

server.tool(
  'factory_plan',
  'Submit a feature to the dev factory for autonomous implementation. Creates a job that will be planned, implemented, reviewed, QA tested, and staged for your approval.',
  {
    project: z.string().describe('Project slug'),
    title: z.string().describe('Feature title'),
    spec: z.string().describe('Feature requirements and description'),
    priority: z.number().optional().default(0).describe('Priority (higher = more urgent)'),
    assigned_cli: z.string().optional().describe('CLI to use: claude, codex, gemini (default: claude)'),
    submitted_by: z.string().optional().describe('Dev identifier (SSH user) who submitted this job'),
    branch: branchSchema,
  },
  async ({ project, title, spec, priority, assigned_cli, submitted_by, branch }) => {
    const projectId = await resolveProjectId(project)
    if (!projectId) {
      return { content: [{ type: 'text', text: `Project "${project}" not found.` }] }
    }

    const result = await query<{ id: string }>(
      `INSERT INTO devbrain.factory_jobs
          (project_id, title, spec, status, priority, current_phase, assigned_cli, max_retries, submitted_by, branch_name)
       VALUES ($1, $2, $3, 'queued', $4, 'queued', $5, 5, $6, $7)
       RETURNING id`,
      [projectId, title, spec, priority, assigned_cli ?? 'claude', submitted_by ?? process.env.USER ?? null, branch ?? null],
    )

    const jobId = result.rows[0].id

    // Spawn the factory orchestrator as a detached background process.
    // It runs the full pipeline: planning → implementing → reviewing → QA → approval.
    // Detached + unref() ensures it outlives the MCP tool call.
    try {
      const factoryPython = resolve(import.meta.dirname, '../../.venv/bin/python')
      const child = spawn(factoryPython, [FACTORY_RUNNER, jobId], {
        detached: true,
        stdio: ['ignore', 'pipe', 'pipe'],
        cwd: resolve(import.meta.dirname, '../..'),
      })
      child.unref()
      console.error(`[factory] Spawned orchestrator for job ${jobId.slice(0, 8)} (pid ${child.pid})`)
    } catch (err) {
      console.error(`[factory] Failed to spawn orchestrator: ${err}`)
      // Job is still created in DB — can be run manually via: python3 factory/run.py <job_id>
    }

    return {
      content: [{
        type: 'text',
        text: `Factory job created: ${jobId}\nTitle: ${title}\nStatus: queued\nCLI: ${assigned_cli ?? 'claude'}\n\nThe factory orchestrator has been launched. Use factory_status to check progress.`,
      }],
    }
  },
)

// ─── Tool: factory_status ────────────────────────────────────────────────────

server.tool(
  'factory_status',
  'Check dev factory job status. Shows active jobs, their current phase, and any issues.',
  {
    job_id: z.string().optional().describe('Specific job ID, or omit for all active jobs'),
    project: z.string().optional().describe('Filter by project slug'),
  },
  async ({ job_id, project }) => {
    let sql: string
    const params: unknown[] = []

    if (job_id) {
      sql = `
        SELECT j.id, p.slug, j.title, j.status, j.current_phase,
               j.branch_name, j.error_count, j.max_retries,
               j.assigned_cli, j.created_at, j.updated_at
        FROM devbrain.factory_jobs j
        JOIN devbrain.projects p ON j.project_id = p.id
        WHERE j.id = $1`
      params.push(job_id)
    } else {
      const slug = project ?? DEFAULT_PROJECT
      sql = `
        SELECT j.id, p.slug, j.title, j.status, j.current_phase,
               j.branch_name, j.error_count, j.max_retries,
               j.assigned_cli, j.created_at, j.updated_at
        FROM devbrain.factory_jobs j
        JOIN devbrain.projects p ON j.project_id = p.id
        WHERE j.status NOT IN ('approved', 'rejected', 'deployed', 'failed')
        ${slug ? 'AND p.slug = $1' : ''}
        ORDER BY j.priority DESC, j.created_at ASC`
      if (slug) params.push(slug)
    }

    const result = await query(sql, params)

    if (result.rows.length === 0) {
      return { content: [{ type: 'text', text: job_id ? `Job ${job_id} not found.` : 'No active factory jobs.' }] }
    }

    // If single job, also get artifacts
    let artifacts: unknown[] = []
    if (job_id && result.rows.length === 1) {
      const artResult = await query(
        `SELECT phase, artifact_type, findings_count, blocking_count, model_used, created_at
         FROM devbrain.factory_artifacts WHERE job_id = $1 ORDER BY created_at ASC`,
        [job_id],
      )
      artifacts = artResult.rows
    }

    return {
      content: [{
        type: 'text',
        text: JSON.stringify({ jobs: result.rows, artifacts }, null, 2),
      }],
    }
  },
)

// ─── Tool: factory_approve ───────────────────────────────────────────────────

server.tool(
  'factory_approve',
  'Approve, reject, or request changes on a dev factory job that is ready for review. On approve, pushes the job\'s branch to origin; on push failure, reverts status so the caller can retry after fixing auth/network.',
  {
    job_id: z.string().describe('Job ID to approve/reject'),
    action: z.enum(['approve', 'reject', 'request_changes']).describe('Action to take'),
    notes: z.string().optional().describe('Optional notes for the decision'),
  },
  async ({ job_id, action, notes }) => {
    // Fetch everything we need in one query — the branch to push and
    // the project root to push from.
    const jobQuery = await query<{
      status: string
      title: string
      branch_name: string | null
      root_path: string | null
    }>(
      `SELECT fj.status, fj.title, fj.branch_name, p.root_path
       FROM devbrain.factory_jobs fj
       JOIN devbrain.projects p ON p.id = fj.project_id
       WHERE fj.id = $1`,
      [job_id],
    )

    if (jobQuery.rows.length === 0) {
      return { content: [{ type: 'text', text: `Job ${job_id} not found.` }] }
    }

    const { status, title, branch_name, root_path } = jobQuery.rows[0]

    if (action === 'approve') {
      if (status !== 'ready_for_approval') {
        return { content: [{ type: 'text', text: `Job is not ready for approval (status: ${status}).` }] }
      }

      // Transition to APPROVED first. If the subsequent push fails we
      // revert — this makes the state reflect reality at every point
      // (previously the UPDATE went through unconditionally, so the DB
      // said "approved" even when nothing left the machine).
      await query(
        "UPDATE devbrain.factory_jobs SET status = 'approved', current_phase = 'approved', updated_at = now() WHERE id = $1",
        [job_id],
      )

      if (!branch_name) {
        return { content: [{ type: 'text', text: `Job "${title}" APPROVED, but the job has no branch_name on record — nothing to push. Push any commits manually.` }] }
      }
      if (!root_path) {
        return { content: [{ type: 'text', text: `Job "${title}" APPROVED, but the project has no root_path on record — can't locate the git worktree. Push branch '${branch_name}' manually.` }] }
      }

      // Worktree-aware cwd: factory jobs run in per-job worktrees at
      // ~/devbrain-worktrees/<job_id>/. Mirrors Python's _get_job_cwd.
      // Falls back to root_path for pre-worktree jobs or planning-only.
      const worktreeDir = join(homedir(), 'devbrain-worktrees', job_id)
      const gitCwd = existsSync(worktreeDir) ? worktreeDir : root_path

      // Sync the worktree with origin before pushing. If a human
      // pushed commits to this branch from another machine between
      // factory completion and approval, our worktree is behind
      // origin and `git push` would be rejected as non-fast-forward.
      // Fetch + ff-only merge catches that silently; divergent
      // history fails loud so we can surface it.
      const fetchResult = spawnSync(
        'git',
        ['fetch', 'origin', branch_name],
        { cwd: gitCwd, encoding: 'utf-8', timeout: 30_000 },
      )

      // Only attempt ff-merge when fetch succeeded. A fetch miss
      // (e.g. origin has no such branch yet — first push) falls
      // through to the push; the push itself will surface any real
      // problem.
      if (!fetchResult.error && fetchResult.status === 0) {
        const mergeResult = spawnSync(
          'git',
          ['merge', '--ff-only', `origin/${branch_name}`],
          { cwd: gitCwd, encoding: 'utf-8', timeout: 30_000 },
        )

        if (mergeResult.error || mergeResult.status !== 0) {
          // Divergent history — revert status, record the detail in
          // the job's metadata so the human can inspect, and return.
          const combined = `${mergeResult.stderr ?? ''}${mergeResult.stdout ?? ''}`.trim()
          const detail = (combined || mergeResult.error?.message || '(no git output)').slice(-2048)
          await query(
            "UPDATE devbrain.factory_jobs "
            + "SET status = 'ready_for_approval', "
            + "    current_phase = 'ready_for_approval', "
            + "    metadata = metadata || $2::jsonb, "
            + "    updated_at = now() "
            + "WHERE id = $1",
            [job_id, JSON.stringify({ approve_sync_error: detail })],
          )
          return {
            content: [{
              type: 'text',
              text: `Job "${title}" approval SYNC FAILED (worktree diverged from origin/${branch_name}). Status reverted to ready_for_approval.\n\ngit output tail:\n${detail}\n\nResolve the divergence in the worktree (rebase or reset to origin) then re-run factory_approve.`,
            }],
          }
        }
      }

      // Now actually push. 60s is plenty for a single-branch push;
      // anything longer is a stuck auth prompt or network hang we want
      // to surface, not wait on.
      const push = spawnSync('git', ['push', '-u', 'origin', branch_name], {
        cwd: gitCwd,
        encoding: 'utf-8',
        timeout: 60_000,
      })

      if (push.error) {
        // Couldn't even spawn git (missing binary, bad cwd, signal).
        await query(
          "UPDATE devbrain.factory_jobs SET status = 'ready_for_approval', current_phase = 'ready_for_approval', updated_at = now() WHERE id = $1",
          [job_id],
        )
        return { content: [{ type: 'text', text: `Job "${title}" approval PUSH FAILED: ${push.error.message}. Status reverted to ready_for_approval.` }] }
      }

      if (push.status === 0) {
        // Post-push success — trim any warnings from stdout/stderr for the caller.
        const trimmedStderr = (push.stderr ?? '').trim()
        const hint = trimmedStderr ? `\n\n(git output:\n${trimmedStderr.slice(-512)})` : ''
        return { content: [{ type: 'text', text: `Job "${title}" APPROVED and PUSHED to origin/${branch_name}.\n\nNext: create a PR with \`gh pr create --base main --head ${branch_name}\`.${hint}` }] }
      }

      // Non-zero exit — auth failure, diverged ref, network, etc.
      // Revert the DB so the user can retry after fixing the underlying
      // issue. Include the tail of stderr (capped at ~2KB) so the
      // specific error surfaces to the caller.
      await query(
        "UPDATE devbrain.factory_jobs SET status = 'ready_for_approval', current_phase = 'ready_for_approval', updated_at = now() WHERE id = $1",
        [job_id],
      )
      const combined = `${push.stderr ?? ''}${push.stdout ?? ''}`.trim()
      const tail = combined.slice(-2048)
      return {
        content: [{
          type: 'text',
          text: `Job "${title}" approval PUSH FAILED (exit ${push.status}). Status reverted to ready_for_approval so you can retry after fixing the underlying issue.\n\ngit output tail:\n${tail || '(empty)'}\n\nOnce fixed, re-run factory_approve to retry.`,
        }],
      }
    } else if (action === 'reject') {
      await query(
        "UPDATE devbrain.factory_jobs SET status = 'rejected', current_phase = 'rejected', updated_at = now() WHERE id = $1",
        [job_id],
      )
      return { content: [{ type: 'text', text: `Job "${title}" REJECTED.${notes ? ' Reason: ' + notes : ''}` }] }
    } else {
      // request_changes — back to fix loop
      await query(
        "UPDATE devbrain.factory_jobs SET status = 'fix_loop', current_phase = 'fix_loop', error_count = error_count + 1, updated_at = now() WHERE id = $1",
        [job_id],
      )
      return { content: [{ type: 'text', text: `Job "${title}" sent back for changes.${notes ? ' Notes: ' + notes : ''}` }] }
    }
  },
)

// ─── Tool: factory_cleanup ──────────────────────────────────────────────────

server.tool(
  'factory_cleanup',
  'Manually archive a terminal factory job (approved, rejected, deployed, or failed). Removes it from the recent completed list.',
  {
    job_id: z.string().describe('Job ID to archive'),
  },
  async ({ job_id }) => {
    const jobResult = await query(
      'SELECT status, title, archived_at FROM devbrain.factory_jobs WHERE id = $1',
      [job_id],
    )

    if (jobResult.rows.length === 0) {
      return { content: [{ type: 'text', text: `Job ${job_id} not found.` }] }
    }

    const job = jobResult.rows[0]
    const status = job.status as string
    const title = job.title as string

    if (job.archived_at) {
      return { content: [{ type: 'text', text: `Job "${title}" is already archived (archived at ${job.archived_at}).` }] }
    }

    const terminalStatuses = ['approved', 'rejected', 'deployed', 'failed']
    if (!terminalStatuses.includes(status)) {
      return { content: [{ type: 'text', text: `Cannot clean up active jobs. Job "${title}" is currently in status: ${status}.` }] }
    }

    await query(
      'UPDATE devbrain.factory_jobs SET archived_at = now() WHERE id = $1',
      [job_id],
    )

    // Check for existing cleanup report
    const reportResult = await query(
      'SELECT outcome, summary FROM devbrain.factory_cleanup_reports WHERE job_id = $1 ORDER BY created_at DESC LIMIT 1',
      [job_id],
    )

    let reportInfo = ''
    if (reportResult.rows.length > 0) {
      const report = reportResult.rows[0]
      reportInfo = `\nCleanup report: ${report.outcome} — ${report.summary}`
    }

    return {
      content: [{
        type: 'text',
        text: `Job "${title}" archived successfully (status: ${status}).${reportInfo}`,
      }],
    }
  },
)

// ─── Tool: devbrain_notify ─────────────────────────────────────────────

server.tool(
  'devbrain_notify',
  'Send a notification to a registered dev through their configured channels (tmux, email, chat, telegram, webhooks). Use for agent-driven notifications during factory runs.',
  {
    recipient: z.string().describe('dev_id of the recipient (SSH username)'),
    event_type: z.enum([
      'job_ready', 'job_failed', 'lock_conflict',
      'unblocked', 'needs_human',
    ]).describe('Event type that determines notification routing'),
    title: z.string().describe('Notification title'),
    body: z.string().describe('Notification body'),
  },
  async ({ recipient, event_type, title, body }) => {
    const titleFile = join(tmpdir(), `devbrain-notif-title-${Date.now()}.txt`)
    const bodyFile = join(tmpdir(), `devbrain-notif-body-${Date.now()}.txt`)
    writeFileSync(titleFile, title)
    writeFileSync(bodyFile, body)

    try {
      const pythonBin = resolve(import.meta.dirname, '../../.venv/bin/python')
      const notifyScript = resolve(import.meta.dirname, '../../factory/notify_cli.py')

      const { spawnSync } = await import('child_process')
      const result = spawnSync(
        pythonBin,
        [notifyScript, recipient, event_type, titleFile, bodyFile],
        { encoding: 'utf-8' },
      )

      const output = result.stdout || result.stderr || 'No output'
      return {
        content: [{
          type: 'text',
          text: `devbrain_notify result:\n${output.trim()}`,
        }],
      }
    } finally {
      try { unlinkSync(titleFile) } catch {}
      try { unlinkSync(bodyFile) } catch {}
    }
  },
)

// ─── Tool: factory_file_locks ───────────────────────────────────────────────

server.tool(
  'factory_file_locks',
  'Show currently locked files in the factory. Use to debug why a job is WAITING, or see what other devs are working on.',
  {
    project: z.string().optional().describe('Project slug (defaults to DEVBRAIN_PROJECT)'),
  },
  async ({ project }) => {
    const slug = project ?? DEFAULT_PROJECT
    if (!slug) {
      return { content: [{ type: 'text', text: 'No project specified.' }] }
    }

    const projectId = await resolveProjectId(slug)
    if (!projectId) {
      return { content: [{ type: 'text', text: `Project "${slug}" not found.` }] }
    }

    const result = await query(
      `SELECT fl.file_path, fl.dev_id, fl.locked_at, fl.expires_at,
              j.title as job_title, j.status as job_status, j.id as job_id
       FROM devbrain.file_locks fl
       JOIN devbrain.factory_jobs j ON fl.job_id = j.id
       WHERE fl.project_id = $1 AND fl.expires_at > now()
       ORDER BY fl.locked_at ASC`,
      [projectId],
    )

    if (result.rows.length === 0) {
      return { content: [{ type: 'text', text: `No file locks active for project "${slug}".` }] }
    }

    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          project: slug,
          active_locks: result.rows.length,
          locks: result.rows,
        }, null, 2),
      }],
    }
  },
)

// ─── Tool: devbrain_resolve_blocked ──────────────────────────────────────

server.tool(
  'devbrain_resolve_blocked',
  'Resolve a blocked factory job. Call after investigating via factory_status/factory_file_locks and discussing with the dev. Sets the resolution and spawns a factory process to execute it.',
  {
    job_id: z.string().describe('Job ID (full UUID or first 8 chars)'),
    action: z.enum(['proceed', 'replan', 'cancel']).describe(
      'proceed: use original plan once locks free. replan: re-run planning with updated code. cancel: kill the job.'
    ),
    notes: z.string().optional().describe('Optional notes about why this decision was made'),
  },
  async ({ job_id, action, notes }) => {
    // Resolve short job_id to full UUID if needed
    let fullJobId = job_id
    if (job_id.length < 32) {
      const result = await query<{ id: string }>(
        "SELECT id FROM devbrain.factory_jobs WHERE id::text LIKE $1 AND status = 'blocked' LIMIT 1",
        [`${job_id}%`],
      )
      if (result.rows.length === 0) {
        return {
          content: [{
            type: 'text',
            text: `No blocked job found matching "${job_id}".`,
          }],
        }
      }
      fullJobId = result.rows[0].id
    }

    // Verify the job exists and is blocked
    const job = await query(
      "SELECT id, title, status, submitted_by FROM devbrain.factory_jobs WHERE id = $1",
      [fullJobId],
    )

    if (job.rows.length === 0) {
      return {
        content: [{
          type: 'text',
          text: `Job ${fullJobId} not found.`,
        }],
      }
    }

    const status = job.rows[0].status as string
    const title = job.rows[0].title as string

    if (status !== 'blocked') {
      return {
        content: [{
          type: 'text',
          text: `Job "${title}" is not blocked (status: ${status}). Cannot apply resolution.`,
        }],
      }
    }

    // Write resolution + notes to the DB
    await query(
      `UPDATE devbrain.factory_jobs
       SET blocked_resolution = $1,
           metadata = metadata || jsonb_build_object('resolution_notes', $2::text),
           updated_at = now()
       WHERE id = $3`,
      [action, notes ?? '', fullJobId],
    )

    // Spawn a detached factory process to execute the resolution
    try {
      const factoryPython = resolve(import.meta.dirname, '../../.venv/bin/python')
      const child = spawn(factoryPython, [FACTORY_RUNNER, fullJobId], {
        detached: true,
        stdio: ['ignore', 'pipe', 'pipe'],
        cwd: resolve(import.meta.dirname, '../..'),
      })
      child.unref()
      console.error(`[factory] Spawned resolver for blocked job ${fullJobId.slice(0, 8)} (pid ${child.pid})`)
    } catch (err) {
      return {
        content: [{
          type: 'text',
          text: `Resolution "${action}" saved but factory spawn failed: ${err}. Run manually: python factory/run.py ${fullJobId}`,
        }],
      }
    }

    return {
      content: [{
        type: 'text',
        text: `✅ Resolution "${action}" applied to job "${title}" (${fullJobId.slice(0, 8)}). Factory process spawned to execute.`,
      }],
    }
  },
)

// ─── Tool: agent_remote_prompt ───────────────────────────────────────────────
//
// Talks to a remote agent-bus daemon (github.com/nooma-stack/agent-bus) over
// HTTP. The daemon exposes an authenticated `claude -p` session on a remote
// host, typically reached through an SSH tunnel. This tool lets this Claude
// session drive another Claude session running on a different machine —
// useful for multi-host orchestration, remote-runner dev loops, etc.
//
// Config lives at ~/.devbrain/agent-bus.yaml (override via
// DEVBRAIN_AGENT_BUS_CONFIG):
//
//   targets:
//     mac-studio:
//       url: http://127.0.0.1:18900
//       token: <bearer token from remote daemon's ~/.agent-bus/token>
//
// The URL is the tunnel-exposed loopback address on THIS machine — set up
// the SSH tunnel separately (e.g., `ssh -L 18900:127.0.0.1:18900 mac-studio`).

import { readFileSync } from 'fs'
import YAML from 'yaml'

interface AgentBusTarget {
  url: string
  token: string
}

function loadAgentBusConfig(): Record<string, AgentBusTarget> {
  const configPath = process.env.DEVBRAIN_AGENT_BUS_CONFIG
    ?? join(homedir(), '.devbrain', 'agent-bus.yaml')
  if (!existsSync(configPath)) {
    return {}
  }
  try {
    const parsed = YAML.parse(readFileSync(configPath, 'utf-8'))
    return (parsed?.targets ?? {}) as Record<string, AgentBusTarget>
  } catch {
    return {}
  }
}

server.tool(
  'agent_remote_prompt',
  'Send a prompt to a remote Claude session via an agent-bus daemon. Preserves conversation context across calls when the same session_id is reused. Config: ~/.devbrain/agent-bus.yaml.',
  {
    target: z.string().describe('Target name from agent-bus.yaml (e.g., "mac-studio")'),
    prompt: z.string().describe('Prompt to send to the remote Claude session'),
    session_id: z.string().uuid().optional().describe('UUID for conversation continuity. Omit to start a new session (daemon generates one).'),
    cwd: z.string().optional().describe('Working directory for the remote claude subprocess. Lets callers scope file access.'),
  },
  async ({ target, prompt, session_id, cwd }) => {
    const targets = loadAgentBusConfig()
    const cfg = targets[target]
    if (!cfg) {
      const available = Object.keys(targets)
      const hint = available.length > 0
        ? `Available targets: ${available.join(', ')}.`
        : 'No agent-bus targets configured yet. See github.com/nooma-stack/agent-bus#use for provisioning.'
      return { content: [{ type: 'text', text: `Target "${target}" not found in agent-bus config. ${hint}` }] }
    }
    if (!cfg.url || !cfg.token) {
      return { content: [{ type: 'text', text: `Target "${target}" is missing url or token in agent-bus.yaml.` }] }
    }

    const body: Record<string, unknown> = { prompt }
    if (session_id) body.session_id = session_id
    if (cwd) body.cwd = cwd

    let response: Response
    try {
      response = await fetch(`${cfg.url.replace(/\/$/, '')}/prompt`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${cfg.token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(body),
      })
    } catch (err) {
      return { content: [{ type: 'text', text: `Network error reaching ${cfg.url}: ${err}. Is the SSH tunnel up and the daemon running?` }] }
    }

    const payload = await response.json().catch(() => null) as {
      session_id?: string
      result?: string
      stderr?: string
      exit_code?: number
    } | null

    if (!response.ok) {
      const detail = payload ? JSON.stringify(payload) : await response.text().catch(() => '(no body)')
      return { content: [{ type: 'text', text: `Daemon returned ${response.status}: ${detail}` }] }
    }

    if (!payload) {
      return { content: [{ type: 'text', text: 'Daemon returned unparseable response.' }] }
    }

    if (payload.exit_code !== 0) {
      return {
        content: [{
          type: 'text',
          text: `Remote claude exited ${payload.exit_code}\nstderr: ${payload.stderr ?? '(empty)'}\nsession_id: ${payload.session_id ?? '(none)'}`,
        }],
      }
    }

    return {
      content: [{
        type: 'text',
        text: `[session: ${payload.session_id ?? 'unknown'}]\n\n${payload.result ?? '(empty response)'}`,
      }],
    }
  },
)

// ─── Start server ────────────────────────────────────────────────────────────

async function main() {
  const transport = new StdioServerTransport()
  await server.connect(transport)
}

main().catch((err) => {
  console.error('Fatal error:', err)
  process.exit(1)
})
