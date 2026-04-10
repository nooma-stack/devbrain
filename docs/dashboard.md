# DevBrain Factory Dashboard

A real-time terminal UI for watching factory jobs as they progress.

## Launch

```bash
devbrain dashboard                     # All projects
devbrain dashboard --project brightbot # Filter to one project
```

Runs in any terminal (including over SSH, inside tmux). Built with [Textual](https://textual.textualize.io/).

## Layout

Four panels, top to bottom:

1. **Active Jobs** — Jobs currently in flight. Shows status icon, short ID, title, current phase, progress fraction (e.g. `3/6`), submitting dev, and last-update age. Select a row (↑↓ + Enter) to open the detail modal.

2. **Recent Events** — Scrolling log of factory activity from the last hour. Each line shows timestamp, job ID, job title, and a summary of what happened (e.g. `arch review: 2 blocking, 1 warning`). Color-coded by event type.

3. **File Locks** — All currently-held file locks across the team. Shows file path, owning job, dev, and job status. Useful for spotting why your job might be blocked before it even happens.

4. **Recently Completed** — Jobs that reached a terminal state in the last 24 hours. Shows status emoji, ID, title, final status, dev, and retry count.

## Keyboard Shortcuts

- `q` — quit
- `r` — manual refresh (auto-refresh runs every 2 seconds anyway)
- `↑` `↓` — navigate rows in a table
- `Enter` — open job detail modal for selected row
- `Esc` or `q` — close the detail modal

## Job Detail Modal

Pressing Enter on a job row opens a modal with:

- Full title, status, branch, retry count
- Spec (the original feature request)
- List of artifacts with phase, findings count, blocking count
- Cleanup reports (post-run, recovery, blocked investigations) with summaries

## How It Works

Polls the DevBrain DB every 2 seconds. No new schema — all data comes from existing tables:

- `devbrain.factory_jobs` for active and completed jobs
- `devbrain.factory_artifacts` for recent events
- `devbrain.file_locks` for lock info
- `devbrain.factory_cleanup_reports` for investigation reports in the detail modal

The dashboard is **read-only** — it never writes to the DB. It's purely observational, so you can run multiple instances without conflict.

## Status Icons

| Icon | Status | Meaning |
|------|--------|---------|
| ⏳ | queued | Waiting in the queue |
| 📝 | planning | Planning agent running |
| 🔒 | blocked | Blocked on file lock conflicts, awaiting dev resolution |
| 🟢 | implementing | Implementation agent running |
| 👁 | reviewing | Architecture/security review |
| 🧪 | qa | Lint and tests running |
| 🔄 | fix_loop | Applying fixes from review |
| ✅ | ready_for_approval | Done — waiting for your approval |
| 👍 | approved | You approved — ready to push |
| 🚀 | deployed | Branch pushed, deployed |
| 🚫 | rejected | Rejected (manually or blocked-resolved-cancel) |
| ❌ | failed | Failed after exhausting retries |

## Tips

- **Run it in a dedicated tmux pane** so you always have factory visibility while coding.
- **Combine with `devbrain watch`** (notification tail) for a full observability setup.
- **On a shared team**, leave it open to spot conflicts before they block you.
- The dashboard shows all projects by default. Use `--project <slug>` to focus on one.

## Troubleshooting

**"Textual not installed"**: Run `.venv/bin/pip install textual` from the DevBrain directory.

**Dashboard shows empty panels**: The DB probably has no active factory jobs. Try submitting one with `factory_plan` via the MCP tool or check `devbrain history --recent 5`.

**Colors look wrong**: Your terminal may not support 256 colors. Try setting `TERM=xterm-256color`.

**Panel is cut off**: Resize your terminal or tmux pane. The dashboard needs at least ~80 columns and ~30 rows for a comfortable view.

## Related

- **Notifications** — Push alerts for critical events. See `docs/notifications/README.md`
- **`devbrain history`** — Query past notifications by dev, job, or natural language
- **`devbrain blocked`** — CLI fallback for listing blocked jobs (same data as the dashboard's "Active Jobs" panel for BLOCKED status)
- **`devbrain resolve`** — CLI fallback for resolving blocked jobs
