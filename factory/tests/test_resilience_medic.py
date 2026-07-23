from __future__ import annotations

import copy
import fcntl
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from ops.resilience.medic import MedicQueue  # noqa: E402
from ops.resilience.medic_service import load_medic_config  # noqa: E402
from ops.resilience.medic_tools import main as medic_tools_main  # noqa: E402
from ops.resilience.signing import (  # noqa: E402
    AuthorizedKey,
    MedicAuthorizationError,
    ReplayLedger,
    encode_private_key,
    encode_public_key,
    generate_keypair,
    load_private_key,
    load_public_key,
    make_task,
    sign_task,
    verify_envelope,
)


def _keys():
    primary_raw, primary_public_raw = generate_keypair()
    confirm_raw, confirm_public_raw = generate_keypair()
    primary = load_private_key(primary_raw)
    confirm = load_private_key(confirm_raw)
    keys = {
        "operator-primary": AuthorizedKey("primary", primary.public_key()),
        "operator-confirm": AuthorizedKey("confirm", confirm.public_key()),
    }
    # Assert the generated public bytes are genuinely tied to the private keys.
    assert primary.public_key().public_bytes_raw() == primary_public_raw
    assert confirm.public_key().public_bytes_raw() == confirm_public_raw
    return primary, confirm, keys


def _envelope(task, *signatures):
    return {"task": task, "signatures": list(signatures)}


def test_diagnose_requires_one_valid_primary_signature():
    primary, _confirm, keys = _keys()
    task = make_task(instance_id="studio-a", action={"type": "diagnose"})
    envelope = _envelope(
        task,
        sign_task(task, key_id="operator-primary", private_key=primary),
    )

    verified = verify_envelope(
        envelope,
        authorized_keys=keys,
        instance_id="studio-a",
    )

    assert verified.task == task
    assert verified.signer_roles == {"primary"}


def test_heal_requires_distinct_primary_and_confirm_signatures():
    primary, confirm, keys = _keys()
    task = make_task(
        instance_id="studio-a",
        action={"type": "heal", "check": "database"},
    )
    primary_only = _envelope(
        task,
        sign_task(task, key_id="operator-primary", private_key=primary),
    )
    with pytest.raises(MedicAuthorizationError, match="confirm"):
        verify_envelope(
            primary_only,
            authorized_keys=keys,
            instance_id="studio-a",
        )

    both = _envelope(
        task,
        sign_task(task, key_id="operator-primary", private_key=primary),
        sign_task(task, key_id="operator-confirm", private_key=confirm),
    )
    assert verify_envelope(
        both,
        authorized_keys=keys,
        instance_id="studio-a",
    ).signer_roles == {"primary", "confirm"}


def test_private_key_encoding_is_rejected_at_public_key_boundary():
    primary, _confirm, _keys_by_role = _keys()

    with pytest.raises(MedicAuthorizationError, match="public-key encoding"):
        load_public_key(encode_private_key(primary))


def test_unsigned_role_change_cannot_reuse_primary_signature():
    primary, _confirm, keys = _keys()
    task = make_task(
        instance_id="studio-a",
        action={"type": "heal", "check": "database"},
    )
    primary_signature = sign_task(
        task,
        key_id="operator-primary",
        private_key=primary,
    )
    forged = copy.deepcopy(primary_signature)
    forged["key_id"] = "operator-confirm"

    with pytest.raises(MedicAuthorizationError, match="invalid signature"):
        verify_envelope(
            _envelope(task, primary_signature, forged),
            authorized_keys=keys,
            instance_id="studio-a",
        )


def test_confirmation_over_different_payload_is_rejected():
    primary, confirm, keys = _keys()
    database_task = make_task(
        instance_id="studio-a",
        action={"type": "heal", "check": "database"},
    )
    tunnel_task = dict(database_task)
    tunnel_task["action"] = {"type": "heal", "check": "tunnel"}

    envelope = _envelope(
        database_task,
        sign_task(
            database_task,
            key_id="operator-primary",
            private_key=primary,
        ),
        sign_task(
            tunnel_task,
            key_id="operator-confirm",
            private_key=confirm,
        ),
    )
    with pytest.raises(MedicAuthorizationError, match="invalid signature"):
        verify_envelope(
            envelope,
            authorized_keys=keys,
            instance_id="studio-a",
        )


def test_wrong_instance_and_expired_tasks_are_rejected():
    primary, _confirm, keys = _keys()
    issued = datetime(2026, 7, 23, tzinfo=timezone.utc)
    task = make_task(
        instance_id="studio-a",
        action={"type": "status"},
        ttl=timedelta(minutes=1),
        now=issued,
    )
    envelope = _envelope(
        task,
        sign_task(task, key_id="operator-primary", private_key=primary),
    )

    with pytest.raises(MedicAuthorizationError, match="different instance"):
        verify_envelope(
            envelope,
            authorized_keys=keys,
            instance_id="studio-b",
            now=issued,
        )
    with pytest.raises(MedicAuthorizationError, match="expired"):
        verify_envelope(
            envelope,
            authorized_keys=keys,
            instance_id="studio-a",
            now=issued + timedelta(minutes=2),
        )


def test_replay_ledger_fails_closed_on_malformed_expiry(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps({"prior-nonce": "not-a-time"}))
    ledger = ReplayLedger(ledger_path)

    with pytest.raises(MedicAuthorizationError, match="invalid expiry"):
        ledger.claim(
            "new-nonce",
            expires_at="2026-07-23T12:00:00Z",
            now=datetime(2026, 7, 23, 11, 0, tzinfo=timezone.utc),
        )

    assert json.loads(ledger_path.read_text()) == {"prior-nonce": "not-a-time"}


def test_queue_consumes_nonce_once_and_uses_only_allowlisted_heal(tmp_path):
    primary, confirm, keys = _keys()
    calls: list[str] = []
    queue = MedicQueue(
        root=tmp_path,
        instance_id="studio-a",
        authorized_keys=keys,
        allowed_heal_checks={"database"},
        status_callback=lambda: {"healthy": True},
        diagnose_callback=lambda: {"checks": []},
        heal_callback=lambda check: calls.append(check) or {"healed": check},
    )
    task = make_task(
        instance_id="studio-a",
        action={"type": "heal", "check": "database"},
    )
    envelope = _envelope(
        task,
        sign_task(task, key_id="operator-primary", private_key=primary),
        sign_task(task, key_id="operator-confirm", private_key=confirm),
    )
    source = queue.inbox / "task.json"
    source.write_text(json.dumps(envelope), encoding="utf-8")

    results = queue.process_once()

    assert calls == ["database"]
    assert results[0].status == "ok"
    assert (queue.outbox / f"{task['task_id']}.json").exists()

    (queue.inbox / "replay.json").write_text(
        json.dumps(envelope),
        encoding="utf-8",
    )
    assert queue.process_once() == []
    reason_files = list(queue.rejected.glob("*.reason.json"))
    assert any("already been used" in path.read_text() for path in reason_files)


def test_queue_rejects_heal_not_in_local_allowlist(tmp_path):
    primary, confirm, keys = _keys()
    queue = MedicQueue(
        root=tmp_path,
        instance_id="studio-a",
        authorized_keys=keys,
        allowed_heal_checks={"database"},
        status_callback=lambda: {},
        diagnose_callback=lambda: {},
        heal_callback=lambda check: pytest.fail(f"unexpected heal: {check}"),
    )
    task = make_task(
        instance_id="studio-a",
        action={"type": "heal", "check": "tunnel"},
    )
    envelope = _envelope(
        task,
        sign_task(task, key_id="operator-primary", private_key=primary),
        sign_task(task, key_id="operator-confirm", private_key=confirm),
    )
    (queue.inbox / "task.json").write_text(json.dumps(envelope), encoding="utf-8")

    assert queue.process_once() == []
    reason = next(queue.rejected.glob("*.reason.json")).read_text()
    assert "not locally allowlisted" in reason


def test_queue_rejects_nonregular_files_without_following_or_blocking(tmp_path):
    _primary, _confirm, keys = _keys()
    queue = MedicQueue(
        root=tmp_path,
        instance_id="studio-a",
        authorized_keys=keys,
        allowed_heal_checks=set(),
        status_callback=lambda: {},
        diagnose_callback=lambda: {},
        heal_callback=lambda check: {},
    )
    (queue.inbox / "zero.json").symlink_to("/dev/zero")

    assert queue.process_once() == []
    reason = next(queue.rejected.glob("*.reason.json")).read_text()
    assert "regular file" in reason or "Too many levels" in reason

    os.mkfifo(queue.inbox / "pipe.json")
    assert queue.process_once() == []
    reasons = [path.read_text() for path in queue.rejected.glob("*.reason.json")]
    assert any("regular file" in value for value in reasons)


def test_queue_recovers_processing_files_without_replaying_claimed_nonce(tmp_path):
    primary, _confirm, keys = _keys()
    calls: list[str] = []
    queue = MedicQueue(
        root=tmp_path,
        instance_id="studio-a",
        authorized_keys=keys,
        allowed_heal_checks=set(),
        status_callback=lambda: calls.append("status") or {"healthy": True},
        diagnose_callback=lambda: {},
        heal_callback=lambda check: {},
    )
    recoverable = make_task(
        instance_id="studio-a",
        action={"type": "status"},
    )
    envelope = _envelope(
        recoverable,
        sign_task(
            recoverable,
            key_id="operator-primary",
            private_key=primary,
        ),
    )
    (queue.processing / "before-claim.json").write_text(json.dumps(envelope))

    assert queue.process_once()[0].status == "ok"
    assert calls == ["status"]

    indeterminate = make_task(
        instance_id="studio-a",
        action={"type": "status"},
    )
    envelope = _envelope(
        indeterminate,
        sign_task(
            indeterminate,
            key_id="operator-primary",
            private_key=primary,
        ),
    )
    (queue.processing / "after-claim.json").write_text(json.dumps(envelope))
    queue.ledger.claim(
        indeterminate["nonce"],
        expires_at=indeterminate["expires_at"],
    )

    assert queue.process_once() == []
    assert calls == ["status"]
    reasons = [path.read_text() for path in queue.rejected.glob("*.reason.json")]
    assert any("already been used" in value for value in reasons)


def test_failed_typed_heal_has_failed_top_level_status(tmp_path):
    primary, confirm, keys = _keys()
    queue = MedicQueue(
        root=tmp_path,
        instance_id="studio-a",
        authorized_keys=keys,
        allowed_heal_checks={"database"},
        status_callback=lambda: {},
        diagnose_callback=lambda: {},
        heal_callback=lambda check: {"ok": False, "detail": "restart failed"},
    )
    task = make_task(
        instance_id="studio-a",
        action={"type": "heal", "check": "database"},
    )
    envelope = _envelope(
        task,
        sign_task(task, key_id="operator-primary", private_key=primary),
        sign_task(task, key_id="operator-confirm", private_key=confirm),
    )
    (queue.inbox / "failed-heal.json").write_text(json.dumps(envelope))

    result = queue.process_once()[0]
    payload = json.loads((queue.outbox / f"{task['task_id']}.json").read_text())

    assert result.status == "failed"
    assert payload["status"] == "failed"


def test_queue_processes_at_most_one_task_per_cycle(tmp_path):
    primary, _confirm, keys = _keys()
    queue = MedicQueue(
        root=tmp_path,
        instance_id="studio-a",
        authorized_keys=keys,
        allowed_heal_checks=set(),
        status_callback=lambda: {"healthy": True},
        diagnose_callback=lambda: {},
        heal_callback=lambda check: {},
    )
    for index in range(2):
        task = make_task(
            instance_id="studio-a",
            action={"type": "status"},
        )
        envelope = _envelope(
            task,
            sign_task(
                task,
                key_id="operator-primary",
                private_key=primary,
            ),
        )
        (queue.inbox / f"{index}.json").write_text(json.dumps(envelope))

    assert len(queue.process_once()) == 1
    assert len(list(queue.inbox.glob("*.json"))) == 1
    assert len(queue.process_once()) == 1
    assert not list(queue.inbox.glob("*.json"))


def test_queue_consumer_lock_prevents_concurrent_processing(tmp_path):
    primary, _confirm, keys = _keys()
    queue = MedicQueue(
        root=tmp_path,
        instance_id="studio-a",
        authorized_keys=keys,
        allowed_heal_checks=set(),
        status_callback=lambda: {"healthy": True},
        diagnose_callback=lambda: {},
        heal_callback=lambda check: {},
    )
    task = make_task(
        instance_id="studio-a",
        action={"type": "status"},
    )
    envelope = _envelope(
        task,
        sign_task(
            task,
            key_id="operator-primary",
            private_key=primary,
        ),
    )
    pending = queue.inbox / "pending.json"
    pending.write_text(json.dumps(envelope))

    lock_fd = os.open(queue.consumer_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert queue.process_once() == []
        assert pending.exists()
        assert not list(queue.processing.glob("*.json"))
        assert not list(queue.outbox.glob("*.json"))
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    assert queue.process_once()[0].status == "ok"


def test_operator_tool_generates_keys_and_two_signature_task(tmp_path):
    primary_private = tmp_path / "primary.key"
    primary_public = tmp_path / "primary.pub"
    confirm_private = tmp_path / "confirm.key"
    confirm_public = tmp_path / "confirm.pub"
    assert (
        medic_tools_main(
            [
                "keygen",
                "--private",
                str(primary_private),
                "--public",
                str(primary_public),
            ]
        )
        == 0
    )
    assert (
        medic_tools_main(
            [
                "keygen",
                "--private",
                str(confirm_private),
                "--public",
                str(confirm_public),
            ]
        )
        == 0
    )
    assert os.stat(primary_private).st_mode & 0o777 == 0o600
    assert primary_private.read_text().startswith(
        "devbrain-ed25519-private-v1:"
    )
    assert primary_public.read_text().startswith(
        "devbrain-ed25519-public-v1:"
    )

    envelope_path = tmp_path / "heal-task.json"
    assert (
        medic_tools_main(
            [
                "task",
                "--instance-id",
                "studio-a",
                "--action",
                "heal",
                "--check",
                "database",
                "--sign",
                f"operator-primary={primary_private}",
                "--sign",
                f"operator-confirm={confirm_private}",
                "--output",
                str(envelope_path),
            ]
        )
        == 0
    )
    envelope = json.loads(envelope_path.read_text())
    keys = {
        "operator-primary": AuthorizedKey(
            "primary",
            load_private_key(primary_private.read_text().strip()).public_key(),
        ),
        "operator-confirm": AuthorizedKey(
            "confirm",
            load_private_key(confirm_private.read_text().strip()).public_key(),
        ),
    }
    verified = verify_envelope(
        envelope,
        authorized_keys=keys,
        instance_id="studio-a",
    )
    assert verified.task["action"] == {"type": "heal", "check": "database"}


def _runtime_config(tmp_path, *, medic_path=None):
    from ops.resilience.core import load_config

    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "studio",
                "state_path": str(tmp_path / "state.json"),
                "heartbeat_path": str(tmp_path / "heartbeat.json"),
                "medic_config_path": str(medic_path) if medic_path else None,
                "checks": [
                    {
                        "id": "database",
                        "type": "docker_container",
                        "enabled": True,
                        "required": True,
                        "timeout_seconds": 5,
                        "settings": {
                            "container": "devbrain-db",
                            "recovery": {
                                "type": "docker_compose_up",
                                "project_dir": str(tmp_path),
                                "services": ["devbrain-db"],
                            },
                        },
                    }
                ],
                "heartbeat": None,
            }
        )
    )
    return load_config(config_path)


def test_medic_config_requires_two_distinct_keys_for_heal_mode(tmp_path):
    primary, _confirm, _keys_map = _keys()
    path = tmp_path / "medic.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "instance_id": "studio-a",
                "mode": "heal",
                "queue_dir": str(tmp_path / "queue"),
                "keys": [
                    {
                        "key_id": "operator-primary",
                        "role": "primary",
                        "public_key": encode_public_key(primary.public_key()),
                    }
                ],
                "allowed_heal_checks": ["database"],
            }
        )
    )
    with pytest.raises(MedicAuthorizationError, match="confirm"):
        load_medic_config(
            path,
            runtime_config=_runtime_config(tmp_path, medic_path=path),
        )


def test_medic_config_binds_heals_to_runtime_recovery_allowlist(tmp_path):
    primary, confirm, _keys_map = _keys()
    path = tmp_path / "medic.json"
    payload = {
        "schema_version": 1,
        "instance_id": "studio-a",
        "mode": "heal",
        "queue_dir": str(tmp_path / "queue"),
        "keys": [
            {
                "key_id": "operator-primary",
                "role": "primary",
                "public_key": encode_public_key(primary.public_key()),
            },
            {
                "key_id": "operator-confirm",
                "role": "confirm",
                "public_key": encode_public_key(confirm.public_key()),
            },
        ],
        "allowed_heal_checks": ["tunnel"],
    }
    path.write_text(json.dumps(payload))
    with pytest.raises(MedicAuthorizationError, match="not recoverable"):
        load_medic_config(
            path,
            runtime_config=_runtime_config(tmp_path, medic_path=path),
        )

    payload["allowed_heal_checks"] = ["database"]
    path.write_text(json.dumps(payload))
    loaded = load_medic_config(
        path,
        runtime_config=_runtime_config(tmp_path, medic_path=path),
    )
    assert loaded.mode == "heal"
    assert loaded.allowed_heal_checks == {"database"}
