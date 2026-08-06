"""Entry point: run a request through the hierarchical agent teams."""

import uuid

from config import SUPER_GRAPH_RECURSION_LIMIT, WORKSPACE_DIR
from logging_config import get_logger, set_request_id, setup_logging
from prompts import DEFAULT_REQUEST

logger = get_logger(__name__)


def run(request: str = DEFAULT_REQUEST) -> None:
    log_file = setup_logging(app_name="agent_run")
    set_request_id(uuid.uuid4().hex[:8])

    # Imported after setup_logging so module-level graph construction is logged.
    from teams.orchestrator import super_graph

    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("=" * 70)
    logger.info("Run started | workspace=%s", WORKSPACE_DIR)
    logger.info("Request: %s", request)
    logger.info("Log file: %s", log_file)
    logger.info("=" * 70)

    steps = 0
    try:
        for step in super_graph.stream(
            {"messages": [{"role": "user", "content": request}]},
            {"recursion_limit": SUPER_GRAPH_RECURSION_LIMIT},
        ):
            steps += 1
            for node, value in step.items():
                logger.info("[step %d] node=%s -> %s", steps, node, _summarize(value))
            logger.debug("[step %d] raw payload: %s", steps, step)
    except Exception:
        logger.exception("Run failed after %d step(s)", steps)
        raise
    finally:
        produced = sorted(p.name for p in WORKSPACE_DIR.glob("*")) if WORKSPACE_DIR.is_dir() else []
        logger.info("=" * 70)
        logger.info("Run finished | steps=%d | documents=%s", steps, produced or "none")
        logger.info("Full log: %s", log_file)
        logger.info("=" * 70)


def _summarize(value) -> str:
    """One-line view of a node's output for the console."""
    if not isinstance(value, dict):
        return "FINISH" if value is None else str(value)
    if "next" in value:
        return f"routing to {value['next']}"
    messages = value.get("messages") or []
    if messages:
        content = (messages[-1].content or "").replace("\n", " ")
        return f"reported: {content[:160]}"
    return str(value)


if __name__ == "__main__":
    run()
