"""An action definition rejects unsupported targets and untyped parameters."""

import pytest

from custom_components.jev.plan_actions import (
    COMPOUND_ACTIONS,
    ActionDefinition,
    IntegerParameter,
)
from custom_components.jev.snapshot import ExposedEntity


def light():
    return ExposedEntity("light.office", "Office light", "light", "Office", "on")


@pytest.mark.parametrize("action", ["turn_on", "turn_off"])
def test_current_actions_have_no_parameters_and_preserve_intent_slots(action):
    assert COMPOUND_ACTIONS[action].build_slots(light(), {}) == {
        "name": {"value": "Office light"},
        "domain": {"value": ["light"]},
        "area": {"value": "Office"},
    }
    assert COMPOUND_ACTIONS[action].build_slots(light(), {"brightness": 40}) is None


@pytest.mark.parametrize(
    "domain,state", [("lock", "locked"), ("switch", "on"), ("light", "unavailable")]
)
def test_the_registry_does_not_expand_the_current_supported_targets(domain, state):
    target = ExposedEntity(f"{domain}.office", "Office device", domain, "Office", state)
    assert COMPOUND_ACTIONS["turn_on"].build_slots(target, {}) is None


def brightness_definition():
    # Exercise the parameter contract without enabling brightness in the planner.
    return ActionDefinition(
        intent_type="HassLightSet",
        description="Set an absolute brightness percentage",
        domains=frozenset({"light"}),
        states=frozenset({"on", "off"}),
        parameters=(IntegerParameter("brightness", 0, 100),),
    )


@pytest.mark.parametrize(
    "supplied",
    [
        {},
        {"brightness": -1},
        {"brightness": 101},
        {"brightness": "40"},
        {"brightness": 40.0},
        {"brightness": True},
        {"brightness": float("nan")},
        {"brightness": 40, "service": "lock.unlock"},
    ],
)
def test_required_parameter_and_range_are_validated_without_coercion(supplied):
    assert brightness_definition().build_slots(light(), supplied) is None


@pytest.mark.parametrize("level", [0, 40, 100])
def test_valid_integer_parameter_is_mapped_to_its_declared_slot(level):
    target = light()
    target.area = None
    slots = brightness_definition().build_slots(target, {"brightness": level})
    assert slots == {
        "name": {"value": "Office light"},
        "domain": {"value": ["light"]},
        "brightness": {"value": level},
    }
