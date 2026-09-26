"""Tests for the workout write tools."""

from __future__ import annotations

from garminconnect import GarminConnectTooManyRequestsError
from garminconnect.workout import RepeatGroup, RunningWorkout

from tests.conftest import get_tool, load_fixture


def _easy_run_steps() -> list[dict]:
    return [
        {"type": "warmup", "end_condition": "time", "end_value": 600},
        {"type": "interval", "end_condition": "time", "end_value": 1800,
         "target_type": "heart_rate_zone", "zone": 2},
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
                     "target_type": "pace", "target_low": 4.0, "target_high": 4.17},
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
             "target_type": "pace", "target_low": 4.0, "target_high": 4.17,
             "description": "stay smooth"},
        ],
    )

    upload_arg = mock_garmin.upload_running_workout.call_args.args[0]
    payload = upload_arg.to_dict()
    step = payload["workoutSegments"][0]["workoutSteps"][0]
    assert step["targetValueOne"] == 4.0
    assert step["targetValueTwo"] == 4.17
    assert step["description"] == "stay smooth"


def test_heart_rate_zone_emits_zone_number(mcp_with_tools, mock_garmin) -> None:
    """`heart_rate_zone` is the zone-flavored HR target: takes `zone: int`
    (1-5) and Garmin renders it as "Zone N". The live test in #26 caught
    the original bug — using `targetValueOne/Two` for HR-zone made
    Connect display "2-2 bpm" instead of "Zone 2".
    """
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    fn(
        date="2026-05-26",
        name="HR zone test",
        steps=[
            {"type": "recovery", "end_condition": "time", "end_value": 120,
             "target_type": "heart_rate_zone", "zone": 2},
        ],
    )

    upload_arg = mock_garmin.upload_running_workout.call_args.args[0]
    payload = upload_arg.to_dict()
    step = payload["workoutSegments"][0]["workoutSteps"][0]
    assert step.get("zoneNumber") == 2
    assert isinstance(step["zoneNumber"], int)
    assert "targetValueOne" not in step
    assert "targetValueTwo" not in step


def test_heart_rate_range_emits_target_values(mcp_with_tools, mock_garmin) -> None:
    """`heart_rate` is the custom-range HR target: takes `target_low` /
    `target_high` in bpm and emits `targetValueOne/Two`. Same Garmin
    target-type key as `heart_rate_zone` (`heart.rate.zone`, id 4) — the
    distinction is the field shape, not the wire enum."""
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    fn(
        date="2026-05-26",
        name="HR range test",
        steps=[
            {"type": "interval", "end_condition": "time", "end_value": 600,
             "target_type": "heart_rate", "target_low": 140, "target_high": 160},
        ],
    )

    upload_arg = mock_garmin.upload_running_workout.call_args.args[0]
    payload = upload_arg.to_dict()
    step = payload["workoutSegments"][0]["workoutSteps"][0]
    assert step["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert step["targetValueOne"] == 140.0
    assert step["targetValueTwo"] == 160.0
    assert "zoneNumber" not in step


def test_pace_zone_emits_zone_number(mcp_with_tools, mock_garmin) -> None:
    """`pace_zone` is now the zone-flavored pace target (parallel to
    `heart_rate_zone`): takes `zone: int` and emits `zoneNumber`. The
    custom m/s range that used to ride under this name is now `pace`."""
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    fn(
        date="2026-05-26",
        name="Pace zone test",
        steps=[
            {"type": "interval", "end_condition": "distance", "end_value": 800,
             "target_type": "pace_zone", "zone": 4},
        ],
    )

    upload_arg = mock_garmin.upload_running_workout.call_args.args[0]
    payload = upload_arg.to_dict()
    step = payload["workoutSegments"][0]["workoutSteps"][0]
    assert step.get("zoneNumber") == 4
    assert step["targetType"]["workoutTargetTypeKey"] == "speed.zone"
    assert "targetValueOne" not in step
    assert "targetValueTwo" not in step


def test_pace_range_emits_target_values(mcp_with_tools, mock_garmin) -> None:
    """`pace` is the custom-range pace target: takes `target_low` /
    `target_high` in m/s and emits `targetValueOne/Two`. This is the
    shape that was verified live in #26 to render as `4:00-4:10 min/km`."""
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    fn(
        date="2026-05-26",
        name="Pace range test",
        steps=[
            {"type": "interval", "end_condition": "distance", "end_value": 800,
             "target_type": "pace", "target_low": 4.0, "target_high": 4.17},
        ],
    )

    upload_arg = mock_garmin.upload_running_workout.call_args.args[0]
    payload = upload_arg.to_dict()
    step = payload["workoutSegments"][0]["workoutSteps"][0]
    assert step["targetType"]["workoutTargetTypeKey"] == "speed.zone"
    assert step["targetValueOne"] == 4.0
    assert step["targetValueTwo"] == 4.17
    assert "zoneNumber" not in step


def test_cadence_range_emits_target_values(mcp_with_tools, mock_garmin) -> None:
    """`cadence` is a custom range only (no `cadence_zone` — Garmin
    doesn't have zone-based cadence)."""
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    fn(
        date="2026-05-26",
        name="Cadence test",
        steps=[
            {"type": "interval", "end_condition": "time", "end_value": 600,
             "target_type": "cadence", "target_low": 170, "target_high": 180},
        ],
    )
    upload_arg = mock_garmin.upload_running_workout.call_args.args[0]
    step = upload_arg.to_dict()["workoutSegments"][0]["workoutSteps"][0]
    assert step["targetValueOne"] == 170.0
    assert step["targetValueTwo"] == 180.0
    assert "zoneNumber" not in step


def test_heart_rate_zone_rejects_out_of_range(mcp_with_tools) -> None:
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(
        date="2026-05-26",
        name="x",
        steps=[
            {"type": "interval", "end_condition": "time", "end_value": 600,
             "target_type": "heart_rate_zone", "zone": 7},
        ],
    )
    assert result.get("error") == "bad_argument"


def test_heart_rate_zone_rejects_non_int_zone(mcp_with_tools) -> None:
    """A caller passing `zone=2.7` would silently truncate to zone 2
    without this guard — a mis-coached step is worse than a loud error.
    """
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(
        date="2026-05-26",
        name="x",
        steps=[
            {"type": "interval", "end_condition": "time", "end_value": 600,
             "target_type": "heart_rate_zone", "zone": 2.7},
        ],
    )
    assert result.get("error") == "bad_argument"


def test_heart_rate_zone_rejects_target_low_with_redirect(
    mcp_with_tools, mock_garmin
) -> None:
    """Wrong-shape rejection must name the redirect target_type so the LLM
    can self-correct in one shot. A caller writing
    `heart_rate_zone` + `target_low: 140` almost certainly meant
    `heart_rate` (the custom-range variant)."""
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(
        date="2026-05-26",
        name="x",
        steps=[
            {"type": "interval", "end_condition": "time", "end_value": 600,
             "target_type": "heart_rate_zone", "target_low": 140, "target_high": 160},
        ],
    )
    assert result.get("error") == "bad_argument"
    assert "heart_rate" in result["message"]
    mock_garmin.upload_running_workout.assert_not_called()


def test_heart_rate_range_rejects_zone_with_redirect(mcp_with_tools) -> None:
    """Mirror redirect: `heart_rate` + `zone` should suggest
    `heart_rate_zone`."""
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(
        date="2026-05-26",
        name="x",
        steps=[
            {"type": "interval", "end_condition": "time", "end_value": 600,
             "target_type": "heart_rate", "zone": 2},
        ],
    )
    assert result.get("error") == "bad_argument"
    assert "heart_rate_zone" in result["message"]


def test_pace_zone_rejects_target_low_with_redirect(mcp_with_tools) -> None:
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(
        date="2026-05-26",
        name="x",
        steps=[
            {"type": "interval", "end_condition": "distance", "end_value": 800,
             "target_type": "pace_zone", "target_low": 4.0, "target_high": 4.17},
        ],
    )
    assert result.get("error") == "bad_argument"
    # Must mention `pace` as the redirect; just substring on the slug to
    # avoid coupling to exact phrasing. `pace_zone` is the input, so
    # finding a bare `'pace'` token requires asserting on the redirect
    # form specifically.
    assert "'pace'" in result["message"]


def test_pace_range_rejects_zone_with_redirect(mcp_with_tools) -> None:
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(
        date="2026-05-26",
        name="x",
        steps=[
            {"type": "interval", "end_condition": "distance", "end_value": 800,
             "target_type": "pace", "zone": 4},
        ],
    )
    assert result.get("error") == "bad_argument"
    assert "pace_zone" in result["message"]


def test_pace_range_requires_both_bounds(mcp_with_tools) -> None:
    """Range targets need both endpoints. Missing one is a loud error."""
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(
        date="2026-05-26",
        name="x",
        steps=[
            {"type": "interval", "end_condition": "distance", "end_value": 800,
             "target_type": "pace", "target_low": 4.0},
        ],
    )
    assert result.get("error") == "bad_argument"


def test_cadence_rejects_no_redirect_hint(mcp_with_tools) -> None:
    """`cadence` has no zone variant — the error should NOT suggest one
    (no `cadence_zone` to redirect to)."""
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(
        date="2026-05-26",
        name="x",
        steps=[
            {"type": "interval", "end_condition": "time", "end_value": 600,
             "target_type": "cadence", "zone": 2},
        ],
    )
    assert result.get("error") == "bad_argument"
    assert "Did you mean" not in result["message"]


def test_open_rejects_stray_zone(mcp_with_tools) -> None:
    """`open` / `none` take no target fields; stray values surface as
    'I added a target but forgot to change target_type' mistakes."""
    fn = get_tool(mcp_with_tools, "schedule_running_workout")
    result = fn(
        date="2026-05-26",
        name="x",
        steps=[
            {"type": "interval", "end_condition": "time", "end_value": 600,
             "target_type": "open", "zone": 2},
        ],
    )
    assert result.get("error") == "bad_argument"


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


def test_list_workouts_shape(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "list_workouts")
    result = fn()
    # Defaults forwarded to the SDK in (start, limit) order.
    mock_garmin.get_workouts.assert_called_once_with(0, 20)
    assert isinstance(result, list)
    assert len(result) == 2
    first = result[0]
    assert first["workoutId"] == 1055637
    assert first["workoutName"] == "Tempo 6x800"
    assert first["sportType"]["sportTypeKey"] == "running"


def test_list_workouts_pagination_args(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "list_workouts")
    fn(limit=50, start=10)
    mock_garmin.get_workouts.assert_called_once_with(10, 50)


def test_list_workouts_trims_noise_and_owner(mcp_with_tools) -> None:
    """`author` (plus the PII nested inside it), `ownerId`, and the
    `displayOrder` UI hint drop on every list item."""
    fn = get_tool(mcp_with_tools, "list_workouts")
    result = fn()
    first = result[0]
    assert "author" not in first
    assert "ownerId" not in first
    assert "displayOrder" not in first["sportType"]


def test_list_workouts_verbose_returns_untrimmed(mcp_with_tools) -> None:
    fn = get_tool(mcp_with_tools, "list_workouts")
    result = fn(verbose=True)
    first = result[0]
    assert first["ownerId"] == 10788552
    assert "author" in first
    assert first["sportType"]["displayOrder"] == 1


def test_get_workout_shape(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_workout")
    result = fn(workout_id=1055637)
    mock_garmin.get_workout_by_id.assert_called_once_with(1055637)
    assert result["workoutId"] == 1055637
    assert result["workoutName"] == "Tempo 6x800"
    steps = result["workoutSegments"][0]["workoutSteps"]
    # warmup, repeat group, cooldown
    assert len(steps) == 3
    assert steps[1]["type"] == "RepeatGroupDTO"
    assert steps[1]["numberOfIterations"] == 6


def test_get_workout_preserves_targets(mcp_with_tools) -> None:
    """The trim must never strip per-step target encoding — that's the
    analytical payload. Both the range (`targetValueOne/Two`) and zone
    (`zoneNumber`) shapes survive, nested inside the repeat group."""
    fn = get_tool(mcp_with_tools, "get_workout")
    result = fn(workout_id=1055637)
    interval, recovery = result["workoutSegments"][0]["workoutSteps"][1]["workoutSteps"]
    assert interval["targetValueOne"] == 4.0
    assert interval["targetValueTwo"] == 4.17
    assert recovery["zoneNumber"] == 2
    assert recovery["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"


def test_get_workout_trims_noise_and_owner(mcp_with_tools) -> None:
    fn = get_tool(mcp_with_tools, "get_workout")
    result = fn(workout_id=1055637)
    assert "ownerId" not in result
    assert "author" not in result
    warmup = result["workoutSegments"][0]["workoutSteps"][0]
    # Per-step swim / gear defaults drop.
    assert "strokeType" not in warmup
    assert "equipmentType" not in warmup
    # UI hints drop even inside nested enum blocks.
    assert "displayOrder" not in warmup["stepType"]
    assert "displayable" not in warmup["endCondition"]


def test_get_workout_verbose_returns_untrimmed(mcp_with_tools) -> None:
    fn = get_tool(mcp_with_tools, "get_workout")
    result = fn(workout_id=1055637, verbose=True)
    assert result["ownerId"] == 10788552
    warmup = result["workoutSegments"][0]["workoutSteps"][0]
    assert "strokeType" in warmup
    assert warmup["stepType"]["displayOrder"] == 1


def test_list_workouts_cache_persists_full_upstream(
    mcp_with_tools, mock_garmin, monkeypatch
) -> None:
    """A verbose=True call after a verbose=False call hits the cache."""
    from garmin_mcp import cache

    monkeypatch.delenv("GARMIN_MCP_NO_CACHE", raising=False)
    cache.clear_all()

    full = load_fixture("workouts_list")
    mock_garmin.get_workouts.return_value = full

    fn = get_tool(mcp_with_tools, "list_workouts")

    trimmed = fn()
    verbose = fn(verbose=True)

    assert mock_garmin.get_workouts.call_count == 1
    assert verbose == full
    assert "ownerId" not in trimmed[0]
    assert "ownerId" in verbose[0]

    cache.clear_all()


def test_get_workout_cache_persists_full_upstream(
    mcp_with_tools, mock_garmin, monkeypatch
) -> None:
    from garmin_mcp import cache

    monkeypatch.delenv("GARMIN_MCP_NO_CACHE", raising=False)
    cache.clear_all()

    full = load_fixture("workout_detail")
    mock_garmin.get_workout_by_id.return_value = full

    fn = get_tool(mcp_with_tools, "get_workout")
    trimmed = fn(workout_id=1055637)
    verbose = fn(workout_id=1055637, verbose=True)

    assert mock_garmin.get_workout_by_id.call_count == 1
    assert verbose == full
    assert "ownerId" not in trimmed
    assert "ownerId" in verbose

    cache.clear_all()


# ---------------------------------------------------------------------------
# schedule_strength_workout
# ---------------------------------------------------------------------------


def _squat_steps() -> list[dict]:
    return [
        {"type": "warmup", "duration_seconds": 600, "description": "Montée en charge"},
        {"type": "exercise", "sets": 4, "reps": 5, "rest_seconds": 180,
         "weight_kg": 100, "category": "SQUAT", "exercise_name": "BARBELL_BACK_SQUAT",
         "description": "Dos gainé"},
        {"type": "exercise", "sets": 3, "duration_seconds": 45, "rest_seconds": 60,
         "category": "PLANK", "exercise_name": "PLANK"},
        {"type": "exercise", "reps": 20, "description": "Wall balls"},
        {"type": "cooldown", "duration_seconds": 300},
    ]


def test_schedule_strength_workout_uploads_and_schedules(mcp_with_tools, mock_garmin) -> None:
    mock_garmin.upload_workout.return_value = {"workoutId": 555}
    fn = get_tool(mcp_with_tools, "schedule_strength_workout")
    result = fn(date="2026-10-01", name="Force bas du corps", steps=_squat_steps(),
                description="Séance force")

    assert result == {
        "workout_id": 555,
        "scheduled_workout_id": 111222333,
        "date": "2026-10-01",
        "name": "Force bas du corps",
        "exercise_keys_dropped": False,
    }
    mock_garmin.schedule_workout.assert_called_once_with(555, "2026-10-01")

    payload = mock_garmin.upload_workout.call_args.args[0]
    assert payload["sportType"]["sportTypeId"] == 5
    assert payload["sportType"]["sportTypeKey"] == "strength_training"
    assert payload["description"] == "Séance force"
    segment = payload["workoutSegments"][0]
    assert segment["sportType"]["sportTypeKey"] == "strength_training"
    steps = segment["workoutSteps"]

    warmup = steps[0]
    assert warmup["stepType"]["stepTypeKey"] == "warmup"
    assert warmup["endCondition"]["conditionTypeId"] == 2
    assert warmup["endConditionValue"] == 600.0

    squat_group = steps[1]
    assert squat_group["type"] == "RepeatGroupDTO"
    assert squat_group["stepType"]["stepTypeId"] == 6
    assert squat_group["numberOfIterations"] == 4
    assert squat_group["endCondition"]["conditionTypeId"] == 7
    assert squat_group["skipLastRestStep"] is True
    squat, rest = squat_group["workoutSteps"]
    assert squat["stepType"]["stepTypeId"] == 3
    assert squat["endCondition"]["conditionTypeId"] == 10
    assert squat["endCondition"]["conditionTypeKey"] == "reps"
    assert squat["endConditionValue"] == 5.0
    assert squat["category"] == "SQUAT"
    assert squat["exerciseName"] == "BARBELL_BACK_SQUAT"
    assert squat["weightValue"] == 100.0
    assert squat["weightUnit"] == {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}
    assert squat["description"] == "Dos gainé"
    assert rest["stepType"]["stepTypeId"] == 5
    assert rest["endConditionValue"] == 180.0

    plank = steps[2]["workoutSteps"][0]
    assert plank["endCondition"]["conditionTypeKey"] == "time"
    assert plank["endConditionValue"] == 45.0

    single = steps[3]
    assert single["type"] == "ExecutableStepDTO"
    assert single["endConditionValue"] == 20.0
    assert "category" not in single

    assert steps[4]["stepType"]["stepTypeKey"] == "cooldown"

    # Step orders are unique and increasing, children numbered after parents.
    orders = [steps[0]["stepOrder"], steps[1]["stepOrder"],
              *[s["stepOrder"] for s in steps[1]["workoutSteps"]],
              steps[2]["stepOrder"],
              *[s["stepOrder"] for s in steps[2]["workoutSteps"]],
              steps[3]["stepOrder"], steps[4]["stepOrder"]]
    assert orders == list(range(1, len(orders) + 1))

    # warmup 600 + squat rests 3 x 180 (no rest after the last set)
    # + plank 3 x 45 + 2 x 60 + cooldown 300; reps steps count 0.
    assert payload["estimatedDurationInSecs"] == 600 + 3 * 180 + 3 * 45 + 2 * 60 + 300


def test_schedule_strength_workout_retries_without_exercise_keys(
    mcp_with_tools, mock_garmin
) -> None:
    from garminconnect import GarminConnectConnectionError

    mock_garmin.upload_workout.side_effect = [
        GarminConnectConnectionError("400 Invalid category"),
        {"workoutId": 777},
    ]
    fn = get_tool(mcp_with_tools, "schedule_strength_workout")
    result = fn(date="2026-10-01", name="Force", steps=_squat_steps())

    assert result["workout_id"] == 777
    assert result["exercise_keys_dropped"] is True
    assert mock_garmin.upload_workout.call_count == 2
    retry = mock_garmin.upload_workout.call_args_list[1].args[0]
    squat = retry["workoutSegments"][0]["workoutSteps"][1]["workoutSteps"][0]
    assert "category" not in squat
    assert "exerciseName" not in squat
    # Everything else is preserved on the retry.
    assert squat["weightValue"] == 100.0
    assert squat["description"] == "Dos gainé"


def test_schedule_strength_workout_no_retry_without_exercise_keys(
    mcp_with_tools, mock_garmin
) -> None:
    from garminconnect import GarminConnectConnectionError

    mock_garmin.upload_workout.side_effect = GarminConnectConnectionError("boom")
    fn = get_tool(mcp_with_tools, "schedule_strength_workout")
    result = fn(date="2026-10-01", name="Force",
                steps=[{"type": "exercise", "sets": 3, "reps": 10}])

    assert result["error"] == "garmin_unreachable"
    assert mock_garmin.upload_workout.call_count == 1
    mock_garmin.schedule_workout.assert_not_called()


def test_schedule_strength_workout_rejects_unknown_category(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "schedule_strength_workout")
    result = fn(date="2026-10-01", name="Force",
                steps=[{"type": "exercise", "reps": 10, "category": "SLED"}])
    assert result["error"] == "bad_argument"
    assert "unknown category" in result["message"]
    mock_garmin.upload_workout.assert_not_called()


def test_schedule_strength_workout_requires_reps_xor_duration(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "schedule_strength_workout")
    both = fn(date="2026-10-01", name="x",
              steps=[{"type": "exercise", "reps": 10, "duration_seconds": 30}])
    neither = fn(date="2026-10-01", name="x", steps=[{"type": "exercise", "sets": 2}])
    assert both["error"] == "bad_argument"
    assert neither["error"] == "bad_argument"
    mock_garmin.upload_workout.assert_not_called()


def test_schedule_strength_workout_rejects_bad_values(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "schedule_strength_workout")
    bad_steps = [
        [{"type": "exercise", "sets": 0, "reps": 10}],
        [{"type": "exercise", "sets": True, "reps": 10}],
        [{"type": "exercise", "reps": 10, "rest_seconds": -1}],
        [{"type": "exercise", "reps": 10, "weight_kg": 0}],
        [{"type": "exercise", "reps": 10, "exercise_name": "BARBELL_BACK_SQUAT"}],
        [{"type": "exercise", "reps": 10, "category": "SQUAT", "exercise_name": "back squat"}],
        [{"type": "rest", "duration_seconds": 0}],
        [{"type": "stretch", "duration_seconds": 30}],
        [],
    ]
    for steps in bad_steps:
        result = fn(date="2026-10-01", name="x", steps=steps)
        assert result["error"] == "bad_argument", steps
    missing_name = fn(date="2026-10-01", name=" ", steps=[{"type": "exercise", "reps": 5}])
    assert missing_name["error"] == "bad_argument"
    mock_garmin.upload_workout.assert_not_called()


def test_schedule_strength_workout_single_set_rest_follows(mcp_with_tools, mock_garmin) -> None:
    mock_garmin.upload_workout.return_value = {"workoutId": 1}
    fn = get_tool(mcp_with_tools, "schedule_strength_workout")
    fn(date="2026-10-01", name="x",
       steps=[{"type": "exercise", "reps": 8, "rest_seconds": 90}])
    steps = mock_garmin.upload_workout.call_args.args[0]["workoutSegments"][0]["workoutSteps"]
    assert [s["stepType"]["stepTypeKey"] for s in steps] == ["interval", "rest"]
    assert steps[1]["endConditionValue"] == 90.0
