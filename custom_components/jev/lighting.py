"""Variable-size lighting plans from per-target typed decisions.

The model chooses participation, tone and brightness. It never supplies entity
IDs, RGB channels or service data. The complete room boundary is checked locally.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from jevclient import Choice, ChoiceAnswer, JevResponse, Noul, NoulAnswer, Question

from .interpret import NONE
from .snapshot import ExposedEntity, HomeSnapshot

# These are curated creative choices, not measured optimal lighting values.
BRIGHTNESSES = tuple(range(10, 101, 10))
RGB_TONES = {
    "amber": (255, 140, 35),
    "orange": (255, 80, 10),
    "red": (255, 30, 15),
    "warm_white": (255, 185, 115),
}


@dataclass(frozen=True, slots=True)
class LightingStep:
    entity_id: str
    room_id: str
    tone: str
    brightness: int

    def service_data(self, target: ExposedEntity) -> dict[str, Any] | None:
        """Translate a validated choice using the light's current capabilities."""
        if (
            target.entity_id != self.entity_id
            or target.area_id != self.room_id
            or not eligible(target)
            or self.brightness not in BRIGHTNESSES
            or self.tone not in tone_options(target)
        ):
            return None
        data: dict[str, Any] = {
            "entity_id": self.entity_id,
            "brightness_pct": self.brightness,
        }
        if "color" in target.capabilities:
            data["rgb_color"] = list(RGB_TONES[self.tone])
        elif self.tone == "warm_white":
            low, high = target.min_color_temp_kelvin, target.max_color_temp_kelvin
            if type(low) is not int or type(high) is not int or not 0 < low <= high:
                return None
            data["color_temp_kelvin"] = max(low, min(2200, high))
        return data


@dataclass(frozen=True, slots=True)
class LightingPlan:
    room_id: str
    steps: tuple[LightingStep, ...]


def eligible(target: ExposedEntity) -> bool:
    return (
        target.domain == "light"
        and target.state in ("on", "off")
        and "brightness" in target.capabilities
        and target.area_id is not None
        and not target.is_group
    )


def tone_options(target: ExposedEntity) -> dict[str, str]:
    if "color" in target.capabilities:
        return {key: key.replace("_", " ") for key in RGB_TONES}
    if "color_temp" in target.capabilities:
        return {"warm_white": "Warm white at 2200 K or the warmest supported temperature"}
    return {
        "keep": "Keep this fixed-white light's existing colour; adjust brightness only"
    }


def route_question() -> Noul:
    return Noul(
        "Does this request ask to apply a creative lighting look, such as sunset, "
        "to lights now? Not an exact numeric command, query or unrelated action.",
        true="Apply a lighting atmosphere or look immediately",
        false="Ordinary device control, exact brightness, a query, report or negation",
    )


def build_lighting_questions(snapshot: HomeSnapshot) -> dict[str, Question]:
    targets = [e for e in snapshot.entities if eligible(e)]
    rooms = {e.area_id: e.area for e in targets if e.area_id is not None}
    if not rooms:
        return {}
    questions: dict[str, Question] = {
        "supported": Noul(
            "Can the ENTIRE request be fulfilled now by applying the listed sunset "
            "tones and brightness levels to exposed lights in one explicitly named "
            "room, or explicitly named lights in that room?",
            true="One immediate lighting look with compatible settings for white lights",
            false="Another style, unlisted values, multiple rooms, a delay or duration, "
            "condition, exclusion, negation, query or unrelated action",
        ),
        "room": Choice(
            "Which one room is requested? Match its name or aliases; do not guess.",
            {
                key: {"name": name, "aliases": snapshot.area_aliases.get(name or "", [])}
                for key, name in rooms.items()
            }
            | {NONE: "No single room"},
        ),
        "scope": Choice(
            "Which lights in that room are requested?",
            {
                "all_in_room": "Apply the look to the room's lights as a whole",
                "named_in_room": "Only lights explicitly named in full in the request",
                NONE: "An exclusion, unclear selection or another scope",
            },
        ),
    }
    for target in targets:
        label = target.as_option()
        questions[f"include_{target.entity_id}"] = Noul(
            f"Should {target.entity_id}: {label} participate in the requested look? "
            "Only include lights in the requested room and scope. Include every "
            "light there for all_in_room, and no lights from another room.",
            true="This light is requested",
            false="This light is outside the requested selection",
        )
        questions[f"color_{target.entity_id}"] = Choice(
            f"Which sunset tone should {label} use? Choose a compatible tone. "
            "Vary tones across participating lights when that suits the request.",
            tone_options(target) | {NONE: "No compatible sunset setting"},
        )
        questions[f"brightness_{target.entity_id}"] = Choice(
            f"Which brightness should {label} use for the requested sunset look? "
            "Choose one supplied level. This is a creative setting, not arithmetic.",
            {str(level): f"{level}%" for level in BRIGHTNESSES}
            | {NONE: "No supported level"},
        )
    return questions


def _named(text: str, name: str) -> bool:
    phrase = r"\s+".join(re.escape(w) for w in name.split())
    return bool(phrase and re.search(rf"(?<!\w){phrase}(?!\w)", text, re.IGNORECASE))


def _explicit_target(
    text: str, target: ExposedEntity, candidates: list[ExposedEntity]
) -> bool:
    """A full unique name, excluding occurrences inside another light's name."""
    for name in target.names:
        if any(
            name.casefold() in {n.casefold() for n in other.names}
            for other in candidates
            if other.entity_id != target.entity_id
        ):
            continue
        phrase = r"\s+".join(re.escape(w) for w in name.split())
        matches = list(re.finditer(rf"(?<!\w){phrase}(?!\w)", text, re.IGNORECASE))
        covered: list[re.Match[str]] = []
        for other in candidates:
            if other.entity_id == target.entity_id:
                continue
            for other_name in other.names:
                other_phrase = r"\s+".join(re.escape(w) for w in other_name.split())
                covered.extend(
                    re.finditer(rf"(?<!\w){other_phrase}(?!\w)", text, re.IGNORECASE)
                )
        if any(
            not any(
                cover.start() <= m.start() and cover.end() >= m.end() for cover in covered
            )
            for m in matches
        ):
            return True
    return False


def _choice(response: JevResponse, key: str, floor: float) -> str | None:
    answer = response.answers.get(key)
    if (
        not isinstance(answer, ChoiceAnswer)
        or not math.isfinite(answer.confidence)
        or not floor <= answer.confidence <= 1
    ):
        return None
    return answer.choice


def read_lighting_plan(
    response: JevResponse, text: str, snapshot: HomeSnapshot, min_confidence: float
) -> LightingPlan | None:
    """Validate the full selected set before any light changes."""
    supported = response.answers.get("supported")
    if not isinstance(supported, NoulAnswer) or not 0.9 <= supported.noul <= 1:
        return None
    floor = max(0.8, min_confidence)
    room = _choice(response, "room", floor)
    scope = _choice(response, "scope", floor)
    targets = [e for e in snapshot.entities if eligible(e)]
    in_room = [e for e in targets if e.area_id == room]
    target_ids = {e.entity_id for e in targets}
    for key, answer in response.answers.items():
        if key.startswith("include_") and key.removeprefix("include_") not in target_ids:
            if not isinstance(answer, NoulAnswer) or not 0 <= answer.noul <= 0.1:
                return None
    if room is None or not in_room or scope not in ("all_in_room", "named_in_room"):
        return None
    if scope == "all_in_room":
        # A capped snapshot cannot prove that every exposed room light was offered.
        if snapshot.left_out or not any(
            _named(text, name)
            for name in [
                in_room[0].area or "",
                *snapshot.area_aliases.get(in_room[0].area or "", []),
            ]
        ):
            return None
        if any(
            e.domain == "light"
            and e.area_id == room
            and not e.is_group
            and not eligible(e)
            for e in snapshot.entities
        ):
            return None
    elif any(_named(text, name) for name in snapshot.hidden_names):
        return None
    room_named = any(
        _named(text, name)
        for name in [
            in_room[0].area or "",
            *snapshot.area_aliases.get(in_room[0].area or "", []),
        ]
    )
    candidates = in_room if room_named else targets
    steps = []
    for target in targets:
        included = response.answers.get(f"include_{target.entity_id}")
        if not isinstance(included, NoulAnswer) or not math.isfinite(included.noul):
            return None
        if 0 <= included.noul <= 0.1:
            if target.area_id == room and (
                scope == "all_in_room"
                or (
                    scope == "named_in_room"
                    and _explicit_target(text, target, candidates)
                )
            ):
                return None
            continue
        if not max(0.9, min_confidence) <= included.noul <= 1 or target.area_id != room:
            return None
        if scope == "named_in_room" and not _explicit_target(text, target, candidates):
            return None
        tone = _choice(response, f"color_{target.entity_id}", floor)
        brightness = _choice(response, f"brightness_{target.entity_id}", floor)
        if tone not in tone_options(target) or brightness not in {
            str(v) for v in BRIGHTNESSES
        }:
            return None
        step = LightingStep(target.entity_id, room, tone, int(brightness))
        if step.service_data(target) is None:
            return None
        steps.append(step)
    return LightingPlan(room, tuple(steps)) if steps else None
