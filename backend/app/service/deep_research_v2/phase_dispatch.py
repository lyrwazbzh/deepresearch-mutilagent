"""Pure phase-dispatch decisions for resumable Deep Research V2 runs."""

from typing import List


_STANDARD_PIPELINE = [
    "planning",
    "researching",
    "analyzing",
    "writing",
    "reviewing",
]


def phases_to_run(phase: str) -> List[str]:
    """Return the ordered work remaining from a persisted phase."""
    if phase == "init":
        return ["init"]
    if phase in _STANDARD_PIPELINE:
        return _STANDARD_PIPELINE[_STANDARD_PIPELINE.index(phase):]
    if phase == "re_researching":
        return ["re_researching", "writing", "reviewing"]
    if phase == "revising":
        return ["revising", "reviewing"]
    if phase in {"awaiting_outline_approval", "quality_gate_failed", "completed"}:
        return []
    raise ValueError(f"Unknown research phase: {phase}")
