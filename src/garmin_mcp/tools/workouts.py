"""Workout MCP tools (write surface — schedule structured running workouts)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from garminconnect import Garmin
from garminconnect.workout import (
    ExecutableStep,
    RepeatGroup,
    RunningWorkout,
    WorkoutSegment,
    create_cooldown_step,
    create_interval_step,
    create_recovery_step,
    create_repeat_group,
    create_warmup_step,
)
from mcp.server.fastmcp import FastMCP

from garmin_mcp.tools._helpers import audited, coerce_date, safe_call
from garmin_mcp.tools._trimmers import trim_scheduled_workouts

ClientFactory = Callable[[], Garmin]


# Garmin's API uses small integer IDs plus a string key for these enums.
# IDs are taken from `garminconnect.workout.{ConditionType,TargetType,StepType}`.
_END_CONDITIONS: dict[str, dict[str, Any]] = {
    "time": {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2,
             "displayable": True},
    "distance": {"conditionTypeId": 1, "conditionTypeKey": "distance", "displayOrder": 1,
                 "displayable": True},
    "heart_rate": {"conditionTypeId": 3, "conditionTypeKey": "heart.rate", "displayOrder": 3,
                   "displayable": True},
    "calories": {"conditionTypeId": 4, "conditionTypeKey": "calories", "displayOrder": 4,
                 "displayable": True},
    "cadence": {"conditionTypeId": 5, "conditionTypeKey": "cadence", "displayOrder": 5,
                "displayable": True},
}

# Garmin's `targetType` enum collapses zone-flavored and range-flavored
# targets under the same id — `heart_rate_zone` and `heart_rate` both ride
# id 4 / key `heart.rate.zone`; `pace_zone` and `pace` both ride id 5 /
# key `speed.zone`. The distinction is purely encoding: a zone target
# emits `step.zoneNumber`, a range target emits `targetValueOne/Two`.
# Garmin renders them differently on the watch and on Connect ("Zone 2"
# vs "140-160 bpm"), but the wire-level target-type block is identical.
_TARGET_TYPES: dict[str, dict[str, Any]] = {
    "none": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1},
    "heart_rate_zone": {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone",
                        "displayOrder": 4},
    "heart_rate": {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone",
                   "displayOrder": 4},
    "pace_zone": {"workoutTargetTypeId": 5, "workoutTargetTypeKey": "speed.zone",
                  "displayOrder": 5},
    "pace": {"workoutTargetTypeId": 5, "workoutTargetTypeKey": "speed.zone",
             "displayOrder": 5},
    "cadence": {"workoutTargetTypeId": 3, "workoutTargetTypeKey": "cadence", "displayOrder": 3},
    "open": {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "open", "displayOrder": 6},
}

# Each `target_type` has exactly one valid input shape:
#   - "zone"  → integer `zone` 1-5; rendered as `step.zoneNumber`
#   - "range" → numeric `target_low` + `target_high`; rendered as
#               `targetValueOne/Two` (bpm for HR, m/s for pace, spm for
#               cadence)
#   - "none"  → no extra fields
#
# `redirect` is the alternative target_type a caller probably meant when
# they pass the wrong field shape. Surfacing it in the error message lets
# the LLM self-correct in one shot instead of trial-and-error round-trips.
_TARGET_SHAPES: dict[str, dict[str, Any]] = {
    "heart_rate_zone": {"kind": "zone",  "redirect": "heart_rate"},
    "pace_zone":       {"kind": "zone",  "redirect": "pace"},
    "heart_rate":      {"kind": "range", "redirect": "heart_rate_zone"},
    "pace":            {"kind": "range", "redirect": "pace_zone"},
    "cadence":         {"kind": "range", "redirect": None},
    "open":            {"kind": "none",  "redirect": None},
    "none":            {"kind": "none",  "redirect": None},
}

_STEP_BUILDERS: dict[str, Callable[..., ExecutableStep]] = {
    "warmup": create_warmup_step,
    "interval": create_interval_step,
    "recovery": create_recovery_step,
    "cooldown": create_cooldown_step,
}


def _build_executable_step(spec: dict[str, Any], step_order: int) -> ExecutableStep:
    step_type = spec.get("type")
    builder = _STEP_BUILDERS.get(step_type) if isinstance(step_type, str) else None
    if builder is None:
        raise ValueError(
            f"unknown step type {step_type!r}; expected one of "
            f"{[*sorted(_STEP_BUILDERS), 'repeat']}"
        )

    end_condition = spec.get("end_condition", "time")
    if end_condition not in _END_CONDITIONS:
        raise ValueError(
            f"unknown end_condition {end_condition!r}; expected one of "
            f"{sorted(_END_CONDITIONS)}"
        )

    raw_end_value = spec.get("end_value")
    if raw_end_value is None:
        raise ValueError(f"end_value is required for step type {step_type!r}")
    if isinstance(raw_end_value, bool):
        raise ValueError(f"end_value must be numeric, got {raw_end_value!r}")
    try:
        end_value = float(raw_end_value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"end_value must be numeric, got {raw_end_value!r}") from e
    if end_value <= 0:
        raise ValueError(f"end_value must be positive, got {raw_end_value!r}")

    target = spec.get("target_type", "none")
    if target not in _TARGET_TYPES:
        raise ValueError(
            f"unknown target_type {target!r}; expected one of {sorted(_TARGET_TYPES)}"
        )
    target_dict = dict(_TARGET_TYPES[target])

    step = builder(end_value, step_order, target_dict)

    # The helpers always default to a TIME end condition; override when callers
    # asked for something else (distance, heart_rate, calories, cadence).
    if end_condition != "time":
        step.endCondition = dict(_END_CONDITIONS[end_condition])
    step.endConditionValue = end_value

    _apply_target(step, target, spec)

    description = spec.get("description")
    if description:
        step.description = description

    return step


def _apply_target(step: ExecutableStep, target: str, spec: dict[str, Any]) -> None:
    """Validate the spec's target fields against `_TARGET_SHAPES[target]` and
    set the corresponding attribute(s) on `step`.

    Each target_type has exactly one valid input shape (zone, range, or
    none). Passing fields that don't belong to the shape is rejected with
    a message that names the expected fields *and* the target_type the
    caller probably meant — the redirect hint matters because the LLM
    will sometimes pick the wrong target_type, and a one-line correction
    saves a round-trip.
    """
    shape = _TARGET_SHAPES[target]
    kind = shape["kind"]
    redirect = shape["redirect"]
    zone = spec.get("zone")
    target_low = spec.get("target_low")
    target_high = spec.get("target_high")

    if kind == "zone":
        if target_low is not None or target_high is not None:
            hint = (
                f" Did you mean target_type={redirect!r} for a custom range?"
                if redirect
                else ""
            )
            raise ValueError(
                f"target_type={target!r} expects a single 'zone' field (int 1-5); "
                f"'target_low'/'target_high' are not valid for this target_type.{hint}"
            )
        if zone is None:
            raise ValueError(
                f"target_type={target!r} requires a 'zone' field (int 1-5)"
            )
        # `bool` is an `int` subclass — reject so `True` doesn't slip
        # through as zone 1. Non-int (float, str) is also rejected:
        # silently truncating `2.7` to zone 2 would mis-coach the runner.
        if not isinstance(zone, int) or isinstance(zone, bool):
            raise ValueError(
                f"target_type={target!r} 'zone' must be an int 1-5, got {zone!r}"
            )
        if zone < 1 or zone > 5:
            raise ValueError(
                f"target_type={target!r} 'zone' must be 1-5, got {zone!r}"
            )
        step.zoneNumber = zone
        return

    if kind == "range":
        if zone is not None:
            hint = (
                f" Did you mean target_type={redirect!r} for a zone-based target?"
                if redirect
                else ""
            )
            raise ValueError(
                f"target_type={target!r} expects 'target_low' and 'target_high' "
                f"(numeric range); 'zone' is not valid for this target_type.{hint}"
            )
        if target_low is None or target_high is None:
            raise ValueError(
                f"target_type={target!r} requires both 'target_low' and "
                "'target_high' (numeric bounds)"
            )
        for label, value in (("target_low", target_low), ("target_high", target_high)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    f"target_type={target!r} {label!r} must be numeric, got {value!r}"
                )
        step.targetValueOne = float(target_low)
        step.targetValueTwo = float(target_high)
        return

    # kind == "none": open / no target — reject any stray target fields so
    # mistakes ("I added a target but didn't change target_type") surface.
    if zone is not None or target_low is not None or target_high is not None:
        raise ValueError(
            f"target_type={target!r} takes no target fields; "
            "remove 'zone', 'target_low', and 'target_high'"
        )


def _build_steps(
    specs: list[dict[str, Any]], start_order: int = 1
) -> tuple[list[ExecutableStep | RepeatGroup], int]:
    """Recursively build a flat list of executable/repeat steps.

    Returns the built list and the next available stepOrder (so the caller
    can continue numbering after a nested repeat group's children).
    """
    if not isinstance(specs, list):
        raise ValueError("steps must be a list")
    result: list[ExecutableStep | RepeatGroup] = []
    order = start_order
    for spec in specs:
        if not isinstance(spec, dict):
            raise ValueError(f"each step must be a dict, got {type(spec).__name__}")
        if spec.get("type") == "repeat":
            iterations = spec.get("iterations")
            if (
                not isinstance(iterations, int)
                or isinstance(iterations, bool)
                or iterations < 1
            ):
                raise ValueError(
                    f"repeat step requires positive integer 'iterations', got {iterations!r}"
                )
            inner = spec.get("steps")
            if not isinstance(inner, list) or not inner:
                raise ValueError("repeat step requires a non-empty 'steps' list")
            repeat_order = order
            inner_steps, order = _build_steps(inner, repeat_order + 1)
            result.append(create_repeat_group(iterations, inner_steps, repeat_order))
        else:
            result.append(_build_executable_step(spec, order))
            order += 1
    return result, order


# Garmin's `POST /schedule/{workout_id}` response shape isn't documented and
# isn't stable across Connect versions. The calendar-listing endpoint exposes
# the scheduled-entry id under `id`; older clients also saw `scheduledWorkoutId`
# and `workoutScheduleId`. Try the named keys first, then fall back to `id` —
# whichever survives is the integer to return.
_SCHEDULED_WORKOUT_ID_KEYS = ("scheduledWorkoutId", "workoutScheduleId", "id")


def _extract_scheduled_workout_id(resp: Any) -> int | None:
    if not isinstance(resp, dict):
        return None
    for key in _SCHEDULED_WORKOUT_ID_KEYS:
        val = resp.get(key)
        if isinstance(val, int) and not isinstance(val, bool):
            return val
    return None


def _estimate_duration_secs(steps: list[ExecutableStep | RepeatGroup]) -> int:
    """Sum time-based end conditions, multiplying through repeat groups.

    Non-time steps contribute 0 — Garmin recomputes this server-side from the
    actual workout, so an approximation is fine.
    """
    total = 0.0
    for step in steps:
        if isinstance(step, RepeatGroup):
            total += step.numberOfIterations * _estimate_duration_secs(step.workoutSteps)
        else:
            cond = step.endCondition or {}
            if cond.get("conditionTypeKey") == "time" and step.endConditionValue is not None:
                total += float(step.endConditionValue)
    return int(total)


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool()
    @audited
    @safe_call
    def schedule_running_workout(
        date: str,
        name: str,
        steps: list[dict[str, Any]],
        description: str = "",
    ) -> dict[str, Any]:
        """Upload a structured running workout and schedule it on the user's calendar.

        Two Garmin API calls in one: create a workout template, then place it on
        `date`. Returns both identifiers so callers can manage either side
        independently afterwards (`unschedule_workout` takes the
        `scheduled_workout_id`; `delete_workout` takes the `workout_id`).

        Args:
            date: Target calendar date (`YYYY-MM-DD`). Required — there is no
                "today" default for scheduling.
            name: Workout name shown in Garmin Connect / on the watch.
            steps: Flat list of step dicts. Supported `type` values are
                `warmup`, `interval`, `recovery`, `cooldown`, and `repeat`.
                For `repeat`: `{"type": "repeat", "iterations": int,
                "steps": [...]}` containing the inner steps.
                Other (non-repeat) step fields:
                  - `end_condition`: one of `time` (seconds), `distance`
                    (meters), `heart_rate` (bpm), `calories`, `cadence`
                  - `end_value`: numeric value matching `end_condition`
                  - `target_type` (optional, default `none`): selects the
                    step target shape. Each target_type has exactly one
                    valid input shape; passing fields from the wrong shape
                    is rejected with a hint about the right target_type:

                    | target_type       | required fields                  | meaning                              |
                    | ----------------- | -------------------------------- | ------------------------------------ |
                    | `heart_rate_zone` | `zone` (int 1-5)                 | run in HR zone N                     |
                    | `heart_rate`      | `target_low`, `target_high` (bpm)| run in a custom bpm range            |
                    | `pace_zone`       | `zone` (int 1-5)                 | run in pace zone N                   |
                    | `pace`            | `target_low`, `target_high` (m/s)| run in a custom m/s pace range       |
                    | `cadence`         | `target_low`, `target_high` (spm)| run in a custom cadence range (live-render unverified — if it shows as a raw spm range instead of a zone label, this is the place that needs the `zone` treatment) |
                    | `open`            | (none)                           | open / freeform target               |
                    | `none`            | (none)                           | no target                            |

                  - `description` (optional): freeform note shown on the step
            description: Optional freeform description attached to the
                workout template.

        Returns:
            `{"workout_id": int, "scheduled_workout_id": int, "date": str,
            "name": str}`.
        """
        if date is None:
            raise ValueError("date is required")
        date_iso = coerce_date(date)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name is required")
        if not isinstance(steps, list) or not steps:
            raise ValueError("steps must be a non-empty list")

        built_steps, _ = _build_steps(steps, start_order=1)
        segment = WorkoutSegment(
            segmentOrder=1,
            sportType={
                "sportTypeId": 1,
                "sportTypeKey": "running",
                "displayOrder": 1,
            },
            workoutSteps=built_steps,
        )
        workout = RunningWorkout(
            workoutName=name,
            estimatedDurationInSecs=_estimate_duration_secs(built_steps),
            workoutSegments=[segment],
            description=description or None,
        )

        client = client_factory()
        upload_resp = client.upload_running_workout(workout)
        workout_id = upload_resp.get("workoutId")
        if workout_id is None:
            raise ValueError(f"Garmin did not return a workoutId; response: {upload_resp!r}")

        schedule_resp = client.schedule_workout(workout_id, date_iso)
        scheduled_workout_id = _extract_scheduled_workout_id(schedule_resp)

        return {
            "workout_id": workout_id,
            "scheduled_workout_id": scheduled_workout_id,
            "date": date_iso,
            "name": name,
        }

    @mcp.tool()
    @audited
    @safe_call
    def unschedule_workout(scheduled_workout_id: int) -> dict[str, Any]:
        """Remove a workout from the calendar without deleting the template.

        Note: this takes `scheduled_workout_id` (returned by
        `schedule_running_workout`), NOT `workout_id`. The template remains in
        the user's workout library — use `delete_workout` to remove the
        template itself.

        Returns the raw Garmin response (typically empty / a status dict).
        """
        result = client_factory().unschedule_workout(scheduled_workout_id)
        return {"scheduled_workout_id": int(scheduled_workout_id), "result": result}

    @mcp.tool()
    @audited
    @safe_call
    def delete_workout(workout_id: int) -> dict[str, Any]:
        """Delete a workout template from the user's library.

        Takes `workout_id` (returned by `schedule_running_workout`), NOT
        `scheduled_workout_id`. If the workout is currently scheduled,
        deleting the template also removes its calendar entry. Returns the
        raw Garmin response.
        """
        result = client_factory().delete_workout(workout_id)
        return {"workout_id": int(workout_id), "result": result}

    @mcp.tool()
    @audited
    @safe_call
    def list_scheduled_workouts(year: int, month: int, verbose: bool = False) -> Any:
        """List calendar items scheduled in the user's calendar for a given month.

        By default (verbose=False) the upstream `/calendar/year/{y}/month/{m}`
        response is trimmed: each entry under `calendarItems` keeps only the
        analytically useful fields. Dropped: ~50 dive / badge / social /
        swim / pack / nap fields that don't apply to scheduled workouts,
        plus any field whose value is null on a given entry. Retained per
        entry: `id` (the scheduled-workout id — pass this to
        `unschedule_workout`), `workoutId`, `title`, `date`, `sportTypeKey`,
        `itemType`, `trainingPlanId`, and a handful of populated activity /
        workout stats.

        Pass verbose=True to get the un-modified upstream response (~75K
        chars for a normal month — mostly `null` noise).

        Args:
            year: 4-digit year (>= 2000).
            month: 1-12. The underlying library converts to Garmin's
                0-indexed month internally — pass the natural 1-12 value.
            verbose: If True, return the full upstream payload (default False).
        """
        full = client_factory().get_scheduled_workouts(year, month)
        return full if verbose else trim_scheduled_workouts(full)
