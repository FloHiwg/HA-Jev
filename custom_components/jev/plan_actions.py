"""Allowed plan actions and their typed parameters, independent of planning.

The registry is deliberately explicit. A model selects one of these actions;
it cannot supply a service name or unrecognised Home Assistant slot.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .interpret import ACTIONS, find_brightness_values
from .snapshot import ExposedEntity


@dataclass(frozen=True, slots=True)
class IntegerParameter:
    """A required whole-number parameter with an inclusive range."""

    name: str
    minimum: int
    maximum: int
    description: str = "An exact whole-number value"
    unit: str = ""
    extractor: Callable[[str], tuple[int, ...]] | None = None

    def candidate_values(self, text: str) -> tuple[int, ...]:
        """Only offer exact in-range values extracted from the original command."""
        if self.extractor is None:
            return ()
        return tuple(v for v in self.extractor(text) if self.accepts(v))

    def accepts(self, value: object) -> bool:
        """Do not coerce strings, floats or booleans into executable values."""
        return type(value) is int and self.minimum <= value <= self.maximum


@dataclass(frozen=True, slots=True)
class ActionDefinition:
    """An intent, the targets it accepts and the parameters it requires."""

    intent_type: str
    description: str
    domains: frozenset[str]
    states: frozenset[str] | None
    parameters: tuple[IntegerParameter, ...] = ()
    service: str | None = None
    service_overrides: tuple[tuple[str, str], ...] = ()
    required_capabilities: frozenset[str] = frozenset()

    def service_for(self, domain: str) -> str | None:
        """The service the built-in intent needs, or None for a read-only action."""
        return dict(self.service_overrides).get(domain, self.service)

    def accepts_target(self, target: ExposedEntity) -> bool:
        """Target eligibility is independent of required parameter values."""
        return (
            target.domain in self.domains
            and self.required_capabilities <= target.capabilities
            and target.state not in ("unknown", "unavailable", "")
            and (self.states is None or target.state in self.states)
        )

    def build_slots(
        self, target: ExposedEntity, supplied: Mapping[str, object]
    ) -> dict[str, Any] | None:
        """Reject the complete step if a target or parameter is unsupported."""
        if not self.accepts_target(target):
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


# Keep this catalogue within the existing Assist snapshot boundary. Locks and
# entrance covers never enter that snapshot. Vacuums have start/stop services,
# not the turn_on/off services these intents need. Scenes can only be activated.
_POWER_DOMAINS = frozenset(
    {
        "light",
        "switch",
        "fan",
        "cover",
        "media_player",
        "climate",
        "input_boolean",
        "script",
    }
)
COMPOUND_ACTIONS: dict[str, ActionDefinition] = {
    "turn_on": ActionDefinition(
        intent_type=ACTIONS["turn_on"],
        description="Switch this instruction's device on, fully open a cover, "
        "or activate a script or scene now. No brightness requested; use "
        "set_brightness if a percentage is specified. Not playback or a vacuum start.",
        domains=_POWER_DOMAINS | {"scene"},
        states=None,
        service="turn_on",
        service_overrides=(("cover", "open_cover"),),
    ),
    "turn_off": ActionDefinition(
        intent_type=ACTIONS["turn_off"],
        description="Switch this instruction's device off, fully close a cover "
        "or stop a script now. Not playback, a scene or a vacuum stop.",
        domains=_POWER_DOMAINS,
        states=None,
        service="turn_off",
        service_overrides=(("cover", "close_cover"),),
    ),
    "set_brightness": ActionDefinition(
        intent_type=ACTIONS["set_brightness"],
        description="Set this instruction's light to an exact absolute percentage "
        "written in digits with a percent sign or percent word. Not a relative "
        "change, bare number or a level said in words.",
        domains=frozenset({"light"}),
        states=frozenset({"on", "off"}),
        parameters=(
            IntegerParameter(
                "brightness",
                0,
                100,
                description="Exact absolute brightness, not a relative amount",
                unit="%",
                extractor=find_brightness_values,
            ),
        ),
        service="turn_on",
        required_capabilities=frozenset({"brightness"}),
    ),
}
