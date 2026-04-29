# Postulate tests

AGM-style postulate tests for DevBrain's discipline layer. One file
per postulate, named `test_pN_*.py`. See
`docs/plans/2026-04-29-phase-3-discipline-layer.md` §3.3 + §7 for
the design.

## Running

These tests require a real Postgres (the substrate they exercise —
`memory_dependencies`, `memory_ledger`, the trigger chain — does
not have a useful in-memory mock). Start the local DB:

```sh
docker compose up -d devbrain-db
```

Set the password (read from your `~/devbrain/.env` or wherever you
keep it) and run pytest from the repo root:

```sh
export DEVBRAIN_DB_PASSWORD=...   # or DEVBRAIN_TEST_DATABASE_URL=...
pip install psycopg2-binary       # if not already installed
python -m pytest tests/postulates/ -v
```

Connection defaults: `127.0.0.1:5433`, user `devbrain`, db
`devbrain`. Override individually via `DEVBRAIN_DB_HOST`,
`DEVBRAIN_DB_HOST_PORT`, `DEVBRAIN_DB_USER`, `DEVBRAIN_DB_NAME`, or
collectively via `DEVBRAIN_TEST_DATABASE_URL`.

## CI

Not yet wired. The repo's `pytest` job runs a curated **no-DB**
allow-list (see `.github/workflows/test.yml`) that deliberately
excludes anything that touches Postgres. A DB-available workflow
with a `pgvector/pgvector:pg17` service container is the planned
follow-up — see the comment block in `test.yml` for the migration
ordering caveat.

## xfail policy

Postulates whose enforcement layer hasn't shipped yet are marked
`@pytest.mark.xfail(strict=True, reason=...)`. Strict mode means an
unexpected pass (`XPASS`) fails the suite. That guarantees:

- adding the enforcement layer (e.g. the curator agent) without
  removing the marker fails CI loudly, forcing you back here to
  own the postulate.
- removing the enforcement layer in the future fails CI loudly.

Today's xfailed postulates and what unblocks them:

| Postulate | Unblocked by |
|-----------|--------------|
| P1 — supersession cascade re-eval | Atlas Step 5 (curator agent + cascade re-eval queue) |
| P2 — archived memory excluded from curator brief | Atlas Step 5 (curator agent) |

## Adding a new postulate

1. Drop a one-paragraph postulate statement at the top of the
   test file (mirrors the format in §3.3 of the plan).
2. Write a deterministic fixture that asserts it.
3. If the enforcement layer doesn't ship yet, mark it
   `xfail(strict=True, reason=...)` with a pointer to the plan
   step that lands it.
4. Document the unblock condition in the table above.
