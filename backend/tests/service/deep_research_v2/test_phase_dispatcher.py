import importlib

import pytest


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("init", ["init"]),
        (
            "planning",
            ["planning", "researching", "analyzing", "writing", "reviewing"],
        ),
        ("researching", ["researching", "analyzing", "writing", "reviewing"]),
        ("analyzing", ["analyzing", "writing", "reviewing"]),
        ("writing", ["writing", "reviewing"]),
        ("reviewing", ["reviewing"]),
        ("re_researching", ["re_researching", "writing", "reviewing"]),
        ("revising", ["revising", "reviewing"]),
        ("awaiting_outline_approval", []),
        ("quality_gate_failed", []),
        ("completed", []),
    ],
)
def test_phases_to_run_starts_at_persisted_phase(phase, expected):
    dispatcher = importlib.import_module(
        "service.deep_research_v2.phase_dispatch"
    )

    assert dispatcher.phases_to_run(phase) == expected


def test_phases_to_run_rejects_unknown_phase():
    dispatcher = importlib.import_module(
        "service.deep_research_v2.phase_dispatch"
    )

    with pytest.raises(ValueError, match="Unknown research phase"):
        dispatcher.phases_to_run("mystery")
