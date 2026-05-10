"""Tests for the activities tool module."""

from __future__ import annotations

import json

from garmin_mcp.tools._trimmers import (
    trim_activity_detail,
    trim_activity_splits,
    trim_activity_summary,
)
from tests.conftest import get_tool, load_fixture

# ---------------------------------------------------------------------------
# Basic shape tests (small hand-crafted fixtures, mock-Garmin happy path)
# ---------------------------------------------------------------------------


def test_list_recent_activities_returns_list(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "list_recent_activities")
    result = fn(limit=5, start=0)
    assert isinstance(result, list)
    assert result[0]["activityId"] == 9999000001
    mock_garmin.get_activities.assert_called_once_with(0, 5)


def test_list_recent_activities_normalises_dict(mcp_with_tools, mock_garmin) -> None:
    mock_garmin.get_activities.return_value = {"activityList": [{"activityId": 1}]}
    fn = get_tool(mcp_with_tools, "list_recent_activities")
    result = fn()
    assert result == [{"activityId": 1}]


def test_get_activity_passes_string_id(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_activity")
    result = fn(activity_id=9999000001)
    assert result["activityId"] == 9999000001
    mock_garmin.get_activity.assert_called_once_with("9999000001")


def test_get_activity_splits(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_activity_splits")
    result = fn(activity_id=9999000001)
    assert "lapDTOs" in result
    mock_garmin.get_activity_splits.assert_called_once_with("9999000001")


def test_get_activity_hr_zones(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "get_activity_hr_zones")
    result = fn(activity_id=9999000001)
    assert isinstance(result, list)
    mock_garmin.get_activity_hr_in_timezones.assert_called_once_with("9999000001")


def test_search_activities_by_type_passes_dates(mcp_with_tools, mock_garmin) -> None:
    fn = get_tool(mcp_with_tools, "search_activities_by_type")
    result = fn(type="running", start_date="2026-05-01", end_date="2026-05-08", limit=2)
    assert len(result) == 2
    mock_garmin.get_activities_by_date.assert_called_once_with(
        "2026-05-01", "2026-05-08", activitytype="running"
    )


def test_search_activities_by_type_rejects_bad_date(mcp_with_tools) -> None:
    fn = get_tool(mcp_with_tools, "search_activities_by_type")
    result = fn(type="running", start_date="not-a-date", end_date="2026-05-08")
    # safe_call distinguishes caller-input errors from server bugs.
    assert isinstance(result, dict)
    assert result.get("error") == "bad_argument"


def test_auth_error_returns_structured_payload(mock_garmin, mcp_with_tools) -> None:
    from garmin_mcp.auth import AuthError

    mock_garmin.get_activity.side_effect = AuthError("expired")
    fn = get_tool(mcp_with_tools, "get_activity")
    result = fn(activity_id=1)
    assert result == {
        "error": "auth_expired",
        "message": "expired",
        "remediation": "run `garmin-mcp auth login`",
    }


# ---------------------------------------------------------------------------
# trim_activity_summary — drops + canary on the live-shape fixture
# ---------------------------------------------------------------------------

# Locks for fields that must always be dropped from every summary item.
# The list doubles as documentation of "this is intentional, not an
# oversight" — adding a drop or removing one without updating this list
# fails a test fast.
_SUMMARY_ALWAYS_DROP = (
    # PII / OAuth / image URLs
    "userRoles",
    "ownerId",
    "ownerDisplayName",
    "ownerFullName",
    "ownerProfileImageUrlSmall",
    "ownerProfileImageUrlMedium",
    "ownerProfileImageUrlLarge",
    # Privacy stub + UI hints
    "privacy",
    "userPro",
    "hasVideo",
    "hasImages",
    "hasHeatMap",
    "hasIntensityIntervals",
    "hasSplits",
    "hasPolyline",
    # Default-valued booleans
    "manufacturer",
    "parent",
    "decoDive",
    "qualifyingDive",
    "atpActivity",
    "manualActivity",
    "purposeful",
    "favorite",
    "pr",
    "autoCalcCalories",
    "elevationCorrected",
    # Redundancy / internal identifiers
    "timeZoneId",
    "activityUUID",
    "beginTimestamp",
    "sportTypeId",
)


# Allowlist canary for `list_recent_activities` summary items. The trim is
# denylist-based, so any *new* Garmin field would silently leak through.
# This set asserts every surviving key was at least seen-and-classified;
# fixture additions force an explicit decision (extend this set as
# pass-through, or extend `_ACTIVITY_SUMMARY_DROP_ALWAYS` to drop it).
_EXPECTED_SUMMARY_ITEM_KEYS = frozenset(
    {
        # Identity / type
        "activityId",
        "activityName",
        "activityType",
        "eventType",
        # Timing — only the GMT copy survives; the parallel `*Local` field
        # is dropped by the cross-cutting `_strip_pii` pass.
        "startTimeGMT",
        "endTimeGMT",
        "duration",
        "elapsedDuration",
        "movingDuration",
        "minActivityLapDuration",
        # Distance / speed / location
        "distance",
        "averageSpeed",
        "maxSpeed",
        "startLatitude",
        "startLongitude",
        "endLatitude",
        "endLongitude",
        "locationName",
        "elevationGain",
        "elevationLoss",
        "minElevation",
        "maxElevation",
        "avgElevation",
        "maxVerticalSpeed",
        "avgGradeAdjustedSpeed",
        # Calories / hydration / body battery delta
        "calories",
        "bmrCalories",
        "waterEstimated",
        "differenceBodyBattery",
        # HR / power / cadence stats + zones
        "averageHR",
        "maxHR",
        "averageRunningCadenceInStepsPerMinute",
        "maxRunningCadenceInStepsPerMinute",
        "maxDoubleCadence",
        "avgPower",
        "maxPower",
        "normPower",
        "avgVerticalOscillation",
        "avgGroundContactTime",
        "avgStrideLength",
        "avgVerticalRatio",
        "vO2MaxValue",
        "steps",
        "hrTimeInZone_1",
        "hrTimeInZone_2",
        "hrTimeInZone_3",
        "hrTimeInZone_4",
        "hrTimeInZone_5",
        "powerTimeInZone_1",
        "powerTimeInZone_2",
        "powerTimeInZone_3",
        "powerTimeInZone_4",
        "powerTimeInZone_5",
        # Intensity / training effect
        "aerobicTrainingEffect",
        "anaerobicTrainingEffect",
        "aerobicTrainingEffectMessage",
        "anaerobicTrainingEffectMessage",
        "trainingEffectLabel",
        "activityTrainingLoad",
        "moderateIntensityMinutes",
        "vigorousIntensityMinutes",
        # Splits / structure
        "lapCount",
        "splitSummaries",  # only when non-empty
        "summarizedExerciseSets",
        "totalSets",
        "activeSets",
        "totalReps",
        "fastestSplit_1000",
        "fastestSplit_1609",
        "fastestSplit_5000",
        # Device (kept — useful for cross-activity device tracking)
        "deviceId",
    }
)


def test_trim_activity_summary_drops_always_locked_fields() -> None:
    """The drop-list is the canonical place that documents intentional drops."""
    full = load_fixture("activities_list_payload_full")
    trimmed = trim_activity_summary(full)
    assert trimmed, "fixture should produce some items"
    for item in trimmed:
        for dropped in _SUMMARY_ALWAYS_DROP:
            assert dropped not in item, f"{dropped} should be dropped from every summary item"


def test_trim_activity_summary_keys_subset_of_expected() -> None:
    """Pass-through canary: every surviving key was classified as kept."""
    full = load_fixture("activities_list_payload_full")
    trimmed = trim_activity_summary(full)
    leaked: set[str] = set()
    for item in trimmed:
        leaked |= set(item.keys()) - _EXPECTED_SUMMARY_ITEM_KEYS
    assert not leaked, (
        f"trim_activity_summary leaked unclassified fields: {sorted(leaked)}. "
        "Either add them to _EXPECTED_SUMMARY_ITEM_KEYS (pass-through) "
        "or to _ACTIVITY_SUMMARY_DROP_ALWAYS (drop)."
    )


def test_trim_activity_summary_drops_empty_dive_info() -> None:
    """`summarizedDiveInfo: {summarizedDiveGases: []}` is dropped."""
    full = load_fixture("activities_list_payload_full")
    trimmed = trim_activity_summary(full)
    for item in trimmed:
        # Non-dive activity → wrapper had an empty list → dropped entirely.
        assert "summarizedDiveInfo" not in item


def test_trim_activity_summary_keeps_populated_dive_info() -> None:
    """A populated `summarizedDiveInfo` survives (dive activity case)."""
    payload = [
        {
            "activityId": 1,
            "summarizedDiveInfo": {
                "summarizedDiveGases": [{"gasType": "AIR", "totalDuration": 1200}]
            },
        }
    ]
    trimmed = trim_activity_summary(payload)
    assert trimmed[0]["summarizedDiveInfo"]["summarizedDiveGases"] == [
        {"gasType": "AIR", "totalDuration": 1200}
    ]


def test_trim_activity_summary_drops_empty_split_summaries() -> None:
    """`splitSummaries: []` on the strength/boxing item is dropped."""
    full = load_fixture("activities_list_payload_full")
    trimmed = trim_activity_summary(full)
    # First item in the fixture is a Boxing activity (strength-style) with
    # an empty splitSummaries — must be gone.
    assert trimmed[0]["activityName"] == "Boxing"
    assert "splitSummaries" not in trimmed[0]


def test_trim_activity_summary_keeps_populated_split_summaries() -> None:
    """A non-empty `splitSummaries` list survives (running case)."""
    payload = [
        {
            "activityId": 1,
            "splitSummaries": [
                {"splitType": "RWD_RUN", "noOfSplits": 3, "distance": 1500},
            ],
        }
    ]
    trimmed = trim_activity_summary(payload)
    assert trimmed[0]["splitSummaries"] == [
        {"splitType": "RWD_RUN", "noOfSplits": 3, "distance": 1500},
    ]


def test_trim_activity_summary_passes_through_non_list() -> None:
    """A single dict, None, and error stubs all pass through defensively."""
    # Single dict → trimmed as one item.
    assert trim_activity_summary({"activityId": 1}) == {"activityId": 1}
    # None / non-list scalars pass through.
    assert trim_activity_summary(None) is None
    assert trim_activity_summary("oops") == "oops"


def test_trim_activity_summary_empty_list() -> None:
    assert trim_activity_summary([]) == []


def test_trim_activity_summary_error_stub_in_list_passes_through() -> None:
    """A `{error:...}` stub inside a list survives intact."""
    err = {"error": "fetch_failed", "message": "boom"}
    payload = [{"activityId": 1, "userRoles": ["x"]}, err]
    trimmed = trim_activity_summary(payload)
    assert trimmed[0] == {"activityId": 1}
    assert trimmed[1] is err  # exact pass-through


def test_trim_activity_summary_error_stub_dict_passes_through() -> None:
    """A top-level error stub passes through (single-dict input)."""
    stub = {"error": "fetch_failed", "exception": "RuntimeError"}
    assert trim_activity_summary(stub) == stub


# ---------------------------------------------------------------------------
# trim_activity_detail — top-level allowlist + metadataDTO filter
# ---------------------------------------------------------------------------

_EXPECTED_DETAIL_TOP_LEVEL_KEYS = frozenset(
    {
        "activityId",
        "activityName",
        "activityTypeDTO",
        "eventTypeDTO",
        "summaryDTO",
        "timeZoneUnitDTO",
        "metadataDTO",
    }
)

_EXPECTED_DETAIL_METADATA_KEYS = frozenset(
    {
        # Unconditional keep
        "lapCount",
        "lastUpdateDate",
        "uploadedDate",
        # Conditional keep (only when populated; absent on non-eBike /
        # un-templated / sensor-less activities — i.e. the Strength
        # fixture has none of these).
        "eBikeBatteryUsage",
        "eBikeBatteryRemaining",
        "eBikeMaxAssistModes",
        "eBikeAssistModeInfoDTOList",
        "associatedWorkoutId",
        "associatedCourseId",
        "sensors",
    }
)

_DETAIL_DROPPED_TOP_LEVEL = (
    "accessControlRuleDTO",
    "activityUUID",
    "isMultiSportParent",
    "userProfileId",
)


def test_trim_activity_detail_drops_top_level_intentional_fields() -> None:
    full = load_fixture("activity_payload_full")
    trimmed = trim_activity_detail(full)
    for dropped in _DETAIL_DROPPED_TOP_LEVEL:
        assert dropped not in trimmed


def test_trim_activity_detail_top_level_keys_subset_of_expected() -> None:
    full = load_fixture("activity_payload_full")
    trimmed = trim_activity_detail(full)
    leaked = set(trimmed.keys()) - _EXPECTED_DETAIL_TOP_LEVEL_KEYS
    assert not leaked, (
        f"trim_activity_detail leaked unclassified top-level fields: {sorted(leaked)}. "
        "Either add them to _EXPECTED_DETAIL_TOP_LEVEL_KEYS or extend "
        "_ACTIVITY_DETAIL_TOP_LEVEL_KEEP."
    )


def test_trim_activity_detail_metadata_keys_subset_of_expected() -> None:
    """The `metadataDTO` sub-trim is allowlist — assert no surprise keys leak."""
    full = load_fixture("activity_payload_full")
    trimmed = trim_activity_detail(full)
    meta = trimmed["metadataDTO"]
    leaked = set(meta.keys()) - _EXPECTED_DETAIL_METADATA_KEYS
    assert not leaked, (
        f"metadataDTO leaked unclassified fields: {sorted(leaked)}. "
        "Either extend _EXPECTED_DETAIL_METADATA_KEYS / "
        "_ACTIVITY_DETAIL_METADATA_KEEP."
    )


def test_trim_activity_detail_drops_user_info_and_device_metadata() -> None:
    """The PII / image URL block and device-audit block are gone."""
    full = load_fixture("activity_payload_full")
    trimmed = trim_activity_detail(full)
    meta = trimmed["metadataDTO"]
    for dropped in (
        "userInfoDto",  # PII + S3 profile-image URLs
        "deviceMetaDataDTO",
        "agentApplicationInstallationId",
        "agentString",
        "videoUrl",
        "associatedCourseId",
        "groupRideUUID",
        "eBikeMaxAssistModes",
        "eBikeBatteryUsage",
        "eBikeBatteryRemaining",
        "eBikeAssistModeInfoDTOList",
        "manufacturer",
        "hasPolyline",
        "hasHeatMap",
        "hasIntensityIntervals",
        "hasSplits",
        "hasChartData",
        "hasHrTimeInZones",
        "hasPowerTimeInZones",
        "activityImages",
        "childIds",
        "childActivityTypes",
        "isAtpActivity",
        "isOriginal",
        "trimmed",
        "personalRecord",
        "manualActivity",
        "autoCalcCalories",
        "favorite",
        "elevationCorrected",
        "gcj02",
        "fileFormat",
        "deviceApplicationInstallationId",
    ):
        assert dropped not in meta, f"{dropped} should be dropped from metadataDTO"


def test_trim_activity_detail_drops_null_conditional_metadata_fields() -> None:
    """Strength fixture has null/empty eBike + course + sensors → all dropped.

    Makes the conditional-keep behaviour explicit: when the conditional
    fields are absent/null/empty, they don't leak through.
    """
    full = load_fixture("activity_payload_full")
    trimmed = trim_activity_detail(full)
    meta = trimmed["metadataDTO"]
    for dropped in (
        "eBikeBatteryUsage",
        "eBikeBatteryRemaining",
        "eBikeMaxAssistModes",
        "eBikeAssistModeInfoDTOList",
        "associatedWorkoutId",
        "associatedCourseId",
        "sensors",
    ):
        assert dropped not in meta, (
            f"{dropped} is null/empty on the Strength fixture and should drop"
        )


def test_trim_activity_detail_keeps_populated_ebike_telemetry() -> None:
    """An eBike ride's battery + assist-mode telemetry survives the trim.

    Real eBike rides carry `eBike*` fields the LLM reasons over
    ("how much battery did I burn? Which assist modes did I use?").
    A previous version of this trim used a flat allowlist that dropped
    them unconditionally — this test locks the eBike-friendly behaviour
    in.
    """
    payload = {
        "activityId": 42,
        "activityName": "eBike Commute",
        "metadataDTO": {
            # Unconditional-keep fields (so the allowlist filter doesn't
            # produce an empty metadataDTO and mask the conditional logic).
            "lapCount": 1,
            "lastUpdateDate": "2026-05-10T18:00:00.0",
            "uploadedDate": "2026-05-10T18:00:00.0",
            # Always-drop noise (must NOT leak through).
            "userInfoDto": {"profileImageUrlLarge": "https://s3/x"},
            "manufacturer": "GARMIN",
            # Conditional-keep — eBike telemetry, must survive.
            "eBikeBatteryUsage": 42,
            "eBikeBatteryRemaining": 58,
            "eBikeMaxAssistModes": 3,
            "eBikeAssistModeInfoDTOList": [
                {"mode": "ECO", "durationSeconds": 600},
                {"mode": "TOUR", "durationSeconds": 1200},
            ],
        },
    }
    trimmed = trim_activity_detail(payload)
    meta = trimmed["metadataDTO"]

    # Conditional-keep eBike fields preserved.
    assert meta["eBikeBatteryUsage"] == 42
    assert meta["eBikeBatteryRemaining"] == 58
    assert meta["eBikeMaxAssistModes"] == 3
    assert meta["eBikeAssistModeInfoDTOList"] == [
        {"mode": "ECO", "durationSeconds": 600},
        {"mode": "TOUR", "durationSeconds": 1200},
    ]
    # Always-drop noise is still gone.
    assert "userInfoDto" not in meta
    assert "manufacturer" not in meta


def test_trim_activity_detail_keeps_populated_workout_course_sensors() -> None:
    """Templated workout + course + paired-sensor refs survive when set."""
    payload = {
        "activityId": 43,
        "metadataDTO": {
            "lapCount": 5,
            "associatedWorkoutId": 7788,
            "associatedCourseId": 9911,
            "sensors": [
                {"sensorType": "HEART_RATE", "manufacturer": "GARMIN"},
                {"sensorType": "POWER", "manufacturer": "STAGES"},
            ],
        },
    }
    trimmed = trim_activity_detail(payload)
    meta = trimmed["metadataDTO"]
    assert meta["associatedWorkoutId"] == 7788
    assert meta["associatedCourseId"] == 9911
    assert meta["sensors"] == [
        {"sensorType": "HEART_RATE", "manufacturer": "GARMIN"},
        {"sensorType": "POWER", "manufacturer": "STAGES"},
    ]


def test_trim_activity_detail_drops_empty_sensors_list() -> None:
    """An empty `sensors: []` is treated as absent and dropped.

    `_is_meaningful` returns False for empty containers — keeps the
    conditional-keep path symmetric with the null-value case so a
    non-eBike activity that happens to carry `sensors: []` doesn't
    pay the per-activity null tax.
    """
    payload = {
        "activityId": 44,
        "metadataDTO": {"lapCount": 1, "sensors": []},
    }
    trimmed = trim_activity_detail(payload)
    assert "sensors" not in trimmed["metadataDTO"]


def test_trim_activity_detail_keeps_summary_dto_intact() -> None:
    """`summaryDTO` is the analytically dense block — pass-through untouched
    except for the cross-cutting `_strip_pii` pass that drops the `*Local`
    timestamp parallel.
    """
    full = load_fixture("activity_payload_full")
    trimmed = trim_activity_detail(full)
    expected = {k: v for k, v in full["summaryDTO"].items() if k != "startTimeLocal"}
    assert trimmed["summaryDTO"] == expected
    # `startTimeGMT` is the canonical timestamp — the LLM can derive local
    # from the timezone offset on `timeZoneUnitDTO` if needed.
    assert "startTimeLocal" not in trimmed["summaryDTO"]
    assert trimmed["summaryDTO"]["startTimeGMT"] == full["summaryDTO"]["startTimeGMT"]


def test_trim_activity_detail_passes_through_non_dict_and_error() -> None:
    assert trim_activity_detail(None) is None
    assert trim_activity_detail("oops") == "oops"
    assert trim_activity_detail([]) == []
    stub = {"error": "fetch_failed", "exception": "RuntimeError"}
    assert trim_activity_detail(stub) == stub


# ---------------------------------------------------------------------------
# trim_activity_splits
# ---------------------------------------------------------------------------


def test_trim_activity_splits_drops_event_dtos() -> None:
    """`eventDTOs` (timer-trigger noise) is dropped entirely."""
    full = load_fixture("activity_splits_payload_full")
    trimmed = trim_activity_splits(full)
    assert "eventDTOs" not in trimmed


def test_trim_activity_splits_drops_empty_per_lap_lists() -> None:
    """Empty `lengthDTOs` / `connectIQMeasurement` per lap are dropped."""
    full = load_fixture("activity_splits_payload_full")
    trimmed = trim_activity_splits(full)
    for lap in trimmed["lapDTOs"]:
        assert "lengthDTOs" not in lap, (
            "empty lengthDTOs should be dropped (fixture is a strength session)"
        )
        assert "connectIQMeasurement" not in lap


def test_trim_activity_splits_keeps_populated_lengths() -> None:
    """A swim activity's populated `lengthDTOs` survives."""
    payload = {
        "activityId": 1,
        "lapDTOs": [
            {
                "lapIndex": 1,
                "lengthDTOs": [{"lengthIndex": 1, "distance": 50}],
                "connectIQMeasurement": [],
            }
        ],
        "eventDTOs": [{"sectionTypeDTO": {"key": "timerTrigger"}}],
    }
    trimmed = trim_activity_splits(payload)
    assert "eventDTOs" not in trimmed
    lap = trimmed["lapDTOs"][0]
    assert lap["lengthDTOs"] == [{"lengthIndex": 1, "distance": 50}]
    assert "connectIQMeasurement" not in lap


def test_trim_activity_splits_keeps_lap_dtos_and_activity_id() -> None:
    full = load_fixture("activity_splits_payload_full")
    trimmed = trim_activity_splits(full)
    assert trimmed["activityId"] == full["activityId"]
    assert len(trimmed["lapDTOs"]) == len(full["lapDTOs"])
    # `averageHR` etc. inside laps survive.
    assert trimmed["lapDTOs"][0]["averageHR"] == full["lapDTOs"][0]["averageHR"]


def test_trim_activity_splits_passes_through_non_dict_and_error() -> None:
    assert trim_activity_splits(None) is None
    assert trim_activity_splits([]) == []
    assert trim_activity_splits("oops") == "oops"
    stub = {"error": "fetch_failed", "message": "boom"}
    assert trim_activity_splits(stub) == stub


# ---------------------------------------------------------------------------
# Tool boundary — verbose flag, payload size floors, cache reuse
# ---------------------------------------------------------------------------


def test_list_recent_activities_default_payload_is_trimmed(mcp_with_tools, mock_garmin) -> None:
    """Default call drops PII / OAuth scopes and stays under a 1.5x size floor."""
    full = load_fixture("activities_list_payload_full")
    mock_garmin.get_activities.return_value = full

    fn = get_tool(mcp_with_tools, "list_recent_activities")
    result = fn(limit=5)

    upstream_size = len(json.dumps(full))
    trimmed_size = len(json.dumps(result))
    # Measured ~2.2x on Kamil's live data (19158 → 8609 chars on the
    # 5-item fixture). Floor of 1.5x catches a regression that disables
    # the userRoles + image URL drops (~30% of the savings on its own).
    assert trimmed_size * 3 < upstream_size * 2, (
        f"trimmed={trimmed_size} chars vs upstream={upstream_size}; "
        "expected at least 1.5x reduction on the live-shape fixture"
    )

    for item in result:
        assert "userRoles" not in item
        assert "ownerProfileImageUrlLarge" not in item
        assert "privacy" not in item


def test_list_recent_activities_verbose_returns_unmodified_upstream(
    mcp_with_tools, mock_garmin
) -> None:
    """verbose=True returns a value-equal copy of the full upstream payload."""
    full = load_fixture("activities_list_payload_full")
    mock_garmin.get_activities.return_value = full

    fn = get_tool(mcp_with_tools, "list_recent_activities")
    result = fn(limit=5, verbose=True)

    assert result == full
    # Fields the trim drops are still present in the verbose copy.
    assert "userRoles" in result[0]
    assert "privacy" in result[0]


def test_list_recent_activities_cache_persists_full_upstream(
    mcp_with_tools, mock_garmin, monkeypatch
) -> None:
    """A verbose=True call after a verbose=False call hits the cache."""
    from garmin_mcp import cache

    monkeypatch.delenv("GARMIN_MCP_NO_CACHE", raising=False)
    cache.clear_all()

    full = load_fixture("activities_list_payload_full")
    mock_garmin.get_activities.return_value = full

    fn = get_tool(mcp_with_tools, "list_recent_activities")

    trimmed = fn(limit=5)
    verbose = fn(limit=5, verbose=True)

    assert mock_garmin.get_activities.call_count == 1
    assert verbose == full
    assert "userRoles" not in trimmed[0]
    assert "userRoles" in verbose[0]

    cache.clear_all()


def test_list_recent_activities_passes_through_garmin_error(mcp_with_tools, mock_garmin) -> None:
    """A Garmin SDK exception bubbles into a structured error via safe_call."""
    from garminconnect import GarminConnectAuthenticationError

    mock_garmin.get_activities.side_effect = GarminConnectAuthenticationError("expired")
    fn = get_tool(mcp_with_tools, "list_recent_activities")
    result = fn(limit=5)
    assert result["error"] == "auth_expired"


def test_search_activities_by_type_default_payload_is_trimmed(mcp_with_tools, mock_garmin) -> None:
    """Default call drops PII; running fixture survives more (1.5x floor)."""
    full = load_fixture("activities_by_date_payload_full")
    mock_garmin.get_activities_by_date.return_value = full

    fn = get_tool(mcp_with_tools, "search_activities_by_type")
    result = fn(type="running", start_date="2026-03-01", end_date="2026-05-10", limit=5)

    upstream_size = len(json.dumps(full))
    trimmed_size = len(json.dumps(result))
    # Measured ~1.58x on Kamil's running activities (running keeps
    # lat/lng, power zones, splitSummaries — legitimate signal that
    # survives). Floor of 1.4x catches a regression where only the
    # 4 image-URL+userRoles drops are working (which by themselves
    # would yield ~1.3x); the always-drop booleans + redundant
    # identifiers contribute the rest.
    assert trimmed_size * 7 < upstream_size * 5, (
        f"trimmed={trimmed_size} chars vs upstream={upstream_size}; "
        "expected at least 1.4x reduction on the running fixture"
    )

    for item in result:
        assert "userRoles" not in item
        assert "ownerProfileImageUrlLarge" not in item


def test_search_activities_by_type_verbose_returns_unmodified_upstream(
    mcp_with_tools, mock_garmin
) -> None:
    full = load_fixture("activities_by_date_payload_full")
    mock_garmin.get_activities_by_date.return_value = full

    fn = get_tool(mcp_with_tools, "search_activities_by_type")
    result = fn(
        type="running",
        start_date="2026-03-01",
        end_date="2026-05-10",
        verbose=True,
    )
    assert result == full
    assert "userRoles" in result[0]


def test_search_activities_by_type_cache_persists_full_upstream(
    mcp_with_tools, mock_garmin, monkeypatch
) -> None:
    from garmin_mcp import cache

    monkeypatch.delenv("GARMIN_MCP_NO_CACHE", raising=False)
    cache.clear_all()

    full = load_fixture("activities_by_date_payload_full")
    mock_garmin.get_activities_by_date.return_value = full

    fn = get_tool(mcp_with_tools, "search_activities_by_type")
    trimmed = fn(type="running", start_date="2026-03-01", end_date="2026-05-10")
    verbose = fn(
        type="running",
        start_date="2026-03-01",
        end_date="2026-05-10",
        verbose=True,
    )

    assert mock_garmin.get_activities_by_date.call_count == 1
    assert verbose == full
    assert "userRoles" not in trimmed[0]
    assert "userRoles" in verbose[0]

    cache.clear_all()


def test_get_activity_default_payload_is_trimmed(mcp_with_tools, mock_garmin) -> None:
    """Default call applies the top-level allowlist + metadataDTO filter."""
    full = load_fixture("activity_payload_full")
    mock_garmin.get_activity.return_value = full

    fn = get_tool(mcp_with_tools, "get_activity")
    result = fn(activity_id=22817757923)

    upstream_size = len(json.dumps(full))
    trimmed_size = len(json.dumps(result))
    # Measured ~2.4x (3067 → 1274 chars). Floor of 2x catches a regression.
    assert trimmed_size * 2 < upstream_size, (
        f"trimmed={trimmed_size} chars vs upstream={upstream_size}; expected at least 2x reduction"
    )

    assert "accessControlRuleDTO" not in result
    assert "userProfileId" not in result
    assert "userInfoDto" not in result["metadataDTO"]


def test_get_activity_verbose_returns_unmodified_upstream(mcp_with_tools, mock_garmin) -> None:
    full = load_fixture("activity_payload_full")
    mock_garmin.get_activity.return_value = full

    fn = get_tool(mcp_with_tools, "get_activity")
    result = fn(activity_id=22817757923, verbose=True)

    assert result == full
    assert "userProfileId" in result
    assert "userInfoDto" in result["metadataDTO"]


def test_get_activity_cache_persists_full_upstream(
    mcp_with_tools, mock_garmin, monkeypatch
) -> None:
    from garmin_mcp import cache

    monkeypatch.delenv("GARMIN_MCP_NO_CACHE", raising=False)
    cache.clear_all()

    full = load_fixture("activity_payload_full")
    mock_garmin.get_activity.return_value = full

    fn = get_tool(mcp_with_tools, "get_activity")
    trimmed = fn(activity_id=22817757923)
    verbose = fn(activity_id=22817757923, verbose=True)

    assert mock_garmin.get_activity.call_count == 1
    assert verbose == full
    assert "userProfileId" not in trimmed
    assert "userProfileId" in verbose

    cache.clear_all()


def test_get_activity_splits_default_payload_is_trimmed(mcp_with_tools, mock_garmin) -> None:
    """Default call drops eventDTOs and empty per-lap lists."""
    full = load_fixture("activity_splits_payload_full")
    mock_garmin.get_activity_splits.return_value = full

    fn = get_tool(mcp_with_tools, "get_activity_splits")
    result = fn(activity_id=22817757923)

    upstream_size = len(json.dumps(full))
    trimmed_size = len(json.dumps(result))
    # Measured ~2.1x (792 → 383 chars) on a 1-lap strength session;
    # multi-lap runs have more legit signal and shrink less.
    assert trimmed_size * 3 < upstream_size * 2, (
        f"trimmed={trimmed_size} chars vs upstream={upstream_size}; "
        "expected at least 1.5x reduction"
    )

    assert "eventDTOs" not in result
    for lap in result["lapDTOs"]:
        assert "lengthDTOs" not in lap
        assert "connectIQMeasurement" not in lap


def test_get_activity_splits_verbose_returns_unmodified_upstream(
    mcp_with_tools, mock_garmin
) -> None:
    full = load_fixture("activity_splits_payload_full")
    mock_garmin.get_activity_splits.return_value = full

    fn = get_tool(mcp_with_tools, "get_activity_splits")
    result = fn(activity_id=22817757923, verbose=True)

    assert result == full
    assert "eventDTOs" in result


def test_get_activity_splits_cache_persists_full_upstream(
    mcp_with_tools, mock_garmin, monkeypatch
) -> None:
    from garmin_mcp import cache

    monkeypatch.delenv("GARMIN_MCP_NO_CACHE", raising=False)
    cache.clear_all()

    full = load_fixture("activity_splits_payload_full")
    mock_garmin.get_activity_splits.return_value = full

    fn = get_tool(mcp_with_tools, "get_activity_splits")
    trimmed = fn(activity_id=22817757923)
    verbose = fn(activity_id=22817757923, verbose=True)

    assert mock_garmin.get_activity_splits.call_count == 1
    assert verbose == full
    assert "eventDTOs" not in trimmed
    assert "eventDTOs" in verbose

    cache.clear_all()
