"""Agent transports: one module per way of reaching a coding-agent CLI.

`base` defines the contract and dispatches on config.INVOCATION. The rest
register themselves with it on import.
"""

from adapters.base import (
    ERROR_KINDS,
    AgentResult,
    available_backends,
    classify_failure,
    register_backend,
    resolve_binary,
    run_agent,
)

__all__ = [
    "ERROR_KINDS",
    "AgentResult",
    "available_backends",
    "classify_failure",
    "register_backend",
    "resolve_binary",
    "run_agent",
]
