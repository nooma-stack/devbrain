# Contributing to DevBrain

Thanks for your interest in DevBrain. It's early-stage work, so the most
valuable contributions right now are **real-world install feedback, bug
reports from running the factory pipeline, and ingest adapter
improvements**.

---

## Reporting issues

Open an issue on GitHub. Helpful details:

- Output of `./bin/devbrain doctor --json`
- OS and arch (macOS Apple Silicon, macOS Intel, Linux distro, etc.)
- Relevant log excerpts from `logs/ingest.log`, `logs/factory.log`
- Steps to reproduce

For security issues, please email the maintainers privately instead of
opening a public issue.

---

## Development setup

1. Follow [INSTALL.md](INSTALL.md) to get a working local install.
2. `./bin/devbrain doctor` must exit 0 before you start.
3. Run the test suite:
   ```bash
   .venv/bin/python -m pytest factory/tests/ -q
   ```
   All 173 tests should pass.

---

## Making changes

### Pull requests

- Work on a branch off `main`. Name it descriptively
  (`feat/some-new-thing`, `fix/broken-thing`).
- Keep PRs focused. Smaller is better — one concept per PR.
- Include tests for new behavior and for any bug you fix.
- Update docs if you change user-visible behavior or the install path.

### Commit messages

DevBrain uses Conventional Commits. Pick the prefix that matches your change:

| Prefix       | Use for                                                 | Version bump |
|--------------|---------------------------------------------------------|--------------|
| `feat:`      | New user-facing feature or capability                   | Minor        |
| `fix:`       | Bug fix in user-facing functionality                    | Patch        |
| `refactor:`  | Code restructuring with no behavior change              | None         |
| `docs:`      | Documentation only                                      | None         |
| `chore:`     | Config, deps, maintenance                               | None         |
| `ci:`        | CI/CD pipeline changes                                  | None         |
| `test:`      | Tests only                                              | None         |
| `build:`     | Docker, build system, infrastructure                    | None         |

Use the body of the commit to explain *why*. The diff shows *what*.

Sign off commits per the [Developer Certificate of Origin](https://developercertificate.org/):

```bash
git commit -s -m "feat: add something useful"
```

### Code style

- **Python:** [Ruff](https://docs.astral.sh/ruff/) for lint + format.
  Run `ruff check .` and `ruff format .` before committing.
- **TypeScript:** Prettier + ESLint (see `mcp-server/`).
- **Markdown:** soft-wrap around 80 chars. GitHub-flavored.

---

## Adding an ingest adapter

See [`ingest/adapters/base.py`](ingest/adapters/base.py) for the
`TranscriptAdapter` protocol. Minimum contract:

- `detect(file_path) -> bool` — is this file yours to parse?
- `detect_project(file_path) -> str | None` — which project does it belong to?
- `parse(file_path) -> UniversalSession | None` — turn the file into a
  standard session object.

Tests go in `ingest/` alongside the adapter.

## Adding a notification channel

See [`factory/notifications/base.py`](factory/notifications/base.py) for
the `NotificationChannel` protocol. Register new channels in the channel
registry. Add tests mirroring the existing `test_channel_*.py` files.

---

## Non-goals

DevBrain is intentionally opinionated. Before proposing these, open an
issue to discuss:

- Adding a second database (Neo4j, Redis, etc.) — Postgres-only is a core principle.
- Running in a fully cloud-hosted mode — DevBrain is local-first.
- Auto-calling paid model APIs on the user's behalf.
- Adding a web UI before the TUI dashboard is feature-complete.

---

## Questions?

Open a GitHub discussion or issue. For substantial proposals (new pipeline
phase, schema change, new MCP tool), it's worth discussing before writing
code.
