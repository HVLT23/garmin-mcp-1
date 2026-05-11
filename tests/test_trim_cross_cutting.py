"""Tests for the cross-cutting `_strip_pii` final-pass helper.

The per-tool trim helpers (`trim_sleep`, `trim_hrv`, …) each apply
`_strip_pii` as their final pass — these tests verify the helper itself
(recursive correctness, error-stub passthrough, scalar pass-through) and
then assert end-to-end that every helper actually strips the cross-cutting
fields at arbitrary nesting depth.
"""

from __future__ import annotations

import json

from garmin_mcp.tools._trimmers import (
    _is_local_timestamp_key,
    _strip_pii,
    trim_activity_detail,
    trim_activity_splits,
    trim_activity_summary,
    trim_body_battery,
    trim_daily_summary,
    trim_hrv,
    trim_sleep,
    trim_sleep_streams,
    trim_steps,
    trim_stress,
)
from tests.conftest import load_fixture

# ---------------------------------------------------------------------------
# _strip_pii — unit behaviour
# ---------------------------------------------------------------------------


def test_strip_pii_passes_through_non_dict_non_list_scalars() -> None:
    """Scalars (str, int, float, None, bool) pass through unchanged."""
    assert _strip_pii("foo") == "foo"
    assert _strip_pii(42) == 42
    assert _strip_pii(3.14) == 3.14
    assert _strip_pii(None) is None
    assert _strip_pii(True) is True


def test_strip_pii_passes_through_error_stub() -> None:
    """Error stubs (dicts with an `error` key) pass through unchanged so
    upstream diagnostics from `safe_call` aren't swallowed by the trim.
    """
    stub = {"error": "fetch_failed", "message": "boom", "userProfileId": 1}
    # Even though `userProfileId` is a PII key, the stub's whole purpose is
    # to surface the diagnostic — pass-through is the correct behaviour.
    assert _strip_pii(stub) == stub


def test_strip_pii_drops_top_level_user_identity_keys() -> None:
    payload = {
        "calendarDate": "2026-05-09",
        "userProfileId": 121491731,
        "userProfilePK": 12345,
        "userProfilePk": 12345,
        "userId": 999,
        "displayName": "alice",
        "displayname": "alice",
        "fullName": "Alice Example",
        "fullname": "Alice Example",
        "userPro": False,
    }
    result = _strip_pii(payload)
    assert result == {"calendarDate": "2026-05-09"}


def test_strip_pii_drops_owner_fields() -> None:
    payload = {
        "activityId": 1,
        "ownerId": 999999,
        "ownerDisplayName": "uuid-here",
        "ownerFullName": "Test User",
        "ownerProfileImageUrlSmall": "https://s3/x-prth.png",
        "ownerProfileImageUrlMedium": "https://s3/x-prfr.png",
        "ownerProfileImageUrlLarge": "https://s3/x-prof.png",
    }
    assert _strip_pii(payload) == {"activityId": 1}


def test_strip_pii_drops_profile_image_url_variants() -> None:
    payload = {
        "activityId": 1,
        "profileImageUrlSmall": "https://s3/small",
        "profileImageUrlMedium": "https://s3/medium",
        "profileImageUrlLarge": "https://s3/large",
    }
    assert _strip_pii(payload) == {"activityId": 1}


def test_strip_pii_drops_image_url_both_casings() -> None:
    """`imageURL` (recordedDevices product image) and `imageUrl` both drop."""
    payload = {
        "activityId": 1,
        "imageURL": "https://res.garmin.com/devices/x.png",
        "imageUrl": "https://res.garmin.com/devices/y.png",
    }
    assert _strip_pii(payload) == {"activityId": 1}


def test_strip_pii_drops_nested_user_identity() -> None:
    """PII drops recurse into nested dicts and lists."""
    payload = {
        "activityId": 1,
        "metadataDTO": {
            "lapCount": 3,
            "userInfoDto": {
                "displayName": "alice",
                "fullName": "Alice Example",
                "profileImageUrlLarge": "https://s3/x",
            },
        },
        "owners": [
            {"ownerId": 1, "ownerDisplayName": "alice"},
            {"ownerId": 2, "ownerDisplayName": "bob"},
        ],
    }
    result = _strip_pii(payload)
    assert result == {
        "activityId": 1,
        "metadataDTO": {"lapCount": 3, "userInfoDto": {}},
        "owners": [{}, {}],
    }


def test_strip_pii_recurses_through_deeply_nested_structures() -> None:
    """PII at depth 4 still drops."""
    payload = {
        "outer": {
            "middle": {
                "inner": {
                    "userProfileId": 999,
                    "kept": "value",
                }
            }
        }
    }
    result = _strip_pii(payload)
    assert result == {"outer": {"middle": {"inner": {"kept": "value"}}}}


def test_strip_pii_drops_local_timestamp_suffixes() -> None:
    """Both `*TimeLocal` and `*TimestampLocal` suffixes drop."""
    payload = {
        "startTimestampGMT": 1000,
        "startTimestampLocal": 2000,
        "endTimestampGMT": 9000,
        "endTimestampLocal": 10000,
        "readingTimeGmt": "2026-05-09T05:30:00.0",
        "readingTimeLocal": "2026-05-09T07:30:00.0",
        "wellnessStartTimeGmt": "2026-05-09T00:00:00.0",
        "wellnessStartTimeLocal": "2026-05-09T02:00:00.0",
    }
    result = _strip_pii(payload)
    assert result == {
        "startTimestampGMT": 1000,
        "endTimestampGMT": 9000,
        "readingTimeGmt": "2026-05-09T05:30:00.0",
        "wellnessStartTimeGmt": "2026-05-09T00:00:00.0",
    }


def test_strip_pii_drops_lowercase_timestamp_local_variants() -> None:
    """The predicate matches case-insensitively, so lowercase `timestampLocal`
    (the bare-key form training_readiness emits) and other mixed/upper-case
    `*timelocal` / `*timestamplocal` variants all drop.
    """
    payload = {
        "timestampLocal": "2026-05-10T19:19:36.0",
        "someTimeLocal": "2026-05-10T19:19:36.0",
        "someTimestampLocal": "2026-05-10T19:19:36.0",
        "TIMESTAMPLOCAL": "2026-05-10T19:19:36.0",
        "foo": "bar",
    }
    assert _strip_pii(payload) == {"foo": "bar"}


def test_strip_pii_drops_known_mixed_case_local_timestamps() -> None:
    """Regression guard: every `*Local` key currently emitted by Garmin
    fixtures still drops after the predicate became case-insensitive.
    """
    payload = {
        "startTimestampLocal": 1,
        "endTimestampLocal": 2,
        "sleepStartTimestampLocal": 3,
        "sleepEndTimestampLocal": 4,
        "startTimeLocal": "2026-05-09T00:00:00.0",
        "readingTimeLocal": "2026-05-09T05:30:00.0",
        "wellnessStartTimeLocal": "2026-05-09T00:00:00.0",
        "wellnessEndTimeLocal": "2026-05-09T23:59:59.0",
        "keep": "me",
    }
    assert _strip_pii(payload) == {"keep": "me"}


def test_strip_pii_does_not_drop_bare_local_keys() -> None:
    """A key ending in just `Local` (no `Time`/`Timestamp` prefix) is kept.

    Guards against the broad-suffix risk called out in the spec: if a
    future Garmin field were named `someLocal` (not a timestamp), the
    narrow suffix predicate must let it through.
    """
    payload = {
        "isLocal": True,
        "someLocal": "unrelated",
        "datumLocal": 42,  # not a timestamp — kept
        "calendarDate": "2026-05-09",  # date-only, never had Local suffix
    }
    assert _strip_pii(payload) == payload


def test_strip_pii_keeps_calendarDate_and_gmt_fields() -> None:
    payload = {
        "calendarDate": "2026-05-09",
        "startTimestampGMT": 1000,
        "wellnessStartTimeGmt": "2026-05-09T00:00:00.0",
    }
    assert _strip_pii(payload) == payload


def test_strip_pii_drops_local_timestamps_inside_lists() -> None:
    """Nested `*Local` keys inside list items drop, just like dicts."""
    payload = [
        {"startTimestampGMT": 1, "startTimestampLocal": 2},
        {"startTimestampGMT": 3, "startTimestampLocal": 4},
    ]
    assert _strip_pii(payload) == [
        {"startTimestampGMT": 1},
        {"startTimestampGMT": 3},
    ]


def test_strip_pii_is_local_timestamp_key_helper() -> None:
    assert _is_local_timestamp_key("startTimestampLocal")
    assert _is_local_timestamp_key("readingTimeLocal")
    assert _is_local_timestamp_key("wellnessEndTimeLocal")
    assert _is_local_timestamp_key("latestSpo2ReadingTimeLocal")
    # Case-insensitive: the lowercase parallel `timestampLocal` (which
    # training_readiness emits) and other casings match.
    assert _is_local_timestamp_key("timestampLocal")
    assert _is_local_timestamp_key("TIMESTAMPLOCAL")
    assert _is_local_timestamp_key("someTimestamplocal")
    # Non-suffixed `Local` keys are kept.
    assert not _is_local_timestamp_key("someLocal")
    assert not _is_local_timestamp_key("isLocal")
    assert not _is_local_timestamp_key("datumLocal")
    # Non-strings (defensive — dict keys could theoretically be ints).
    assert not _is_local_timestamp_key(42)
    assert not _is_local_timestamp_key(None)


def test_strip_pii_does_not_mutate_input() -> None:
    """The helper returns a new dict; the caller's payload is unchanged."""
    payload = {
        "userProfileId": 1,
        "metadataDTO": {"userInfoDto": {"displayName": "alice"}},
    }
    snapshot = json.loads(json.dumps(payload))
    _strip_pii(payload)
    assert payload == snapshot


# ---------------------------------------------------------------------------
# End-to-end: every per-tool trim helper strips cross-cutting fields
# ---------------------------------------------------------------------------


_CROSS_CUTTING_PII_KEYS = {
    "userProfileId",
    "userId",
    "userProfilePK",
    "userProfilePk",
    "displayName",
    "displayname",
    "fullName",
    "fullname",
    "profileImageUrlSmall",
    "profileImageUrlMedium",
    "profileImageUrlLarge",
    "ownerDisplayName",
    "ownerFullName",
    "ownerId",
    "ownerProfileImageUrlSmall",
    "ownerProfileImageUrlMedium",
    "ownerProfileImageUrlLarge",
    "userPro",
    "imageURL",
    "imageUrl",
}


def _walk_keys(obj):
    """Yield every key encountered at any nesting depth."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


def _assert_no_pii_keys(obj, helper_name: str) -> None:
    """Walk the trimmed output and assert no cross-cutting drop survives."""
    all_keys = list(_walk_keys(obj))
    for key in all_keys:
        assert key not in _CROSS_CUTTING_PII_KEYS, (
            f"{helper_name} leaked cross-cutting PII key {key!r}"
        )
        assert not (
            isinstance(key, str)
            and (key.endswith("TimeLocal") or key.endswith("TimestampLocal"))
        ), f"{helper_name} leaked `*Local` timestamp key {key!r}"


def test_trim_sleep_strips_cross_cutting_fields() -> None:
    full = load_fixture("sleep_payload_full")
    _assert_no_pii_keys(trim_sleep(full), "trim_sleep")


def test_trim_sleep_streams_strips_cross_cutting_fields() -> None:
    full = load_fixture("sleep_payload_full")
    _assert_no_pii_keys(trim_sleep_streams(full), "trim_sleep_streams")


def test_trim_hrv_strips_cross_cutting_fields() -> None:
    full = load_fixture("hrv_payload_full")
    _assert_no_pii_keys(trim_hrv(full), "trim_hrv")


def test_trim_stress_strips_cross_cutting_fields() -> None:
    full = load_fixture("stress_payload_full")
    _assert_no_pii_keys(trim_stress(full), "trim_stress")


def test_trim_body_battery_strips_cross_cutting_fields() -> None:
    full = load_fixture("body_battery_payload_full")
    _assert_no_pii_keys(trim_body_battery(full), "trim_body_battery")


def test_trim_steps_strips_cross_cutting_fields() -> None:
    full = load_fixture("steps_payload_full")
    _assert_no_pii_keys(trim_steps(full), "trim_steps")


def test_trim_daily_summary_strips_cross_cutting_fields() -> None:
    full = load_fixture("daily_summary_payload_full")
    _assert_no_pii_keys(trim_daily_summary(full), "trim_daily_summary")


def test_trim_activity_summary_strips_cross_cutting_fields() -> None:
    full = load_fixture("activities_list_payload_full")
    _assert_no_pii_keys(trim_activity_summary(full), "trim_activity_summary")


def test_trim_activity_detail_strips_cross_cutting_fields() -> None:
    full = load_fixture("activity_payload_full")
    _assert_no_pii_keys(trim_activity_detail(full), "trim_activity_detail")


def test_trim_activity_splits_strips_cross_cutting_fields() -> None:
    full = load_fixture("activity_splits_payload_full")
    _assert_no_pii_keys(trim_activity_splits(full), "trim_activity_splits")


# ---------------------------------------------------------------------------
# End-to-end: synthetic payloads with PII / image URLs at depth confirm the
# final pass strips them even when the per-tool denylist wouldn't.
# ---------------------------------------------------------------------------


def test_trim_activity_summary_drops_nested_image_url_recursively() -> None:
    """A hypothetical `imageURL` inside a `recordedDevices`-style block on
    an activity-summary item drops at the cross-cutting pass.
    """
    payload = [
        {
            "activityId": 1,
            "activityName": "Run",
            "recordedDevices": [
                {"deviceId": 100, "imageURL": "https://res.garmin.com/x.png"},
            ],
        }
    ]
    trimmed = trim_activity_summary(payload)
    assert trimmed[0]["recordedDevices"] == [{"deviceId": 100}]


def test_trim_activity_detail_drops_user_info_at_depth() -> None:
    """A nested `userInfoDto.profileImageUrlLarge` drops even when the
    parent block survives the per-tool allowlist.
    """
    # `metadataDTO` is allowlisted; the per-tool trim drops `userInfoDto`
    # at the top of `metadataDTO`. We construct a hypothetical case where
    # the PII sits inside an allowlisted sub-block to verify the
    # cross-cutting pass strips it even when the per-tool filter passes
    # the container through.
    payload = {
        "activityId": 1,
        "metadataDTO": {
            "lapCount": 1,
            "sensors": [
                {
                    "sensorType": "POWER",
                    "manufacturer": "STAGES",
                    # PII at depth — cross-cutting drop should fire.
                    "ownerFullName": "Test User",
                    "imageUrl": "https://res.garmin.com/x.png",
                }
            ],
        },
    }
    trimmed = trim_activity_detail(payload)
    sensor = trimmed["metadataDTO"]["sensors"][0]
    assert "ownerFullName" not in sensor
    assert "imageUrl" not in sensor
    assert sensor["sensorType"] == "POWER"
    assert sensor["manufacturer"] == "STAGES"


def test_trim_body_battery_drops_local_timestamps_recursively() -> None:
    """Per-day `*Local` timestamps + nested event-level ones both drop."""
    payload = [
        {
            "date": "2026-05-09",
            "startTimestampGMT": 1746745200000,
            "startTimestampLocal": 1746748800000,
            "endTimestampGMT": 1746831600000,
            "endTimestampLocal": 1746835200000,
            "bodyBatteryActivityEvent": [
                {
                    "eventType": "SLEEP",
                    "eventStartTimeGmt": 1746745200000,
                    "eventStartTimeLocal": 1746748800000,
                }
            ],
        }
    ]
    trimmed = trim_body_battery(payload)
    entry = trimmed[0]
    assert "startTimestampLocal" not in entry
    assert "endTimestampLocal" not in entry
    assert entry["startTimestampGMT"] == 1746745200000
    # Nested event-level Local timestamp also drops.
    event = entry["bodyBatteryActivityEvent"][0]
    assert "eventStartTimeLocal" not in event
    assert event["eventStartTimeGmt"] == 1746745200000
