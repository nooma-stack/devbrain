"""Tests for the cross-platform resilience service installer."""

from __future__ import annotations

import json
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from ops.resilience.install import (  # noqa: E402
    InstallError,
    SERVICE_LABEL,
    SYSTEMD_UNIT,
    _reject_incompatible_args,
    build_parser,
    build_plan,
    create_request,
    install,
    restart,
    uninstall,
)
from ops.resilience.core import load_config  # noqa: E402


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict]] = []

    def __call__(self, args, **kwargs):
        self.calls.append((tuple(args), dict(kwargs)))
        returncode = 0
        if "print" in args:
            returncode = 113
        elif "is-active" in args:
            returncode = 3
        return subprocess.CompletedProcess(
            args,
            returncode,
            "",
            "",
        )


def _request(tmp_path: Path, **overrides):
    values = {
        "profile": "workstation",
        "platform_name": "macos",
        "repo_root": REPO_ROOT,
        "home": tmp_path,
        "username": "alice",
        "uid": 501,
        "python_executable": Path("/usr/bin/python3"),
    }
    values.update(overrides)
    return create_request(**values)


def _check(plan, check_id: str) -> dict:
    return next(item for item in plan.config["checks"] if item["id"] == check_id)


def test_workstation_renders_user_launchagent_with_local_only_defaults(tmp_path):
    request = _request(tmp_path)
    plan = build_plan(request)

    assert request.service_scope == "user"
    assert request.service_path == (
        tmp_path / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
    )
    assert "<key>UserName</key>" not in plan.service_text
    assert "<string>watch</string>" in plan.service_text
    assert "http://localhost:11434/api/tags" in plan.config_text
    assert plan.config["heartbeat"] is None
    assert _check(plan, "tunnel")["enabled"] is False
    assert _check(plan, "agent_bus")["enabled"] is False
    assert all(check["id"] != "backup" for check in plan.config["checks"])
    assert plan.config["state_path"] == str(request.state_dir / "state.json")
    assert plan.config["heartbeat_path"] == str(request.state_dir / "heartbeat.json")
    assert request.container_runtime == "docker_desktop"
    assert request.docker_context == "desktop-linux"
    assert plan.manifest["container_runtime"] == "docker-desktop"
    assert plan.manifest["docker_context"] == "desktop-linux"
    assert _check(plan, "container_runtime")["settings"]["context"] == (
        "desktop-linux"
    )
    assert _check(plan, "postgres")["settings"]["recovery"]["context"] == (
        "desktop-linux"
    )
    assert "72.60." not in plan.config_text + plan.service_text
    assert "2.24." not in plan.config_text + plan.service_text
    assert "127.0.0.1" not in plan.config_text + plan.service_text


def test_studio_renders_system_launchdaemon_as_invoking_nonroot_user(tmp_path):
    request = _request(tmp_path, profile="studio")
    plan = build_plan(request)

    assert request.service_scope == "system"
    assert request.service_path == Path(f"/Library/LaunchDaemons/{SERVICE_LABEL}.plist")
    assert "<key>UserName</key>" in plan.service_text
    assert "<string>alice</string>" in plan.service_text
    assert "<string>root</string>" not in plan.service_text
    assert plan.config["profile"] == "studio"
    assert plan.config["interval_seconds"] == 60
    assert request.container_runtime == "colima"
    assert request.docker_context == "colima"
    assert all(check["id"] != "backup" for check in plan.config["checks"])
    assert plan.manifest["backup_path"] is None
    # Studio is useful without any public VPS or remote-control companion.
    assert _check(plan, "tunnel")["enabled"] is False
    assert _check(plan, "agent_bus")["enabled"] is False
    assert plan.config["heartbeat"] is None
    parsed = plistlib.loads(plan.service_text.encode())
    assert parsed["UserName"] == "alice"
    assert parsed["ProgramArguments"][-1] == "watch"
    assert parsed["Umask"] == 0o77


def test_docker_context_accepts_docker_supported_plus_character(tmp_path):
    request = _request(tmp_path, docker_context="nooma+studio")
    plan = build_plan(request)

    assert request.docker_context == "nooma+studio"
    assert plan.manifest["docker_context"] == "nooma+studio"
    assert _check(plan, "container_runtime")["settings"]["context"] == (
        "nooma+studio"
    )


def test_backup_freshness_requires_explicit_opt_in_and_path(tmp_path):
    with pytest.raises(InstallError, match="requires --backup-path"):
        _request(tmp_path / "missing", profile="studio", with_backup_check=True)

    with pytest.raises(InstallError, match="requires --with-backup-check"):
        _request(
            tmp_path / "unselected",
            profile="studio",
            backup_path=tmp_path / "backups",
        )

    backup_path = tmp_path / "external-backups"
    request = _request(
        tmp_path / "enabled",
        profile="studio",
        with_backup_check=True,
        backup_path=backup_path,
    )
    plan = build_plan(request)
    backup = _check(plan, "backup")

    assert backup["required"] is False
    assert backup["settings"] == {
        "path": str(backup_path.resolve()),
        "pattern": "*",
        "max_age_seconds": 172800,
        "minimum_matches": 1,
    }
    assert plan.manifest["backup_path"] == str(backup_path.resolve())


def test_linux_uses_user_systemd_for_both_profiles(tmp_path):
    for profile in ("workstation", "studio"):
        request = _request(
            tmp_path / profile,
            profile=profile,
            platform_name="linux",
        )
        plan = build_plan(request)
        assert request.service_manager == "systemd-user"
        assert request.service_scope == "user"
        assert request.service_path == (
            tmp_path / profile / ".config" / "systemd" / "user" / SYSTEMD_UNIT
        )
        assert "systemctl" not in plan.service_text
        assert "ops.resilience" in plan.service_text
        assert " watch" in plan.service_text
        assert "UMask=0077" in plan.service_text


def test_optional_checks_are_disabled_unless_explicitly_enabled(tmp_path):
    baseline = build_plan(_request(tmp_path / "baseline"))
    assert _check(baseline, "tunnel")["enabled"] is False
    assert _check(baseline, "agent_bus")["enabled"] is False

    enabled = build_plan(
        _request(
            tmp_path / "enabled",
            with_tunnel_check=True,
            tunnel_label="com.example.safe-tunnel",
            with_agent_bus_check=True,
            agent_bus_url="http://localhost:18900/healthz",
        )
    )
    assert _check(enabled, "tunnel")["enabled"] is True
    assert _check(enabled, "tunnel")["settings"]["label"] == "com.example.safe-tunnel"
    assert _check(enabled, "agent_bus")["enabled"] is True
    assert _check(enabled, "agent_bus")["settings"]["host"] == "localhost"
    assert _check(enabled, "agent_bus")["settings"]["port"] == 18900


def test_heartbeat_references_secret_env_name_but_never_secret_value(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text(
        "MY_HEARTBEAT_SECRET=never-write-this-secret\n"
    )
    monkeypatch.setenv("MY_HEARTBEAT_SECRET", "never-write-this-secret")
    request = _request(
        tmp_path,
        repo_root=repo,
        with_heartbeat=True,
        heartbeat_url="https://health.example.invalid/v1/ping",
        heartbeat_secret_env="MY_HEARTBEAT_SECRET",
    )
    plan = build_plan(request)
    combined = plan.config_text + plan.service_text + plan.manifest_text

    assert plan.config["heartbeat"] == {
        "url": "https://health.example.invalid/v1/ping",
        "secret_env": "MY_HEARTBEAT_SECRET",
    }
    assert "MY_HEARTBEAT_SECRET" in plan.config_text
    assert "never-write-this-secret" not in combined
    assert "MY_HEARTBEAT_SECRET" not in plan.service_text
    assert "MY_HEARTBEAT_SECRET" not in plan.manifest_text


def test_optional_values_load_from_dotenv_without_shell_evaluation(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = tmp_path / "must-not-exist"
    (repo / ".env").write_text(
        "\n".join(
            [
                "DEVBRAIN_HEARTBEAT_URL=https://health.example.invalid/ping",
                "DEVBRAIN_HEARTBEAT_TOKEN=secret-stays-in-dotenv",
                "DEVBRAIN_TUNNEL_LABEL=com.example.tunnel",
                "DEVBRAIN_AGENT_BUS_HEALTH_URL=http://127.0.0.1:18900/healthz",
                f'IGNORED_SHELL="$(touch {marker})"',
            ]
        )
    )

    request = _request(
        tmp_path / "home",
        repo_root=repo,
        env={},
        with_heartbeat=True,
        with_tunnel_check=True,
        with_agent_bus_check=True,
    )

    assert request.heartbeat_url == "https://health.example.invalid/ping"
    assert request.tunnel_label == "com.example.tunnel"
    assert request.agent_bus_url == "http://127.0.0.1:18900/healthz"
    assert not marker.exists()


def test_heartbeat_url_rejects_embedded_credentials(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("HEARTBEAT_TOKEN=test-secret\n")
    with pytest.raises(InstallError, match="must not contain embedded credentials"):
        _request(
            tmp_path,
            repo_root=repo,
            with_heartbeat=True,
            heartbeat_url="https://user:secret@example.invalid/ping",
            heartbeat_secret_env="HEARTBEAT_TOKEN",
        )


def test_heartbeat_url_requires_https(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("HEARTBEAT_TOKEN=test-secret\n")
    with pytest.raises(InstallError, match="absolute HTTPS URL"):
        _request(
            tmp_path,
            repo_root=repo,
            with_heartbeat=True,
            heartbeat_url="http://health.example.invalid/ping",
            heartbeat_secret_env="HEARTBEAT_TOKEN",
        )


def test_heartbeat_requires_runtime_token_in_repository_dotenv(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text(
        "DEVBRAIN_HEARTBEAT_URL=https://health.example.invalid/ping\n"
    )

    with pytest.raises(InstallError, match="must be set"):
        _request(
            tmp_path,
            repo_root=repo,
            env={},
            with_heartbeat=True,
        )


def test_container_runtime_default_and_explicit_override(tmp_path):
    assert _request(tmp_path / "workstation").container_runtime == "docker_desktop"
    assert _request(tmp_path / "studio", profile="studio").container_runtime == "colima"
    overridden = _request(
        tmp_path / "override",
        profile="studio",
        container_runtime="docker-desktop",
    )
    assert overridden.container_runtime == "docker_desktop"
    recovery = _check(build_plan(overridden), "container_runtime")["settings"][
        "recovery"
    ]
    assert recovery == {"type": "runtime_start", "runtime": "docker_desktop"}
    linux = _request(tmp_path / "linux", platform_name="linux")
    assert linux.container_runtime == "docker_engine"
    assert linux.docker_context == "default"
    linux_plan = build_plan(linux)
    assert linux_plan.manifest["container_runtime"] == "docker-engine"
    linux_runtime = _check(linux_plan, "container_runtime")
    assert "recovery" not in linux_runtime["settings"]


def test_service_preserves_virtualenv_python_symlink(tmp_path):
    interpreter = tmp_path / "python-real"
    interpreter.touch()
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(interpreter)

    request = _request(tmp_path, python_executable=venv_python)
    plan = build_plan(request)
    parsed = plistlib.loads(plan.service_text.encode())

    assert request.python_executable == venv_python
    assert request.python_executable.resolve() == interpreter
    assert parsed["ProgramArguments"][0] == str(venv_python)


def test_tunnel_check_requires_explicit_valid_label(tmp_path):
    with pytest.raises(InstallError, match="requires a valid --tunnel-label"):
        _request(tmp_path, with_tunnel_check=True)
    with pytest.raises(InstallError, match="requires a valid --tunnel-label"):
        _request(
            tmp_path,
            with_tunnel_check=True,
            tunnel_label="not a launchd label!",
        )


def test_service_rejects_root_identity(tmp_path):
    with pytest.raises(InstallError, match="non-root"):
        _request(tmp_path, username="root", uid=0)


def test_owned_artifact_paths_must_be_distinct(tmp_path):
    collision = tmp_path / ".devbrain" / "resilience" / "state.json"
    with pytest.raises(InstallError, match="fixed per-user"):
        _request(tmp_path, config_path=collision)


def test_dry_run_renders_without_writing_or_running_commands(tmp_path):
    runner = RecordingRunner()
    request = _request(tmp_path)
    plan = install(request, dry_run=True, runner=runner)

    assert plan.config["schema_version"] == 1
    assert not request.config_path.exists()
    assert not request.manifest_path.exists()
    assert not request.service_path.exists()
    assert runner.calls == []


def test_workstation_install_is_idempotent_and_manifest_is_mode_0600(tmp_path):
    runner = RecordingRunner()
    request = _request(tmp_path)

    first = install(request, runner=runner)
    first_files = {
        request.config_path: request.config_path.read_bytes(),
        request.service_path: request.service_path.read_bytes(),
        request.manifest_path: request.manifest_path.read_bytes(),
    }
    second = install(request, runner=runner)

    assert first.config_text == second.config_text
    for path, content in first_files.items():
        assert path.read_bytes() == content
    assert stat.S_IMODE(request.config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(request.manifest_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(request.service_path.stat().st_mode) == 0o644
    assert stat.S_IMODE(request.state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(request.log_dir.stat().st_mode) == 0o700
    manifest = json.loads(request.manifest_path.read_text())
    assert {item["path"] for item in manifest["managed_files"]} == {
        str(request.config_path),
        str(request.service_path),
        str(request.state_dir / "state.json"),
        str(request.state_dir / "heartbeat.json"),
    }
    bootstrap_calls = [call for call, _ in runner.calls if "bootstrap" in call]
    assert len(bootstrap_calls) == 2


def test_install_hardens_repository_dotenv_permissions(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    dotenv = repo / ".env"
    dotenv.write_text("EXAMPLE=value\n")
    dotenv.chmod(0o644)
    runner = RecordingRunner()

    install(_request(tmp_path / "home", repo_root=repo), runner=runner)

    assert stat.S_IMODE(dotenv.stat().st_mode) == 0o600


def test_uninstall_removes_only_manifest_owned_files_and_is_idempotent(tmp_path):
    runner = RecordingRunner()
    request = _request(tmp_path)
    install(request, runner=runner)
    unrelated = request.service_path.parent / "com.example.unrelated.plist"
    unrelated.write_text("keep me")

    result = uninstall(request.manifest_path, runner=runner)
    assert result["installed"] is True
    assert not request.config_path.exists()
    assert not request.service_path.exists()
    assert not request.manifest_path.exists()
    assert unrelated.read_text() == "keep me"

    second = uninstall(request.manifest_path, runner=runner)
    assert second["installed"] is False
    assert unrelated.exists()


@pytest.mark.parametrize("platform_name", ["macos", "linux"])
def test_restart_preserves_rendered_policy_and_reactivates_service(
    tmp_path,
    platform_name,
):
    runner = RecordingRunner()
    request = _request(tmp_path, platform_name=platform_name)
    install(request, runner=runner)
    rendered = {
        request.config_path: request.config_path.read_bytes(),
        request.service_path: request.service_path.read_bytes(),
        request.manifest_path: request.manifest_path.read_bytes(),
    }
    runner.calls.clear()

    result = restart(request.manifest_path, runner=runner)

    assert result["installed"] is True
    assert result["operation"] == "restart"
    for path, content in rendered.items():
        assert path.read_bytes() == content
    commands = [call for call, _ in runner.calls]
    assert any("bootout" in call or "disable" in call for call in commands)
    assert any("bootstrap" in call or "enable" in call for call in commands)


def test_restart_is_a_noop_when_resilience_is_not_installed(tmp_path):
    runner = RecordingRunner()

    result = restart(
        tmp_path / ".devbrain" / "resilience" / "install-manifest.json",
        runner=runner,
    )

    assert result["installed"] is False
    assert runner.calls == []


@pytest.mark.parametrize("platform_name", ["macos", "linux"])
def test_uninstall_refuses_to_delete_files_while_service_is_active(
    tmp_path,
    platform_name,
):
    request = _request(tmp_path, platform_name=platform_name)
    install(request, runner=RecordingRunner())

    # A zero exit from launchctl print/systemctl is-active means the service
    # survived the requested stop.
    active_runner = RecordingRunner()
    original_call = active_runner.__call__

    def report_active(args, **kwargs):
        if "print" in args or "is-active" in args:
            active_runner.calls.append((tuple(args), dict(kwargs)))
            return subprocess.CompletedProcess(args, 0, "", "")
        return original_call(args, **kwargs)

    with pytest.raises(InstallError, match="while .* (loaded|active)"):
        uninstall(request.manifest_path, runner=report_active)

    assert request.config_path.exists()
    assert request.service_path.exists()
    assert request.manifest_path.exists()


@pytest.mark.parametrize("platform_name", ["macos", "linux"])
def test_uninstall_fails_closed_when_inactive_state_cannot_be_verified(
    tmp_path,
    platform_name,
):
    request = _request(tmp_path, platform_name=platform_name)
    install(request, runner=RecordingRunner())

    def uncertain_runner(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "permission denied")

    with pytest.raises(InstallError, match="could not verify"):
        uninstall(request.manifest_path, runner=uncertain_runner)

    assert request.config_path.exists()
    assert request.service_path.exists()
    assert request.manifest_path.exists()


def test_uninstall_rejects_tampered_service_artifact_path(tmp_path):
    runner = RecordingRunner()
    request = _request(tmp_path)
    install(request, runner=runner)
    manifest = json.loads(request.manifest_path.read_text())
    service_item = next(
        item for item in manifest["managed_files"] if item["kind"] == "service"
    )
    service_item["path"] = "/tmp/not-the-installed-service"
    request.manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(InstallError, match="does not match"):
        uninstall(request.manifest_path, runner=runner)


def test_uninstall_rejects_paired_manifest_path_tampering(tmp_path):
    runner = RecordingRunner()
    request = _request(tmp_path)
    install(request, runner=runner)
    manifest = json.loads(request.manifest_path.read_text())

    arbitrary_service = tmp_path / "do-not-delete-service"
    arbitrary_service.write_text("keep")
    manifest["service"]["path"] = str(arbitrary_service)
    service_item = next(
        item for item in manifest["managed_files"] if item["kind"] == "service"
    )
    service_item["path"] = str(arbitrary_service)
    request.manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(InstallError, match="fixed location"):
        uninstall(request.manifest_path, runner=runner)
    assert arbitrary_service.exists()

    manifest = build_plan(request).manifest
    arbitrary_config = tmp_path / "do-not-delete-config"
    arbitrary_config.write_text("keep")
    manifest["config_path"] = str(arbitrary_config)
    config_item = next(
        item for item in manifest["managed_files"] if item["kind"] == "config"
    )
    config_item["path"] = str(arbitrary_config)
    request.manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(InstallError, match="fixed location"):
        uninstall(request.manifest_path, runner=runner)
    assert arbitrary_config.exists()


def test_manifest_rejects_invalid_docker_context(tmp_path):
    runner = RecordingRunner()
    request = _request(tmp_path)
    install(request, runner=runner)
    manifest = json.loads(request.manifest_path.read_text())
    manifest["docker_context"] = "../../unsafe"
    request.manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(InstallError, match="Docker context"):
        uninstall(request.manifest_path, runner=runner)


def test_linux_install_and_uninstall_use_user_systemd(tmp_path):
    runner = RecordingRunner()
    request = _request(tmp_path, platform_name="linux")

    install(request, runner=runner)
    uninstall(request.manifest_path, runner=runner)

    commands = [call for call, _ in runner.calls]
    assert ("systemctl", "--user", "daemon-reload") in commands
    assert (
        "systemctl",
        "--user",
        "enable",
        "--now",
        SYSTEMD_UNIT,
    ) in commands
    assert (
        "systemctl",
        "--user",
        "disable",
        "--now",
        SYSTEMD_UNIT,
    ) in commands
    assert all("sudo" not in call for call in commands)


def test_linux_studio_enables_user_linger_for_boot_persistence(tmp_path):
    runner = RecordingRunner()
    request = _request(
        tmp_path,
        platform_name="linux",
        profile="studio",
    )

    install(request, runner=runner)

    commands = [call for call, _ in runner.calls]
    assert (
        "sudo",
        "loginctl",
        "enable-linger",
        "alice",
    ) in commands


def test_parser_rejects_unknown_and_abbreviated_flags():
    parser = build_parser()
    with pytest.raises(SystemExit) as unknown:
        parser.parse_args(["--definitely-unknown"])
    assert unknown.value.code == 2
    with pytest.raises(SystemExit) as abbreviated:
        parser.parse_args(["--prof", "studio"])
    assert abbreviated.value.code == 2


def test_parser_rejects_incompatible_uninstall_options():
    parser = build_parser()
    args = parser.parse_args(
        ["--uninstall", "--profile", "studio", "--container-runtime", "colima"]
    )
    with pytest.raises(InstallError, match="incompatible"):
        _reject_incompatible_args(args)
    restart_args = parser.parse_args(["--restart", "--profile", "studio"])
    with pytest.raises(InstallError, match="incompatible"):
        _reject_incompatible_args(restart_args)
    restart_yes = parser.parse_args(["--restart", "--yes"])
    with pytest.raises(InstallError, match="only valid"):
        _reject_incompatible_args(restart_yes)
    install_args = parser.parse_args(["--yes"])
    with pytest.raises(InstallError, match="only valid"):
        _reject_incompatible_args(install_args)


@pytest.mark.parametrize("platform_name", ["macos", "linux"])
@pytest.mark.parametrize("profile", ["workstation", "studio"])
def test_every_platform_profile_render_loads_with_production_core(
    tmp_path, platform_name, profile
):
    request = _request(
        tmp_path / platform_name / profile,
        platform_name=platform_name,
        profile=profile,
    )
    plan = build_plan(request)
    request.config_path.parent.mkdir(parents=True, exist_ok=True)
    request.config_path.write_text(plan.config_text)

    loaded = load_config(request.config_path)

    assert loaded.profile == profile
    assert loaded.state_path == request.state_dir / "state.json"
    assert loaded.heartbeat_path == request.state_dir / "heartbeat.json"
    expected_checks = {
        "container_runtime",
        "postgres",
        "ollama",
        "ingest",
        "disk",
        "tunnel",
        "agent_bus",
    }
    assert {check["id"] for check in loaded.checks} == expected_checks
