"""A bounded, signed queue for optional DevBrain recovery requests.

This module deliberately has no arbitrary command, shell, reboot, or config
write action.  A heal request may name only a check already declared by the
local resilience configuration; the watchdog's typed recovery executor remains
the sole authority that can perform the action.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .signing import (
    AuthorizedKey,
    MedicAuthorizationError,
    ReplayLedger,
    verify_envelope,
)

MAX_TASK_BYTES = 64 * 1024
MAX_TASKS_PER_CYCLE = 1


@dataclass(frozen=True)
class MedicResult:
    task_id: str
    action: str
    status: str
    detail: Any


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


class MedicQueue:
    """Process a filesystem inbox containing signed JSON task envelopes."""

    def __init__(
        self,
        *,
        root: str | Path,
        instance_id: str,
        authorized_keys: Mapping[str, AuthorizedKey],
        allowed_heal_checks: set[str] | frozenset[str],
        status_callback: Callable[[], Any],
        diagnose_callback: Callable[[], Any],
        heal_callback: Callable[[str], Any],
        max_task_bytes: int = MAX_TASK_BYTES,
        max_tasks_per_cycle: int = MAX_TASKS_PER_CYCLE,
    ):
        self.root = Path(root).expanduser().resolve()
        self.instance_id = instance_id
        self.authorized_keys = authorized_keys
        self.allowed_heal_checks = frozenset(allowed_heal_checks)
        self.status_callback = status_callback
        self.diagnose_callback = diagnose_callback
        self.heal_callback = heal_callback
        self.max_task_bytes = max_task_bytes
        self.max_tasks_per_cycle = max_tasks_per_cycle
        self.inbox = self.root / "inbox"
        self.processing = self.root / "processing"
        self.outbox = self.root / "outbox"
        self.archive = self.root / "archive"
        self.rejected = self.root / "rejected"
        self.ledger = ReplayLedger(self.root / "replay-ledger.json")
        for path in (
            self.root,
            self.inbox,
            self.processing,
            self.outbox,
            self.archive,
            self.rejected,
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        self.consumer_lock_path = self.root / "consumer.lock"

    def _result_payload(
        self,
        *,
        task_id: str,
        action: str,
        status: str,
        detail: Any,
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "instance_id": self.instance_id,
            "task_id": task_id,
            "action": action,
            "status": status,
            "completed_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "detail": detail,
        }

    def _reject(self, claimed: Path, reason: str) -> None:
        destination = self.rejected / claimed.name
        if destination.exists() or destination.is_symlink():
            destination = self.rejected / (
                f"{claimed.stem}.{uuid.uuid4()}{claimed.suffix}"
            )
        os.replace(claimed, destination)
        _atomic_json(
            destination.with_suffix(destination.suffix + ".reason.json"),
            {
                "status": "rejected",
                "reason": reason,
                "rejected_at": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            },
        )

    def _execute(self, task: Mapping[str, Any]) -> MedicResult:
        action = task["action"]
        action_type = action["type"]
        if action_type == "status":
            detail = self.status_callback()
        elif action_type == "diagnose":
            # "Diagnose" means a fresh typed health snapshot.  It does not
            # invoke an AI CLI or grant filesystem/process execution.
            detail = self.diagnose_callback()
        elif action_type == "heal":
            check = action["check"]
            if check not in self.allowed_heal_checks:
                raise MedicAuthorizationError(
                    f"heal check is not locally allowlisted: {check}"
                )
            detail = self.heal_callback(check)
        else:  # verify_envelope rejects this; defensive for future versions.
            raise MedicAuthorizationError("unsupported medic action")
        status = (
            "failed"
            if action_type == "heal"
            and isinstance(detail, Mapping)
            and detail.get("ok") is False
            else "ok"
        )
        return MedicResult(
            task_id=task["task_id"],
            action=action_type,
            status=status,
            detail=detail,
        )

    def _load_envelope(self, path: Path) -> Any:
        """Read one regular file through a single bounded, no-follow fd."""

        flags = os.O_RDONLY
        flags |= getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise MedicAuthorizationError("task must be a regular file")
            if metadata.st_size > self.max_task_bytes:
                raise MedicAuthorizationError("task exceeds maximum size")
            chunks: list[bytes] = []
            remaining = self.max_task_bytes + 1
            while remaining:
                chunk = os.read(fd, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            if len(content) > self.max_task_bytes:
                raise MedicAuthorizationError("task exceeds maximum size")
            try:
                return json.loads(content.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise MedicAuthorizationError("task is not valid UTF-8") from exc
        finally:
            os.close(fd)

    def _try_consumer_lock(self) -> int | None:
        """Take the queue-wide consumer lock without waiting."""

        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.consumer_lock_path, flags, 0o600)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise MedicAuthorizationError(
                    "medic consumer lock must be a regular file"
                )
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return None
            return fd
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    def process_once(self) -> list[MedicResult]:
        """Recover or claim and process a bounded number of task files.

        Files already in ``processing`` are handled first after a restart. If
        their nonce was durably claimed but no result exists, they are
        quarantined as indeterminate and are never executed a second time. A
        queue-wide nonblocking lock prevents a foreground ``run-once`` from
        racing the daemon consumer.
        """

        lock_fd = self._try_consumer_lock()
        if lock_fd is None:
            return []
        try:
            return self._process_locked()
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def _process_locked(self) -> list[MedicResult]:
        results: list[MedicResult] = []
        processing = [(path, True) for path in sorted(self.processing.glob("*.json"))]
        inbox = [(path, False) for path in sorted(self.inbox.glob("*.json"))]
        candidates = (processing + inbox)[: self.max_tasks_per_cycle]
        for source, was_processing in candidates:
            claimed = source if was_processing else self.processing / source.name
            if not was_processing:
                if claimed.exists() or claimed.is_symlink():
                    self._reject(source, "processing filename collision")
                    continue
                try:
                    os.replace(source, claimed)
                except FileNotFoundError:
                    continue
            try:
                envelope = self._load_envelope(claimed)
                verified = verify_envelope(
                    envelope,
                    authorized_keys=self.authorized_keys,
                    instance_id=self.instance_id,
                )
                task = verified.task
                result_path = self.outbox / f"{task['task_id']}.json"
                archive_path = self.archive / f"{task['task_id']}.json"
                if result_path.exists() or result_path.is_symlink():
                    if was_processing:
                        if archive_path.exists() or archive_path.is_symlink():
                            raise MedicAuthorizationError(
                                "task_id already has archive and outbox records"
                            )
                        os.replace(claimed, archive_path)
                        continue
                self.ledger.claim(
                    task["nonce"],
                    expires_at=task["expires_at"],
                )
                if (
                    result_path.exists()
                    or result_path.is_symlink()
                    or archive_path.exists()
                    or archive_path.is_symlink()
                ):
                    raise MedicAuthorizationError("task_id already has an audit record")
                result = self._execute(task)
                payload = self._result_payload(
                    task_id=result.task_id,
                    action=result.action,
                    status=result.status,
                    detail=result.detail,
                )
                _atomic_json(result_path, payload)
                os.replace(claimed, archive_path)
                results.append(result)
            except (
                MedicAuthorizationError,
                json.JSONDecodeError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as exc:
                if claimed.exists() or claimed.is_symlink():
                    self._reject(claimed, str(exc))
        return results
