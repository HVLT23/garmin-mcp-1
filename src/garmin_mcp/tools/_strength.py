"""Build Garmin Connect strength-training workout payloads.

Garmin's `garminconnect` SDK has no typed strength workout, so the payload is
built as raw JSON and sent through `Garmin.upload_workout`.

Wire format references (all IDs below were cross-checked, none are guessed):

- sport type `strength_training` = id 5
- step types: warmup 1, cooldown 2, interval 3, rest 5, repeat 6
- end conditions: time 2, iterations 7 (repeat groups), reps 10
- weight unit: kilogram = unit id 8, factor 1000
- `category` must be one of Garmin Connect's exercise categories, otherwise
  Garmin rejects the upload (HTTP 400 "Invalid category"). `exerciseName` is
  kept only when it matches a Garmin exercise key, otherwise stored empty.

Source: https://github.com/n1t3k/garmin-strength-api (docs/api-reference.md,
examples/simple-workout.json) and the Garmin FIT SDK exercise profile.
"""

from __future__ import annotations

import re
from typing import Any

STRENGTH_SPORT_TYPE: dict[str, Any] = {
    "sportTypeId": 5,
    "sportTypeKey": "strength_training",
    "displayOrder": 5,
}

_STEP_TYPES: dict[str, dict[str, Any]] = {
    "warmup": {"stepTypeId": 1, "stepTypeKey": "warmup", "displayOrder": 1},
    "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown", "displayOrder": 2},
    "interval": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
    "rest": {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5},
    "repeat": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
}

_CONDITION_TIME: dict[str, Any] = {
    "conditionTypeId": 2,
    "conditionTypeKey": "time",
    "displayOrder": 2,
    "displayable": True,
}

_CONDITION_ITERATIONS: dict[str, Any] = {
    "conditionTypeId": 7,
    "conditionTypeKey": "iterations",
    "displayOrder": 7,
    "displayable": False,
}

_CONDITION_REPS: dict[str, Any] = {
    "conditionTypeId": 10,
    "conditionTypeKey": "reps",
    "displayOrder": 10,
    "displayable": True,
}

_NO_TARGET: dict[str, Any] = {
    "workoutTargetTypeId": 1,
    "workoutTargetTypeKey": "no.target",
    "displayOrder": 1,
}

_KILOGRAM: dict[str, Any] = {
    "unitId": 8,
    "unitKey": "kilogram",
    "factor": 1000.0,
}

# Exercise categories accepted by Garmin Connect workouts.
STRENGTH_CATEGORIES: frozenset[str] = frozenset(
    {
        "BENCH_PRESS",
        "CALF_RAISE",
        "CARDIO",
        "CARRY",
        "CHOP",
        "CORE",
        "CRUNCH",
        "CURL",
        "DEADLIFT",
        "FLYE",
        "HIP_RAISE",
        "HIP_STABILITY",
        "HIP_SWING",
        "HYPEREXTENSION",
        "LATERAL_RAISE",
        "LEG_CURL",
        "LEG_RAISE",
        "LUNGE",
        "OLYMPIC_LIFT",
        "PLANK",
        "PLYO",
        "PULL_UP",
        "PUSH_UP",
        "ROW",
        "SHOULDER_PRESS",
        "SHOULDER_STABILITY",
        "SHRUG",
        "SIT_UP",
        "SQUAT",
        "TOTAL_BODY",
        "TRICEPS_EXTENSION",
        "WARM_UP",
        "RUN",
    }
)

_EXERCISE_KEY = re.compile(r"^[A-Z0-9_]{1,80}$")

MAX_DESCRIPTION_CHARS = 512

_STEP_KINDS = ("exercise", "rest", "warmup", "cooldown")


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer, got {value!r}")
    return value


def _description(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"description must be a string, got {value!r}")
    text = value.strip()
    if not text:
        return None
    return text[:MAX_DESCRIPTION_CHARS]


def _timed_step(
    kind: str,
    seconds: int,
    order: int,
    description: str | None,
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": dict(_STEP_TYPES[kind]),
        "endCondition": dict(_CONDITION_TIME),
        "endConditionValue": float(seconds),
        "targetType": dict(_NO_TARGET),
    }
    if description:
        step["description"] = description
    return step


def _exercise_step(spec: dict[str, Any], order: int, with_exercise_keys: bool) -> dict[str, Any]:
    reps = spec.get("reps")
    duration = spec.get("duration_seconds")
    if (reps is None) == (duration is None):
        raise ValueError("an exercise step needs exactly one of 'reps' or 'duration_seconds'")

    step: dict[str, Any] = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": dict(_STEP_TYPES["interval"]),
        "targetType": dict(_NO_TARGET),
    }
    if reps is not None:
        step["endCondition"] = dict(_CONDITION_REPS)
        step["endConditionValue"] = float(_positive_int(reps, "reps"))
    else:
        step["endCondition"] = dict(_CONDITION_TIME)
        step["endConditionValue"] = float(_positive_int(duration, "duration_seconds"))

    category = spec.get("category")
    exercise_name = spec.get("exercise_name")
    if exercise_name is not None and category is None:
        raise ValueError("'exercise_name' requires 'category'")
    if category is not None:
        if category not in STRENGTH_CATEGORIES:
            raise ValueError(
                f"unknown category {category!r}; expected one of {sorted(STRENGTH_CATEGORIES)}"
            )
        if exercise_name is not None and (
            not isinstance(exercise_name, str) or not _EXERCISE_KEY.match(exercise_name)
        ):
            raise ValueError(
                f"exercise_name must be an UPPER_SNAKE_CASE Garmin key, got {exercise_name!r}"
            )
        if with_exercise_keys:
            step["category"] = category
            if exercise_name is not None:
                step["exerciseName"] = exercise_name

    weight = spec.get("weight_kg")
    if weight is not None:
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight <= 0:
            raise ValueError(f"weight_kg must be a positive number, got {weight!r}")
        step["weightValue"] = float(weight)
        step["weightUnit"] = dict(_KILOGRAM)

    description = _description(spec.get("description"))
    if description:
        step["description"] = description
    return step


def build_strength_steps(
    specs: list[dict[str, Any]],
    with_exercise_keys: bool = True,
) -> list[dict[str, Any]]:
    """Validate the caller's step specs and build Garmin workout steps.

    Spec shapes:
      - `{"type": "exercise", "sets": int, "reps": int | "duration_seconds": int,
         "rest_seconds": int (between sets, optional), "weight_kg": float
         (optional), "category": str (optional), "exercise_name": str
         (optional, requires category), "description": str (optional)}`
      - `{"type": "rest" | "warmup" | "cooldown", "duration_seconds": int,
         "description": str (optional)}`

    `with_exercise_keys=False` omits `category` / `exerciseName` (used for the
    retry when Garmin rejects an exercise key).
    """
    if not isinstance(specs, list) or not specs:
        raise ValueError("steps must be a non-empty list")

    steps: list[dict[str, Any]] = []
    order = 1

    for spec in specs:
        if not isinstance(spec, dict):
            raise ValueError(f"each step must be a dict, got {type(spec).__name__}")
        kind = spec.get("type")
        if kind not in _STEP_KINDS:
            raise ValueError(f"unknown step type {kind!r}; expected one of {list(_STEP_KINDS)}")

        if kind in ("rest", "warmup", "cooldown"):
            seconds = _positive_int(spec.get("duration_seconds"), "duration_seconds")
            steps.append(_timed_step(kind, seconds, order, _description(spec.get("description"))))
            order += 1
            continue

        sets = _positive_int(spec.get("sets", 1), "sets")
        rest_seconds = spec.get("rest_seconds", 0)
        if isinstance(rest_seconds, bool) or not isinstance(rest_seconds, int) or rest_seconds < 0:
            raise ValueError(f"rest_seconds must be a non-negative integer, got {rest_seconds!r}")

        if sets == 1:
            steps.append(_exercise_step(spec, order, with_exercise_keys))
            order += 1
            if rest_seconds > 0:
                steps.append(_timed_step("rest", rest_seconds, order, None))
                order += 1
            continue

        repeat_order = order
        order += 1
        inner = [_exercise_step(spec, order, with_exercise_keys)]
        order += 1
        if rest_seconds > 0:
            inner.append(_timed_step("rest", rest_seconds, order, None))
            order += 1

        steps.append(
            {
                "type": "RepeatGroupDTO",
                "stepOrder": repeat_order,
                "stepType": dict(_STEP_TYPES["repeat"]),
                "numberOfIterations": sets,
                "endCondition": dict(_CONDITION_ITERATIONS),
                "endConditionValue": float(sets),
                "skipLastRestStep": True,
                "smartRepeat": False,
                "workoutSteps": inner,
            }
        )

    return steps


def has_exercise_keys(steps: list[dict[str, Any]]) -> bool:
    for step in steps:
        if "category" in step or "exerciseName" in step:
            return True
        if has_exercise_keys(step.get("workoutSteps") or []):
            return True
    return False


def estimate_duration_secs(steps: list[dict[str, Any]]) -> int:
    """Sum time-based steps (reps-based steps count 0; Garmin recomputes)."""
    total = 0.0
    for step in steps:
        if step.get("type") == "RepeatGroupDTO":
            inner = step.get("workoutSteps") or []
            total += step.get("numberOfIterations", 1) * estimate_duration_secs(inner)
            # skipLastRestStep: no rest after the final set.
            if (
                step.get("skipLastRestStep")
                and inner
                and (inner[-1].get("stepType") or {}).get("stepTypeKey") == "rest"
            ):
                total -= estimate_duration_secs([inner[-1]])
            continue
        condition = step.get("endCondition") or {}
        if condition.get("conditionTypeKey") == "time":
            total += float(step.get("endConditionValue") or 0)
    return int(total)


def build_strength_workout(
    name: str,
    steps: list[dict[str, Any]],
    description: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sportType": dict(STRENGTH_SPORT_TYPE),
        "workoutName": name,
        "estimatedDurationInSecs": estimate_duration_secs(steps),
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": dict(STRENGTH_SPORT_TYPE),
                "workoutSteps": steps,
            }
        ],
    }
    text = _description(description)
    if text:
        payload["description"] = text
    return payload
