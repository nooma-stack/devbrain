"""Tests for the export-memory / import-memory pipeline (#5.b).

Same DB-prefix isolation pattern as ``test_backfill_memory.py``: every
seeded row carries ``EXPORT_IMPORT_TEST_PREFIX`` in a string column so
the autouse cleanup fixture can wipe both source and destination
artifacts even if a previous run aborted.

Each test seeds rows directly, exercises export → import via either
``export_to_dict`` (in-memory) or ``write_export_file`` (round-trip
through disk), and asserts on the imported devbrain.memory /
raw_sessions / projects / devs rows.
"""
from __future__ import annotations

import gzip
import json
import sys
import uuid
from pathlib import Path

import pytest

# Mirror production sys.path layout — same shim test_backfill_memory.py uses.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import export_memory  # noqa: E402
import import_memory  # noqa: E402
from config import DATABASE_URL  # noqa: E402
from state_machine import FactoryDB  # noqa: E402

# Every seeded row's content/title/slug starts with this prefix so the
# autouse cleanup fixture can wipe them with a handful of LIKE queries.
EXPORT_IMPORT_TEST_PREFIX = "export_import_test_"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def _cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM devbrain.memory WHERE content LIKE %s OR title LIKE %s",
            (f"{EXPORT_IMPORT_TEST_PREFIX}%", f"{EXPORT_IMPORT_TEST_PREFIX}%"),
        )
        cur.execute(
            "DELETE FROM devbrain.raw_sessions WHERE source_hash LIKE %s",
            (f"{EXPORT_IMPORT_TEST_PREFIX}%",),
        )
        cur.execute(
            "DELETE FROM devbrain.devs WHERE dev_id LIKE %s",
            (f"{EXPORT_IMPORT_TEST_PREFIX}%",),
        )
        cur.execute(
            "DELETE FROM devbrain.projects WHERE slug LIKE %s",
            (f"{EXPORT_IMPORT_TEST_PREFIX}%",),
        )
        conn.commit()


# ─── seed helpers ───────────────────────────────────────────────────────────


def _seed_project(db, *, slug: str, name: str | None = None) -> str:
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devbrain.projects (slug, name)
            VALUES (%s, %s)
            ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
            """,
            (slug, name or slug),
        )
        pid = str(cur.fetchone()[0])
        conn.commit()
    return pid


def _embedding_text(seed: float) -> str:
    return "[" + ",".join(f"{seed + i * 1e-6:.9f}" for i in range(1024)) + "]"


def _seed_memory(
    db,
    *,
    project_id: str,
    kind: str,
    content: str,
    title: str | None = None,
    embedding_seed: float | None = None,
    provenance_id: str | None = None,
) -> str:
    """INSERT directly into devbrain.memory bypassing any adapter."""
    embedding = _embedding_text(embedding_seed) if embedding_seed is not None else None
    if provenance_id is None:
        provenance_id = str(uuid.uuid4())
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devbrain.memory
                (project_id, kind, title, content, embedding, provenance_id)
            VALUES (%s, %s, %s, %s, %s::vector, %s)
            RETURNING id
            """,
            (project_id, kind, title, content, embedding, provenance_id),
        )
        mid = str(cur.fetchone()[0])
        conn.commit()
    return mid


def _seed_raw_session(
    db,
    *,
    project_id: str | None,
    source_hash: str | None = None,
    summary: str | None = None,
) -> str:
    src_hash = source_hash or f"{EXPORT_IMPORT_TEST_PREFIX}{uuid.uuid4().hex[:32]}"
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devbrain.raw_sessions
                (project_id, source_app, source_path, source_hash,
                 raw_content, summary)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                project_id, "test", "/tmp/x", src_hash,
                "raw transcript", summary or f"{EXPORT_IMPORT_TEST_PREFIX}sum",
            ),
        )
        sid = str(cur.fetchone()[0])
        conn.commit()
    return sid


def _seed_dev(db, *, dev_id: str, channels: list[dict] | None = None) -> str:
    db.register_dev(
        dev_id=dev_id,
        full_name=f"{EXPORT_IMPORT_TEST_PREFIX}name",
        channels=channels or [],
    )
    return dev_id


def _read_memory_for_provenance(db, provenance_id: str) -> list[tuple]:
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT kind, title, content, embedding::text "
            "FROM devbrain.memory WHERE provenance_id = %s",
            (provenance_id,),
        )
        return cur.fetchall()


# ─── 1. round-trip via disk ─────────────────────────────────────────────────


def test_round_trip_via_disk_preserves_rows(db, tmp_path):
    """Disk round-trip is the canonical happy-path: seed source rows,
    write to disk, re-import, then verify the destination ended up with
    matching rows. Same DB throughout — we use the prefix to identify
    "exported" rows, delete them locally, then re-import to prove the
    importer can restore them from the file.
    """
    pid = _seed_project(
        db, slug=f"{EXPORT_IMPORT_TEST_PREFIX}roundtrip", name="Roundtrip"
    )
    prov_a = str(uuid.uuid4())
    prov_b = str(uuid.uuid4())
    _seed_memory(
        db, project_id=pid, kind="decision",
        title=f"{EXPORT_IMPORT_TEST_PREFIX}T1",
        content=f"{EXPORT_IMPORT_TEST_PREFIX}body 1",
        provenance_id=prov_a,
    )
    _seed_memory(
        db, project_id=pid, kind="chunk",
        content=f"{EXPORT_IMPORT_TEST_PREFIX}chunk body",
        embedding_seed=0.123,
        provenance_id=prov_b,
    )

    out = tmp_path / "rt.json"
    counts = export_memory.write_export_file(db, out)
    assert counts["memory"] >= 2
    assert counts["projects"] >= 1
    assert out.exists()

    # Wipe just our seeded memory rows so the import has work to do.
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM devbrain.memory WHERE provenance_id IN (%s, %s)",
            (prov_a, prov_b),
        )
        conn.commit()

    payload = import_memory.read_import_file(out)
    results = import_memory.import_from_dict(db, payload)
    assert results["memory"]["inserted"] >= 2

    rows_a = _read_memory_for_provenance(db, prov_a)
    rows_b = _read_memory_for_provenance(db, prov_b)
    assert len(rows_a) == 1
    assert len(rows_b) == 1
    assert rows_a[0][0] == "decision"
    assert rows_a[0][2] == f"{EXPORT_IMPORT_TEST_PREFIX}body 1"
    assert rows_b[0][0] == "chunk"


# ─── 2. --project slug filter ───────────────────────────────────────────────


def test_slug_filter_excludes_other_projects(db, tmp_path):
    pid_a = _seed_project(db, slug=f"{EXPORT_IMPORT_TEST_PREFIX}slugA")
    pid_b = _seed_project(db, slug=f"{EXPORT_IMPORT_TEST_PREFIX}slugB")
    prov_a = str(uuid.uuid4())
    prov_b = str(uuid.uuid4())
    _seed_memory(
        db, project_id=pid_a, kind="decision",
        title=f"{EXPORT_IMPORT_TEST_PREFIX}A",
        content=f"{EXPORT_IMPORT_TEST_PREFIX}A body",
        provenance_id=prov_a,
    )
    _seed_memory(
        db, project_id=pid_b, kind="decision",
        title=f"{EXPORT_IMPORT_TEST_PREFIX}B",
        content=f"{EXPORT_IMPORT_TEST_PREFIX}B body",
        provenance_id=prov_b,
    )

    payload = export_memory.export_to_dict(
        db, project_slugs=[f"{EXPORT_IMPORT_TEST_PREFIX}slugA"],
    )
    slugs = {p["slug"] for p in payload["projects"]}
    assert f"{EXPORT_IMPORT_TEST_PREFIX}slugA" in slugs
    assert f"{EXPORT_IMPORT_TEST_PREFIX}slugB" not in slugs

    provenances = {m["provenance_id"] for m in payload["memory"]}
    assert prov_a in provenances
    assert prov_b not in provenances


# ─── 3. idempotent re-import ────────────────────────────────────────────────


def test_idempotent_reimport_inserts_zero_second_time(db, tmp_path):
    """Re-running the importer against the same export file must not
    duplicate our seeded rows, no matter how often we re-run.

    The DB may have other rows (e.g. an ingest daemon running in the
    background) whose lifecycle we don't control, so we assert only
    on the rows we seeded. The seeds use the prefix; counting prefix-
    matching rows before vs. after a re-import is the deterministic
    way to prove idempotency on this shared DB.
    """
    pid = _seed_project(db, slug=f"{EXPORT_IMPORT_TEST_PREFIX}idem")
    prov = str(uuid.uuid4())
    _seed_memory(
        db, project_id=pid, kind="issue",
        title=f"{EXPORT_IMPORT_TEST_PREFIX}I",
        content=f"{EXPORT_IMPORT_TEST_PREFIX}I body",
        provenance_id=prov,
    )
    sess = _seed_raw_session(db, project_id=pid)

    def _our_counts():
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM devbrain.memory WHERE content LIKE %s",
                (f"{EXPORT_IMPORT_TEST_PREFIX}%",),
            )
            mem = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM devbrain.raw_sessions "
                "WHERE source_hash LIKE %s",
                (f"{EXPORT_IMPORT_TEST_PREFIX}%",),
            )
            rs = cur.fetchone()[0]
        return mem, rs

    out = tmp_path / "idem.json"
    export_memory.write_export_file(db, out)
    payload = import_memory.read_import_file(out)

    before = _our_counts()
    import_memory.import_from_dict(db, payload)
    after_first = _our_counts()
    import_memory.import_from_dict(db, payload)
    after_second = _our_counts()

    assert after_first == before, (
        f"first re-import duplicated rows: {before} → {after_first}"
    )
    assert after_second == before, (
        f"second re-import duplicated rows: {before} → {after_second}"
    )
    # Sanity: the seeds are still there (raw_sessions row keyed by sess).
    assert sess
    assert before[0] >= 1 and before[1] >= 1


# ─── 4. devs: existing local channels are preserved ─────────────────────────


def test_dev_channels_preserved_on_reimport(db, tmp_path):
    """PR #38 posture: re-running install / import must not overwrite
    user-customized channels. The export carries empty channels for a
    dev; the local copy has channels added; after import the local
    channels must still be there.
    """
    pid = _seed_project(db, slug=f"{EXPORT_IMPORT_TEST_PREFIX}devs")
    dev_id = f"{EXPORT_IMPORT_TEST_PREFIX}user1"
    _seed_dev(db, dev_id=dev_id, channels=[])  # source: no channels

    out = tmp_path / "devs.json"
    export_memory.write_export_file(db, out)

    # Now the operator on the destination customizes channels locally.
    db.register_dev(
        dev_id=dev_id,
        full_name=f"{EXPORT_IMPORT_TEST_PREFIX}name",
        channels=[{"type": "telegram_bot", "address": "@me"}],
    )

    payload = import_memory.read_import_file(out)
    results = import_memory.import_from_dict(db, payload)
    assert results["devs"]["preserved"] >= 1

    after = db.get_dev(dev_id)
    assert after is not None
    assert after["channels"] == [{"type": "telegram_bot", "address": "@me"}]
    assert pid  # unused but proves project setup ran


# ─── 5. missing project slug auto-creates a stub ───────────────────────────


def test_import_creates_missing_project(db, tmp_path):
    """Memory rows whose slug isn't already in destination.projects
    must still import — the importer creates a minimal project stub
    so the FK holds.
    """
    pid = _seed_project(db, slug=f"{EXPORT_IMPORT_TEST_PREFIX}orig")
    prov = str(uuid.uuid4())
    _seed_memory(
        db, project_id=pid, kind="pattern",
        title=f"{EXPORT_IMPORT_TEST_PREFIX}P",
        content=f"{EXPORT_IMPORT_TEST_PREFIX}P body",
        provenance_id=prov,
    )

    payload = export_memory.export_to_dict(
        db, project_slugs=[f"{EXPORT_IMPORT_TEST_PREFIX}orig"],
    )
    # Wipe local copy so the importer has to create the project AND
    # the memory row from scratch.
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM devbrain.memory WHERE provenance_id = %s", (prov,),
        )
        cur.execute(
            "DELETE FROM devbrain.projects WHERE slug = %s",
            (f"{EXPORT_IMPORT_TEST_PREFIX}orig",),
        )
        conn.commit()

    results = import_memory.import_from_dict(db, payload)
    assert results["memory"]["inserted"] == 1

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT slug FROM devbrain.projects WHERE slug = %s",
            (f"{EXPORT_IMPORT_TEST_PREFIX}orig",),
        )
        assert cur.fetchone() is not None


# ─── 6. embedding bit-equality across round-trip ────────────────────────────


def test_embedding_bit_equality_round_trip(db, tmp_path):
    """The legacy embedding has cost a real Ollama call. The export →
    import path must round-trip the vector bit-equally; otherwise
    cosine distances drift on the destination."""
    pid = _seed_project(db, slug=f"{EXPORT_IMPORT_TEST_PREFIX}emb")
    prov = str(uuid.uuid4())
    _seed_memory(
        db, project_id=pid, kind="chunk",
        content=f"{EXPORT_IMPORT_TEST_PREFIX}embedding row",
        embedding_seed=0.4242,
        provenance_id=prov,
    )

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT embedding::text FROM devbrain.memory WHERE provenance_id = %s",
            (prov,),
        )
        before = cur.fetchone()[0]

    out = tmp_path / "emb.json"
    export_memory.write_export_file(db, out)

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM devbrain.memory WHERE provenance_id = %s", (prov,),
        )
        conn.commit()

    payload = import_memory.read_import_file(out)
    import_memory.import_from_dict(db, payload)

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT embedding::text FROM devbrain.memory WHERE provenance_id = %s",
            (prov,),
        )
        after = cur.fetchone()[0]

    assert before == after, (
        "embedding text literal must be bit-equal across export → import"
    )


# ─── 7. gzip output is smaller than plain ───────────────────────────────────


def test_gzip_output_is_smaller(db, tmp_path):
    pid = _seed_project(db, slug=f"{EXPORT_IMPORT_TEST_PREFIX}gz")
    # Seed enough repetitive content that gzip has something to chew on.
    for i in range(20):
        _seed_memory(
            db, project_id=pid, kind="chunk",
            content=f"{EXPORT_IMPORT_TEST_PREFIX}{'x' * 200}{i}",
            embedding_seed=0.0 + i * 0.001,
            provenance_id=str(uuid.uuid4()),
        )

    plain = tmp_path / "g.json"
    gz = tmp_path / "g.json.gz"
    export_memory.write_export_file(
        db, plain, project_slugs=[f"{EXPORT_IMPORT_TEST_PREFIX}gz"],
    )
    export_memory.write_export_file(
        db, gz, project_slugs=[f"{EXPORT_IMPORT_TEST_PREFIX}gz"],
    )

    plain_size = plain.stat().st_size
    gz_size = gz.stat().st_size
    assert gz_size < plain_size, (
        f"gzipped export should be smaller; got {gz_size} >= {plain_size}"
    )

    # The gzipped file must be readable as gzip and parse as JSON.
    with gzip.open(gz, "rt", encoding="utf-8") as fh:
        parsed = json.load(fh)
    assert parsed["version"] == export_memory.EXPORT_VERSION


# ─── 8. dry-run rolls back ──────────────────────────────────────────────────


def test_dry_run_does_not_commit(db, tmp_path):
    pid = _seed_project(db, slug=f"{EXPORT_IMPORT_TEST_PREFIX}dry")
    prov = str(uuid.uuid4())
    _seed_memory(
        db, project_id=pid, kind="decision",
        title=f"{EXPORT_IMPORT_TEST_PREFIX}D",
        content=f"{EXPORT_IMPORT_TEST_PREFIX}D body",
        provenance_id=prov,
    )

    out = tmp_path / "dry.json"
    export_memory.write_export_file(db, out)

    # Wipe the source row so dry-run import has actual work to *not* do.
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM devbrain.memory WHERE provenance_id = %s", (prov,),
        )
        conn.commit()

    payload = import_memory.read_import_file(out)
    results = import_memory.import_from_dict(db, payload, dry_run=True)
    # Dry-run reports the inserts it *would* have done.
    assert results["memory"]["inserted"] >= 1
    assert results["dry_run"] is True

    # But nothing is actually committed.
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM devbrain.memory WHERE provenance_id = %s",
            (prov,),
        )
        assert cur.fetchone()[0] == 0


# ─── 9. schema-mismatch import is rejected with an actionable error ────────


def test_schema_mismatch_rejected(db, tmp_path):
    """Surface schema drift loudly at the entry point. If the export's
    schema_migration_top differs from the destination's, the importer
    must refuse before touching any rows."""
    pid = _seed_project(db, slug=f"{EXPORT_IMPORT_TEST_PREFIX}schema")
    prov = str(uuid.uuid4())
    _seed_memory(
        db, project_id=pid, kind="decision",
        title=f"{EXPORT_IMPORT_TEST_PREFIX}S",
        content=f"{EXPORT_IMPORT_TEST_PREFIX}S body",
        provenance_id=prov,
    )

    payload = export_memory.export_to_dict(db)
    payload["source"]["schema_migration_top"] = "999_bogus.sql"

    with pytest.raises(ValueError) as excinfo:
        import_memory.import_from_dict(db, payload)
    msg = str(excinfo.value)
    assert "schema" in msg.lower()
    assert "999_bogus.sql" in msg

    # And the rejection happened before any insert: the source row is
    # still the only one in memory for that provenance.
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM devbrain.memory WHERE provenance_id = %s",
            (prov,),
        )
        assert cur.fetchone()[0] == 1
