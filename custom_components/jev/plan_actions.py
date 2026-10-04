"""Allowed plan actions and their typed parameters, independent of planning.

The registry is deliberately explicit. A model selects one of these actions;
it cannot supply a service name or unrecognised Home Assistant slot.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .interpret import ACTIONS
from .snapshot import ExposedEntity


@dataclass(frozen=True, slots=True)
class IntegerParameter:
    """A required whole-number parameter with an inclusive range."""

    name: str
    minimum: int
    maximum: int

    def accepts(self, value: object) -> bool:
        """Do not coerce strings, floats or booleans into executable values."""
        return type(value) is int and self.minimum <= value <= self.maximum


@dataclass(frozen=True, slots=True)
class ActionDefinition:
    """An intent, the targets it accepts and the parameters it requires."""

    intent_type: str
    description: str
    domains: frozenset[str]
    states: frozenset[str]
    parameters: tuple[IntegerParameter, ...] = ()

    def build_slots(
        self, target: ExposedEntity, supplied: Mapping[str, object]
    ) -> dict[str, Any] | None:
        """Reject the complete step if a target or parameter is unsupported."""
        if target.domain not in self.domains or target.state not in self.states:
            return None
        if set(supplied) != {p.name for p in self.parameters}:
            return None
        if any(not p.accepts(supplied[p.name]) for p in self.parameters):
            return None
        slots = {
            "name": {"value": target.slot_name},
            "domain": {"value": [target.domain]},
        }
        if target.area is not None:
            slots["area"] = {"value": target.area}
        for parameter in self.parameters:
            slots[parameter.name] = {"value": supplied[parameter.name]}
        return slots


# This first refactor preserves the existing experimental scope. Additional
# actions can be introduced individually without changing the plan executor.
COMPOUND_ACTIONS: dict[str, ActionDefinition] = {
    action: ActionDefinition(
        intent_type=ACTIONS[action],
        description=f"Switch this instruction's light {state} now",
        domains=frozenset({"light"}),
        states=frozenset({"on", "off"}),
    )
    for action, state in (("turn_on", "on"), ("turn_off", "off"))
}
