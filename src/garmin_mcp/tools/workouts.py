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

_TARGET_TYPES: dict[str, dict[str, Any]] = {
    "none": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1},
    "heart_rate_zone": {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone",
                        "displayOrder": 4},
    "pace_zone": {"workoutTargetTypeId": 5, "workoutTargetTypeKey": "speed.zone",
                  "displayOrder": 5},
    "cadence": {"workoutTargetTypeId": 3, "workoutTargetTypeKey": "cadence", "displayOrder": 3},
    "open": {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "open", "displayOrder": 6},
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

    target_low = spec.get("target_low")
    target_high = spec.get("target_high")
    if target_low is not None:
        step.targetValueOne = float(target_low)
    if target_high is not None:
        step.targetValueTwo = float(target_high)

    description = spec.get("description")
    if description:
        step.description = description

    return step


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
                  - `target_type` (optional): `none`, `heart_rate_zone`,
                    `pace_zone`, `cadence`, `open`
                  - `target_low`, `target_high` (optional): numeric bounds.
                    HR zone uses 1-5; pace zone uses m/s; cadence uses spm
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
        scheduled_workout_id = schedule_resp.get("scheduledWorkoutId")

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
    def list_scheduled_workouts(year: int, month: int) -> Any:
        """List workouts scheduled in the user's calendar for a given month.

        Returns the raw upstream payload — typically a list of dicts each
        carrying at least `scheduledWorkoutId`, `workoutId`, `workoutName`,
        `date`, and `sportType`. Garmin may include additional fields; this
        tool does not trim or normalize them.

        Args:
            year: 4-digit year (>= 2000).
            month: 1-12. The underlying library converts to Garmin's
                0-indexed month internally — pass the natural 1-12 value.
        """
        return client_factory().get_scheduled_workouts(year, month)
