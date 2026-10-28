"""One schema-constrained `codex exec` call, shared by resummarize and edges.

Same recipe as fanout._codex_classify and extract._codex_extract, built from
the same pieces (extract's binary/model/effort/error helpers and the isolated
CODEX_HOME in codex_home): a read-only, ephemeral, non-git sandbox, an
explicit ``-m``, ``--output-schema`` so the final answer is parseable JSON,
and ``-o`` to capture only that answer. stdin is closed (codex exec blocks
on stdin otherwise). codex bills the ChatGPT login of the isolated
CODEX_HOME, so there is no token spend to record.

Failure labels match the other cognify paths:
  refused     — codex not in cognify.allowed_backends
  no_auth     — the isolated CODEX_HOME is missing, unsafe or not logged in
  api         — binary missing, timeout, or an error from codex/the API
  json_parse  — the output file was not valid JSON
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile

logger = logging.getLogger(__name__)


def run_json(
    prompt: str,
    schema: dict,
    *,
    pass_name: str,
    required_keys: tuple[str, ...],
    model: str | None = None,
) -> tuple[dict | None, str | None, str | None]:
    """Run ``prompt`` through codex with ``schema`` as the output schema.

    Returns (parsed | None, failure | None, detail | None). ``model`` None or
    the "codex" sentinel means cognify.codex_model (extract's
    _CODEX_DEFAULT_MODEL). ``required_keys`` is what _salvage_last_json
    needs to recognise our object when codex skipped writing the -o file.
    """
    from cognify import backends, codex_home  # noqa: PLC0415
    from cognify.extract import (  # noqa: PLC0415
        _CODEX_BIN,
        _CODEX_DEFAULT_MODEL,
        _CODEX_TIMEOUT_S,
        _salvage_last_json,
        codex_config_args,
        codex_error,
    )

    if not backends.check("codex", pass_name):
        return None, "refused", "codex not in cognify.allowed_backends"
    home = codex_home.resolve_home()
    ok, reason = codex_home.check(home)
    if not ok:
        logger.error("cognify_%s(codex): refusing to run codex: %s", pass_name, reason)
        return None, "no_auth", f"codex isolation: {reason}"
    if not _CODEX_BIN or not os.path.exists(_CODEX_BIN):
        return None, "api", f"codex binary not found at {_CODEX_BIN}"

    codex_model = model if (model and model.lower() != "codex") else _CODEX_DEFAULT_MODEL
    with tempfile.TemporaryDirectory(prefix=f"{pass_name}-codex-") as wd:
        schema_path = os.path.join(wd, "schema.json")
        out_path = os.path.join(wd, "out.json")
        with open(schema_path, "w") as fh:
            json.dump(schema, fh)
        cmd = [
            _CODEX_BIN, "exec", "--sandbox", "read-only",
            "--skip-git-repo-check", "--ephemeral", "-C", wd,
            "--output-schema", schema_path, "-o", out_path,
            "-m", codex_model,
        ]
        cmd += codex_config_args()
        cmd.append(prompt)
        try:
            proc = subprocess.run(
                cmd, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=_CODEX_TIMEOUT_S,
                env=codex_home.env_for(home),
            )
        except subprocess.TimeoutExpired:
            return None, "api", "codex exec timeout"
        except OSError as exc:
            return None, "api", f"codex exec failed to start: {exc}"[:300]
        if not os.path.exists(out_path):
            salvaged = _salvage_last_json(
                proc.stdout or "", proc.stderr or "", required_keys=required_keys
            )
            if salvaged is not None:
                return salvaged, None, None
            err = codex_error(proc.stdout or "", proc.stderr or "")
            if err:
                logger.error(
                    "cognify_%s(codex): codex -m %s failed: %s", pass_name, codex_model, err
                )
                return None, "api", f"codex error (rc={proc.returncode}): {err}"
            return None, "api", (
                f"codex no output (rc={proc.returncode}): "
                f"{(proc.stderr or '')[-300:]}"
            )
        try:
            with open(out_path) as fh:
                parsed = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            return None, "json_parse", str(exc)[:200]
    if not isinstance(parsed, dict):
        return None, "json_parse", "non-object output"
    return parsed, None, None
