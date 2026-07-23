"""Operator-side key and task tooling for the optional signed medic queue."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from .signing import (
    MedicAuthorizationError,
    encode_private_key,
    encode_public_key,
    generate_keypair,
    load_private_key,
    make_task,
    sign_task,
)


def _write_new(path: Path, value: str, *, mode: int) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        raise MedicAuthorizationError(f"refusing to overwrite existing file: {path}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            if not value.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _parse_signer(value: str) -> tuple[str, Path]:
    key_id, separator, raw_path = value.partition("=")
    if not separator or not key_id or not raw_path:
        raise argparse.ArgumentTypeError("signer must be KEY_ID=/path/to/private-key")
    return key_id, Path(raw_path).expanduser().resolve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DevBrain signed medic tooling")
    commands = parser.add_subparsers(dest="command", required=True)

    keygen = commands.add_parser(
        "keygen",
        help="generate an off-host Ed25519 signing keypair",
    )
    keygen.add_argument("--private", required=True)
    keygen.add_argument("--public", required=True)

    task = commands.add_parser("task", help="create a signed medic task envelope")
    task.add_argument("--instance-id", required=True)
    task.add_argument(
        "--action",
        required=True,
        choices=("status", "diagnose", "heal"),
    )
    task.add_argument("--check", help="required only for a heal action")
    task.add_argument("--ttl-seconds", type=int, default=300)
    task.add_argument(
        "--sign",
        action="append",
        required=True,
        type=_parse_signer,
        metavar="KEY_ID=PRIVATE_KEY_PATH",
    )
    task.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "keygen":
            private_raw, _public_raw = generate_keypair()
            private = load_private_key(private_raw)
            _write_new(
                Path(args.private),
                encode_private_key(private),
                mode=0o600,
            )
            _write_new(
                Path(args.public),
                encode_public_key(private.public_key()),
                mode=0o644,
            )
            print(f"private key: {Path(args.private).expanduser().resolve()}")
            print(f"public key:  {Path(args.public).expanduser().resolve()}")
            return 0

        if args.action == "heal":
            if not args.check:
                raise MedicAuthorizationError("--check is required for heal")
            action = {"type": "heal", "check": args.check}
        else:
            if args.check:
                raise MedicAuthorizationError("--check is valid only for heal")
            action = {"type": args.action}
        task = make_task(
            instance_id=args.instance_id,
            action=action,
            ttl=timedelta(seconds=args.ttl_seconds),
        )
        signatures = []
        for key_id, key_path in args.sign:
            private = load_private_key(key_path.read_text(encoding="utf-8").strip())
            signatures.append(sign_task(task, key_id=key_id, private_key=private))
        envelope = {"task": task, "signatures": signatures}
        _write_new(
            Path(args.output),
            json.dumps(envelope, sort_keys=True, indent=2),
            mode=0o600,
        )
        print(Path(args.output).expanduser().resolve())
        return 0
    except (MedicAuthorizationError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
