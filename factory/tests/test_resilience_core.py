from __future__ import annotations

import hashlib
import hmac
import json
import sys
from collections import namedtuple
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from ops.resilience.__main__ import (  # noqa: E402
    load_dotenv_no_override,
    main as resilience_main,
    watch_forever,
)
from ops.resilience.core import (  # noqa: E402
    ActionResult,
    CheckResult,
    ConfigError,
    Monitor,
    ProcessResult,
    execute_recovery,
    load_config,
    run_check,
    send_webhook,
)


def _base_config(tmp_path: Path, *, checks=None, heartbeat=None) -> dict:
    return {
        "schema_version": 1,
        "profile": "workstation",
        "host": "test-studio",
        "state_path": str(tmp_path / "state.json"),
        "heartbeat_path": str(tmp_path / "heartbeat.json"),
        "interval_seconds": 60,
        "failure_threshold": 2,
        "cooldown_seconds": 300,
        "max_attempts": 2,
        "executables": {
            name: "/bin/echo"
            for name in (
                "brew",
                "colima",
                "docker",
                "launchctl",
                "open",
                "systemctl",
            )
        },
        "checks": checks
        or [
            {
                "id": "runtime",
                "type": "docker_runtime",
                "enabled": True,
                "required": True,
                "timeout_seconds": 5,
                "settings": {
                    "recovery": {
                        "type": "runtime_start",
                        "runtime": "colima",
                    }
                },
            }
        ],
        "heartbeat": heartbeat,
    }


def _load(tmp_path: Path, *, checks=None, heartbeat=None):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(_base_config(tmp_path, checks=checks, heartbeat=heartbeat))
    )
    return load_config(path)


def test_loader_normalizes_typed_config_and_rejects_shell_escape(tmp_path):
    config = _load(tmp_path)
    assert config.state_path.is_absolute()
    assert config.heartbeat_path.is_absolute()
    assert config.checks[0]["settings"]["recovery"] == {
        "type": "runtime_start",
        "runtime": "colima",
        "timeout_seconds": 60.0,
    }

    raw = _base_config(tmp_path)
    raw["checks"][0]["settings"]["command"] = "touch /tmp/not-allowed"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ConfigError, match="forbidden"):
        load_config(path)


def test_all_check_primitives_use_injected_non_shell_dependencies(tmp_path):
    checks = [
        {
            "id": "runtime",
            "type": "docker_runtime",
            "enabled": True,
            "required": True,
            "timeout_seconds": 5,
            "settings": {},
        },
        {
            "id": "container",
            "type": "docker_container",
            "enabled": True,
            "required": True,
            "timeout_seconds": 5,
            "settings": {"container": "devbrain-db", "require_healthcheck": True},
        },
        {
            "id": "http",
            "type": "http",
            "enabled": True,
            "required": True,
            "timeout_seconds": 5,
            "settings": {"url": "http://127.0.0.1:11434/api/tags"},
        },
        {
            "id": "tcp",
            "type": "tcp",
            "enabled": True,
            "required": False,
            "timeout_seconds": 5,
            "settings": {"host": "127.0.0.1", "port": 18900},
        },
        {
            "id": "launchd",
            "type": "launchd_label",
            "enabled": True,
            "required": False,
            "timeout_seconds": 5,
            "settings": {
                "label": "com.devbrain.ingest",
                "domain": "gui/501",
            },
        },
        {
            "id": "systemd",
            "type": "systemd_user_unit",
            "enabled": True,
            "required": False,
            "timeout_seconds": 5,
            "settings": {"unit": "devbrain-ingest.service"},
        },
        {
            "id": "disk",
            "type": "disk",
            "enabled": True,
            "required": True,
            "timeout_seconds": 5,
            "settings": {
                "path": str(tmp_path),
                "min_free_percent": 5,
                "min_free_bytes": 100,
            },
        },
        {
            "id": "backup",
            "type": "file_freshness",
            "enabled": True,
            "required": False,
            "timeout_seconds": 5,
            "settings": {
                "path": str(tmp_path / "backups"),
                "pattern": "*.dump",
                "max_age_seconds": 3600,
                "minimum_matches": 1,
            },
        },
    ]
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "latest.dump").write_text("test")
    config = _load(tmp_path, checks=checks)
    process_calls = []

    def process_runner(argv, timeout, cwd):
        process_calls.append((argv, timeout, cwd))
        if "inspect" in argv:
            return ProcessResult(
                0,
                json.dumps(
                    {
                        "Running": True,
                        "Status": "running",
                        "Health": {"Status": "healthy"},
                    }
                ),
                "",
            )
        if "print" in argv:
            return ProcessResult(0, "state = running\n", "")
        return ProcessResult(0, "25.0.0\n", "")

    class Response:
        status = 204

        def close(self):
            return None

    class Connection:
        def close(self):
            return None

    DiskUsage = namedtuple("DiskUsage", "total used free")
    results = []
    for check in config.checks:
        results.append(
            run_check(
                config,
                check,
                process_runner=process_runner,
                urlopen=lambda *_a, **_k: Response(),
                socket_connector=lambda *_a, **_k: Connection(),
                disk_usage=lambda _path: DiskUsage(1_000, 100, 900),
                time_fn=lambda: (backup_dir / "latest.dump").stat().st_mtime + 30,
            )
        )

    assert all(result.ok for result in results)
    assert any("inspect" in call[0] for call in process_calls)
    assert any("print" in call[0] for call in process_calls)
    assert any("--context" in call[0] for call in process_calls)
    assert all(not isinstance(call[0], str) for call in process_calls)


@pytest.mark.parametrize(
    ("action", "expected_tail", "cwd"),
    [
        (
            {"type": "runtime_start", "runtime": "colima", "timeout_seconds": 5},
            ["start"],
            None,
        ),
        (
            {
                "type": "docker_compose_up",
                "context": "colima",
                "project_dir": "/tmp/devbrain-test",
                "services": ["devbrain-db"],
                "timeout_seconds": 5,
            },
            ["--context", "colima", "compose", "up", "-d", "devbrain-db"],
            Path("/tmp/devbrain-test"),
        ),
        (
            {
                "type": "launchd_kickstart",
                "domain": "gui/501",
                "label": "com.devbrain.ingest",
                "timeout_seconds": 5,
            },
            ["kickstart", "-k", "gui/501/com.devbrain.ingest"],
            None,
        ),
        (
            {
                "type": "systemd_restart",
                "unit": "devbrain-ingest.service",
                "timeout_seconds": 5,
            },
            ["--user", "restart", "--", "devbrain-ingest.service"],
            None,
        ),
        (
            {
                "type": "homebrew_service_start",
                "service": "ollama",
                "timeout_seconds": 5,
            },
            ["services", "start", "ollama"],
            None,
        ),
    ],
)
def test_recovery_actions_are_fixed_argv(action, expected_tail, cwd, tmp_path):
    config = _load(tmp_path)
    calls = []

    def runner(argv, timeout, actual_cwd):
        calls.append((argv, timeout, actual_cwd))
        return ProcessResult(0, "", "")

    result = execute_recovery(
        config,
        config.checks[0],
        action,
        process_runner=runner,
    )

    assert result.ok
    assert calls[0][0][1:] == expected_tail
    assert calls[0][2] == cwd


def test_monitor_damps_recovery_and_persists_state_and_heartbeat(tmp_path):
    config = _load(tmp_path)
    now = [1_000.0]
    recoveries = []

    def failed_check(_config, check):
        return CheckResult(check["id"], check["type"], False, "down")

    def recover(_config, check, action):
        recoveries.append((check["id"], action["type"], now[0]))
        return ActionResult(check["id"], action["type"], True, "started")

    monitor = Monitor(
        config,
        check_executor=failed_check,
        action_executor=recover,
        webhook_sender=lambda *_: type(
            "Webhook", (), {"as_dict": lambda self: {}, "detail": "off"}
        )(),
        time_fn=lambda: now[0],
    )
    assert monitor.run_cycle().heartbeat["recoveries"] == []
    now[0] += 1
    assert len(monitor.run_cycle().heartbeat["recoveries"]) == 1
    now[0] += 1
    assert monitor.run_cycle().heartbeat["recoveries"] == []
    now[0] += 301
    assert len(monitor.run_cycle().heartbeat["recoveries"]) == 1
    now[0] += 301
    assert monitor.run_cycle().heartbeat["recoveries"] == []

    assert len(recoveries) == 2
    assert (
        json.loads(config.state_path.read_text())["checks"]["runtime"][
            "recovery_attempts"
        ]
        == 2
    )
    heartbeat = json.loads(config.heartbeat_path.read_text())
    assert heartbeat["healthy"] is False
    assert heartbeat["checks"]["runtime"]["detail"] == "down"


def test_targeted_recovery_requires_failure_and_respects_local_policy(tmp_path):
    config = _load(tmp_path)
    now = [2_000.0]
    healthy = [False]
    recoveries = []

    def check(_config, item):
        return CheckResult(
            item["id"],
            item["type"],
            healthy[0],
            "up" if healthy[0] else "down",
        )

    def recover(_config, item, action):
        recoveries.append((item["id"], now[0]))
        return ActionResult(item["id"], action["type"], True, "started")

    monitor = Monitor(
        config,
        check_executor=check,
        action_executor=recover,
        time_fn=lambda: now[0],
    )

    first = monitor.run_targeted_recovery("runtime")
    second = monitor.run_targeted_recovery("runtime")
    now[0] += 1
    cooldown = monitor.run_targeted_recovery("runtime")
    healthy[0] = True
    reset = monitor.run_targeted_recovery("runtime")
    healthy[0] = False
    after_reset = monitor.run_targeted_recovery("runtime")

    assert not first.ok and "threshold" in first.detail
    assert second.ok
    assert not cooldown.ok and "cooldown" in cooldown.detail
    assert not reset.ok and "healthy" in reset.detail
    assert not after_reset.ok and "threshold" in after_reset.detail
    assert recoveries == [("runtime", 2_000.0)]


def test_webhook_uses_hmac_and_never_embeds_secret_in_body(tmp_path, monkeypatch):
    config = _load(
        tmp_path,
        heartbeat={
            "url": "https://monitor.example.invalid/heartbeat",
            "secret_env": "DEVBRAIN_TEST_HEARTBEAT_SECRET",
        },
    )
    monkeypatch.setenv("DEVBRAIN_TEST_HEARTBEAT_SECRET", "top-secret")
    captured = {}

    class Response:
        status = 202

        def close(self):
            return None

    def urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    heartbeat = {"healthy": True, "host": "test-studio"}
    result = send_webhook(config, heartbeat, urlopen=urlopen)

    body = captured["request"].data
    expected = hmac.new(b"top-secret", body, hashlib.sha256).hexdigest()
    assert result.delivered
    assert captured["request"].headers["X-devbrain-signature"] == (f"sha256={expected}")
    assert b"top-secret" not in body


def test_cli_status_returns_health_exit_code(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_base_config(tmp_path)))
    heartbeat_path = tmp_path / "heartbeat.json"
    heartbeat_path.write_text(json.dumps({"version": 1, "healthy": True, "checks": {}}))

    assert resilience_main(["--config", str(config_path), "status"]) == 0
    assert '"healthy": true' in capsys.readouterr().out


def test_dotenv_loader_never_evaluates_shell_and_preserves_environment(
    tmp_path, monkeypatch
):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "EXISTING=from-file\n"
        "PLAIN=value\n"
        "QUOTED='hello world'\n"
        "NOT_EXECUTED=$(touch /tmp/devbrain-resilience-should-not-exist)\n"
    )
    monkeypatch.setenv("EXISTING", "from-caller")
    marker = Path("/tmp/devbrain-resilience-should-not-exist")
    marker.unlink(missing_ok=True)

    load_dotenv_no_override(env_file)

    assert __import__("os").environ["EXISTING"] == "from-caller"
    assert __import__("os").environ["PLAIN"] == "value"
    assert __import__("os").environ["QUOTED"] == "hello world"
    assert __import__("os").environ["NOT_EXECUTED"].startswith("$(")
    assert not marker.exists()


def test_watch_schedules_medic_independently_from_health_interval():
    clock = [0.0]
    monitor_times = []
    medic_times = []

    class Config:
        interval_seconds = 30

    class Cycle:
        heartbeat = {"healthy": True, "recoveries": []}
        webhook = type("Webhook", (), {"detail": "off"})()

    class FakeMonitor:
        config = Config()

        def run_cycle(self):
            monitor_times.append(clock[0])
            return Cycle()

    class MedicConfig:
        poll_interval_seconds = 10

    class FakeMedic:
        medic_config = MedicConfig()

        def process_once(self):
            medic_times.append(clock[0])
            return []

    def sleep(seconds):
        clock[0] += seconds

    watch_forever(
        FakeMonitor(),
        FakeMedic(),
        monotonic=lambda: clock[0],
        sleep=sleep,
        max_wakeups=7,
    )

    assert monitor_times == [0.0, 30.0, 60.0]
    assert medic_times == [0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
