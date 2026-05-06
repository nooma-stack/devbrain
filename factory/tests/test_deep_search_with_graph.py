"""Integration tests for the deep_search graph-aware extension (Phase 5d).

These tests exercise the Python-side graph enrichment logic directly —
not the TypeScript MCP layer. They verify that the deep_search_graph_entry
CLI correctly aggregates walker results from multiple seeds and deduplicates
memories across seeds.

The MCP integration (TypeScript side) is covered by the TypeScript build
succeeding and the existing smoke-test checklist.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


FACTORY_DIR = str(Path(__file__).parent.parent)


def run_entry(payload: dict, env_extra: dict | None = None) -> dict:
    """Run deep_search_graph_entry.py with the given payload and return parsed output."""
    import os
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)

    python = str(Path(sys.executable))
    result = subprocess.run(
        [python, "-m", "graph.deep_search_graph_entry"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=FACTORY_DIR,
        env=env,
    )
    if result.returncode != 0:
        # Try to get the error from stdout
        try:
            return json.loads(result.stdout)
        except Exception:
            pytest.fail(f"Entry script failed (exit {result.returncode}): {result.stderr}")
    return json.loads(result.stdout)


@pytest.mark.db
def test_with_graph_empty_seeds(conn, project_factory):
    """Empty seed list returns empty graph without error."""
    payload = {
        "seed_memory_ids": [],
        "graph_max_hops": 3,
        "graph_max_nodes": 50,
    }
    result = run_entry(payload)
    assert result["memories"] == []
    assert result["edges"] == []
    assert result["seeds"] == []


@pytest.mark.db
def test_with_graph_single_seed_no_edges(conn, project_factory, memory_factory):
    """Single seed with no edges returns just the seed."""
    project = project_factory("ds_graph_single")
    seed_mem = memory_factory(project["id"], kind="decision", title="ds_seed")
    conn.commit()

    payload = {
        "seed_memory_ids": [str(seed_mem["id"])],
        "graph_max_hops": 3,
        "graph_max_nodes": 50,
    }
    result = run_entry(payload)

    returned_ids = {m["id"] for m in result["memories"]}
    assert str(seed_mem["id"]) in returned_ids
    assert len(result["seeds"]) == 1


@pytest.mark.db
def test_with_graph_expands_neighbors(conn, project_factory, memory_factory):
    """Graph walk from seed includes connected neighbors."""
    project = project_factory("ds_graph_expand")
    seed_mem = memory_factory(project["id"], kind="decision", title="ds_seed_exp")
    neighbor = memory_factory(project["id"], kind="pattern", title="ds_neighbor")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'test_ds_graph')",
            (seed_mem["id"], neighbor["id"]),
        )
    conn.commit()

    payload = {
        "seed_memory_ids": [str(seed_mem["id"])],
        "graph_max_hops": 3,
        "graph_max_nodes": 50,
    }
    result = run_entry(payload)

    returned_ids = {m["id"] for m in result["memories"]}
    assert str(seed_mem["id"]) in returned_ids
    assert str(neighbor["id"]) in returned_ids


@pytest.mark.db
def test_with_graph_deduplicates_across_seeds(conn, project_factory, memory_factory):
    """When two seeds share a common neighbor, it appears only once in result."""
    project = project_factory("ds_graph_dedup")
    seed1 = memory_factory(project["id"], kind="decision", title="ds_seed1")
    seed2 = memory_factory(project["id"], kind="decision", title="ds_seed2")
    shared = memory_factory(project["id"], kind="pattern", title="ds_shared")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'test_ds_graph')",
            (seed1["id"], shared["id"]),
        )
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'test_ds_graph')",
            (seed2["id"], shared["id"]),
        )
    conn.commit()

    payload = {
        "seed_memory_ids": [str(seed1["id"]), str(seed2["id"])],
        "graph_max_hops": 3,
        "graph_max_nodes": 50,
    }
    result = run_entry(payload)

    returned_ids = [m["id"] for m in result["memories"]]
    # No duplicates
    assert len(returned_ids) == len(set(returned_ids)), "No duplicates in memories"

    # Shared node appears exactly once
    shared_count = returned_ids.count(str(shared["id"]))
    assert shared_count == 1, f"Shared node must appear once, got {shared_count}"


@pytest.mark.db
def test_with_graph_false_returns_no_graph_field(conn, project_factory, memory_factory):
    """The entry point handles the with_graph=false case (empty seeds) gracefully."""
    # When deep_search is called without with_graph, no seeds are passed.
    # Verify the entry point returns empty graph for empty seeds.
    payload = {
        "seed_memory_ids": [],
        "graph_max_hops": 3,
        "graph_max_nodes": 50,
    }
    result = run_entry(payload)
    assert "memories" in result
    assert "edges" in result
    assert "seeds" in result
    assert result["memories"] == []
