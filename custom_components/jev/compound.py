"""A bounded plan for two named lights, with no action until both are valid."""

from __future__ import annotations

import math
import re

from jevclient import Choice, ChoiceAnswer, JevResponse, Noul, NoulAnswer, Question

from .interpret import NONE, Interpretation
from .plan_actions import COMPOUND_ACTIONS
from .snapshot import HomeSnapshot


def build_compound_questions(snapshot: HomeSnapshot) -> dict[str, Question]:
    """Ask paired questions against the original sentence, without splitting text."""
    domains = {
        domain for action in COMPOUND_ACTIONS.values() for domain in action.domains
    }
    entities = {
        e.entity_id: e.as_option() for e in snapshot.entities if e.domain in domains
    }
    entities[NONE] = "No single explicitly named light, or an ambiguous target"
    actions = {
        **{key: action.description for key, action in COMPOUND_ACTIONS.items()},
        NONE: "Anything else, including brightness, a delay, a condition or negation",
    }
    questions: dict[str, Question] = {
        "supported": Noul(
            "Can the ENTIRE request be fulfilled by exactly two immediate on/off "
            "instructions, each for one distinct explicitly named light?",
            true="Exactly two named lights with an on/off action for each, now",
            false="Ambiguous, more or fewer instructions, a room/group, a pronoun "
            "instead of a name, a delay, duration, condition, exception, negation, "
            "brightness, order-dependent action or any unsupported request",
        )
    }
    for ordinal in ("first", "second"):
        questions[f"{ordinal}_action"] = Choice(
            f"What action belongs to the {ordinal} instruction in sentence order? "
            "Consider only that instruction, not the other instruction.",
            actions,
        )
        questions[f"{ordinal}_entity"] = Choice(
            f"Which single explicitly named light belongs to the {ordinal} "
            "instruction in sentence order? Match names and aliases exactly. "
            "Do not pick a light for the other instruction or guess a target.",
            entities,
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
    still be an exposed light, and the HA name/area slots must match only that
    light. Two conflicting actions on one light never become a partial plan.
    """
    if any(_named(text, name) for name in snapshot.hidden_names):
        return None
    supported = response.answers.get("supported")
    if not isinstance(supported, NoulAnswer) or not 0.9 <= supported.noul <= 1:
        return None
    floor = max(0.8, min_confidence)
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
        slots = definition.build_slots(target, {})
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
                confidence=min(action.confidence, entity.confidence),
                reason="validated compound light instruction",
                fallback=False,
            )
        )
    return decisions[0], decisions[1]
