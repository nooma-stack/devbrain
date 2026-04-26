"""Atomic credential rotation with dependent-process reload + verify.

The ``rotate-db-password`` CLI delegates to :func:`rotate_with_dependents`
in this module. The flow:

1. Pre-flight: every dependent declared in ``factory.cred_dependents`` is
   checked. If ``require_all_healthy=True`` (default) and any are
   unhealthy, abort before mutating anything — clean signal.
2. ALTER USER + write ``.env`` + write ``config/devbrain.yaml``.
3. Sanity check: connect with the new password.
4. For each dependent: reload (e.g. ``launchctl unload && load``), then
   verify (e.g. tail the daemon's err log for ``authentication failed``
   in a window).
5. If any verification fails: ALTER USER back to the OLD password,
   revert ``.env`` and yaml, return ``rolled_back=True``.
6. On success: report which dependents auto-reloaded vs need manual
   restart.

Background: 2026-04-25 incident — com.devbrain.ingest LaunchAgent had
been retrying with pre-rotation creds for ~18 days, generating ~8 auth
failures per 5 minutes against Postgres. The fix is to make rotation
itself responsible for re-validating every cred-dependent process.
"""
from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg2
from psycopg2 import sql

logger = logging.getLogger(__name__)


@dataclass
class DependentCheck:
    """Result of verifying a single registered dependent."""
    id: str
    type: str  # 'launchagent' | 'pidfile' | 'manual_restart'
    healthy: bool
    error: str | None = None


@dataclass
class RotationContext:
    """All state the orchestrator needs to mutate (and roll back).

    Built by the CLI from ``load_config()`` + ``DEVBRAIN_HOME``. Tests
    construct one directly with tmp paths so the live ``.env`` / yaml
    are never touched.
    """
    user: str
    host: str
    port: int
    database: str
    old_password: str
    env_path: Path
    yaml_path: Path

    def connect_kwargs(self, password: str) -> dict:
        """Keyword form for psycopg2.connect.

        Avoids URL-style ``postgresql://user:password@host:...`` strings:
        passwords containing ``@``, ``:``, ``/``, ``?``, ``#``, ``%``
        break libpq URL parsing, and the malformed URL — still containing
        the secret — would surface in OperationalError messages echoed to
        the operator.
        """
        return dict(
            host=self.host, port=self.port,
            user=self.user, password=password, dbname=self.database,
        )


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def list_dependents(config: dict) -> list[dict]:
    """Read ``factory.cred_dependents`` from config, expand ``~`` in
    every path field, return a list of dependent specs (deep-copied so
    the caller can mutate without aliasing the cached config).
    """
    raw = (config.get("factory", {}) or {}).get("cred_dependents") or []
    out: list[dict] = []
    for item in raw:
        d = dict(item)
        for key in ("plist", "verify_log", "pidfile"):
            if key in d and isinstance(d[key], str):
                d[key] = str(Path(d[key]).expanduser())
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Pre-flight & verify
# ---------------------------------------------------------------------------

def precheck_baseline(dependents: list[dict]) -> list[DependentCheck]:
    """Verify every dependent BEFORE rotation, so we have a clean
    baseline to compare against post-reload.

    For ``manual_restart`` dependents this is always healthy=True
    (they're informational, not verifiable). Pre-flight passes
    ``lookback=True`` so the verifier scans the tail of the existing log
    — that's what catches a daemon that's been silently retrying with
    stale creds for hours/days, which would otherwise only register if a
    retry happened to land inside the verify window.
    """
    return [verify_dependent(dep, lookback=True) for dep in dependents]


def verify_dependent(dep: dict, *, lookback: bool = False) -> DependentCheck:
    """Dispatch on ``dep['verify']``.

    ``lookback`` is set by pre-flight only — the post-reload verify path
    must not scan pre-rotation log content (it would always find the old
    auth-failure churn and reject every rotation).
    """
    if dep.get("type") == "manual_restart":
        return DependentCheck(id=dep["id"], type="manual_restart", healthy=True)
    verify = dep.get("verify", "tail_log_no_auth_errors")
    if verify == "tail_log_no_auth_errors":
        return _verify_tail_log_no_auth_errors(dep, lookback=lookback)
    if verify == "connect_via_proxy":
        # Fail closed: returning healthy=True for an unimplemented verifier
        # would silently green-light rotations whose dependents may still
        # be auth-failing — exactly the integrity guarantee this feature
        # is meant to provide.
        return DependentCheck(
            id=dep["id"], type=dep.get("type", "?"), healthy=False,
            error="verify mode 'connect_via_proxy' is not implemented yet — "
                  "remove the dependent or pick a supported verify mode",
        )
    return DependentCheck(
        id=dep["id"], type=dep.get("type", "?"), healthy=False,
        error=f"unknown verify mode: {verify}",
    )


def _verify_tail_log_no_auth_errors(
    dep: dict, *, lookback: bool = False,
) -> DependentCheck:
    log_path = Path(dep.get("verify_log", "")).expanduser()
    window = int(dep.get("verify_window_seconds", 10))
    if not log_path.exists():
        # Missing log = can't verify; treat as unhealthy so the operator
        # is forced to address it, since silently passing would defeat
        # the whole point of the check.
        return DependentCheck(
            id=dep["id"], type=dep.get("type", "?"), healthy=False,
            error=f"verify_log not found: {log_path}",
        )

    # Snapshot the file size at the start; only consider lines APPENDED
    # during the window, so pre-existing auth-failure lines from before
    # the rotation don't poison the result.
    try:
        start_offset = log_path.stat().st_size
    except OSError as exc:
        return DependentCheck(
            id=dep["id"], type=dep.get("type", "?"), healthy=False,
            error=f"could not stat verify_log: {exc}",
        )

    pattern = re.compile(r"authentication failed", re.IGNORECASE)

    # Pre-flight only: scan the tail of the EXISTING log so a daemon
    # that's been silently retrying with stale creds for hours/days is
    # caught even if no retry lands inside the verify_window_seconds.
    if lookback:
        lookback_bytes = int(dep.get("verify_lookback_bytes", 65536))
        try:
            with log_path.open("rb") as f:
                if start_offset > lookback_bytes:
                    f.seek(start_offset - lookback_bytes)
                tail = f.read(start_offset if start_offset <= lookback_bytes
                              else lookback_bytes)
        except OSError as exc:
            return DependentCheck(
                id=dep["id"], type=dep.get("type", "?"), healthy=False,
                error=f"could not read verify_log: {exc}",
            )
        if pattern.search(tail.decode("utf-8", errors="replace")):
            return DependentCheck(
                id=dep["id"], type=dep.get("type", "?"), healthy=False,
                error=f"saw 'authentication failed' in last "
                      f"{lookback_bytes} bytes of {log_path} (pre-existing "
                      f"churn — fix the dependent first or rotate with "
                      f"--no-require-all-healthy)",
            )

    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        try:
            with log_path.open("rb") as f:
                f.seek(start_offset)
                new_bytes = f.read()
        except OSError as exc:
            return DependentCheck(
                id=dep["id"], type=dep.get("type", "?"), healthy=False,
                error=f"could not read verify_log: {exc}",
            )
        if pattern.search(new_bytes.decode("utf-8", errors="replace")):
            return DependentCheck(
                id=dep["id"], type=dep.get("type", "?"), healthy=False,
                error=f"saw 'authentication failed' in {log_path} during {window}s window",
            )
        time.sleep(0.5)
    return DependentCheck(id=dep["id"], type=dep.get("type", "?"), healthy=True)


# ---------------------------------------------------------------------------
# Reload
# ---------------------------------------------------------------------------

def reload_dependent(dep: dict) -> None:
    """Dispatch on ``dep['type']``. Raises on subprocess failure."""
    t = dep.get("type")
    if t == "launchagent":
        plist = str(Path(dep["plist"]).expanduser())
        # unload first; if it's not loaded, launchctl returns nonzero —
        # we tolerate that on unload because the goal-state is "loaded
        # with new env". load() failure is fatal.
        subprocess.run(
            ["launchctl", "unload", plist],
            check=False, capture_output=True, text=True,
        )
        result = subprocess.run(
            ["launchctl", "load", plist],
            check=False, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"launchctl load failed for {dep['id']}: "
                f"rc={result.returncode} stderr={result.stderr.strip()}"
            )
        return
    if t == "pidfile":
        pid_path = Path(dep["pidfile"]).expanduser()
        pid = int(pid_path.read_text().strip())
        os.kill(pid, signal.SIGHUP)
        return
    if t == "manual_restart":
        return  # informational; verify step is a no-op too
    raise ValueError(f"unknown cred_dependents type: {t}")


# ---------------------------------------------------------------------------
# .env / yaml writers (moved from factory/cli.py)
# ---------------------------------------------------------------------------

def rewrite_env_password(env_path: Path, new_password: str) -> None:
    """Replace (or append) DEVBRAIN_DB_PASSWORD in a .env file."""
    if env_path.exists():
        lines = [
            ln for ln in env_path.read_text().splitlines()
            if not ln.startswith("DEVBRAIN_DB_PASSWORD=")
        ]
    else:
        lines = []
    lines.append("")
    lines.append(f"# Database password — rotated {time.strftime('%Y-%m-%d')}")
    lines.append(f"DEVBRAIN_DB_PASSWORD={new_password}")
    env_path.write_text("\n".join(lines) + "\n")


def rewrite_yaml_db_password(yaml_path: Path, new_password: str) -> None:
    """Replace ``password:`` under ``database:`` in config/devbrain.yaml.

    Line-scoped regex rewrite (not a PyYAML round-trip) to preserve
    comments and key ordering. Raises ValueError if no ``password:`` is
    found in the database block.
    """
    out: list[str] = []
    in_db_block = False
    replaced = False
    for line in yaml_path.read_text().splitlines():
        if re.match(r"^database:", line):
            in_db_block = True
            out.append(line)
            continue
        if in_db_block and re.match(r"^  password:", line):
            out.append(f"  password: {new_password}")
            replaced = True
            continue
        if re.match(r"^[^\s#]", line):
            in_db_block = False
        out.append(line)
    if not replaced:
        raise ValueError(
            f"Could not find 'password:' under 'database:' in {yaml_path}"
        )
    yaml_path.write_text("\n".join(out) + "\n")


# ---------------------------------------------------------------------------
# DB primitives
# ---------------------------------------------------------------------------

def _alter_user_password(ctx: RotationContext, current_password: str,
                         new_password: str) -> None:
    """Issue ALTER USER. Connects with current_password (so rollback can
    pass new_password as 'current' and old_password as 'new').
    """
    conn = psycopg2.connect(
        **ctx.connect_kwargs(current_password), connect_timeout=5,
    )
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("ALTER USER {user} PASSWORD {pw}").format(
                        user=sql.Identifier(ctx.user),
                        pw=sql.Literal(new_password),
                    )
                )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def rotate_with_dependents(
    ctx: RotationContext,
    new_password: str,
    *,
    config: dict,
    require_all_healthy: bool = True,
    skip_dependents: bool = False,
    dry_run: bool = False,
) -> dict:
    """Top-level orchestrator. Returns a result dict — the CLI formats it."""
    dependents = [] if skip_dependents else list_dependents(config)

    # 1) Pre-flight baseline ---------------------------------------------
    if dependents:
        baseline = precheck_baseline(dependents)
        unhealthy = [c for c in baseline if not c.healthy]
        if unhealthy and require_all_healthy:
            return {
                "rolled_back": False,
                "aborted_baseline": True,
                "unhealthy": unhealthy,
            }

    if dry_run:
        return {
            "rolled_back": False,
            "dry_run": True,
            "would_reload": [d["id"] for d in dependents
                             if d.get("type") != "manual_restart"],
            "would_manual": [d["id"] for d in dependents
                             if d.get("type") == "manual_restart"],
        }

    # 2) ALTER USER + .env + yaml ----------------------------------------
    env_snapshot = ctx.env_path.read_bytes() if ctx.env_path.exists() else None
    yaml_snapshot = ctx.yaml_path.read_bytes()
    _alter_user_password(ctx, ctx.old_password, new_password)
    rewrite_env_password(ctx.env_path, new_password)
    rewrite_yaml_db_password(ctx.yaml_path, new_password)

    # 3) Sanity check with new creds -------------------------------------
    try:
        psycopg2.connect(
            **ctx.connect_kwargs(new_password), connect_timeout=5,
        ).close()
    except psycopg2.Error as exc:
        # New creds don't connect — roll the DB back; restore files.
        return _rollback(
            ctx, new_password,
            env_snapshot=env_snapshot, yaml_snapshot=yaml_snapshot,
            reloaded=[], reloaded_deps=[], failed=[],
            reason=f"sanity check connect failed: {exc}",
        )

    # 4) Reload + verify each dependent ----------------------------------
    reloaded: list[DependentCheck] = []
    reloaded_deps: list[dict] = []  # parallel list — needed to re-reload on rollback
    manual: list[DependentCheck] = []
    failed: list[DependentCheck] = []
    for dep in dependents:
        if dep.get("type") == "manual_restart":
            manual.append(DependentCheck(
                id=dep["id"], type="manual_restart", healthy=True,
            ))
            continue
        try:
            reload_dependent(dep)
        except Exception as exc:  # noqa: BLE001 — any reload failure is a failure
            failed.append(DependentCheck(
                id=dep["id"], type=dep.get("type", "?"),
                healthy=False, error=f"reload failed: {exc}",
            ))
            break
        check = verify_dependent(dep)
        if check.healthy:
            reloaded.append(check)
            reloaded_deps.append(dep)
        else:
            failed.append(check)
            break

    # 5) Roll back if any dependent failed -------------------------------
    if failed:
        return _rollback(
            ctx, new_password,
            env_snapshot=env_snapshot, yaml_snapshot=yaml_snapshot,
            reloaded=reloaded, reloaded_deps=reloaded_deps, failed=failed,
            reason=f"dependent verification failed: {failed[0].id}",
        )

    # 6) Success ---------------------------------------------------------
    return {
        "rolled_back": False,
        "reloaded": reloaded,
        "manual": manual,
        "skipped": skip_dependents,
    }


def _rollback(
    ctx: RotationContext,
    new_password: str,
    *,
    env_snapshot: bytes | None,
    yaml_snapshot: bytes,
    reloaded: list[DependentCheck],
    reloaded_deps: list[dict],
    failed: list[DependentCheck],
    reason: str,
) -> dict:
    """Revert DB password + .env + yaml, then re-reload any dependents
    that already picked up the new (now-reverted) creds.

    Returns ``rollback_failed=True`` if the ALTER USER itself raises —
    the operator needs an actionable error rather than a stack trace,
    since at that point .env/yaml are still in their post-write state
    and the live DB password is in an unknown state.
    """
    rollback_errors: list[str] = []
    try:
        _alter_user_password(ctx, new_password, ctx.old_password)
    except Exception as exc:  # noqa: BLE001 — any failure here is operator-actionable
        # .env/yaml are still showing the new password but the live DB
        # may or may not have been reverted. Surface this distinctly so
        # the operator doesn't trust the "old creds remain authoritative"
        # message.
        return {
            "rolled_back": False,
            "rollback_failed": True,
            "reason": reason,
            "rollback_error": f"ALTER USER rollback failed: {exc}",
            "failed": failed,
            "reloaded": reloaded,
        }

    _restore_file(ctx.env_path, env_snapshot)
    _restore_file(ctx.yaml_path, yaml_snapshot)

    # Re-reload every dependent that already picked up the (now reverted)
    # new .env. Without this, processes loaded in the failed window keep
    # running with the new password against a DB reverted to the old one
    # — recreating the very 'daemon stuck on wrong creds' state this
    # feature exists to prevent.
    for dep in reloaded_deps:
        try:
            reload_dependent(dep)
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            rollback_errors.append(f"{dep.get('id', '?')}: {exc}")

    result: dict = {
        "rolled_back": True,
        "reason": reason,
        "failed": failed,
        "reloaded": reloaded,
    }
    if rollback_errors:
        result["reload_rollback_errors"] = rollback_errors
    return result


def _restore_file(path: Path, snapshot: bytes | None) -> None:
    if snapshot is None:
        if path.exists():
            path.unlink()
    else:
        path.write_bytes(snapshot)
