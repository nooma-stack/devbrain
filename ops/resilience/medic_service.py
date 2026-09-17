"""Connect the signed medic queue to the typed resilience runtime."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from cryptography.hazmat.primitives import serialization

from .core import (
    Monitor,
    MonitorConfig,
    load_config,
    read_heartbeat,
    run_check,
)
from .medic import MedicQueue, MedicResult
from .signing import AuthorizedKey, MedicAuthorizationError, load_public_key

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class MedicServiceConfig:
    source_path: Path
    instance_id: str
    mode: str
    queue_dir: Path
    poll_interval_seconds: float
    authorized_keys: Mapping[str, AuthorizedKey]
    allowed_heal_checks: frozenset[str]


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MedicAuthorizationError(f"{field} must be an object")
    return dict(value)


def _safe_name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
        raise MedicAuthorizationError(f"{field} contains unsupported characters")
    return value


def load_medic_config(
    path: str | Path,
    *,
    runtime_config: MonitorConfig,
) -> MedicServiceConfig:
    source = Path(path).expanduser().resolve()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MedicAuthorizationError(f"medic config not found: {source}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise MedicAuthorizationError(f"medic config is unreadable: {exc}") from exc
    config = _object(raw, "medic config")
    allowed_fields = {
        "schema_version",
        "instance_id",
        "mode",
        "queue_dir",
        "poll_interval_seconds",
        "keys",
        "allowed_heal_checks",
    }
    unknown = sorted(set(config) - allowed_fields)
    if unknown:
        raise MedicAuthorizationError(
            "medic config has unknown fields: " + ", ".join(unknown)
        )
    if config.get("schema_version") != 1:
        raise MedicAuthorizationError("medic config schema_version must be 1")
    instance_id = _safe_name(config.get("instance_id"), "instance_id")
    mode = config.get("mode")
    if mode not in {"diagnose", "heal"}:
        raise MedicAuthorizationError("medic mode must be diagnose or heal")
    queue_dir = Path(str(config.get("queue_dir", ""))).expanduser()
    if not queue_dir.is_absolute():
        raise MedicAuthorizationError("medic queue_dir must be absolute")
    queue_dir = queue_dir.resolve()
    interval = config.get("poll_interval_seconds", 10)
    if (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not 1 <= float(interval) <= 3600
    ):
        raise MedicAuthorizationError(
            "medic poll_interval_seconds must be between 1 and 3600"
        )

    keys_raw = config.get("keys")
    if not isinstance(keys_raw, list) or not keys_raw:
        raise MedicAuthorizationError("medic keys must be a non-empty array")
    keys: dict[str, AuthorizedKey] = {}
    public_material: set[bytes] = set()
    for index, raw_key in enumerate(keys_raw):
        key_obj = _object(raw_key, f"keys[{index}]")
        if set(key_obj) != {"key_id", "role", "public_key"}:
            raise MedicAuthorizationError(
                f"keys[{index}] must contain key_id, role, and public_key"
            )
        key_id = _safe_name(key_obj["key_id"], f"keys[{index}].key_id")
        if key_id in keys:
            raise MedicAuthorizationError(f"duplicate medic key_id: {key_id}")
        role = key_obj["role"]
        if role not in {"primary", "confirm"}:
            raise MedicAuthorizationError(
                f"keys[{index}].role must be primary or confirm"
            )
        public_key = load_public_key(key_obj["public_key"])
        raw_public = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        if raw_public in public_material:
            raise MedicAuthorizationError("medic roles must use distinct public keys")
        public_material.add(raw_public)
        keys[key_id] = AuthorizedKey(role=role, public_key=public_key)
    roles = {key.role for key in keys.values()}
    if "primary" not in roles:
        raise MedicAuthorizationError("medic requires a primary public key")
    if mode == "heal" and "confirm" not in roles:
        raise MedicAuthorizationError(
            "medic heal mode requires a distinct confirm public key"
        )

    allowed_raw = config.get("allowed_heal_checks", [])
    if not isinstance(allowed_raw, list):
        raise MedicAuthorizationError("allowed_heal_checks must be an array")
    allowed = frozenset(
        _safe_name(value, f"allowed_heal_checks[{index}]")
        for index, value in enumerate(allowed_raw)
    )
    if mode == "diagnose" and allowed:
        raise MedicAuthorizationError(
            "diagnose mode cannot declare allowed_heal_checks"
        )
    recoverable = {
        str(check["id"])
        for check in runtime_config.checks
        if check.get("enabled", True)
        and isinstance(check.get("settings", {}).get("recovery"), dict)
    }
    unknown_checks = sorted(allowed - recoverable)
    if unknown_checks:
        raise MedicAuthorizationError(
            "medic heal checks are not recoverable in the runtime config: "
            + ", ".join(unknown_checks)
        )
    return MedicServiceConfig(
        source_path=source,
        instance_id=instance_id,
        mode=mode,
        queue_dir=queue_dir,
        poll_interval_seconds=float(interval),
        authorized_keys=keys,
        allowed_heal_checks=allowed if mode == "heal" else frozenset(),
    )


class MedicService:
    """Bounded queue processor backed by read-only checks and typed remedies."""

    def __init__(
        self,
        runtime_config: MonitorConfig,
        medic_config: MedicServiceConfig,
        *,
        monitor: Monitor | None = None,
    ):
        self.runtime_config = runtime_config
        self.medic_config = medic_config
        self.monitor = monitor or Monitor(runtime_config)
        self.queue = MedicQueue(
            root=medic_config.queue_dir,
            instance_id=medic_config.instance_id,
            authorized_keys=medic_config.authorized_keys,
            allowed_heal_checks=medic_config.allowed_heal_checks,
            status_callback=self._status,
            diagnose_callback=self._diagnose,
            heal_callback=self._heal,
        )

    def _status(self) -> dict[str, Any]:
        try:
            return read_heartbeat(self.runtime_config)
        except RuntimeError as exc:
            return {"available": False, "detail": str(exc)}

    def _diagnose(self) -> dict[str, Any]:
        checks = {}
        for check in self.runtime_config.checks:
            if not check.get("enabled", True):
                continue
            result = run_check(self.runtime_config, check)
            checks[result.check_id] = result.as_dict()
        return {
            "instance_id": self.medic_config.instance_id,
            "checks": checks,
            "healthy": all(
                result["ok"]
                for check_id, result in checks.items()
                if next(
                    check
                    for check in self.runtime_config.checks
                    if check["id"] == check_id
                ).get("required", True)
            ),
        }

    def _heal(self, check_id: str) -> dict[str, Any]:
        return self.monitor.run_targeted_recovery(check_id).as_dict()

    def process_once(self) -> list[MedicResult]:
        return self.queue.process_once()


def create_medic_service(runtime_config_path: str | Path) -> MedicService | None:
    """Create the optional service declared by a runtime config."""

    runtime_config = load_config(runtime_config_path)
    if runtime_config.medic_config_path is None:
        return None
    medic_config = load_medic_config(
        runtime_config.medic_config_path,
        runtime_config=runtime_config,
    )
    return MedicService(runtime_config, medic_config)
