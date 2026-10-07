"""An action definition rejects unsupported targets and untyped parameters."""

import pytest

from custom_components.jev.plan_actions import (
    COMPOUND_ACTIONS,
    IntegerParameter,
)
from custom_components.jev.snapshot import ExposedEntity


def light():
    return ExposedEntity(
        "light.office",
        "Office light",
        "light",
        "Office",
        "on",
        capabilities=frozenset({"brightness"}),
    )


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
    return COMPOUND_ACTIONS["set_brightness"]


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


@pytest.mark.parametrize(
    "text,values",
    [
        ("Set Kitchen light to 40% and Office light to 60%", (40, 60)),
        ("Set Kitchen light to 0 percent and Office light to 100 percent", (0, 100)),
        ("Schalte Kitchen light auf 40 Prozent und Office light auf 60%", (40, 60)),
        ("把灯调到百分之40", (40,)),
        ("set Kitchen light to 40", ()),
        ("set Kitchen light to forty percent", ()),
        ("set Kitchen light to 101%", ()),
        ("set Kitchen light to 12.5%", ()),
        ("set Kitchen light to -40%", ()),
        ("dim Kitchen light by 40%", ()),
        ("set Kitchen light 40% brighter", ()),
        ("set Kitchen light to 40% and Office light to 40%", (40,)),
    ],
)
def test_brightness_choices_are_exact_absolute_percentages(text, values):
    parameter = brightness_definition().parameters[0]
    assert parameter.candidate_values(text) == values


def test_a_parameter_without_an_extractor_offers_no_guessed_values():
    assert IntegerParameter("unimplemented", 0, 100).candidate_values("40%") == ()


@pytest.mark.parametrize(
    "domain,capabilities", [("light", frozenset()), ("switch", frozenset({"brightness"}))]
)
def test_brightness_requires_a_light_with_the_reported_capability(domain, capabilities):
    target = ExposedEntity(
        f"{domain}.office", "Office device", domain, None, "on", capabilities=capabilities
    )
    assert brightness_definition().build_slots(target, {"brightness": 40}) is None
