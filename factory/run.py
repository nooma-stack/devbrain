#!/usr/bin/env python3
"""Factory runner — entry point for executing a queued job.

Called by the MCP server when factory_plan creates a new job.
Can also be run manually: python factory/run.py <job_id>
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Add parent dir to path so we can import from factory package
sys.path.insert(0, str(Path(__file__).parent))

from orchestrator import FactoryOrchestrator  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent.parent / "logs" / "factory.log"),
    ],
)
logger = logging.getLogger("devbrain.factory")

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python factory/run.py <job_id>")
        sys.exit(1)

    job_id = sys.argv[1]
    logger.info("Factory runner starting for job %s", job_id[:8])

    orchestrator = FactoryOrchestrator(DATABASE_URL)
    try:
        job = orchestrator.run_job(job_id)
        logger.info("Job %s finished with status: %s", job_id[:8], job.status.value)
    except Exception as exc:
        logger.exception("Factory runner failed for job %s: %s", job_id[:8], exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
