"""Deterministic Discord -> Freebuff -> Cursor workflow bridge.

Transport and control are normal Python code. No LLM is involved in the
bridge itself: Hermes is the transport, Freebuff is the engineering
supervisor, and Cursor (`agent`) is the implementation worker.
"""

__version__ = "0.1.0"

# Canonical state machine (also defined in state.py; kept here for consumers
# that only want a stable constant).
STATES = (
    "IDLE",
    "TASK_READY",
    "FREEBUFF_STARTING",
    "FREEBUFF_RUNNING",
    "CURSOR_RUNNING",
    "REVIEWING",
    "REPAIR_REQUIRED",
    "DECISION_REQUIRED",
    "PASS",
    "BLOCKED",
    "STOPPED",
    "FAILED",
)
