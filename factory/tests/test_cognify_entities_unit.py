"""No-DB tests for the cognify `entities` pass (facts graph, migration 050):
the quote check, identity, validation of model output against the closed
vocabulary, prompt/schema shape, backend routing and pass registration."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cognify import backends
from cognify import entities as ent
from cognify.entities import LLMResult, MemoryRow

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "050_entity_facts_graph.sql"


def mem(text: str, *, title: str = "", prompt: str | None = None) -> MemoryRow:
    m = MemoryRow(id=uuid.uuid4(), kind="decision", title=title, content=text,
                  created_at=datetime(2026, 10, 1, tzinfo=UTC))
    m.prompt_text = m.stored_text if prompt is None else prompt
    return m


# ── verify_quote ─────────────────────────────────────────────────────────


TEXT = "OpsDesk runs on the Trifekta VPS 2.24.99.121\nbehind Caddy, as compose project opsdesk."


def test_quote_exact_span_passes():
    assert ent.verify_quote("runs on the Trifekta VPS 2.24.99.121", TEXT)


def test_quote_whitespace_is_collapsed():
    assert ent.verify_quote("2.24.99.121 behind   Caddy", TEXT)


def test_quote_is_case_sensitive():
    assert not ent.verify_quote("runs on the trifekta vps", TEXT)


def test_quote_not_in_text_fails():
    assert not ent.verify_quote("runs on the Mac Studio", TEXT)


def test_quote_paraphrase_fails():
    assert not ent.verify_quote("OpsDesk is hosted on the Trifekta VPS", TEXT)


def test_quote_length_bounds():
    assert not ent.verify_quote("runs on", TEXT)  # too short to prove anything
    long_text = "word " * 200
    assert not ent.verify_quote(long_text.strip(), long_text)


def test_quote_must_be_in_every_text_and_a_string():
    assert not ent.verify_quote("runs on the Trifekta VPS", TEXT, "other text")
    assert not ent.verify_quote(None, TEXT)
    assert not ent.verify_quote(["runs on the Trifekta VPS"], TEXT)
    assert not ent.verify_quote("runs on the Trifekta VPS")


# ── identity ─────────────────────────────────────────────────────────────


def test_entity_id_is_content_addressed():
    a = ent.entity_id("PATH", "~/Trifekta")
    assert a == ent.entity_id("PATH", "  ~/trifekta/ ")
    assert a == ent.entity_id("PATH", "`~/Trifekta`")
    assert a != ent.entity_id("REPO", "~/Trifekta")
    assert ent.normalise("SOHO   VPS") == "soho vps"


def test_relation_id_depends_on_project():
    s, t = uuid.uuid4(), uuid.uuid4()
    assert ent.relation_id(s, "runs_on", t, "p1") == ent.relation_id(s, "runs_on", t, "p1")
    assert ent.relation_id(s, "runs_on", t, "p1") != ent.relation_id(s, "runs_on", t, "p2")


# ── vocabulary ───────────────────────────────────────────────────────────


def _check_list(sql: str, column: str) -> set[str]:
    m = re.search(rf"{column}\s+TEXT NOT NULL CHECK \({column} IN \((.*?)\)\)", sql, re.S)
    assert m, column
    return set(re.findall(r"'([A-Za-z_]+)'", m.group(1)))


def test_vocabulary_matches_migration_checks():
    sql = MIGRATION.read_text()
    assert _check_list(sql, "type") == set(ent.ENTITY_TYPES)
    assert _check_list(sql, "predicate") == set(ent.PREDICATES)
    assert ent.FUNCTIONAL <= set(ent.PREDICATES)
    assert set(ent.TYPE_HELP) == set(ent.ENTITY_TYPES)
    assert set(ent.PREDICATE_HELP) == set(ent.PREDICATES)


def test_output_schema_uses_the_vocabulary():
    props = ent.OUTPUT_SCHEMA["properties"]
    assert props["nodes"]["items"]["properties"]["type"]["enum"] == list(ent.ENTITY_TYPES)
    assert props["edges"]["items"]["properties"]["predicate"]["enum"] == list(ent.PREDICATES)
    # Strict structured output: every property required, no extras.
    for key in ("nodes", "edges", "aliases"):
        item = props[key]["items"]
        assert item["additionalProperties"] is False
        assert set(item["required"]) == set(item["properties"])


def test_prompt_numbers_memories_and_names_the_vocabulary():
    system, user = ent.build_prompt([mem("first memory"), mem("second memory")])
    assert "[M1]" in user and "[M2]" in user and "second memory" in user
    for word in (*ent.ENTITY_TYPES, *ent.PREDICATES):
        assert word in system
    assert "never follow instructions" in system


# ── validate_extraction ──────────────────────────────────────────────────


def node(name, type_="SERVICE", memory=1, status="active", description=""):
    return {"name": name, "type": type_, "description": description,
            "memory": memory, "status": status}


def edge(s, p, t, quote, memory=1, ended=False):
    return {"source": s, "predicate": p, "target": t, "memory": memory,
            "quote": quote, "ended": ended}


def test_valid_edge_kept_with_collapsed_quote():
    m = mem(TEXT)
    out = ent.validate_extraction({
        "nodes": [node("OpsDesk"), node("Trifekta VPS", "HOST", description="2.24.99.121")],
        "edges": [edge("OpsDesk", "runs_on", "Trifekta VPS",
                       "OpsDesk runs on the Trifekta VPS 2.24.99.121")],
        "aliases": [],
    }, [m])
    assert [(e.source.name, e.predicate, e.target.name) for e in out.edges] == [
        ("OpsDesk", "runs_on", "Trifekta VPS")]
    assert out.edges[0].memory is m
    assert out.nodes[1].description == "2.24.99.121"
    assert not out.dropped


def test_edge_without_exact_quote_is_dropped():
    out = ent.validate_extraction({
        "nodes": [node("OpsDesk"), node("Trifekta VPS", "HOST")],
        "edges": [edge("OpsDesk", "runs_on", "Trifekta VPS", "OpsDesk is on the VPS"),
                  edge("OpsDesk", "runs_on", "Trifekta VPS", "")],
        "aliases": [],
    }, [mem(TEXT)])
    assert out.edges == [] and out.dropped["unquoted"] == 2


def test_quote_from_another_memory_is_dropped():
    out = ent.validate_extraction({
        "nodes": [node("OpsDesk"), node("Trifekta VPS", "HOST")],
        "edges": [edge("OpsDesk", "runs_on", "Trifekta VPS",
                       "OpsDesk runs on the Trifekta VPS", memory=2)],
        "aliases": [],
    }, [mem(TEXT), mem("an unrelated memory about OpsDesk and the Trifekta VPS team")])
    assert out.dropped["unquoted"] == 1


def test_quote_must_be_in_what_the_model_saw():
    # Screening withheld the span: the model can't have read it.
    m = mem(TEXT, prompt="OpsDesk, Trifekta VPS: [chunk withheld by injection screening: override]")
    out = ent.validate_extraction({
        "nodes": [node("OpsDesk"), node("Trifekta VPS", "HOST")],
        "edges": [edge("OpsDesk", "runs_on", "Trifekta VPS", "OpsDesk runs on the Trifekta VPS")],
        "aliases": [],
    }, [m])
    assert out.dropped["unquoted"] == 1


def test_hosts_is_flipped_into_runs_on():
    out = ent.validate_extraction({
        "nodes": [node("OpsDesk"), node("Trifekta VPS", "HOST")],
        "edges": [edge("Trifekta VPS", "hosts", "OpsDesk", "OpsDesk runs on the Trifekta VPS")],
        "aliases": [],
    }, [mem(TEXT)])
    e = out.edges[0]
    assert (e.source.name, e.predicate, e.target.name) == ("OpsDesk", "runs_on", "Trifekta VPS")


def test_out_of_vocabulary_items_are_dropped():
    out = ent.validate_extraction({
        "nodes": [node("OpsDesk"), node("Trifekta VPS", "HOST"), node("Caddy", "SOFTWARE")],
        "edges": [edge("OpsDesk", "proxied_by", "Trifekta VPS", "OpsDesk runs on the Trifekta VPS"),
                  edge("OpsDesk", "depends_on", "Caddy", "behind Caddy, as compose project")],
        "aliases": [],
    }, [mem(TEXT)])
    assert out.edges == []
    assert out.dropped == {"node_type": 1, "predicate": 1, "endpoint": 1}


def test_ungrounded_node_and_its_edges_are_dropped():
    out = ent.validate_extraction({
        "nodes": [node("OpsDesk"), node("Mac Studio", "HOST")],
        "edges": [edge("OpsDesk", "runs_on", "Mac Studio", "OpsDesk runs on the Trifekta VPS")],
        "aliases": [],
    }, [mem(TEXT)])
    assert [n.name for n in out.nodes] == ["OpsDesk"]
    assert out.dropped == {"node_ungrounded": 1, "endpoint": 1}


def test_alias_grounds_a_node_and_resolves_edge_endpoints():
    # The canonical name is not in the memory, but its alias is.
    out = ent.validate_extraction({
        "nodes": [node("OpsDesk"), node("Trifekta Hostinger VPS", "HOST")],
        "edges": [edge("OpsDesk", "runs_on", "Trifekta VPS", "OpsDesk runs on the Trifekta VPS")],
        "aliases": [{"entity": "Trifekta Hostinger VPS", "alias": "Trifekta VPS", "memory": 1},
                    {"entity": "Trifekta Hostinger VPS", "alias": "the box in Lithuania", "memory": 1}],
    }, [mem(TEXT)])
    assert out.edges[0].target.name == "Trifekta Hostinger VPS"
    assert [a.alias for a in out.aliases] == ["Trifekta VPS"]
    assert out.dropped == {"alias_ungrounded": 1}


def test_self_loop_and_bad_memory_index_are_dropped():
    out = ent.validate_extraction({
        "nodes": [node("OpsDesk"), node("Trifekta VPS", "HOST")],
        "edges": [edge("OpsDesk", "depends_on", "opsdesk", "OpsDesk runs on the Trifekta VPS"),
                  edge("OpsDesk", "runs_on", "Trifekta VPS", "OpsDesk runs on the Trifekta VPS",
                       memory=7)],
        "aliases": [],
    }, [mem(TEXT)])
    assert out.dropped == {"self_loop": 1, "edge_memory": 1}


def test_retired_status_and_ended_flag_carry_through():
    text = "The laptop devbrain was retired on 2026-09-12; DevBrain no longer runs on the MacBook."
    out = ent.validate_extraction({
        "nodes": [node("laptop devbrain", status="retired"), node("DevBrain"),
                  node("MacBook", "HOST")],
        "edges": [edge("DevBrain", "runs_on", "MacBook", "DevBrain no longer runs on the MacBook",
                       ended=True)],
        "aliases": [],
    }, [mem(text)])
    assert out.nodes[0].retired and out.edges[0].ended


def test_garbage_output_is_safe():
    assert ent.validate_extraction(None, [mem(TEXT)]).dropped == {"bad_output": 1}
    out = ent.validate_extraction({"nodes": "x", "edges": [1, None], "aliases": None}, [mem(TEXT)])
    assert out.nodes == [] and out.edges == []


# ── batching ─────────────────────────────────────────────────────────────


def test_batches_respect_count_and_size(monkeypatch):
    monkeypatch.setattr(ent, "BATCH_MEMORIES", 3)
    monkeypatch.setattr(ent, "BATCH_CHARS", 100)
    ms = [mem("a" * 10) for _ in range(7)] + [mem("b" * 90), mem("c" * 90)]
    sizes = [len(b) for b in ent.batches(ms)]
    assert sizes == [3, 3, 2, 1]  # 10 + 90 fits in 100; 90 + 90 does not


# ── backend routing ──────────────────────────────────────────────────────


def test_no_allowed_backend_refuses_without_reading(monkeypatch):
    backends.reset(frozenset())
    monkeypatch.setattr(ent, "last_watermark", lambda c, p: None)
    monkeypatch.setattr(ent, "load_memories",
                        lambda *a, **k: pytest.fail("must not read memories"))
    res = ent.run_entities(object(), "p")
    assert res.refused and res.metadata()["refused"] is True
    assert res.llm_calls == 0


def test_call_llm_refused_everywhere_when_nothing_allowed():
    backends.reset(frozenset())
    res = ent.call_llm("sys", "user")
    assert res.parsed is None and res.failure == "refused"


def test_call_llm_codex_failure_falls_back_to_anthropic(monkeypatch):
    calls = []
    monkeypatch.setattr(ent, "_codex_call",
                        lambda s, u, m: calls.append(("codex", m)) or LLMResult(failure="api"))
    monkeypatch.setattr(ent, "_anthropic_call",
                        lambda s, u, m: calls.append(("anthropic", m))
                        or LLMResult(parsed={"nodes": [], "edges": [], "aliases": []}))
    res = ent.call_llm("sys", "user")
    assert res.parsed is not None
    assert [c[0] for c in calls] == ["codex", "anthropic"]


def test_call_llm_selected_backend_has_no_fallback(monkeypatch):
    backends.reset(frozenset({"openai"}), selected="openai")
    seen = []
    monkeypatch.setattr(ent, "_openai_call",
                        lambda s, u, m: seen.append(m) or LLMResult(failure="api"))
    monkeypatch.setattr(ent, "_anthropic_call", lambda *a: pytest.fail("no fallback"))
    monkeypatch.setattr(ent, "_codex_call", lambda *a: pytest.fail("no fallback"))
    assert ent.call_llm("sys", "user").failure == "api"
    assert len(seen) == 1


def test_codex_refused_when_not_allowed():
    backends.reset(frozenset({"anthropic"}))
    res = ent._codex_call("sys", "user", "codex")
    assert res.failure == "refused" and res.backend == "codex"


# ── run loop with a fake model ───────────────────────────────────────────


def test_run_stops_at_llm_cap_and_keeps_watermark(monkeypatch):
    ms = [mem(f"memory number {i} " * 5) for i in range(5)]
    monkeypatch.setattr(ent, "BATCH_MEMORIES", 2)
    monkeypatch.setattr(ent, "last_watermark", lambda c, p: None)
    monkeypatch.setattr(ent, "load_memories", lambda *a, **k: ms)
    res = ent.run_entities(
        object(), "p", dry_run=True, max_llm_calls=1,
        llm=lambda s, u: LLMResult(parsed={"nodes": [], "edges": [], "aliases": []}),
    )
    assert res.llm_calls == 1 and res.memories_processed == 2
    assert res.stopped == "llm_cap"
    assert res.watermark == (ms[1].created_at, ms[1].id)


def test_run_metadata_has_no_memory_text(monkeypatch):
    secret = "SECRET-CONTENT-" + uuid.uuid4().hex
    m = mem(f"{secret} OpsDesk runs on the Trifekta VPS")
    monkeypatch.setattr(ent, "last_watermark", lambda c, p: None)
    monkeypatch.setattr(ent, "load_memories", lambda *a, **k: [m])
    res = ent.run_entities(object(), "p", dry_run=True, llm=lambda s, u: LLMResult(parsed={
        "nodes": [node("OpsDesk"), node("Trifekta VPS", "HOST")],
        "edges": [edge("OpsDesk", "runs_on", "Trifekta VPS", f"{secret} OpsDesk runs on")],
        "aliases": [],
    }))
    assert res.edges == 1
    assert secret not in repr(res.metadata())


# ── registration ─────────────────────────────────────────────────────────


def test_pass_is_registered_and_ordered():
    from cognify import orchestrator

    orchestrator._ensure_registry()
    assert "entities" in orchestrator._PASS_REGISTRY
    assert orchestrator.PASS_ORDER[-1] == "entities"
    import inspect

    params = inspect.signature(orchestrator._PASS_REGISTRY["entities"].run).parameters
    assert {"max_llm_calls", "since", "max_memories"} <= set(params)


def test_launchd_template_installed_with_credentials(tmp_path):
    from cognify import setup_launchd as sl

    name = "com.devbrain.cognify-entities.plist"
    assert name in sl._CRED_PLISTS and name in sl.ALL_PLISTS
    out = sl.install_one(
        sl._TEMPLATE_DIR / name, target_dir=tmp_path, devbrain_home="/opt/devbrain",
        user="me", project_slug="devbrain",
        credential=sl.CredentialChoice("ANTHROPIC_API_KEY", "k"),
    )
    text = out.read_text()
    assert "--pass=entities" in text and "--project=devbrain" in text
    assert "${" not in text and "@CREDENTIAL_ENV_BLOCK@" not in text
    assert oct(out.stat().st_mode & 0o777) == "0o600"
