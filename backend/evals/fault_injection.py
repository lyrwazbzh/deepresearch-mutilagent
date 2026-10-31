"""Reusable fault scenarios for measuring dependency recovery rates."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FaultScenario:
    name: str
    failures_before_success: int
    expected_recovery: bool


DEFAULT_SCENARIOS = [
    FaultScenario("transient_timeout", 1, True),
    FaultScenario("rate_limit_burst", 2, True),
    FaultScenario("persistent_provider_failure", 100, False),
]


async def recovery_rate(execute, scenarios: list[FaultScenario] = DEFAULT_SCENARIOS) -> float:
    recovered = 0
    recoverable = 0
    for scenario in scenarios:
        if not scenario.expected_recovery:
            continue
        recoverable += 1
        try:
            await execute(scenario)
        except Exception:
            continue
        recovered += 1
    return recovered / recoverable if recoverable else 1.0
