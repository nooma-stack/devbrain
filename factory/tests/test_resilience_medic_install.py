from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from ops.resilience.core import load_config  # noqa: E402
from ops.resilience.install import (  # noqa: E402
    InstallError,
    build_plan,
    create_request,
    install,
    uninstall,
)
from ops.resilience.medic_service import load_medic_config  # noqa: E402
from ops.resilience.signing import (  # noqa: E402
    encode_private_key,
    encode_public_key,
    generate_keypair,
    load_private_key,
)


class RecordingRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((tuple(args), kwargs))
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


def _public_key_file(path: Path) -> Path:
    private_raw, _public_raw = generate_keypair()
    private = load_private_key(private_raw)
    path.write_text(encode_public_key(private.public_key()))
    return path


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


def test_diagnose_medic_renders_public_key_only_and_loads(tmp_path):
    primary = _public_key_file(tmp_path / "primary.pub")
    request = _request(
        tmp_path,
        medic_mode="diagnose",
        medic_instance_id="studio-a",
        medic_primary_public_key_path=primary,
    )
    plan = build_plan(request)

    assert plan.config["medic_config_path"] == str(request.medic_config_path)
    assert plan.medic_config["mode"] == "diagnose"
    assert plan.medic_config["allowed_heal_checks"] == []
    assert {key["role"] for key in plan.medic_config["keys"]} == {"primary"}
    assert "PRIVATE" not in plan.medic_config_text
    assert {item["kind"] for item in plan.manifest["managed_files"]} >= {"medic_config"}

    request.config_path.parent.mkdir(parents=True, exist_ok=True)
    request.config_path.write_text(plan.config_text)
    request.medic_config_path.write_text(plan.medic_config_text)
    runtime = load_config(request.config_path)
    medic = load_medic_config(request.medic_config_path, runtime_config=runtime)
    assert medic.mode == "diagnose"
    assert medic.instance_id == "studio-a"


def test_heal_medic_requires_two_keys_and_explicit_recoverable_checks(tmp_path):
    primary = _public_key_file(tmp_path / "primary.pub")
    confirm = _public_key_file(tmp_path / "confirm.pub")
    request = _request(
        tmp_path,
        profile="studio",
        medic_mode="heal",
        medic_instance_id="studio-a",
        medic_primary_public_key_path=primary,
        medic_confirm_public_key_path=confirm,
        medic_allowed_heal_checks=["postgres", "ollama"],
    )
    plan = build_plan(request)

    assert {key["role"] for key in plan.medic_config["keys"]} == {
        "primary",
        "confirm",
    }
    assert plan.medic_config["allowed_heal_checks"] == ["postgres", "ollama"]

    with pytest.raises(InstallError, match="confirm"):
        _request(
            tmp_path / "missing-confirm",
            medic_mode="heal",
            medic_instance_id="studio-a",
            medic_primary_public_key_path=primary,
            medic_allowed_heal_checks=["postgres"],
        )
    with pytest.raises(InstallError, match="not enabled/recoverable"):
        build_plan(
            _request(
                tmp_path / "bad-check",
                medic_mode="heal",
                medic_instance_id="studio-a",
                medic_primary_public_key_path=primary,
                medic_confirm_public_key_path=confirm,
                medic_allowed_heal_checks=["tunnel"],
            )
        )


def test_installer_rejects_private_key_file_in_public_key_option(tmp_path):
    private_raw, _public_raw = generate_keypair()
    private = load_private_key(private_raw)
    private_path = tmp_path / "must-stay-off-host.key"
    private_path.write_text(encode_private_key(private))

    with pytest.raises(InstallError, match="primary public key is invalid"):
        _request(
            tmp_path / "host",
            medic_mode="diagnose",
            medic_instance_id="studio-a",
            medic_primary_public_key_path=private_path,
        )


def test_medic_artifact_is_mode_0600_and_removed_when_option_is_disabled(tmp_path):
    primary = _public_key_file(tmp_path / "primary.pub")
    runner = RecordingRunner()
    enabled = _request(
        tmp_path,
        medic_mode="diagnose",
        medic_instance_id="studio-a",
        medic_primary_public_key_path=primary,
    )
    install(enabled, runner=runner)
    assert enabled.medic_config_path.exists()
    assert enabled.medic_config_path.stat().st_mode & 0o777 == 0o600
    installed_manifest = json.loads(enabled.manifest_path.read_text())
    assert any(
        item["kind"] == "medic_config" for item in installed_manifest["managed_files"]
    )
    inbox = enabled.state_dir / "medic-queue" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "pending.json").write_text("{}")

    disabled = _request(tmp_path)
    install(disabled, runner=runner)
    assert not enabled.medic_config_path.exists()
    assert load_config(disabled.config_path).medic_config_path is None
    assert not inbox.exists()
    quarantined = list(
        (enabled.state_dir / "medic-queue" / "quarantine").glob(
            "policy-change-*/inbox/pending.json"
        )
    )
    assert len(quarantined) == 1


def test_medic_reconfiguration_stops_old_consumer_before_replacing_policy(
    tmp_path,
):
    primary = _public_key_file(tmp_path / "primary.pub")
    runner = RecordingRunner()
    request = _request(
        tmp_path,
        medic_mode="diagnose",
        medic_instance_id="studio-a",
        medic_primary_public_key_path=primary,
    )
    install(request, runner=runner)

    runner.calls.clear()
    install(request, runner=runner)

    commands = [call for call, _ in runner.calls]
    assert commands[0][:2] == ("launchctl", "bootout")
    assert commands[1][:2] == ("launchctl", "print")


def test_runtime_recovery_binding_change_quarantines_pending_medic_task(
    tmp_path,
):
    primary = _public_key_file(tmp_path / "primary.pub")
    runner = RecordingRunner()
    original = _request(
        tmp_path,
        medic_mode="diagnose",
        medic_instance_id="studio-a",
        medic_primary_public_key_path=primary,
    )
    install(original, runner=runner)
    inbox = original.state_dir / "medic-queue" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "pending.json").write_text("{}")

    changed = _request(
        tmp_path,
        docker_context="alternate-context",
        medic_mode="diagnose",
        medic_instance_id="studio-a",
        medic_primary_public_key_path=primary,
    )
    install(changed, runner=runner)

    assert not inbox.exists()
    quarantined = list(
        (
            original.state_dir
            / "medic-queue"
            / "quarantine"
        ).glob("policy-change-*/inbox/pending.json")
    )
    assert len(quarantined) == 1


def test_policy_change_and_uninstall_refuse_to_race_active_medic_consumer(
    tmp_path,
):
    primary = _public_key_file(tmp_path / "primary.pub")
    runner = RecordingRunner()
    request = _request(
        tmp_path,
        medic_mode="diagnose",
        medic_instance_id="studio-a",
        medic_primary_public_key_path=primary,
    )
    install(request, runner=runner)
    inbox = request.state_dir / "medic-queue" / "inbox"
    inbox.mkdir(parents=True)
    pending = inbox / "pending.json"
    pending.write_text("{}")

    lock_path = request.state_dir / "medic-queue" / "consumer.lock"
    lock_fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runner.calls.clear()
        with pytest.raises(InstallError, match="medic queue is active"):
            install(request, runner=runner)
        assert runner.calls == []
        assert pending.exists()
        assert request.manifest_path.exists()

        with pytest.raises(InstallError, match="medic queue is active"):
            uninstall(request.manifest_path, runner=runner)
        assert runner.calls == []
        assert pending.exists()
        assert request.config_path.exists()
        assert request.service_path.exists()
        assert request.manifest_path.exists()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def test_uninstall_quarantines_pending_tasks_and_retains_audit_root(tmp_path):
    primary = _public_key_file(tmp_path / "primary.pub")
    runner = RecordingRunner()
    request = _request(
        tmp_path,
        medic_mode="diagnose",
        medic_instance_id="studio-a",
        medic_primary_public_key_path=primary,
    )
    install(request, runner=runner)
    inbox = request.state_dir / "medic-queue" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "pending.json").write_text("{}")

    result = uninstall(request.manifest_path, runner=runner)

    assert result["quarantined_pending"]
    assert not inbox.exists()
    assert (request.state_dir / "medic-queue" / "quarantine").is_dir()
    assert not request.medic_config_path.exists()
