"""Focused tests for resilience entry points outside ops.resilience."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import click
import config
from click.testing import CliRunner

import cli as cli_module
import setup
from cli import cli


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = REPO_ROOT / "scripts" / "install.sh"
REINSTALLER = REPO_ROOT / "scripts" / "reinstall.sh"
DEVBRAIN_WRAPPER = REPO_ROOT / "bin" / "devbrain"


def _prepare_setup_script(monkeypatch, tmp_path: Path) -> Path:
    script = tmp_path / "scripts" / "install-resilience.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\n")
    monkeypatch.setattr(setup, "DEVBRAIN_HOME", tmp_path)
    monkeypatch.setattr(setup.Path, "home", staticmethod(lambda: tmp_path))
    return script


def test_main_installer_help_exposes_profiles_and_optional_remote_checks():
    result = subprocess.run(
        ["bash", str(INSTALLER), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--profile=workstation|studio" in result.stdout
    assert "--container-runtime=RUNTIME" in result.stdout
    assert "--with-resilience" in result.stdout
    assert "--no-resilience" in result.stdout
    assert "--with-heartbeat" in result.stdout
    assert "--with-tunnel-check" in result.stdout
    assert "--with-agent-bus-check" in result.stdout
    assert "--with-backup-check" in result.stdout
    assert "--backup-path=PATH" in result.stdout
    assert "--with-medic=diagnose|heal" in result.stdout
    assert "--medic-allow-check=CHECK" in result.stdout
    assert "works locally without a VPS" in result.stdout


def test_main_installer_keeps_workstation_yes_opt_in_and_studio_default():
    source = INSTALLER.read_text()

    assert 'DEPLOYMENT_PROFILE="workstation"' in source
    assert 'elif [[ "$DEPLOYMENT_PROFILE" == "studio" ]]' in source
    assert "elif $AUTO_YES; then" in source
    assert "enable=false" in source
    assert 'CONTAINER_RUNTIME="colima"' in source
    assert 'CONTAINER_RUNTIME="docker-desktop"' in source
    assert 'bash "$DEVBRAIN_HOME/scripts/install-resilience.sh"' in source
    assert '--container-runtime "$CONTAINER_RUNTIME"' in source
    assert "A Docker CLI installed for Colima is not evidence" in source
    assert "[[ -d /Applications/Docker.app ]]" in source
    assert 'INSTALL_TARGET_PATH="$HOME/.devbrain/install-target.json"' in source
    assert "record_install_target" in source
    assert "load_existing_install_target" in source
    assert "Refusing to switch" in source
    assert "'  \"schema_version\": 2,'" in source


def test_main_installer_rejects_unknown_options_before_installing():
    result = subprocess.run(
        ["bash", str(INSTALLER), "--no-resilence"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "unknown installer option" in result.stderr


def test_main_installer_rejects_backup_path_without_explicit_opt_in(tmp_path):
    result = subprocess.run(
        ["bash", str(INSTALLER), "--backup-path", str(tmp_path / "backups")],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "--backup-path requires --with-backup-check" in result.stderr


def test_main_installer_rerun_preserves_recorded_profile_and_runtime(tmp_path):
    source = INSTALLER.read_text()
    prefix = source.split(
        "# ─── Formatting ─────────────────────────────────────────────────",
        1,
    )[0]
    resolver = tmp_path / "resolve-installer-target.sh"
    resolver.write_text(
        prefix
        + '\nprintf "%s|%s|%s\\n" "$DEPLOYMENT_PROFILE" '
        + '"$CONTAINER_RUNTIME" "$DOCKER_CONTEXT_NAME"\n'
    )
    target = tmp_path / ".devbrain" / "install-target.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "generated_by": "devbrain-installer",
                "profile": "studio",
                "container_runtime": "colima",
                "docker_context": "colima-nooma",
            }
        )
    )
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "DEVBRAIN_HOME": str(tmp_path / "devbrain"),
    }

    rerun = subprocess.run(
        ["bash", str(resolver)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    switch = subprocess.run(
        [
            "bash",
            str(resolver),
            "--container-runtime",
            "docker-desktop",
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert rerun.returncode == 0, rerun.stderr
    assert rerun.stdout.strip() == "studio|colima|colima-nooma"
    assert switch.returncode == 2
    assert "Refusing to switch" in switch.stderr


def test_setup_resilience_local_only_uses_argument_vector(monkeypatch, tmp_path):
    script = _prepare_setup_script(monkeypatch, tmp_path)
    confirms = iter([True, False, False, False, False, False])
    prompts = iter(["workstation", "docker-desktop"])
    captured = {}

    monkeypatch.setattr(setup, "_confirm", lambda *args, **kwargs: next(confirms))
    monkeypatch.setattr(setup, "_prompt", lambda *args, **kwargs: next(prompts))

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(setup.subprocess, "run", fake_run)

    setup.setup_resilience()

    assert captured["args"] == [
        "bash",
        str(script),
        "--profile",
        "workstation",
        "--container-runtime",
        "docker-desktop",
    ]
    assert captured["kwargs"] == {"check": False}


def test_setup_resilience_passes_only_selected_remote_values(monkeypatch, tmp_path):
    script = _prepare_setup_script(monkeypatch, tmp_path)
    confirms = iter([True, True, True, True, False, False])
    prompts = iter(
        [
            "studio",
            "colima",
            "https://heartbeat.example.invalid/ping",
            "MY_HEARTBEAT_TOKEN",
            "com.example.reverse-tunnel",
            "http://127.0.0.1:18900/healthz",
        ]
    )
    captured = {}

    monkeypatch.setattr(setup, "_confirm", lambda *args, **kwargs: next(confirms))
    monkeypatch.setattr(setup, "_prompt", lambda *args, **kwargs: next(prompts))

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(setup.subprocess, "run", fake_run)

    setup.setup_resilience()

    args = captured["args"]
    assert args[:6] == [
        "bash",
        str(script),
        "--profile",
        "studio",
        "--container-runtime",
        "colima",
    ]
    assert args[args.index("--heartbeat-url") + 1] == (
        "https://heartbeat.example.invalid/ping"
    )
    assert args[args.index("--heartbeat-secret-env") + 1] == ("MY_HEARTBEAT_TOKEN")
    assert args[args.index("--tunnel-label") + 1] == ("com.example.reverse-tunnel")
    assert args[args.index("--agent-bus-url") + 1] == ("http://127.0.0.1:18900/healthz")
    assert captured["kwargs"] == {"check": False}


def test_setup_resilience_surfaces_installer_failure(monkeypatch, tmp_path):
    _prepare_setup_script(monkeypatch, tmp_path)
    confirms = iter([True, False, False, False, False, False])
    prompts = iter(["workstation", "docker-desktop"])
    monkeypatch.setattr(setup, "_confirm", lambda *args, **kwargs: next(confirms))
    monkeypatch.setattr(setup, "_prompt", lambda *args, **kwargs: next(prompts))
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 17),
    )

    try:
        setup.setup_resilience()
    except click.ClickException as exc:
        assert "status 17" in str(exc)
    else:
        raise AssertionError("installer failure should be surfaced")


def test_setup_cli_help_and_dispatch_include_resilience(monkeypatch):
    runner = CliRunner()
    help_result = runner.invoke(cli, ["setup", "--help"])
    assert help_result.exit_code == 0
    assert "setup resilience" in help_result.output

    calls = []
    monkeypatch.setattr(setup, "run_setup", lambda section=None: calls.append(section))
    result = runner.invoke(cli, ["setup", "resilience"])
    assert result.exit_code == 0
    assert calls == ["resilience"]
    assert any(key == "resilience" for _, key, _ in setup.MENU_SECTIONS)

    ops_help = runner.invoke(cli, ["ops", "--help"])
    assert ops_help.exit_code == 0
    assert "run-once" in ops_help.output
    assert "medic" in ops_help.output
    medic_help = runner.invoke(cli, ["ops", "medic", "--help"])
    assert medic_help.exit_code == 0
    assert "keygen" in medic_help.output
    assert "task" in medic_help.output


def test_full_setup_invokes_resilience_before_verification(monkeypatch):
    calls = []
    monkeypatch.setattr(setup, "setup_github", lambda: calls.append("github"))
    monkeypatch.setattr(setup, "setup_ai_cli_logins", lambda: calls.append("ai-clis"))
    monkeypatch.setattr(
        setup,
        "setup_identity",
        lambda: calls.append("identity") or "alice",
    )
    monkeypatch.setattr(setup, "setup_projects", lambda: calls.append("projects"))
    monkeypatch.setattr(
        setup,
        "setup_notifications",
        lambda dev_id: calls.append(f"notifications:{dev_id}"),
    )
    monkeypatch.setattr(setup, "setup_mcp_client", lambda: calls.append("mcp"))
    monkeypatch.setattr(setup, "setup_resilience", lambda: calls.append("resilience"))
    monkeypatch.setattr(setup, "setup_pkrelay", lambda: calls.append("pkrelay"))
    monkeypatch.setattr(setup, "run_verification", lambda: calls.append("verify"))
    monkeypatch.setattr(setup, "print_post_actions", lambda: calls.append("actions"))

    setup._run_full_setup()

    assert "resilience" in calls
    assert calls.index("resilience") < calls.index("verify")


def test_setup_resilience_adds_explicit_backup_path(monkeypatch, tmp_path):
    script = _prepare_setup_script(monkeypatch, tmp_path)
    backup_path = tmp_path / "backup-output"
    confirms = iter([True, False, False, False, True, False])
    prompts = iter(["studio", "colima", str(backup_path)])
    captured = {}
    monkeypatch.setattr(
        setup, "_confirm", lambda *args, **kwargs: next(confirms)
    )
    monkeypatch.setattr(
        setup, "_prompt", lambda *args, **kwargs: next(prompts)
    )
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda args, **kwargs: (
            captured.update({"args": args, "kwargs": kwargs})
            or subprocess.CompletedProcess(args, 0)
        ),
    )

    setup.setup_resilience()

    args = captured["args"]
    assert args[:6] == [
        "bash",
        str(script),
        "--profile",
        "studio",
        "--container-runtime",
        "colima",
    ]
    assert args[args.index("--backup-path") + 1] == str(backup_path)
    assert "--with-backup-check" in args


def test_ops_status_and_run_once_forward_exact_config_and_exit_code(
    monkeypatch, tmp_path
):
    calls = []

    def fake_main(argv):
        calls.append(argv)
        return 0 if argv[-1] == "status" else 1

    monkeypatch.setattr(cli_module, "_load_resilience_cli", lambda: fake_main)
    config = tmp_path / "config.json"
    runner = CliRunner()

    status = runner.invoke(
        cli,
        ["ops", "status", "--config", str(config)],
    )
    run_once = runner.invoke(
        cli,
        ["ops", "run-once", "--config", str(config)],
    )

    assert status.exit_code == 0
    assert run_once.exit_code == 1
    assert calls == [
        ["--config", str(config.resolve()), "status"],
        ["--config", str(config.resolve()), "run-once"],
    ]


def test_setup_resilience_adds_diagnose_only_medic_without_private_key(
    monkeypatch, tmp_path
):
    _prepare_setup_script(monkeypatch, tmp_path)
    confirms = iter([True, False, False, False, False, True])
    prompts = iter(
        [
            "studio",
            "colima",
            "diagnose",
            "/operator-share/primary.pub",
            "nooma-studio",
        ]
    )
    captured = {}
    monkeypatch.setattr(setup, "_confirm", lambda *args, **kwargs: next(confirms))
    monkeypatch.setattr(setup, "_prompt", lambda *args, **kwargs: next(prompts))
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda args, **kwargs: (
            captured.update({"args": args, "kwargs": kwargs})
            or subprocess.CompletedProcess(args, 0)
        ),
    )

    setup.setup_resilience()

    args = captured["args"]
    assert args[args.index("--with-medic") + 1] == "diagnose"
    assert args[args.index("--medic-primary-public-key") + 1] == (
        "/operator-share/primary.pub"
    )
    assert args[args.index("--medic-instance-id") + 1] == "nooma-studio"
    assert "--medic-confirm-public-key" not in args
    assert "--sign" not in args
    assert not any(value.endswith(".key") for value in args)


def test_reinstall_uninstalls_from_manifest_before_repo_removal():
    source = REINSTALLER.read_text()
    uninstall = 'bash "$DEVBRAIN_HOME/scripts/install-resilience.sh" --uninstall --yes'
    remove_repo = 'rm -rf "$DEVBRAIN_HOME"'

    assert (
        'RESILIENCE_MANIFEST="$HOME/.devbrain/resilience/install-manifest.json"'
        in source
    )
    assert uninstall in source
    assert source.index(uninstall) < source.index(remove_repo)
    assert "from ops.resilience.install import _load_manifest" in source
    assert "devbrain_devbrain-wal-archive" in source
    assert 'INSTALL_TARGET_PATH="$HOME/.devbrain/install-target.json"' in source
    assert 'docker --context "$TARGET_DOCKER_CONTEXT"' in source
    assert "--container-runtime=RUNTIME --docker-context=CONTEXT" in source
    assert "resilience and install target metadata disagree" in source
    assert "Explicit container target disagrees with recorded metadata" in source
    assert "Could not verify the post-cleanup container inventory" in source
    assert "Could not remove the confirmed devbrain-db container" in source
    assert "label=com.docker.compose.volume=" in source
    assert ".Mounts" in source
    assert "compose down" not in source
    assert source.index("_capture_container_target") < source.index(
        uninstall
    )


def test_reinstall_help_exposes_safe_target_override_and_rejects_unknown_flags():
    help_result = subprocess.run(
        ["bash", str(REINSTALLER), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    unknown_result = subprocess.run(
        ["bash", str(REINSTALLER), "--unknown"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert help_result.returncode == 0
    assert "--container-runtime=RUNTIME" in help_result.stdout
    assert "--docker-context=CONTEXT" in help_result.stdout
    assert unknown_result.returncode == 2
    assert "unknown reinstall option" in unknown_result.stderr


def test_upgrade_refreshes_requirements_and_restarts_installed_resilience(
    monkeypatch, tmp_path
):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("cryptography>=42\n")
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("")
    installer = tmp_path / "scripts" / "install-resilience.sh"
    installer.parent.mkdir(parents=True)
    installer.write_text("#!/bin/bash\n")
    manifest = (
        tmp_path / ".devbrain" / "resilience" / "install-manifest.json"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")

    calls = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(config, "DEVBRAIN_HOME", tmp_path)
    monkeypatch.setattr(
        subprocess,
        "call",
        lambda args, **kwargs: calls.append((args, kwargs)) or 0,
    )
    monkeypatch.setattr(
        cli_module.devdoctor,
        "callback",
        lambda as_json, fix: None,
    )

    result = CliRunner().invoke(
        cli,
        [
            "upgrade",
            "--yes",
            "--no-pull",
            "--no-rebuild",
            "--no-rotate",
            "--no-tier",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-q",
                "-r",
                str(requirements),
            ],
            {"cwd": str(tmp_path)},
        ),
        (
            ["bash", str(installer), "--restart"],
            {"cwd": str(tmp_path)},
        ),
    ]


def test_recorded_db_context_resolution_is_exact(
    monkeypatch,
    tmp_path,
):
    target = tmp_path / ".devbrain" / "install-target.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "generated_by": "devbrain-installer",
                "profile": "studio",
                "container_runtime": "colima",
                "docker_context": "colima",
            }
        )
    )
    calls = []
    container_id = "a" * 64

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, f"{container_id}\n", "")

    monkeypatch.setattr(cli_module.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    assert cli_module._resolve_devbrain_docker_context() == "colima"
    assert calls == [
        [
            "docker",
            "--context",
            "colima",
            "container",
            "ls",
            "--all",
            "--no-trunc",
            "--filter",
            "name=^/devbrain-db$",
            "--format",
            "{{.ID}}",
        ]
    ]


def test_legacy_db_context_resolution_fails_if_any_context_is_unprobeable(
    monkeypatch,
    tmp_path,
):
    def fake_run(args, **kwargs):
        if args[:4] == ["docker", "context", "ls", "--format"]:
            return subprocess.CompletedProcess(args, 0, "default\nold-remote\n", "")
        if args[2] == "default":
            return subprocess.CompletedProcess(args, 0, f"{'a' * 64}\n", "")
        return subprocess.CompletedProcess(args, 1, "", "connection failed")

    monkeypatch.setattr(cli_module.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    try:
        cli_module._resolve_devbrain_docker_context()
    except click.ClickException as exc:
        assert "old-remote" in str(exc)
        assert "unavailable" in str(exc)
    else:
        raise AssertionError("an unprobeable legacy context must fail closed")


def test_db_context_resolution_rejects_nonregular_metadata(
    monkeypatch,
    tmp_path,
):
    target = tmp_path / ".devbrain" / "install-target.json"
    target.mkdir(parents=True)
    monkeypatch.setattr(cli_module.Path, "home", staticmethod(lambda: tmp_path))

    try:
        cli_module._resolve_devbrain_docker_context()
    except click.ClickException as exc:
        assert "not a regular file" in str(exc)
    else:
        raise AssertionError("non-regular target metadata must fail closed")


def test_legacy_db_context_resolution_rejects_ambiguous_matches(
    monkeypatch,
    tmp_path,
):
    def fake_run(args, **kwargs):
        if args[:4] == ["docker", "context", "ls", "--format"]:
            return subprocess.CompletedProcess(args, 0, "default\ncolima\n", "")
        container_id = "a" * 64 if args[2] == "default" else "b" * 64
        return subprocess.CompletedProcess(args, 0, f"{container_id}\n", "")

    monkeypatch.setattr(cli_module.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    try:
        cli_module._resolve_devbrain_docker_context()
    except click.ClickException as exc:
        assert "found in 2 distinct Docker backends" in str(exc)
    else:
        raise AssertionError("ambiguous legacy targets must fail closed")


def test_legacy_db_context_resolution_accepts_aliases_to_same_backend(
    monkeypatch,
    tmp_path,
):
    container_id = "c" * 64

    def fake_run(args, **kwargs):
        if args[:4] == ["docker", "context", "ls", "--format"]:
            return subprocess.CompletedProcess(
                args,
                0,
                "default\ndesktop-linux\n",
                "",
            )
        return subprocess.CompletedProcess(args, 0, f"{container_id}\n", "")

    monkeypatch.setattr(cli_module.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    assert cli_module._resolve_devbrain_docker_context() == "desktop-linux"


def test_legacy_db_context_resolution_probes_plus_named_context(
    monkeypatch,
    tmp_path,
):
    container_id = "d" * 64
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:4] == ["docker", "context", "ls", "--format"]:
            return subprocess.CompletedProcess(args, 0, "nooma+studio\n", "")
        return subprocess.CompletedProcess(args, 0, f"{container_id}\n", "")

    monkeypatch.setattr(cli_module.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    assert cli_module._resolve_devbrain_docker_context() == "nooma+studio"
    assert any(call[2] == "nooma+studio" for call in calls if len(call) > 2)


def test_password_recreation_source_pins_context_before_password_mutation():
    source = Path(cli_module.__file__).read_text()
    function = source.split("def rotate_db_password(", 1)[1]

    resolve = "_resolve_devbrain_docker_context() if recreate else None"
    mutate = "result = rotate_with_dependents("
    docker = 'docker = ["docker", "--context", docker_context]'
    assert function.index(resolve) < function.index(mutate)
    assert docker in function
    assert '[*docker, "compose", "down"]' in function
    assert '[*docker, "compose", "up", "-d", "devbrain-db"]' in function


def test_wrapper_updates_before_loading_upgrade_python():
    source = DEVBRAIN_WRAPPER.read_text()

    assert '"${1:-}" =~ ^(setup|upgrade)$' in source
    assert '"$_arg" == "--no-pull"' in source
    assert source.index("_auto_update") < source.index(
        'exec "$DEVBRAIN_DIR/.venv/bin/python"'
    )
