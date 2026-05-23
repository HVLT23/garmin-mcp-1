"""Tests for the workout write tools."""

from __future__ import annotations

from garminconnect import GarminConnectTooManyRequestsError
from garminconnect.workout import RepeatGroup, RunningWorkout

from tests.conftest import get_tool


def _easy_run_steps() -> list[dict]:
    return [
        {"type": "warmup", "end_condition": "time", "end_value": 600},
        {"type": "interval", "end_condition": "time", "end_value": 1800,
         "target_type": "heart_rate_zone", "target_low": 2, "target_high": 2},
        {"type": "cooldown", "end_condition": "time", "end_value": 300},
    ]


def test_schedule_running_workout_uploads_and_schedules(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(date="2026-05-26", name="Easy run", steps=_easy_run_steps())

    # Uploaded as a typed RunningWorkout.
    upload_arg = mock_garmin.upload_running_workout.call_args.args[0]
    assert isinstance(upload_arg, RunningWorkout)
    assert upload_arg.workoutName == "Easy run"

    # Scheduled with the id returned by upload + the requested date.
    mock_garmin.schedule_workout.assert_called_once_with(9876543210, "2026-05-26")

    assert result == {
        "workout_id": 9876543210,
        "scheduled_workout_id": 111222333,
        "date": "2026-05-26",
        "name": "Easy run",
    }


def test_schedule_running_workout_builds_repeat_group(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    fn(
        date="2026-05-28",
        name="6x 800m",
        steps=[
            {"type": "warmup", "end_condition": "time", "end_value": 600},
            {
                "type": "repeat",
                "iterations": 6,
                "steps": [
                    {"type": "interval", "end_condition": "distance", "end_value": 800,
                     "target_type": "pace_zone", "target_low": 4.0, "target_high": 4.17},
                    {"type": "recovery", "end_condition": "time", "end_value": 120},
                ],
            },
            {"type": "cooldown", "end_condition": "time", "end_value": 300},
        ],
    )

    upload_arg = mock_garmin.upload_running_workout.call_args.args[0]
    segment_steps = upload_arg.workoutSegments[0].workoutSteps
    # warmup, repeat group, cooldown
    assert len(segment_steps) == 3
    repeat = segment_steps[1]
    assert isinstance(repeat, RepeatGroup)
    assert repeat.numberOfIterations == 6
    assert len(repeat.workoutSteps) == 2
    # The inner interval picked up the distance end condition and pace target.
    interval = repeat.workoutSteps[0]
    assert interval.endCondition["conditionTypeKey"] == "distance"
    assert interval.endConditionValue == 800
    assert interval.targetType["workoutTargetTypeKey"] == "speed.zone"
    assert interval.targetValueOne == 4.0
    assert interval.targetValueTwo == 4.17


def test_schedule_running_workout_bad_date(mcp_with_tools) -> None:
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(date="2026/05/26", name="x", steps=_easy_run_steps())
    assert isinstance(result, dict)
    assert result.get("error") == "bad_argument"


def test_schedule_running_workout_empty_steps(mcp_with_tools) -> None:
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(date="2026-05-26", name="x", steps=[])
    assert result.get("error") == "bad_argument"


def test_schedule_running_workout_unknown_step_type(mcp_with_tools) -> None:
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(
        date="2026-05-26",
        name="x",
        steps=[{"type": "tempo", "end_condition": "time", "end_value": 600}],
    )
    assert result.get("error") == "bad_argument"


def test_schedule_running_workout_unknown_end_condition(mcp_with_tools) -> None:
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(
        date="2026-05-26",
        name="x",
        steps=[{"type": "interval", "end_condition": "vibes", "end_value": 600}],
    )
    assert result.get("error") == "bad_argument"


def test_schedule_running_workout_unknown_target_type(mcp_with_tools) -> None:
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(
        date="2026-05-26",
        name="x",
        steps=[{"type": "interval", "end_condition": "time", "end_value": 600,
                "target_type": "groove"}],
    )
    assert result.get("error") == "bad_argument"


def test_schedule_running_workout_rate_limited(mcp_with_tools, mock_garmin) -> None:
    mock_garmin.upload_running_workout.side_effect = GarminConnectTooManyRequestsError(
        "slow down"
    )
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(date="2026-05-26", name="x", steps=_easy_run_steps())
    assert result.get("error") == "rate_limited"


def test_unschedule_workout_passes_id_through(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "unschedule_workout")
    result = fn(scheduled_workout_id=111222333)
    mock_garmin.unschedule_workout.assert_called_once_with(111222333)
    assert result["scheduled_workout_id"] == 111222333


def test_delete_workout_passes_id_through(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "delete_workout")
    result = fn(workout_id=9876543210)
    mock_garmin.delete_workout.assert_called_once_with(9876543210)
    assert result["workout_id"] == 9876543210


def test_list_scheduled_workouts_shape(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "list_scheduled_workouts")
    result = fn(year=2026, month=5)
    # The lib internally subtracts 1 from month — we pass 1-12 unchanged.
    mock_garmin.get_scheduled_workouts.assert_called_once_with(2026, 5)
    assert isinstance(result, list)
    assert result[0]["scheduledWorkoutId"] == 111222333
    assert result[0]["date"] == "2026-05-26"
