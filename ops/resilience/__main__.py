"""Command-line entrypoint for ``python -m ops.resilience``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from .core import ConfigError, Monitor, load_config, read_heartbeat

logger = logging.getLogger("devbrain.resilience")
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DevBrain local resilience monitor")
    parser.add_argument("--config", required=True, help="absolute path to JSON config")
    parser.add_argument("--verbose", action="store_true")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("run-once", help="run one check/recovery cycle")
    subcommands.add_parser("watch", help="run check/recovery cycles continuously")
    subcommands.add_parser("status", help="print the last local heartbeat")
    return parser


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def load_dotenv_no_override(path: Path) -> None:
    """Load simple KEY=VALUE entries without shell evaluation or overrides."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("cannot read environment file %s: %s", path, exc)
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if (
            not separator
            or not key
            or not key.replace("_", "a").isalnum()
            or key[0].isdigit()
            or key in os.environ
        ):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def watch_forever(
    monitor,
    medic,
    *,
    monotonic=time.monotonic,
    sleep=time.sleep,
    max_wakeups: int | None = None,
) -> None:
    """Run health and medic schedules independently in one bounded process."""

    now = monotonic()
    next_monitor = now
    next_medic = now if medic is not None else None
    wakeups = 0
    while True:
        now = monotonic()
        if now >= next_monitor:
            try:
                cycle = monitor.run_cycle()
                logger.info(
                    "cycle healthy=%s recoveries=%d webhook=%s",
                    cycle.heartbeat["healthy"],
                    len(cycle.heartbeat["recoveries"]),
                    cycle.webhook.detail,
                )
            except Exception:
                logger.exception("resilience health cycle failed")
            next_monitor = monotonic() + monitor.config.interval_seconds

        if medic is not None and next_medic is not None and monotonic() >= next_medic:
            try:
                medic_results = medic.process_once()
                if medic_results:
                    logger.info("medic tasks processed=%d", len(medic_results))
            except Exception:
                logger.exception("resilience medic cycle failed")
            next_medic = monotonic() + medic.medic_config.poll_interval_seconds

        wakeups += 1
        if max_wakeups is not None and wakeups >= max_wakeups:
            return
        deadlines = [next_monitor]
        if next_medic is not None:
            deadlines.append(next_medic)
        try:
            sleep(max(0.0, min(deadlines) - monotonic()))
        except KeyboardInterrupt:
            return


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # launchd/systemd intentionally receive no secret values in their service
    # definitions. Load the repository's mode-0600 .env at runtime, matching
    # bin/devbrain semantics while preserving caller-provided overrides.
    load_dotenv_no_override(_REPO_ROOT / ".env")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        logger.error("%s", exc)
        return 2

    if args.command == "status":
        try:
            heartbeat = read_heartbeat(config)
        except RuntimeError as exc:
            logger.error("%s", exc)
            return 2
        _print(heartbeat)
        return 0 if heartbeat.get("healthy") is True else 1

    monitor = Monitor(config)
    medic = None
    if config.medic_config_path is not None:
        try:
            from .medic_service import MedicService, load_medic_config

            medic = MedicService(
                config,
                load_medic_config(
                    config.medic_config_path,
                    runtime_config=config,
                ),
                monitor=monitor,
            )
        except (ConfigError, ValueError) as exc:
            logger.error("medic configuration is invalid: %s", exc)
            return 2
    if args.command == "run-once":
        cycle = monitor.run_cycle()
        medic_results = medic.process_once() if medic is not None else []
        output = dict(cycle.heartbeat)
        output["webhook"] = cycle.webhook.as_dict()
        output["medic_tasks_processed"] = len(medic_results)
        _print(output)
        return 0 if cycle.heartbeat["healthy"] else 1

    watch_forever(monitor, medic)
    return 0


if __name__ == "__main__":
    sys.exit(main())
