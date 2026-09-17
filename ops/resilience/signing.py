"""Signing primitives for the optional DevBrain medic queue.

The host stores public keys only.  Every signature covers one canonical task
body; confirmations are additional signatures over that *same* body.  This
avoids the common (and dangerous) pattern of trusting unsigned signer metadata
or comparing only a subset of fields between two independently signed blobs.
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

TASK_FIELDS = {
    "version",
    "task_id",
    "instance_id",
    "issued_at",
    "expires_at",
    "nonce",
    "action",
}
SIGNATURE_FIELDS = {"key_id", "signature"}
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DEFAULT_MAX_TTL = timedelta(minutes=15)
DEFAULT_CLOCK_SKEW = timedelta(seconds=60)
PUBLIC_KEY_PREFIX = "devbrain-ed25519-public-v1:"
PRIVATE_KEY_PREFIX = "devbrain-ed25519-private-v1:"


class MedicAuthorizationError(ValueError):
    """Raised when a medic task is malformed or unauthorized."""


@dataclass(frozen=True)
class AuthorizedKey:
    """One authorized public key and the role it may satisfy."""

    role: str
    public_key: Ed25519PublicKey


@dataclass(frozen=True)
class VerifiedTask:
    """A validated task plus the key roles that signed it."""

    task: dict[str, Any]
    signer_roles: frozenset[str]


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str, *, field: str) -> bytes:
    if not isinstance(value, str):
        raise MedicAuthorizationError(f"{field} must be a string")
    try:
        return base64.b64decode(
            value + ("=" * (-len(value) % 4)),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as exc:
        raise MedicAuthorizationError(f"{field} is not valid base64url") from exc


def _parse_time(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise MedicAuthorizationError(f"{field} must be an RFC3339 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MedicAuthorizationError(f"{field} must be an RFC3339 string") from exc
    if parsed.tzinfo is None:
        raise MedicAuthorizationError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def canonical_task_bytes(task: Mapping[str, Any]) -> bytes:
    """Return the single byte representation every signer must sign."""

    return json.dumps(
        task,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def generate_keypair() -> tuple[bytes, bytes]:
    """Generate raw Ed25519 private/public key bytes."""

    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_raw, public_raw


def load_private_key(raw: bytes | str) -> Ed25519PrivateKey:
    """Load raw bytes or a type-tagged Ed25519 private-key string."""

    if isinstance(raw, str):
        if not raw.startswith(PRIVATE_KEY_PREFIX):
            raise MedicAuthorizationError(
                "private key must use the DevBrain private-key encoding"
            )
        decoded = _b64decode(
            raw[len(PRIVATE_KEY_PREFIX) :],
            field="private key",
        )
    else:
        decoded = raw
    try:
        return Ed25519PrivateKey.from_private_bytes(decoded)
    except ValueError as exc:
        raise MedicAuthorizationError("invalid Ed25519 private key") from exc


def load_public_key(raw: bytes | str) -> Ed25519PublicKey:
    """Load raw bytes or a type-tagged Ed25519 public-key string."""

    if isinstance(raw, str):
        if not raw.startswith(PUBLIC_KEY_PREFIX):
            raise MedicAuthorizationError(
                "public key must use the DevBrain public-key encoding"
            )
        decoded = _b64decode(
            raw[len(PUBLIC_KEY_PREFIX) :],
            field="public key",
        )
    else:
        decoded = raw
    try:
        return Ed25519PublicKey.from_public_bytes(decoded)
    except ValueError as exc:
        raise MedicAuthorizationError("invalid Ed25519 public key") from exc


def encode_public_key(key: Ed25519PublicKey) -> str:
    """Encode a public key for a JSON configuration file."""

    raw = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return PUBLIC_KEY_PREFIX + _b64encode(raw)


def encode_private_key(key: Ed25519PrivateKey) -> str:
    """Encode a private key for an operator-owned mode-0600 file."""

    raw = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return PRIVATE_KEY_PREFIX + _b64encode(raw)


def sign_task(
    task: Mapping[str, Any],
    *,
    key_id: str,
    private_key: Ed25519PrivateKey,
) -> dict[str, str]:
    """Create one signature entry for an envelope."""

    if not SAFE_NAME.fullmatch(key_id):
        raise MedicAuthorizationError("key_id contains unsupported characters")
    signature = private_key.sign(canonical_task_bytes(task))
    return {"key_id": key_id, "signature": _b64encode(signature)}


def make_task(
    *,
    instance_id: str,
    action: Mapping[str, Any],
    ttl: timedelta = timedelta(minutes=5),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a new canonical task body for an off-host signer."""

    if ttl <= timedelta(0) or ttl > DEFAULT_MAX_TTL:
        raise MedicAuthorizationError(
            "task ttl must be between 1 second and 15 minutes"
        )
    if not SAFE_NAME.fullmatch(instance_id):
        raise MedicAuthorizationError("instance_id contains unsupported characters")
    issued = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "version": 1,
        "task_id": str(uuid.uuid4()),
        "instance_id": instance_id,
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued + ttl).isoformat().replace("+00:00", "Z"),
        "nonce": _b64encode(os.urandom(24)),
        "action": dict(action),
    }


def _validate_action(action: Any) -> tuple[str, set[str]]:
    if not isinstance(action, dict):
        raise MedicAuthorizationError("action must be an object")
    action_type = action.get("type")
    if action_type in {"status", "diagnose"}:
        if set(action) != {"type"}:
            raise MedicAuthorizationError(
                f"{action_type} action has unsupported fields"
            )
        return action_type, {"primary"}
    if action_type == "heal":
        if set(action) != {"type", "check"}:
            raise MedicAuthorizationError("heal action requires only type and check")
        check = action.get("check")
        if not isinstance(check, str) or not SAFE_NAME.fullmatch(check):
            raise MedicAuthorizationError("heal check has an invalid name")
        return action_type, {"primary", "confirm"}
    raise MedicAuthorizationError("unsupported medic action type")


def verify_envelope(
    envelope: Mapping[str, Any],
    *,
    authorized_keys: Mapping[str, AuthorizedKey],
    instance_id: str,
    now: datetime | None = None,
    max_ttl: timedelta = DEFAULT_MAX_TTL,
    clock_skew: timedelta = DEFAULT_CLOCK_SKEW,
) -> VerifiedTask:
    """Validate structure, lifetime, instance binding, and every signature."""

    if not isinstance(envelope, dict) or set(envelope) != {"task", "signatures"}:
        raise MedicAuthorizationError("envelope must contain only task and signatures")
    task = envelope.get("task")
    signatures = envelope.get("signatures")
    if not isinstance(task, dict) or set(task) != TASK_FIELDS:
        raise MedicAuthorizationError("task fields do not match the version 1 schema")
    if task.get("version") != 1:
        raise MedicAuthorizationError("unsupported task version")
    for field in ("task_id", "instance_id", "nonce"):
        value = task.get(field)
        if not isinstance(value, str) or not value:
            raise MedicAuthorizationError(f"{field} must be a non-empty string")
    try:
        uuid.UUID(task["task_id"])
    except (ValueError, AttributeError) as exc:
        raise MedicAuthorizationError("task_id must be a UUID") from exc
    if task["instance_id"] != instance_id:
        raise MedicAuthorizationError("task targets a different instance")
    if not SAFE_NAME.fullmatch(task["instance_id"]):
        raise MedicAuthorizationError("instance_id contains unsupported characters")
    nonce = _b64decode(task["nonce"], field="nonce")
    if len(nonce) < 16:
        raise MedicAuthorizationError("nonce must contain at least 128 bits")

    issued = _parse_time(task.get("issued_at"), field="issued_at")
    expires = _parse_time(task.get("expires_at"), field="expires_at")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if expires <= issued:
        raise MedicAuthorizationError("expires_at must be after issued_at")
    if expires - issued > max_ttl:
        raise MedicAuthorizationError("task lifetime exceeds the configured maximum")
    if issued > current + clock_skew:
        raise MedicAuthorizationError("task was issued too far in the future")
    if current >= expires:
        raise MedicAuthorizationError("task has expired")

    _action_type, required_roles = _validate_action(task.get("action"))
    if not isinstance(signatures, list) or not signatures:
        raise MedicAuthorizationError("signatures must be a non-empty list")

    signed_bytes = canonical_task_bytes(task)
    signer_roles: set[str] = set()
    signer_keys: set[bytes] = set()
    seen_key_ids: set[str] = set()
    for entry in signatures:
        if not isinstance(entry, dict) or set(entry) != SIGNATURE_FIELDS:
            raise MedicAuthorizationError("signature entry fields are invalid")
        key_id = entry.get("key_id")
        if not isinstance(key_id, str) or not SAFE_NAME.fullmatch(key_id):
            raise MedicAuthorizationError("signature key_id is invalid")
        if key_id in seen_key_ids:
            raise MedicAuthorizationError("duplicate signature key_id")
        seen_key_ids.add(key_id)
        authorized = authorized_keys.get(key_id)
        if authorized is None:
            raise MedicAuthorizationError(f"unknown signing key: {key_id}")
        signature = _b64decode(entry.get("signature"), field="signature")
        try:
            authorized.public_key.verify(signature, signed_bytes)
        except InvalidSignature as exc:
            raise MedicAuthorizationError(
                f"invalid signature for key: {key_id}"
            ) from exc
        raw_public = authorized.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        if raw_public in signer_keys:
            raise MedicAuthorizationError(
                "one public key cannot satisfy multiple roles"
            )
        signer_keys.add(raw_public)
        signer_roles.add(authorized.role)

    missing = required_roles - signer_roles
    if missing:
        raise MedicAuthorizationError(
            "missing required signature role(s): " + ", ".join(sorted(missing))
        )
    return VerifiedTask(task=dict(task), signer_roles=frozenset(signer_roles))


class ReplayLedger:
    """Small persistent nonce ledger with atomic replace semantics."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MedicAuthorizationError("replay ledger is unreadable") from exc
        if not isinstance(data, dict):
            raise MedicAuthorizationError("replay ledger has an invalid format")
        return {str(key): str(value) for key, value in data.items()}

    def claim(
        self,
        nonce: str,
        *,
        expires_at: str,
        now: datetime | None = None,
    ) -> None:
        """Persist a nonce before execution; raise when it was already claimed."""

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+", encoding="utf-8") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
            ledger = self._load()
            retained: dict[str, str] = {}
            for key, expiry in ledger.items():
                try:
                    if _parse_time(expiry, field="ledger expiry") > current:
                        retained[key] = expiry
                except MedicAuthorizationError as exc:
                    raise MedicAuthorizationError(
                        "replay ledger contains an invalid expiry"
                    ) from exc
            if nonce in retained:
                raise MedicAuthorizationError("task nonce has already been used")
            retained[nonce] = expires_at
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                dir=str(self.path.parent),
            )
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(retained, handle, sort_keys=True, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, self.path)
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
