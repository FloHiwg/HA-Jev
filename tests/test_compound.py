"""Compound plans must validate both targets before affecting either light."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.helpers import intent as ha_intent
from jevclient import ChoiceAnswer, JevConnectionError, NoulAnswer

from custom_components.jev.compound import build_compound_questions, read_compound_plan
from custom_components.jev.const import CONF_COMPOUND_LIGHTS, CONF_DAILY_TOKEN_BUDGET
from custom_components.jev.snapshot import ExposedEntity, HomeSnapshot

from .conftest import build_response
from .test_conversation import answer_set, converse
from .test_conversation import house as house

TEXT = "Turn on Kitchen light and turn off Office light"


def plan_answers(**overrides):
    answers = {"supported": NoulAnswer(noul=0.98)}
    for ordinal, action, entity in (
        ("first", "turn_on", "light.kitchen"),
        ("second", "turn_off", "light.office"),
    ):
        answers[f"{ordinal}_action"] = ChoiceAnswer(
            choice=action, probabilities={}, confidence=0.96
        )
        answers[f"{ordinal}_entity"] = ChoiceAnswer(
            choice=entity, probabilities={}, confidence=0.96
        )
    return answers | overrides


def snapshot():
    return HomeSnapshot(
        entities=[
            ExposedEntity("light.kitchen", "Kitchen light", "light", "Kitchen", "off"),
            ExposedEntity("light.office", "Office light", "light", "Office", "on"),
        ]
    )


@pytest.mark.parametrize(
    "override",
    [
        {"supported": NoulAnswer(noul=0.1)},
        {"supported": NoulAnswer(noul=float("nan"))},
        {"supported": NoulAnswer(noul=1.1)},
        {
            "second_entity": ChoiceAnswer(
                choice="light.private", probabilities={}, confidence=1
            )
        },
        {
            "second_entity": ChoiceAnswer(
                choice="light.kitchen", probabilities={}, confidence=1
            )
        },
        {"second_action": ChoiceAnswer(choice="toggle", probabilities={}, confidence=1)},
        {
            "second_action": ChoiceAnswer(
                choice="turn_off", probabilities={}, confidence=0.5
            )
        },
        {
            "second_action": ChoiceAnswer(
                choice="turn_off", probabilities={}, confidence=float("nan")
            )
        },
        {"second_entity": NoulAnswer(noul=1)},
    ],
)
def test_invalid_plan_is_not_executable(override):
    assert (
        read_compound_plan(
            build_response(**plan_answers(**override)), TEXT, snapshot(), 0.6
        )
        is None
    )


@pytest.mark.parametrize("missing", list(plan_answers()))
def test_missing_plan_answers_fail_closed(missing):
    answers = plan_answers()
    del answers[missing]
    assert read_compound_plan(build_response(**answers), TEXT, snapshot(), 0.6) is None


@pytest.mark.parametrize(
    "text",
    [
        "Turn on Kitchen light and turn off that one",
        "Turn on Kitchen lighting and turn off Office light",
    ],
)
def test_names_must_be_explicit_and_whole(text):
    assert (
        read_compound_plan(build_response(**plan_answers()), text, snapshot(), 0.6)
        is None
    )


def test_name_collision_does_not_expand_an_intent():
    state = snapshot()
    state.entities.append(
        ExposedEntity("light.other", "Office light", "light", "Office", "on")
    )
    assert read_compound_plan(build_response(**plan_answers()), TEXT, state, 0.6) is None


@pytest.mark.parametrize("change", ["domain", "state", "exposure"])
def test_targets_must_still_be_available_exposed_lights(change):
    state = snapshot()
    if change == "domain":
        state.entities[1].domain = "switch"
    elif change == "state":
        state.entities[1].state = "unavailable"
    else:
        state.entities.pop()
    assert read_compound_plan(build_response(**plan_answers()), TEXT, state, 0.6) is None


def test_only_light_choices_and_no_hidden_names_leave_home_assistant():
    state = snapshot()
    state.entities.append(
        ExposedEntity("climate.heater", "Heater", "climate", "Office", "heat")
    )
    state.hidden_names = ["Private light"]
    questions = build_compound_questions(state)
    assert set(questions["first_entity"].criteria) == {
        "light.kitchen",
        "light.office",
        "none_of_these",
    }
    assert "Private light" not in str(questions)


async def enable(hass, entry):
    hass.config_entries.async_update_entry(entry, options={CONF_COMPOUND_LIGHTS: True})


def responses(mock_client, final=None):
    mock_client.ask.reset_mock()
    mock_client.ask.side_effect = [
        build_response(**answer_set(compound=NoulAnswer(noul=0.95))),
        final if final is not None else build_response(**plan_answers()),
    ]


@pytest.mark.parametrize(
    "text,language",
    [(TEXT, "en"), ("Schalte Kitchen light ein und Office light aus", "de")],
)
async def test_both_lights_change_and_both_actions_are_reported(
    hass, house, mock_client, text, language
):
    await enable(hass, house)
    hass.states.async_set("light.office", "on", {"friendly_name": "Office light"})
    responses(mock_client)
    calls = []
    for service, value in (("turn_on", "on"), ("turn_off", "off")):

        async def record(call, value=value):
            calls.append(call)
            for entity_id in call.data["entity_id"]:
                hass.states.async_set(
                    entity_id, value, {"friendly_name": hass.states.get(entity_id).name}
                )

        hass.services.async_register("light", service, record)
    result = await converse(hass, text, language=language)
    assert hass.states.get("light.kitchen").state == "on"
    assert hass.states.get("light.office").state == "off"
    assert len(calls) == 2
    assert result.response.error_code is None
    assert result.response.speech["plain"]["speech"]
    assert mock_client.ask.await_count == 2
    assert mock_client.ask.call_args.args[0]["command"] == text
    assert len(house.runtime_data.conversation_traces[0]["plan"]) == 2


async def test_default_remains_refusal(hass, house, mock_client):
    mock_client.ask.return_value = build_response(
        **answer_set(compound=NoulAnswer(noul=0.95))
    )
    mock_client.ask.reset_mock()
    calls = []
    hass.services.async_register("light", "turn_on", lambda call: calls.append(call))
    result = await converse(hass, TEXT)
    assert not calls
    assert result.response.error_code is not None
    assert mock_client.ask.await_count == 1


async def test_bad_second_target_changes_neither_light(hass, house, mock_client):
    await enable(hass, house)
    responses(
        mock_client,
        build_response(
            **plan_answers(
                second_entity=ChoiceAnswer(
                    choice="light.private", probabilities={}, confidence=1
                )
            )
        ),
    )
    calls = []
    hass.services.async_register("light", "turn_on", lambda call: calls.append(call))
    result = await converse(hass, TEXT)
    assert not calls
    assert result.response.error_code is not None


async def test_unanswered_plan_does_not_act(hass, house, mock_client):
    await enable(hass, house)
    responses(mock_client, JevConnectionError("unavailable"))
    calls = []
    hass.services.async_register("light", "turn_on", lambda call: calls.append(call))
    result = await converse(hass, TEXT)
    assert not calls
    assert result.response.error_code is not None


async def test_plan_request_obeys_remaining_budget(hass, house, mock_client):
    await enable(hass, house)
    responses(mock_client)
    original = mock_client.ask.side_effect

    async def spend_remaining(state, questions):
        response = next(iter(original))
        hass.config_entries.async_update_entry(
            house, options={CONF_COMPOUND_LIGHTS: True, CONF_DAILY_TOKEN_BUDGET: 1}
        )
        return response

    mock_client.ask.side_effect = spend_remaining
    calls = []
    hass.services.async_register("light", "turn_on", lambda call: calls.append(call))
    result = await converse(hass, TEXT)
    assert not calls
    assert mock_client.ask.await_count == 1
    assert result.response.error_code is not None


@pytest.mark.parametrize(
    "error",
    [ha_intent.MatchFailedError, ha_intent.IntentHandleError, ha_intent.IntentError],
)
async def test_partial_failure_never_replays_through_fallback(
    hass, house, mock_client, error
):
    await enable(hass, house)
    responses(mock_client)
    done = ha_intent.IntentResponse(language="en")
    done.async_set_speech("Kitchen light switched on.")
    done.response_type = ha_intent.IntentResponseType.ACTION_DONE
    failure = (
        error(
            ha_intent.MatchTargetsResult(is_match=False),
            ha_intent.MatchTargetsConstraints(),
        )
        if error is ha_intent.MatchFailedError
        else error("test failure")
    )
    handler = AsyncMock(side_effect=[done, failure])
    fallback = AsyncMock()
    with (
        patch("custom_components.jev.conversation.ha_intent.async_handle", handler),
        patch(
            "custom_components.jev.conversation.JevConversationEntity._fall_back",
            fallback,
        ),
    ):
        result = await converse(hass, TEXT)
    assert handler.await_count == 2
    fallback.assert_not_awaited()
    assert result.response.error_code is not None
    assert "Kitchen light switched on." in result.response.speech["plain"]["speech"]
    assert "did not work" in result.response.speech["plain"]["speech"]


@pytest.mark.parametrize("change", ["exposure", "availability"])
async def test_a_target_changed_during_planning_prevents_both_actions(
    hass, house, mock_client, change
):
    await enable(hass, house)
    responses(mock_client)
    queued = iter(mock_client.ask.side_effect)

    async def change_before_plan_returns(state, questions):
        if "supported" in questions:
            if change == "exposure":
                async_expose_entity(hass, conversation.DOMAIN, "light.office", False)
            else:
                hass.states.async_set(
                    "light.office", "unavailable", {"friendly_name": "Office light"}
                )
        return next(queued)

    mock_client.ask.side_effect = change_before_plan_returns
    calls = []
    hass.services.async_register("light", "turn_on", lambda call: calls.append(call))
    result = await converse(hass, TEXT)
    assert not calls
    assert result.response.error_code is not None


def test_hidden_longer_name_cannot_select_a_shorter_exposed_light():
    state = snapshot()
    state.hidden_names = ["Desk Office light"]
    text = "Turn on Kitchen light and turn off Desk Office light"
    assert read_compound_plan(build_response(**plan_answers()), text, state, 0.6) is None
