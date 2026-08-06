"""Entry point. Both of these work, from anywhere:

    python temp_agents/main.py
    python -m temp_agents.main
"""

import sys
import uuid
from pathlib import Path

# Before any project import. Running this file directly puts temp_agents/ on
# sys.path instead of the project root, so `config` and the temp_agents package
# are both invisible and the run dies on its first import. Adding the root makes
# the two ways of starting it equivalent.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import TEMP_AGENTS_DIR  # noqa: E402
from logging_config import get_logger, set_request_id, setup_logging  # noqa: E402
from temp_agents.agents import AGENT_A, AGENT_B, FILE_A, FILE_B  # noqa: E402

logger = get_logger(__name__)


def run() -> None:
    log_file = setup_logging(app_name="temp_agents")
    set_request_id(uuid.uuid4().hex[:8])

    TEMP_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    # Removed up front so the supervisor's check means something: a file left
    # over from an earlier run would pass while this run wrote nothing.
    for path in (FILE_A, FILE_B):
        path.unlink(missing_ok=True)

    logger.info("=" * 70)
    logger.info("temp_agents | %s=%s | %s=%s", AGENT_A.name, AGENT_A.backend, AGENT_B.name, AGENT_B.backend)
    logger.info("Log file: %s", log_file)
    logger.info("=" * 70)

    # Imported here so the supervisor's module-level logging lands in the file.
    from temp_agents.supervisor import temp_supervisor

    verdict = temp_supervisor()

    logger.info("=" * 70)
    logger.info("Supervisor verdict:\n%s", verdict.text if verdict.ok else verdict.error)
    for path in (FILE_A, FILE_B):
        logger.info(
            "%s: %s", path.name, repr(path.read_text(encoding="utf-8")) if path.exists() else "MISSING"
        )
    logger.info("=" * 70)


if __name__ == "__main__":
    run()
