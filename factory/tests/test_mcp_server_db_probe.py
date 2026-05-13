"""Integration test for the MCP server's startup DB probe (D2 fix).

When the DB is unreachable, the MCP server should exit fast (within
~6s) with a clear stderr message, rather than coming up and letting
the first tool call hang or return opaquely.

This test:
  1. Spawns `node mcp-server/dist/index.js` pointing at a port that's
     never listening (port 1).
  2. Asserts the process exits non-zero within the timeout window.
  3. Asserts stderr contains the actionable DB-down message.
  4. Verifies the DEVBRAIN_MCP_SKIP_DB_PROBE=1 escape hatch lets it
     start (smoke test of the bypass).

The MCP server reads database config from `config/devbrain.yaml`. We
patch the host/port via a tmp config + DEVBRAIN_CONFIG env var to keep
the test hermetic.

Marked `skipif` when the MCP server hasn't been built or when node
isn't available — both required for this test.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MCP_BUNDLE = REPO_ROOT / "mcp-server" / "dist" / "index.js"
CONFIG_TEMPLATE_PATH = REPO_ROOT / "config" / "devbrain.yaml"

_node_path = shutil.which("node")

pytestmark = pytest.mark.skipif(
    not MCP_BUNDLE.exists() or _node_path is None,
    reason="MCP server dist/index.js not built or node not on PATH",
)


def _write_temp_config_with_dead_db(tmp_path: Path) -> Path:
    """Copy config/devbrain.yaml and rewrite the database host+port to
    something that's guaranteed to refuse connections."""
    src = CONFIG_TEMPLATE_PATH.read_text()
    # Point at port 1 on localhost — kernel will RST every SYN.
    # Use a simple regex-ish replacement; keep the rest of the config
    # intact (chunking + embedding + summarization stay valid).
    out = []
    for line in src.splitlines():
        stripped = line.lstrip()
        # YAML format: "  host: <value>" inside the database: block.
        # Naive matching is fine here because devbrain.yaml has only one
        # "host:" key under database.
        if stripped.startswith("host:") and "127.0.0.1" not in line and "localhost" not in line:
            out.append(line)
            continue
        if stripped.startswith("host:"):
            indent = line[: len(line) - len(stripped)]
            out.append(f"{indent}host: 127.0.0.1")
        elif stripped.startswith("port:") and " 543" in line:
            indent = line[: len(line) - len(stripped)]
            out.append(f"{indent}port: 1")
        else:
            out.append(line)
    out_path = tmp_path / "devbrain.yaml"
    out_path.write_text("\n".join(out) + "\n")
    return out_path


def test_mcp_server_exits_fast_when_db_unreachable(tmp_path):
    """With Postgres unreachable, the MCP server should exit non-zero
    within ~7 seconds and surface a useful error message on stderr."""
    config_path = _write_temp_config_with_dead_db(tmp_path)
    env = os.environ.copy()
    env["DEVBRAIN_CONFIG"] = str(config_path)
    # Make sure the bypass isn't accidentally set in the test env.
    env.pop("DEVBRAIN_MCP_SKIP_DB_PROBE", None)

    proc = subprocess.Popen(
        [_node_path, str(MCP_BUNDLE)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Wait up to 10s for the process to exit on its own.
        stdout, stderr = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        pytest.fail(
            "MCP server did not exit within 10s — startup DB probe "
            "appears not to fail fast. stderr so far:\n"
            f"{stderr.decode('utf-8', 'replace')}"
        )

    err = stderr.decode("utf-8", "replace")
    assert proc.returncode != 0, (
        f"MCP server exited 0 with DB unreachable — expected non-zero. "
        f"stderr:\n{err}"
    )
    # The friendly message should mention DB unreachable + Docker hint.
    assert "[devbrain-mcp]" in err
    assert "DB unreachable" in err or "cannot reach Postgres" in err
    assert "DEVBRAIN_MCP_SKIP_DB_PROBE" in err  # mentions the escape hatch


def test_mcp_server_skip_probe_env_var_bypasses_check(tmp_path):
    """With DEVBRAIN_MCP_SKIP_DB_PROBE=1 set, the server should NOT
    exit on startup just because the DB is unreachable. It should
    proceed to the StdioServerTransport setup.

    We verify by setting up a dead DB config + the skip flag, starting
    the process, then asserting it's still alive after 3 seconds and
    killing it.
    """
    config_path = _write_temp_config_with_dead_db(tmp_path)
    env = os.environ.copy()
    env["DEVBRAIN_CONFIG"] = str(config_path)
    env["DEVBRAIN_MCP_SKIP_DB_PROBE"] = "1"

    proc = subprocess.Popen(
        [_node_path, str(MCP_BUNDLE)],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # The server should still be running after 3s because the probe
        # was bypassed. We don't send any MCP frames — just keep stdin
        # held open.
        time.sleep(3)
        assert proc.poll() is None, (
            "MCP server exited unexpectedly with DEVBRAIN_MCP_SKIP_DB_PROBE=1; "
            f"stderr: {proc.stderr.read().decode('utf-8', 'replace') if proc.stderr else ''}"
        )
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
