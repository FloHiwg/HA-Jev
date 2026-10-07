"""A bounded plan for two named devices, with no action until both are valid."""

from __future__ import annotations

import math
import re

from jevclient import Choice, ChoiceAnswer, JevResponse, Noul, NoulAnswer, Question

from .interpret import NONE, Interpretation, without_names
from .plan_actions import COMPOUND_ACTIONS
from .snapshot import HomeSnapshot


def _parameter_text(text: str, snapshot: HomeSnapshot) -> str:
    # Numbers that belong to names are not parameter values.
    names = (
        [n for e in snapshot.entities for n in e.names]
        + snapshot.hidden_names
        + snapshot.areas
        + [n for aliases in snapshot.area_aliases.values() for n in aliases]
        + snapshot.floors
        + [n for aliases in snapshot.floor_aliases.values() for n in aliases]
    )
    return without_names(text, names)


def build_compound_questions(
    snapshot: HomeSnapshot, text: str = ""
) -> dict[str, Question]:
    """Ask paired questions against the original sentence, without splitting text."""
    parameter_text = _parameter_text(text, snapshot)
    available_actions = {
        key: action
        for key, action in COMPOUND_ACTIONS.items()
        if any(action.accepts_target(target) for target in snapshot.entities)
        and all(p.candidate_values(parameter_text) for p in action.parameters)
    }
    entities = {}
    for target in snapshot.entities:
        supported = [
            key
            for key, action in available_actions.items()
            if action.accepts_target(target)
        ]
        if supported:
            entities[target.entity_id] = (
                f"{target.as_option()}; supported actions: {', '.join(supported)}"
            )
    if len(entities) < 2:
        return {}
    entities[NONE] = "No single explicitly named device, or an ambiguous target"
    actions = {
        **{key: action.description for key, action in available_actions.items()},
        NONE: "Anything else, including a relative brightness change, a delay, "
        "a condition or negation",
    }
    questions: dict[str, Question] = {
        "supported": Noul(
            "Can the ENTIRE request be fulfilled by exactly two immediate supported "
            "instructions, each for one distinct explicitly named device, using only "
            "the listed actions and targets?",
            true="Exactly two named devices with an action and every required parameter "
            "from the listed choices for each, now",
            false="Ambiguous, more or fewer instructions, a room/group, a pronoun "
            "instead of a name, a delay, duration, condition, exception, negation, "
            "a relative or unspecified brightness, a part-way cover position, "
            "a thermostat setpoint, playback, "
            "order-dependent action or any unsupported request",
        )
    }
    for ordinal in ("first", "second"):
        questions[f"{ordinal}_action"] = Choice(
            f"What action belongs to the {ordinal} instruction in sentence order? "
            "Consider only that instruction, not the other instruction.",
            actions,
        )
        questions[f"{ordinal}_entity"] = Choice(
            f"Which single explicitly named device belongs to the {ordinal} "
            "instruction in sentence order? Match names and aliases exactly. "
            "Do not pick a device for the other instruction or guess a target.",
            entities,
        )
        for key, action in available_actions.items():
            for parameter in action.parameters:
                values = parameter.candidate_values(parameter_text)
                questions[f"{ordinal}_{key}_{parameter.name}"] = Choice(
                    f"Which {parameter.name} belongs to the {ordinal} instruction, "
                    f"if its action is {key}? {parameter.description}. "
                    "Choose none_of_these when this action is not requested, "
                    "the value is missing or relative, or none fits exactly. "
                    "Do not copy the other instruction's value.",
                    {str(value): f"{value}{parameter.unit}" for value in values}
                    | {NONE: "Not requested or no exact supported value"},
                )
    return questions


def _named(text: str, name: str) -> bool:
    phrase = r"\s+".join(re.escape(w) for w in name.split())
    return bool(phrase and re.search(rf"(?<!\w){phrase}(?!\w)", text, re.IGNORECASE))


def read_compound_plan(
    response: JevResponse, text: str, snapshot: HomeSnapshot, min_confidence: float
) -> tuple[Interpretation, Interpretation] | None:
    """Fail closed on missing answers, guessed targets and overlapping intent names.

    The model's plan is not an executable service call. Every selected id must
    still be an exposed supported device, and the HA name/area slots must match
    only that device. Two conflicting actions on one device never become a partial plan.
    """
    if any(_named(text, name) for name in snapshot.hidden_names):
        return None
    supported = response.answers.get("supported")
    if not isinstance(supported, NoulAnswer) or not 0.9 <= supported.noul <= 1:
        return None
    floor = max(0.8, min_confidence)
    parameter_text = _parameter_text(text, snapshot)
    decisions: list[Interpretation] = []
    selected: set[str] = set()
    for ordinal in ("first", "second"):
        action = response.answers.get(f"{ordinal}_action")
        entity = response.answers.get(f"{ordinal}_entity")
        if not isinstance(action, ChoiceAnswer) or not isinstance(entity, ChoiceAnswer):
            return None
        if any(
            not math.isfinite(a.confidence) or not floor <= a.confidence <= 1
            for a in (action, entity)
        ):
            return None
        target = snapshot.by_id(entity.choice)
        definition = COMPOUND_ACTIONS.get(action.choice)
        if (
            definition is None
            or target is None
            or target.entity_id in selected
            or not any(_named(text, name) for name in target.names)
        ):
            return None
        supplied: dict[str, object] = {}
        parameter_confidence = 1.0
        for parameter in definition.parameters:
            answer = response.answers.get(f"{ordinal}_{action.choice}_{parameter.name}")
            candidates = {str(v): v for v in parameter.candidate_values(parameter_text)}
            if (
                not isinstance(answer, ChoiceAnswer)
                or not math.isfinite(answer.confidence)
                or not floor <= answer.confidence <= 1
                or answer.choice not in candidates
            ):
                return None
            supplied[parameter.name] = candidates[answer.choice]
            parameter_confidence = min(parameter_confidence, answer.confidence)
        slots = definition.build_slots(target, supplied)
        if slots is None:
            return None
        # A built-in intent matches names, not the model's entity id. Do not let
        # two identically named devices or aliases widen a singleton target.
        matches = [
            e
            for e in snapshot.entities
            if e.domain == target.domain
            and (target.area is None or e.area == target.area)
            and target.slot_name.casefold() in {n.casefold() for n in e.names}
        ]
        if len(matches) != 1:
            return None
        selected.add(target.entity_id)
        decisions.append(
            Interpretation(
                intent_type=definition.intent_type,
                slots=slots,
                action=action.choice,
                confidence=min(
                    action.confidence, entity.confidence, parameter_confidence
                ),
                reason="validated compound device instruction",
                fallback=False,
            )
        )
    return decisions[0], decisions[1]
