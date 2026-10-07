"""A variable target list stays inside the exposed room and executes typed settings."""

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from jevclient import ChoiceAnswer, NoulAnswer

from custom_components.jev.const import CONF_DAILY_TOKEN_BUDGET, CONF_LIGHTING_PLANS
from custom_components.jev.lighting import (
    LightingStep,
    build_lighting_questions,
    read_lighting_plan,
)
from custom_components.jev.snapshot import ExposedEntity, HomeSnapshot, async_snapshot

from .conftest import build_response
from .test_conversation import answer_set, converse
from .test_conversation import house as house

TEXT = "Make all lights in Living room look like a sunset"


def choice(value, confidence=0.96):
    return ChoiceAnswer(choice=value, probabilities={}, confidence=confidence)


def snapshot(count=3):
    targets = [
        ExposedEntity(
            f"light.sunset_{i}",
            f"Sunset light {i}",
            "light",
            "Living room",
            "on",
            area_id="living",
            capabilities=frozenset({"brightness", "color"}),
        )
        for i in range(count)
    ]
    targets.append(
        ExposedEntity(
            "light.office",
            "Office light",
            "light",
            "Office",
            "off",
            area_id="office",
            capabilities=frozenset({"brightness", "color"}),
        )
    )
    return HomeSnapshot(entities=targets, area_aliases={"Living room": ["Wohnbereich"]})


def answers(state):
    room = next(e.area_id for e in state.entities if e.name.startswith("Sunset light"))
    values = {
        "supported": NoulAnswer(noul=0.99),
        "room": choice(room),
        "scope": choice("all_in_room"),
    }
    for target in state.entities:
        if "brightness" not in target.capabilities or target.is_group:
            continue
        included = target.area_id == room
        values[f"include_{target.entity_id}"] = NoulAnswer(
            noul=0.99 if included else 0.01
        )
        if included:
            tones = ["orange", "amber", "red"]
            index = int(target.entity_id.rsplit("_", 1)[-1])
            values[f"color_{target.entity_id}"] = choice(tones[index % 3])
            values[f"brightness_{target.entity_id}"] = choice(str(20 + (index % 3) * 10))
    return values


@pytest.mark.parametrize("count", [1, 3, 7])
def test_target_count_is_variable_and_settings_are_separate(count):
    state = snapshot(count)
    plan = read_lighting_plan(build_response(**answers(state)), TEXT, state, 0.6)
    assert len(plan.steps) == count
    assert plan.steps[0].service_data(state.entities[0]) == {
        "entity_id": "light.sunset_0",
        "brightness_pct": 20,
        "rgb_color": [255, 80, 10],
    }
    questions = build_lighting_questions(state)
    assert len(questions) == 3 + 3 * (count + 1)


@pytest.mark.parametrize(
    "key,value",
    [
        ("supported", NoulAnswer(noul=0.5)),
        ("room", choice("none_of_these")),
        ("room", choice("living", 0.5)),
        ("scope", choice("none_of_these")),
        ("include_light.sunset_1", NoulAnswer(noul=0.01)),
        ("include_light.sunset_1", NoulAnswer(noul=0.5)),
        ("include_light.sunset_1", NoulAnswer(noul=float("nan"))),
        ("include_light.sunset_1", NoulAnswer(noul=1.1)),
        ("include_light.office", NoulAnswer(noul=0.99)),
        ("color_light.sunset_1", choice("not_a_colour")),
        ("color_light.sunset_1", choice("amber", 0.5)),
        ("brightness_light.sunset_1", choice("37")),
        ("brightness_light.sunset_1", choice("30", float("nan"))),
    ],
)
def test_invalid_second_target_or_scope_rejects_the_whole_plan(key, value):
    state = snapshot()
    assert (
        read_lighting_plan(
            build_response(**(answers(state) | {key: value})), TEXT, state, 0.6
        )
        is None
    )


@pytest.mark.parametrize(
    "missing",
    [
        "supported",
        "room",
        "scope",
        "include_light.sunset_1",
        "color_light.sunset_1",
        "brightness_light.sunset_1",
    ],
)
def test_missing_answers_reject_the_plan(missing):
    state = snapshot()
    values = answers(state)
    del values[missing]
    assert read_lighting_plan(build_response(**values), TEXT, state, 0.6) is None


@pytest.mark.parametrize(
    "change", ["unavailable", "no_brightness", "capped", "wrong_room_text"]
)
def test_all_room_lights_cannot_be_silently_omitted(change):
    state = snapshot()
    values = answers(state)
    text = TEXT
    if change == "unavailable":
        state.entities[1].state = "unavailable"
    elif change == "no_brightness":
        state.entities[1].capabilities = frozenset()
    elif change == "capped":
        state.left_out = 1
    else:
        text = "Make Office look like a sunset"
    assert read_lighting_plan(build_response(**values), text, state, 0.6) is None


@pytest.mark.parametrize(
    "capabilities,tone,low,high,expected",
    [
        (frozenset({"brightness"}), "keep", None, None, {}),
        (
            frozenset({"brightness", "color_temp"}),
            "warm_white",
            2700,
            6500,
            {"color_temp_kelvin": 2700},
        ),
        (
            frozenset({"brightness", "color_temp"}),
            "warm_white",
            2000,
            6500,
            {"color_temp_kelvin": 2200},
        ),
    ],
)
def test_white_lights_receive_only_compatible_settings(
    capabilities, tone, low, high, expected
):
    target = replace(
        snapshot().entities[0],
        capabilities=capabilities,
        min_color_temp_kelvin=low,
        max_color_temp_kelvin=high,
    )
    data = LightingStep(target.entity_id, "living", tone, 30).service_data(target)
    assert data == {"entity_id": target.entity_id, "brightness_pct": 30} | expected


@pytest.mark.parametrize(
    "low,high", [(None, None), (6500, 2700), ("2700", 6500), (0, 6500)]
)
def test_invalid_reported_temperature_ranges_are_not_used(low, high):
    target = replace(
        snapshot().entities[0],
        capabilities=frozenset({"brightness", "color_temp"}),
        min_color_temp_kelvin=low,
        max_color_temp_kelvin=high,
    )
    assert (
        LightingStep(target.entity_id, "living", "warm_white", 30).service_data(target)
        is None
    )


def test_empty_or_group_only_house_has_no_lighting_request():
    assert build_lighting_questions(HomeSnapshot()) == {}
    state = snapshot(1)
    state.entities[0].is_group = True
    state.entities[1].is_group = True
    assert build_lighting_questions(state) == {}


async def make_house(hass, house, count=3):
    hass.config_entries.async_update_entry(house, options={CONF_LIGHTING_PLANS: True})
    area = ar.async_get(hass).async_get_or_create("Living room")
    ar.async_get(hass).async_update(area.id, aliases={"Wohnbereich"})
    registry = er.async_get(hass)
    for i in range(count):
        entry = registry.async_get_or_create(
            "light", "demo", f"sunset_{i}", suggested_object_id=f"sunset_{i}"
        )
        name = f"Sunset light {i}"
        registry.async_update_entity(entry.entity_id, name=name, area_id=area.id)
        hass.states.async_set(
            entry.entity_id,
            "off",
            {"friendly_name": name, "supported_color_modes": ["rgb"]},
        )
        async_expose_entity(hass, conversation.DOMAIN, entry.entity_id, True)
    hass.states.async_set(
        "light.office",
        "off",
        {"friendly_name": "Office light", "supported_color_modes": ["rgb"]},
    )
    hass.states.async_set(
        "light.private",
        "off",
        {"friendly_name": "Private light", "supported_color_modes": ["rgb"]},
    )
    return async_snapshot(hass, 150)


def responses(mock_client, state):
    mock_client.ask.reset_mock()
    mock_client.ask.side_effect = [
        build_response(**answer_set(lighting_plan=NoulAnswer(noul=0.99))),
        build_response(**answers(state)),
    ]


@pytest.mark.parametrize(
    "count,language,text",
    [
        (1, "en", TEXT),
        (3, "de", "Mach den Wohnbereich wie einen Sonnenuntergang"),
        (5, "en", TEXT),
    ],
)
async def test_room_plan_changes_variable_lights_with_combined_colour_and_brightness(
    hass, house, mock_client, count, language, text
):
    state = await make_house(hass, house, count)
    responses(mock_client, state)
    calls = []

    async def record(call):
        calls.append(call)
        entity_id = call.data["entity_id"]
        old = hass.states.get(entity_id)
        hass.states.async_set(entity_id, "on", dict(old.attributes) | dict(call.data))

    hass.services.async_register("light", "turn_on", record)
    result = await converse(hass, text, language=language)
    assert len(calls) == count
    assert all(
        "rgb_color" in call.data and "brightness_pct" in call.data for call in calls
    )
    assert all(hass.states.get(f"light.sunset_{i}").state == "on" for i in range(count))
    assert hass.states.get("light.office").state == "off"
    assert hass.states.get("light.private").state == "off"
    assert result.response.error_code is None
    assert str(count) in result.response.speech["plain"]["speech"]
    assert mock_client.ask.await_count == 2
    assert "Private light" not in str(mock_client.ask.call_args.args)
    assert len(house.runtime_data.conversation_traces[0]["plan"]) == count


async def test_option_off_never_routes_into_room_planning(hass, house, mock_client):
    mock_client.ask.return_value = build_response(**answer_set())
    mock_client.ask.reset_mock()
    hass.services.async_register("light", "turn_on", lambda call: None)
    await converse(hass, "Turn on Kitchen light")
    assert mock_client.ask.await_count == 1
    assert "lighting_plan" not in mock_client.ask.call_args.args[1]


@pytest.mark.parametrize("change", ["exposure", "capability", "new_light", "group"])
async def test_changes_during_planning_prevent_all_actions(
    hass, house, mock_client, change
):
    state = await make_house(hass, house)
    responses(mock_client, state)
    queued = iter(mock_client.ask.side_effect)

    async def mutate(state, questions):
        if "supported" in questions:
            if change == "exposure":
                async_expose_entity(hass, conversation.DOMAIN, "light.sunset_1", False)
            elif change == "new_light":
                await make_house(hass, house, 4)
            else:
                attrs = {
                    "friendly_name": "Sunset light 1",
                    "supported_color_modes": ["onoff"],
                }
                if change == "group":
                    attrs = {
                        "friendly_name": "Sunset light 1",
                        "supported_color_modes": ["rgb"],
                        "entity_id": ["light.private"],
                    }
                hass.states.async_set("light.sunset_1", "off", attrs)
        return next(queued)

    mock_client.ask.side_effect = mutate
    calls = []
    hass.services.async_register("light", "turn_on", lambda call: calls.append(call))
    result = await converse(hass, TEXT)
    assert not calls
    assert result.response.error_code is not None


async def test_invalid_plan_never_starts_execution(hass, house, mock_client):
    state = await make_house(hass, house)
    responses(mock_client, state)
    values = answers(state)
    values["brightness_light.sunset_2"] = choice("37")
    mock_client.ask.side_effect = [
        build_response(**answer_set(lighting_plan=NoulAnswer(noul=0.99))),
        build_response(**values),
    ]
    calls = []
    hass.services.async_register("light", "turn_on", lambda call: calls.append(call))
    result = await converse(hass, TEXT)
    assert not calls
    assert result.response.error_code is not None


@pytest.mark.parametrize("failure", [HomeAssistantError("failed"), TimeoutError()])
async def test_partial_failure_stops_without_fallback_or_replay(
    hass, house, mock_client, failure
):
    state = await make_house(hass, house)
    responses(mock_client, state)
    calls = []

    async def record(call):
        calls.append(call)
        if len(calls) == 2:
            raise failure

    hass.services.async_register("light", "turn_on", record)
    fallback = AsyncMock()
    with patch(
        "custom_components.jev.conversation.JevConversationEntity._fall_back", fallback
    ):
        result = await converse(hass, TEXT)
    assert len(calls) == 2
    fallback.assert_not_awaited()
    assert result.response.error_code is not None
    assert "1" in result.response.speech["plain"]["speech"]


async def test_planning_obeys_remaining_budget(hass, house, mock_client):
    state = await make_house(hass, house)
    responses(mock_client, state)
    queued = iter(mock_client.ask.side_effect)

    async def spend(state, questions):
        hass.config_entries.async_update_entry(
            house, options={CONF_LIGHTING_PLANS: True, CONF_DAILY_TOKEN_BUDGET: 1}
        )
        return next(queued)

    mock_client.ask.side_effect = spend
    calls = []
    hass.services.async_register("light", "turn_on", lambda call: calls.append(call))
    result = await converse(hass, TEXT)
    assert not calls
    assert mock_client.ask.await_count == 1
    assert result.response.error_code is not None


def test_named_subset_can_select_more_than_two_without_expanding_the_room():
    state = snapshot(5)
    values = answers(state)
    values["scope"] = choice("named_in_room")
    for i in (3, 4):
        values[f"include_light.sunset_{i}"] = NoulAnswer(noul=0.01)
    text = "Make Sunset light 0, Sunset light 1 and Sunset light 2 look like a sunset"
    plan = read_lighting_plan(build_response(**values), text, state, 0.6)
    assert [step.entity_id for step in plan.steps] == [
        f"light.sunset_{i}" for i in range(3)
    ]


@pytest.mark.parametrize("case", ["unnamed", "duplicate", "shorter", "omitted"])
def test_named_selection_cannot_guess_or_silently_skip_targets(case):
    state = snapshot(1)
    values = answers(state)
    values["scope"] = choice("named_in_room")
    text = "Make Sunset light 0 look like a sunset"
    if case == "unnamed":
        text = "Make a light look like a sunset"
    elif case == "duplicate":
        state.entities[1].name = "Sunset light 0"
    elif case == "shorter":
        state.entities[1].name = "Desk Sunset light 0"
        text = "Make Desk Sunset light 0 look like a sunset"
    else:
        values["include_light.sunset_0"] = NoulAnswer(noul=0.01)
    assert read_lighting_plan(build_response(**values), text, state, 0.6) is None


async def test_exposure_changed_during_execution_stops_without_replay(
    hass, house, mock_client
):
    state = await make_house(hass, house)
    responses(mock_client, state)
    calls = []

    async def record(call):
        calls.append(call)
        async_expose_entity(hass, conversation.DOMAIN, "light.sunset_1", False)

    hass.services.async_register("light", "turn_on", record)
    fallback = AsyncMock()
    with patch(
        "custom_components.jev.conversation.JevConversationEntity._fall_back", fallback
    ):
        result = await converse(hass, TEXT)
    assert len(calls) == 1
    fallback.assert_not_awaited()
    assert result.response.error_code is not None
    assert "1" in result.response.speech["plain"]["speech"]


async def test_no_eligible_room_targets_never_sends_a_planning_call(
    hass, house, mock_client
):
    hass.config_entries.async_update_entry(house, options={CONF_LIGHTING_PLANS: True})
    mock_client.ask.return_value = build_response(
        **answer_set(lighting_plan=NoulAnswer(noul=0.99))
    )
    mock_client.ask.reset_mock()
    result = await converse(hass, TEXT)
    assert mock_client.ask.await_count == 1
    assert result.response.error_code is not None


@pytest.mark.parametrize("probability", [None, 0.5, float("nan"), 1.1])
async def test_uncertain_lighting_classification_does_not_approximate_as_power(
    hass, house, mock_client, probability
):
    hass.config_entries.async_update_entry(house, options={CONF_LIGHTING_PLANS: True})
    values = answer_set()
    if probability is not None:
        values["lighting_plan"] = NoulAnswer(noul=probability)
    mock_client.ask.return_value = build_response(**values)
    calls = []
    hass.services.async_register("light", "turn_on", lambda call: calls.append(call))
    result = await converse(hass, TEXT)
    assert not calls
    assert result.response.error_code is not None


async def test_opted_in_ordinary_command_still_has_one_request(hass, house, mock_client):
    hass.config_entries.async_update_entry(house, options={CONF_LIGHTING_PLANS: True})
    mock_client.ask.return_value = build_response(
        **answer_set(lighting_plan=NoulAnswer(noul=0.01))
    )
    mock_client.ask.reset_mock()
    calls = []
    hass.services.async_register("light", "turn_on", lambda call: calls.append(call))
    result = await converse(hass, "Turn on Kitchen light")
    assert len(calls) == 1
    assert mock_client.ask.await_count == 1
    assert result.response.error_code is None
