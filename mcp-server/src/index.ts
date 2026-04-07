#!/usr/bin/env node

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { z } from 'zod'
import { query } from './db.js'
import { embed, toSqlVector } from './embeddings.js'
import { summarizeSession } from './summarize.js'

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

    const [projectInfo, decisions, issues, patterns, jobs] = await Promise.all([
      query('SELECT name, description, root_path, constraints, tech_stack FROM devbrain.projects WHERE id = $1', [projectId]),
      query('SELECT title, decision, rationale, created_at FROM devbrain.decisions WHERE project_id = $1 AND status = \'active\' ORDER BY created_at DESC LIMIT 5', [projectId]),
      query('SELECT title, category, description, fix_applied, created_at FROM devbrain.issues WHERE project_id = $1 ORDER BY created_at DESC LIMIT 5', [projectId]),
      query('SELECT name, category, description FROM devbrain.patterns WHERE project_id = $1 ORDER BY created_at DESC LIMIT 5', [projectId]),
      query('SELECT title, status, current_phase, branch_name FROM devbrain.factory_jobs WHERE project_id = $1 AND status NOT IN (\'approved\', \'rejected\') ORDER BY created_at DESC LIMIT 5', [projectId]),
    ])

    const ctx = {
      project: projectInfo.rows[0] ?? null,
      recent_decisions: decisions.rows,
      recent_issues: issues.rows,
      relevant_patterns: patterns.rows,
      active_factory_jobs: jobs.rows,
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

    let whereClause = ''
    const params: unknown[] = [vectorStr, limit]
    let paramIdx = 3

    if (!cross_project) {
      const slug = project ?? DEFAULT_PROJECT
      if (slug) {
        const projectId = await resolveProjectId(slug)
        if (projectId) {
          whereClause += ` AND c.project_id = $${paramIdx}`
          params.push(projectId)
          paramIdx++
        }
      }
    }

    if (source_types && source_types.length > 0) {
      whereClause += ` AND c.source_type = ANY($${paramIdx})`
      params.push(source_types)
      paramIdx++
    }

    const sql = `
      SELECT
        c.id as chunk_id,
        c.content,
        1 - (c.embedding <=> $1::vector) as score,
        c.source_type,
        c.source_id,
        c.source_line_start,
        c.source_line_end,
        c.metadata,
        p.slug as project
      FROM devbrain.chunks c
      JOIN devbrain.projects p ON c.project_id = p.id
      WHERE c.embedding IS NOT NULL ${whereClause}
      ORDER BY c.embedding <=> $1::vector
      LIMIT $2
    `

    const result = await query(sql, params)

    const results = await Promise.all(
      result.rows.map(async (row) => {
        const r: Record<string, unknown> = {
          chunk_id: row.chunk_id,
          content: row.content,
          score: Number(Number(row.score).toFixed(4)),
          source_type: row.source_type,
          source_ref: row.source_id
            ? `${row.source_type}_${String(row.source_id).slice(0, 8)}:${row.source_line_start ?? '?'}-${row.source_line_end ?? '?'}`
            : null,
          project: row.project,
          has_full_context: row.source_type === 'session' && row.source_id != null,
        }

        // Auto drill-down: fetch raw context for top results if depth is full or auto with high scores
        if (
          (depth === 'full' || (depth === 'auto' && Number(row.score) > 0.6)) &&
          row.source_type === 'session' &&
          row.source_id
        ) {
          const rawResult = await query(
            'SELECT raw_content FROM devbrain.raw_sessions WHERE id = $1',
            [row.source_id],
          )
          if (rawResult.rows[0]) {
            const raw = rawResult.rows[0].raw_content as string
            const lines = raw.split('\n')
            const start = Math.max(0, (Number(row.source_line_start) || 0) - 25)
            const end = Math.min(lines.length, (Number(row.source_line_end) || lines.length) + 25)
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
      // Generic note stored as a chunk
      const result = await query<{ id: string }>(
        `INSERT INTO devbrain.chunks (project_id, source_type, content, embedding, metadata)
         VALUES ($1, 'note', $2, $3::vector, $4) RETURNING id`,
        [projectId, `${title}\n\n${content}`, vectorStr, JSON.stringify({ tags: tags ?? [], category: category ?? '' })],
      )
      recordId = result.rows[0].id
      return { content: [{ type: 'text', text: `Stored note "${title}" (${recordId.slice(0, 8)}).` }] }
    }

    // Also create an embedded chunk for the record
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

    await query(
      `INSERT INTO devbrain.chunks (project_id, source_type, content, embedding, metadata)
       VALUES ($1, 'session_summary', $2, $3::vector, $4)`,
      [projectId, fullContent, toSqlVector(embedding), JSON.stringify({
        decisions_made: decisions_made ?? [],
        files_changed: files_changed ?? [],
        issues_found: issues_found ?? [],
        next_steps: next_steps ?? [],
        timestamp: new Date().toISOString(),
      })],
    )

    return { content: [{ type: 'text', text: `Session summary stored for project "${project}".` }] }
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

server.tool(
  'factory_plan',
  'Submit a feature to the dev factory for autonomous implementation. Creates a job that will be planned, implemented, reviewed, QA tested, and staged for your approval.',
  {
    project: z.string().describe('Project slug'),
    title: z.string().describe('Feature title'),
    spec: z.string().describe('Feature requirements and description'),
    priority: z.number().optional().default(0).describe('Priority (higher = more urgent)'),
    assigned_cli: z.string().optional().describe('CLI to use: claude, codex, gemini (default: claude)'),
  },
  async ({ project, title, spec, priority, assigned_cli }) => {
    const projectId = await resolveProjectId(project)
    if (!projectId) {
      return { content: [{ type: 'text', text: `Project "${project}" not found.` }] }
    }

    const result = await query<{ id: string }>(
      `INSERT INTO devbrain.factory_jobs
          (project_id, title, spec, status, priority, current_phase, assigned_cli)
       VALUES ($1, $2, $3, 'queued', $4, 'queued', $5)
       RETURNING id`,
      [projectId, title, spec, priority, assigned_cli ?? 'claude'],
    )

    const jobId = result.rows[0].id
    return {
      content: [{
        type: 'text',
        text: `Factory job created: ${jobId}\nTitle: ${title}\nStatus: queued\nCLI: ${assigned_cli ?? 'claude'}\n\nThe job will be processed by the factory pipeline. Use factory_status to check progress.`,
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
  'Approve or reject a dev factory job that is ready for review. Shows the plan, review findings, and QA results.',
  {
    job_id: z.string().describe('Job ID to approve/reject'),
    action: z.enum(['approve', 'reject', 'request_changes']).describe('Action to take'),
    notes: z.string().optional().describe('Optional notes for the decision'),
  },
  async ({ job_id, action, notes }) => {
    const job = await query(
      'SELECT status, title FROM devbrain.factory_jobs WHERE id = $1',
      [job_id],
    )

    if (job.rows.length === 0) {
      return { content: [{ type: 'text', text: `Job ${job_id} not found.` }] }
    }

    const status = job.rows[0].status as string
    const title = job.rows[0].title as string

    if (action === 'approve') {
      if (status !== 'ready_for_approval') {
        return { content: [{ type: 'text', text: `Job is not ready for approval (status: ${status}).` }] }
      }
      await query(
        "UPDATE devbrain.factory_jobs SET status = 'approved', current_phase = 'approved', updated_at = now() WHERE id = $1",
        [job_id],
      )
      return { content: [{ type: 'text', text: `Job "${title}" APPROVED. Branch is ready to push.` }] }
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

// ─── Start server ────────────────────────────────────────────────────────────

async function main() {
  const transport = new StdioServerTransport()
  await server.connect(transport)
}

main().catch((err) => {
  console.error('Fatal error:', err)
  process.exit(1)
})
