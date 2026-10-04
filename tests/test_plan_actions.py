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
    "domain,state",
    [
        ("lock", "locked"),
        ("vacuum", "cleaning"),
        ("light", "unavailable"),
        ("fan", "unknown"),
    ],
)
def test_unsupported_domains_and_missing_states_are_refused(domain, state):
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


@pytest.mark.parametrize(
    "domain,state",
    [
        ("light", "off"),
        ("switch", "on"),
        ("fan", "on"),
        ("cover", "closed"),
        ("media_player", "playing"),
        ("climate", "heat"),
        ("input_boolean", "off"),
        ("script", "off"),
    ],
)
@pytest.mark.parametrize("action", ["turn_on", "turn_off"])
def test_power_actions_accept_each_supported_domain_with_its_own_state(
    domain, state, action
):
    target = ExposedEntity(f"{domain}.office", "Office device", domain, "Office", state)
    slots = COMPOUND_ACTIONS[action].build_slots(target, {})
    assert slots["domain"] == {"value": [domain]}
    expected_service = (
        {"turn_on": "open_cover", "turn_off": "close_cover"}[action]
        if domain == "cover"
        else action
    )
    assert COMPOUND_ACTIONS[action].service_for(domain) == expected_service


def test_a_scene_can_be_activated_but_not_turned_off():
    target = ExposedEntity(
        "scene.evening", "Evening scene", "scene", None, "2026-10-04T10:00:00+00:00"
    )
    assert COMPOUND_ACTIONS["turn_on"].build_slots(target, {}) is not None
    assert COMPOUND_ACTIONS["turn_off"].build_slots(target, {}) is None
