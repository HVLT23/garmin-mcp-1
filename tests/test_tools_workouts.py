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

    # stepOrder is a single depth-first sequence across the whole workout —
    # repeat group gets N, its children N+1, N+2, next top-level continues
    # after them. Load-bearing assumption Garmin relies on, so assert it.
    warmup, repeat_grp, cooldown = segment_steps
    inner_interval, inner_recovery = repeat_grp.workoutSteps
    assert warmup.stepOrder == 1
    assert repeat_grp.stepOrder == 2
    assert inner_interval.stepOrder == 3
    assert inner_recovery.stepOrder == 4
    assert cooldown.stepOrder == 5


def test_extra_fields_survive_to_dict(mcp_with_tools, mock_garmin) -> None:
    """Targets and step descriptions are attribute-assigned after construction,
    which only round-trips through `to_dict()` because ExecutableStep uses
    pydantic `extra="allow"`. Regression-guard that contract — if a future SDK
    release tightens it to `extra="ignore"`, this test fails loudly instead of
    silently dropping targets from uploaded workouts.
    """
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    fn(
        date="2026-05-26",
        name="With targets",
        steps=[
            {"type": "interval", "end_condition": "distance", "end_value": 800,
             "target_type": "pace_zone", "target_low": 4.0, "target_high": 4.17,
             "description": "stay smooth"},
        ],
    )

    upload_arg = mock_garmin.upload_running_workout.call_args.args[0]
    payload = upload_arg.to_dict()
    step = payload["workoutSegments"][0]["workoutSteps"][0]
    assert step["targetValueOne"] == 4.0
    assert step["targetValueTwo"] == 4.17
    assert step["description"] == "stay smooth"


def test_hr_zone_target_emits_zone_number(mcp_with_tools, mock_garmin) -> None:
    """Garmin renders HR-zone targets from a `zoneNumber` field, not a
    bpm range. Live test: a recovery step with target_low=target_high=2
    rendered as `2-2 bpm` (raw heart-rate range) when we sent
    targetValueOne/Two. The fix is to set `zoneNumber` instead.
    """
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    fn(
        date="2026-05-26",
        name="HR zone test",
        steps=[
            {"type": "recovery", "end_condition": "time", "end_value": 120,
             "target_type": "heart_rate_zone", "target_low": 2, "target_high": 2},
        ],
    )

    upload_arg = mock_garmin.upload_running_workout.call_args.args[0]
    payload = upload_arg.to_dict()
    step = payload["workoutSegments"][0]["workoutSteps"][0]
    assert step.get("zoneNumber") == 2
    assert isinstance(step["zoneNumber"], int)
    assert "targetValueOne" not in step
    assert "targetValueTwo" not in step


def test_pace_zone_target_keeps_value_one_two(mcp_with_tools, mock_garmin) -> None:
    """Regression guard for the HR-zone fix: pace_zone steps must continue
    to emit raw m/s bounds in targetValueOne/Two (verified live to render
    correctly as e.g. `4:00-4:10 min/km`).
    """
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    fn(
        date="2026-05-26",
        name="Pace zone test",
        steps=[
            {"type": "interval", "end_condition": "distance", "end_value": 800,
             "target_type": "pace_zone", "target_low": 4.0, "target_high": 4.17},
        ],
    )

    upload_arg = mock_garmin.upload_running_workout.call_args.args[0]
    payload = upload_arg.to_dict()
    step = payload["workoutSegments"][0]["workoutSteps"][0]
    assert step["targetValueOne"] == 4.0
    assert step["targetValueTwo"] == 4.17
    assert "zoneNumber" not in step


def test_hr_zone_target_rejects_out_of_range(mcp_with_tools) -> None:
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(
        date="2026-05-26",
        name="x",
        steps=[
            {"type": "interval", "end_condition": "time", "end_value": 600,
             "target_type": "heart_rate_zone", "target_low": 7, "target_high": 7},
        ],
    )
    assert result.get("error") == "bad_argument"


def test_hr_zone_target_rejects_non_int_target_low(mcp_with_tools) -> None:
    """A caller passing `target_low=2.7` would silently truncate to zone 2
    without this guard — a mis-coached step is worse than a loud error.
    """
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(
        date="2026-05-26",
        name="x",
        steps=[
            {"type": "interval", "end_condition": "time", "end_value": 600,
             "target_type": "heart_rate_zone", "target_low": 2.7},
        ],
    )
    assert result.get("error") == "bad_argument"


def test_hr_zone_target_rejects_mismatched_high(mcp_with_tools) -> None:
    """`target_low=2, target_high=4` looks like "zones 2-4" but Garmin
    only takes a single zone. Reject loudly rather than silently shipping
    just zone 2."""
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(
        date="2026-05-26",
        name="x",
        steps=[
            {"type": "interval", "end_condition": "time", "end_value": 600,
             "target_type": "heart_rate_zone", "target_low": 2, "target_high": 4},
        ],
    )
    assert result.get("error") == "bad_argument"


def test_hr_zone_target_accepts_omitted_high(mcp_with_tools, mock_garmin) -> None:
    """`target_high` is optional — omitting it is the cleanest call shape
    now that "must equal or be omitted" is the rule."""
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(
        date="2026-05-26",
        name="x",
        steps=[
            {"type": "interval", "end_condition": "time", "end_value": 600,
             "target_type": "heart_rate_zone", "target_low": 3},
        ],
    )
    assert "error" not in result
    upload_arg = mock_garmin.upload_running_workout.call_args.args[0]
    step = upload_arg.to_dict()["workoutSegments"][0]["workoutSteps"][0]
    assert step["zoneNumber"] == 3


def test_schedule_response_id_field(mcp_with_tools, mock_garmin) -> None:
    """The live Garmin response uses `id` for the scheduled-workout id —
    not `scheduledWorkoutId`. Conftest fixture matches the live shape;
    this asserts the extractor handles it.
    """
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(date="2026-05-26", name="x", steps=_easy_run_steps())
    assert result["scheduled_workout_id"] == 111222333


def test_schedule_response_scheduledworkoutid_field(mcp_with_tools, mock_garmin) -> None:
    """Some Garmin Connect versions use `scheduledWorkoutId` — keep that
    name in the extraction priority list.
    """
    mock_garmin.schedule_workout.return_value = {"scheduledWorkoutId": 999}
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(date="2026-05-26", name="x", steps=_easy_run_steps())
    assert result["scheduled_workout_id"] == 999


def test_schedule_response_workoutscheduleid_field(mcp_with_tools, mock_garmin) -> None:
    """Cover the third key in the priority chain so deleting it from
    `_SCHEDULED_WORKOUT_ID_KEYS` would break a test (otherwise the branch
    is dead code per coverage)."""
    mock_garmin.schedule_workout.return_value = {"workoutScheduleId": 555}
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(date="2026-05-26", name="x", steps=_easy_run_steps())
    assert result["scheduled_workout_id"] == 555


def test_schedule_response_priority_order(mcp_with_tools, mock_garmin) -> None:
    """When multiple recognised keys are present, `scheduledWorkoutId` wins
    over `id`. Pins the priority order in `_SCHEDULED_WORKOUT_ID_KEYS` so
    reordering the tuple (e.g. promoting `id` because it's the live shape)
    would surface as a regression instead of a silent miscompute."""
    mock_garmin.schedule_workout.return_value = {"id": 1, "scheduledWorkoutId": 2}
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(date="2026-05-26", name="x", steps=_easy_run_steps())
    assert result["scheduled_workout_id"] == 2


def test_schedule_response_unknown_shape_returns_none(mcp_with_tools, mock_garmin) -> None:
    """If Garmin returns a shape we don't recognise, we surface None rather
    than guessing the wrong field. The schedule itself still succeeded —
    the caller can recover the id via `list_scheduled_workouts`.
    """
    mock_garmin.schedule_workout.return_value = {"unexpected": "shape"}
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(date="2026-05-26", name="x", steps=_easy_run_steps())
    assert result["scheduled_workout_id"] is None
    assert result["workout_id"] == 9876543210


def test_schedule_running_workout_negative_end_value(mcp_with_tools) -> None:
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(
        date="2026-05-26",
        name="x",
        steps=[{"type": "interval", "end_condition": "time", "end_value": -600}],
    )
    assert result.get("error") == "bad_argument"


def test_schedule_running_workout_zero_end_value(mcp_with_tools) -> None:
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(
        date="2026-05-26",
        name="x",
        steps=[{"type": "interval", "end_condition": "time", "end_value": 0}],
    )
    assert result.get("error") == "bad_argument"


def test_schedule_running_workout_bool_end_value(mcp_with_tools) -> None:
    """`bool` is an `int` subclass — guard against `end_value=True` slipping
    through as a 1-second step."""
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(
        date="2026-05-26",
        name="x",
        steps=[{"type": "interval", "end_condition": "time", "end_value": True}],
    )
    assert result.get("error") == "bad_argument"


def test_schedule_running_workout_bool_iterations(mcp_with_tools) -> None:
    """Same `bool is int` trap on repeat iterations."""
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(
        date="2026-05-26",
        name="x",
        steps=[
            {
                "type": "repeat",
                "iterations": True,
                "steps": [{"type": "interval", "end_condition": "time", "end_value": 60}],
            }
        ],
    )
    assert result.get("error") == "bad_argument"


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
    # Trimmed: dict with `calendarItems`, each entry keeps `id` (the
    # scheduled-workout id), `workoutId`, `title`, `date`, `itemType`.
    assert isinstance(result, dict)
    items = result["calendarItems"]
    assert len(items) == 2
    first = items[0]
    assert first["id"] == 1657942990
    assert first["workoutId"] == 1576479785
    assert first["date"] == "2026-05-24"
    assert first["itemType"] == "workout"


def test_list_scheduled_workouts_trims_noise(mcp_with_tools) -> None:
    """The trim must drop the dive / badge / swim noise fields plus any
    value that is null on the entry. All assertions here target keys that
    are only dropped by `_SCHEDULED_WORKOUT_DROP_ALWAYS` — `*Local`
    suffixes are handled separately by `_strip_pii` and tested elsewhere.
    """
    fn = get_tool(mcp_with_tools, "list_scheduled_workouts")
    result = fn(year=2026, month=5)
    first = result["calendarItems"][0]
    for dropped in ("bottomTime", "userBadgeId", "maxDepth",
                    "shareableEventUuid", "wellnessActivityUuid", "strokes"):
        assert dropped not in first, f"{dropped!r} should be trimmed"
    # Null-valued fields on this specific entry also drop.
    assert "duration" not in first  # was null on the workout entry
    assert "calories" not in first


def test_list_scheduled_workouts_verbose_returns_untrimmed(mcp_with_tools) -> None:
    fn = get_tool(mcp_with_tools, "list_scheduled_workouts")
    result = fn(year=2026, month=5, verbose=True)
    # Untrimmed: noise fields survive (null values kept).
    first = result["calendarItems"][0]
    assert "napStartTimeLocal" in first
    assert "bottomTime" in first
    assert "userBadgeId" in first
