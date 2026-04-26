"""Tests for attribute-orphans (factory/attribute_orphans.py).

Each test seeds rows directly into ``raw_sessions`` and/or ``chunks``
with content/source-hash starting with ``TEST_CONTENT_PREFIX`` so the
autouse cleanup fixture can wipe them with one ``LIKE`` query, even if
a previous run aborted partway through.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

# Mirror production sys.path layout: factory/ for config + state_machine
# + attribute_orphans; factory/tests has no package __init__ adjustments.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import attribute_orphans  # noqa: E402
from cli import cli as devbrain_cli  # noqa: E402
from config import DATABASE_URL  # noqa: E402
from state_machine import FactoryDB  # noqa: E402

TEST_CONTENT_PREFIX = "attribute_orphans_test_"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def _cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        # Chunks first — they FK on raw_sessions via source_id, but
        # the constraint isn't declared. Order matches our LIKE shape.
        cur.execute(
            "DELETE FROM devbrain.chunks WHERE content LIKE %s",
            (f"{TEST_CONTENT_PREFIX}%",),
        )
        cur.execute(
            "DELETE FROM devbrain.raw_sessions WHERE source_hash LIKE %s",
            (f"{TEST_CONTENT_PREFIX}%",),
        )
        conn.commit()


# ─── helpers ─────────────────────────────────────────────────────────────────


def _devbrain_project_id(db) -> str:
    """The seeded 'devbrain' project (migration 001) — used as a real
    FK target instead of creating a throwaway project per test."""
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.projects WHERE slug = 'devbrain'"
        )
        return str(cur.fetchone()[0])


def _embedding_sql(value: float = 0.0) -> str:
    return "[" + ",".join([str(value)] * 1024) + "]"


def _seed_orphan_session(
    db,
    *,
    source_path: str,
    project_id: str | None = None,
    source_app: str = "claude_code",
) -> str:
    """Direct INSERT into devbrain.raw_sessions. source_hash uses the
    test prefix so cleanup can find it."""
    source_hash = f"{TEST_CONTENT_PREFIX}{uuid.uuid4().hex[:32]}"
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devbrain.raw_sessions
                (project_id, source_app, source_path, source_hash, raw_content)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (project_id, source_app, source_path, source_hash,
             f"{TEST_CONTENT_PREFIX}raw"),
        )
        sess_id = str(cur.fetchone()[0])
        conn.commit()
    return sess_id


def _seed_orphan_chunk(
    db,
    *,
    source_id: str | None,
    project_id: str | None = None,
) -> str:
    embedding_sql = _embedding_sql(0.1)
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devbrain.chunks
                (project_id, source_type, source_id, content, embedding)
            VALUES (%s, %s, %s, %s, %s::vector)
            RETURNING id
            """,
            (project_id, "session", source_id,
             f"{TEST_CONTENT_PREFIX}{uuid.uuid4().hex[:8]}",
             embedding_sql),
        )
        chunk_id = str(cur.fetchone()[0])
        conn.commit()
    return chunk_id


def _read_session_pid(db, sess_id: str) -> str | None:
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT project_id FROM devbrain.raw_sessions WHERE id = %s",
            (sess_id,),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _read_chunk_pid(db, chunk_id: str) -> str | None:
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT project_id FROM devbrain.chunks WHERE id = %s",
            (chunk_id,),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


# ─── 1. decode simple ───────────────────────────────────────────────────────


def test_decode_simple_path():
    home = str(Path.home())
    encoded = f"{home}/.claude/projects/-Users-x-devbrain/sess.jsonl"
    assert attribute_orphans.decode_claude_code_path(encoded) == "/Users/x/devbrain"


# ─── 2. decode subagent path → still resolves to parent project ──────────────


def test_decode_subagent_path_uses_first_segment():
    home = str(Path.home())
    encoded = (
        f"{home}/.claude/projects/-Users-x-devbrain/agents/sub/sess.jsonl"
    )
    # Only the first encoded segment matters; the suffix is the
    # transcript path under the project's claude_code dir.
    assert attribute_orphans.decode_claude_code_path(encoded) == "/Users/x/devbrain"


# ─── 3. decode worktree path → re-glued ──────────────────────────────────────


def test_decode_worktree_path_reglues_dash():
    home = str(Path.home())
    encoded = (
        f"{home}/.claude/projects/-Users-x-devbrain-worktrees-abc12345/sess.jsonl"
    )
    # Naive decode lands at /Users/x/devbrain/worktrees/abc12345 (not a
    # real path); the worktree regex re-glues the missing dash.
    assert (
        attribute_orphans.decode_claude_code_path(encoded)
        == "/Users/x/devbrain-worktrees/abc12345"
    )


# ─── 4. decode degenerate path → None ───────────────────────────────────────


def test_decode_degenerate_returns_none():
    home = str(Path.home())
    # Encoded segment is just "-" → strips to empty → unrecoverable.
    encoded = f"{home}/.claude/projects/-/sess.jsonl"
    assert attribute_orphans.decode_claude_code_path(encoded) is None


def test_decode_non_claude_path_returns_none():
    # Any path not under ~/.claude/projects/ is opaque to this decoder.
    assert attribute_orphans.decode_claude_code_path("/var/log/x.jsonl") is None


# ─── 5. resolve_project_id longest-prefix match ─────────────────────────────


def test_resolve_project_id_longest_prefix_wins(db):
    # Seed a sub-slug project so the longest prefix can win against
    # the seed 'devbrain' project. Cleanup deletes it at the end.
    sub_slug = f"{TEST_CONTENT_PREFIX}sub_{uuid.uuid4().hex[:8]}"
    sub_path = "/Users/x/devbrain/subdir"
    parent_path = "/Users/x/devbrain"
    parent_slug = "devbrain"

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.projects (slug, name) VALUES (%s, %s)",
            (sub_slug, "test-sub"),
        )
        conn.commit()

    try:
        # patch.dict so the in-memory dict the function reads gets the
        # test mappings, then is restored on exit.
        new_paths = {parent_slug: parent_path, sub_slug: sub_path}
        with patch.dict(
            attribute_orphans.FACTORY_CONFIG,
            {"project_paths": new_paths},
            clear=False,
        ):
            # Path under the sub project must resolve to sub.
            sub_pid = attribute_orphans.resolve_project_id(
                db, "/Users/x/devbrain/subdir/file.txt"
            )
            # Path only under parent must resolve to parent.
            parent_pid = attribute_orphans.resolve_project_id(
                db, "/Users/x/devbrain/other/file.txt"
            )
            # No match returns None.
            none_pid = attribute_orphans.resolve_project_id(
                db, "/elsewhere/x"
            )
            # Boundary check: /Users/x/devbrain-foo must NOT match
            # /Users/x/devbrain (no trailing /).
            boundary_pid = attribute_orphans.resolve_project_id(
                db, "/Users/x/devbrain-foo"
            )

        assert sub_pid is not None
        assert parent_pid is not None
        assert sub_pid != parent_pid
        assert none_pid is None
        assert boundary_pid is None
    finally:
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM devbrain.projects WHERE slug = %s",
                        (sub_slug,))
            conn.commit()


# ─── 6. attribute_orphan_sessions writes project_id ─────────────────────────


def test_attribute_orphan_sessions_writes_project_id(db):
    pid = _devbrain_project_id(db)
    home = str(Path.home())
    src = f"{home}/.claude/projects/-Users-x-devbrain/sess.jsonl"
    sess_id = _seed_orphan_session(db, source_path=src)

    new_paths = {"devbrain": "/Users/x/devbrain"}
    with patch.dict(
        attribute_orphans.FACTORY_CONFIG,
        {"project_paths": new_paths},
        clear=False,
    ):
        counts = attribute_orphans.attribute_orphan_sessions(db, batch_size=10)

    assert counts["scanned"] >= 1
    assert counts["attributed"] >= 1
    assert counts["batch_failures"] == 0
    assert _read_session_pid(db, sess_id) == pid


# ─── 7. chunks inherit from parent (sessions then chunks) ───────────────────


def test_attribute_orphan_chunks_inherits_from_parent(db):
    pid = _devbrain_project_id(db)
    home = str(Path.home())
    src = f"{home}/.claude/projects/-Users-x-devbrain/sess.jsonl"
    sess_id = _seed_orphan_session(db, source_path=src)
    chunk_id = _seed_orphan_chunk(db, source_id=sess_id)

    new_paths = {"devbrain": "/Users/x/devbrain"}
    with patch.dict(
        attribute_orphans.FACTORY_CONFIG,
        {"project_paths": new_paths},
        clear=False,
    ):
        results = attribute_orphans.attribute_all(db, batch_size=10)

    assert results["sessions"]["attributed"] >= 1
    assert results["chunks"]["attributed"] >= 1
    assert _read_session_pid(db, sess_id) == pid
    assert _read_chunk_pid(db, chunk_id) == pid


# ─── 8. idempotent — second pass attributes nothing ─────────────────────────


def test_attribute_orphan_sessions_idempotent(db):
    home = str(Path.home())
    src = f"{home}/.claude/projects/-Users-x-devbrain/sess.jsonl"
    _seed_orphan_session(db, source_path=src)

    new_paths = {"devbrain": "/Users/x/devbrain"}
    with patch.dict(
        attribute_orphans.FACTORY_CONFIG,
        {"project_paths": new_paths},
        clear=False,
    ):
        first = attribute_orphans.attribute_orphan_sessions(db, batch_size=10)
        assert first["attributed"] >= 1
        # Second pass: SELECT filters project_id IS NULL so the seeded
        # row no longer matches; counters report zero.
        second = attribute_orphans.attribute_orphan_sessions(db, batch_size=10)

    assert second["attributed"] == 0
    assert second["batch_failures"] == 0


# ─── 9. dry-run doesn't write but reports predictions ───────────────────────


def test_attribute_orphan_sessions_dry_run_does_not_write(db):
    home = str(Path.home())
    src = f"{home}/.claude/projects/-Users-x-devbrain/sess.jsonl"
    sess_id = _seed_orphan_session(db, source_path=src)

    new_paths = {"devbrain": "/Users/x/devbrain"}
    with patch.dict(
        attribute_orphans.FACTORY_CONFIG,
        {"project_paths": new_paths},
        clear=False,
    ):
        counts = attribute_orphans.attribute_orphan_sessions(
            db, batch_size=10, dry_run=True,
        )

    assert counts["scanned"] >= 1
    assert counts["attributed"] >= 1  # predicted, not actual
    assert _read_session_pid(db, sess_id) is None  # no write


# ─── 10. default_project fallback ───────────────────────────────────────────


def test_attribute_orphan_sessions_default_project_fallback(db):
    pid = _devbrain_project_id(db)
    home = str(Path.home())
    # Degenerate path → decoder returns None → falls back to default.
    src = f"{home}/.claude/projects/-/sess.jsonl"
    sess_id = _seed_orphan_session(db, source_path=src)

    counts = attribute_orphans.attribute_orphan_sessions(
        db, batch_size=10, default_project_slug="devbrain",
    )

    assert counts["fallback_to_default"] >= 1
    assert _read_session_pid(db, sess_id) == pid


def test_default_project_unknown_slug_raises_value_error(db):
    bogus = f"{TEST_CONTENT_PREFIX}does_not_exist"
    with pytest.raises(ValueError):
        attribute_orphans.attribute_orphan_sessions(
            db, batch_size=10, default_project_slug=bogus,
        )


# ─── 11. (bonus) does not touch already-attributed rows ─────────────────────


def test_attribute_does_not_touch_already_attributed(db):
    pid = _devbrain_project_id(db)
    home = str(Path.home())
    # Already-attributed session — must remain untouched even with a
    # decode that would otherwise resolve to a *different* project.
    src = f"{home}/.claude/projects/-Users-x-other/sess.jsonl"
    sess_id = _seed_orphan_session(db, source_path=src, project_id=pid)
    chunk_id = _seed_orphan_chunk(db, source_id=sess_id, project_id=pid)

    new_paths = {"devbrain": "/Users/x/other"}  # would resolve to devbrain
    with patch.dict(
        attribute_orphans.FACTORY_CONFIG,
        {"project_paths": new_paths},
        clear=False,
    ):
        attribute_orphans.attribute_all(db, batch_size=10)

    # SELECT filtered them out (project_id IS NOT NULL), so untouched.
    assert _read_session_pid(db, sess_id) == pid
    assert _read_chunk_pid(db, chunk_id) == pid


# ─── 12. (bonus) end-to-end CLI invocation ──────────────────────────────────


def test_cli_attribute_orphans_invokes_module(db):
    """End-to-end: the CLI command runs the module and prints both
    lines without raising. Catches breakage in the click wiring (option
    names, exit codes, callable signature) that pure-module tests
    miss."""
    runner = CliRunner()
    result = runner.invoke(devbrain_cli, ["attribute-orphans", "--dry-run"])
    assert result.exit_code == 0, (
        f"CLI exited with {result.exit_code}; output:\n{result.output}"
    )
    assert "sessions" in result.output
    assert "chunks" in result.output
