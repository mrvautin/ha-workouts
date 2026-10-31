"""Sensor entities for ha_workouts: daily summary stats, per-activity-type
lifetime-cumulative totals (for aggregated charting), and the most recent activity."""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .activity_log import (
    async_backfill_activity_splits,
    async_reset_single_whole_activity_splits,
)
from .backfill_progress import BackfillProgress
from .const import (
    CONF_BACKFILL_DAYS,
    CONF_COROS_MCP_ACCESS_TOKEN,
    CONF_SOURCE_TYPE,
    CONF_WEEK_START_DAY,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_WEEK_START_DAY,
    DOMAIN,
    SOURCE_APPLE_HEALTH,
    SOURCE_COROS,
    SOURCE_GARMIN,
    SOURCE_OURA,
)
from .coordinator import WorkoutDataUpdateCoordinator
from .models import Activity, ActivityType, WorkoutData
from .period_sensors import PeriodTotals, async_get_period_totals
from .personal_bests import DISTANCES as PERSONAL_BEST_DISTANCES
from .personal_bests import async_get_personal_bests
from .statistics_import import (
    DISTANCE_ACTIVITY_TYPES,
    async_apply_activity_deltas,
    async_backfill_activity_statistics,
    async_backfill_personal_bests,
    async_get_earliest_known_activity_day,
    entity_id_slug,
    statistic_id_slug,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class WorkoutSensorDescription(SensorEntityDescription):
    """Describes a daily-summary sensor and how to read its value from WorkoutData."""

    value_fn: Callable[[WorkoutData], object | None] = lambda data: None


#: Daily-summary sensors (steps, heart rate, body battery, etc.) — only
#: meaningful for sources that expose device-level daily stats (Garmin).
#: Strava and Apple Health never populate WorkoutData.daily_summary, so these
#: would sit permanently unavailable for those sources; see SOURCES_WITH_DAILY_SUMMARY.
DAILY_SUMMARY_SENSORS: tuple[WorkoutSensorDescription, ...] = (
    WorkoutSensorDescription(
        key="steps",
        translation_key="steps",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.daily_summary.steps if data.daily_summary else None,
    ),
    WorkoutSensorDescription(
        key="resting_heart_rate",
        translation_key="resting_heart_rate",
        native_unit_of_measurement="bpm",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data.daily_summary.resting_heart_rate if data.daily_summary else None
        ),
    ),
    WorkoutSensorDescription(
        key="active_calories",
        translation_key="active_calories",
        native_unit_of_measurement="kcal",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: (
            data.daily_summary.active_calories if data.daily_summary else None
        ),
    ),
    WorkoutSensorDescription(
        key="floors_climbed",
        translation_key="floors_climbed",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: (
            data.daily_summary.floors_climbed if data.daily_summary else None
        ),
    ),
    WorkoutSensorDescription(
        key="stress_avg",
        translation_key="stress_avg",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.daily_summary.stress_avg if data.daily_summary else None,
    ),
    WorkoutSensorDescription(
        key="body_battery_max",
        translation_key="body_battery_max",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data.daily_summary.body_battery_max if data.daily_summary else None
        ),
    ),
    # VO2 max and Training Readiness (score/level/feedback) are deliberately
    # NOT sensors here: Garmin's official Personal API (see sources/garmin.py's
    # module docstring) has no equivalent to either — confirmed absent from
    # Garmin's own published OpenAPI spec, AND confirmed (by direct testing
    # against a real account whose Garmin Connect app clearly shows a current
    # VO2max) that the API's one VO2max-shaped field
    # (SummarizedActivityDTO.vO2MaxValue) is never actually populated, across
    # 50 real activities checked. Both come from Garmin's internal metrics/
    # training-status service, which only the OLD unofficial garminconnect
    # library's session-cookie login could reach — the new Personal API's
    # bearer token is rejected by those endpoints entirely (plain sign-in
    # page returned, not even a 401). A genuine feature loss from the pivot
    # to official auth, not a parsing gap; DailySummary no longer even has
    # these fields.
)

#: HRV sensors — split out from DAILY_SUMMARY_SENSORS since, unlike the rest
#: of that set (steps, body battery, etc.), Coros also populates
#: DailySummary's HRV fields (see sources/coros.py's _fetch_daily_summary) —
#: just not any of DAILY_SUMMARY_SENSORS' other fields, which would sit
#: permanently "unknown" for a Coros entry if it opted into that whole set.
#: See SOURCES_WITH_HRV.
HRV_SENSORS: tuple[WorkoutSensorDescription, ...] = (
    WorkoutSensorDescription(
        key="hrv_last_night_avg",
        translation_key="hrv_last_night_avg",
        native_unit_of_measurement="ms",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data.daily_summary.hrv_last_night_avg if data.daily_summary else None
        ),
    ),
    WorkoutSensorDescription(
        key="hrv_weekly_avg",
        translation_key="hrv_weekly_avg",
        native_unit_of_measurement="ms",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data.daily_summary.hrv_weekly_avg if data.daily_summary else None
        ),
    ),
    WorkoutSensorDescription(
        key="hrv_status",
        translation_key="hrv_status",
        value_fn=lambda data: data.daily_summary.hrv_status if data.daily_summary else None,
    ),
)

#: Sensors backed by Coros MCP (see sources/coros_mcp.py) — an OPTIONAL
#: second connection a Coros entry may or may not have (see
#: config_flow.py's async_step_coros_mcp); added per-entry in
#: async_setup_entry below based on whether THIS entry actually has MCP
#: tokens, not a static source-type set like SOURCES_WITH_DAILY_SUMMARY/
#: SOURCES_WITH_HRV above, since two different Coros entries could differ.
COROS_MCP_SENSORS: tuple[WorkoutSensorDescription, ...] = (
    WorkoutSensorDescription(
        key="steps",
        translation_key="steps",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.daily_summary.steps if data.daily_summary else None,
    ),
    WorkoutSensorDescription(
        key="active_calories",
        translation_key="active_calories",
        native_unit_of_measurement="kcal",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: (
            data.daily_summary.active_calories if data.daily_summary else None
        ),
    ),
    WorkoutSensorDescription(
        key="sleep_score",
        translation_key="sleep_score",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.daily_summary.sleep_score if data.daily_summary else None,
    ),
    WorkoutSensorDescription(
        key="sleep_duration",
        translation_key="sleep_duration",
        native_unit_of_measurement="min",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            round(data.daily_summary.sleep_seconds / 60, 1)
            if data.daily_summary and data.daily_summary.sleep_seconds is not None
            else None
        ),
    ),
    WorkoutSensorDescription(
        key="recovery_percent",
        translation_key="recovery_percent",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data.daily_summary.recovery_percent if data.daily_summary else None
        ),
    ),
    WorkoutSensorDescription(
        key="recovery_level",
        translation_key="recovery_level",
        value_fn=lambda data: (
            data.daily_summary.recovery_level if data.daily_summary else None
        ),
    ),
    WorkoutSensorDescription(
        key="recovery_estimated_full_hours",
        translation_key="recovery_estimated_full_hours",
        native_unit_of_measurement="h",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data.daily_summary.recovery_estimated_full_hours if data.daily_summary else None
        ),
    ),
    WorkoutSensorDescription(
        key="training_load_short_term",
        translation_key="training_load_short_term",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data.daily_summary.training_load_short_term if data.daily_summary else None
        ),
    ),
    WorkoutSensorDescription(
        key="training_load_long_term",
        translation_key="training_load_long_term",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data.daily_summary.training_load_long_term if data.daily_summary else None
        ),
    ),
    WorkoutSensorDescription(
        key="fitness_threshold_pace",
        translation_key="fitness_threshold_pace",
        # A "M:SS /km" string, not a number+unit: seconds-per-km is
        # technically what Coros returns, but nobody reads pace that way —
        # every runner expects e.g. "4:45 /km", not "285 s/km" (a real,
        # user-reported confusion). No state_class/native_unit_of_measurement
        # for the same reason hrv_status/recovery_level above have none —
        # this is a formatted string, not a plain numeric measurement.
        value_fn=lambda data: (
            _format_pace(data.fitness_assessment.threshold_pace_seconds_per_km)
            if data.fitness_assessment
            else None
        ),
    ),
    # VO2max, Running Performance, and the four race predictions are
    # deliberately NOT exposed as sensors: confirmed by directly calling
    # queryFitnessAssessmentOverview against a real account (whose Coros
    # phone app clearly shows all of these — VO2max, a Running Fitness
    # score, race predictions) that the MCP tool itself only ever returned
    # Threshold Pace, nothing else — a real gap in Coros's own API, not a
    # parsing bug (FitnessAssessment/parse_fitness_assessment in
    # coros_mcp_parsers.py already handle these fields correctly whenever
    # Coros's API does start returning them; no code change needed if that
    # ever happens, just re-add the sensor descriptions here).
)

#: Oura's own daily scores and sleep detail (see sources/oura.py and
#: SOURCES_WITH_OURA below) — kept as its own set, not folded into
#: DAILY_SUMMARY_SENSORS/HRV_SENSORS above, since Oura has no
#: floors_climbed/stress_avg/body_battery_*/hrv_status equivalents (those
#: would sit permanently unavailable) but does have its own readiness/
#: activity scores and sleep-stage breakdown neither of those sets covers.
OURA_SENSORS: tuple[WorkoutSensorDescription, ...] = (
    WorkoutSensorDescription(
        key="readiness_score",
        translation_key="readiness_score",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data.daily_summary.readiness_score if data.daily_summary else None
        ),
    ),
    WorkoutSensorDescription(
        key="sleep_score",
        translation_key="sleep_score",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.daily_summary.sleep_score if data.daily_summary else None,
    ),
    WorkoutSensorDescription(
        key="activity_score",
        translation_key="activity_score",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data.daily_summary.activity_score if data.daily_summary else None
        ),
    ),
    WorkoutSensorDescription(
        key="steps",
        translation_key="steps",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.daily_summary.steps if data.daily_summary else None,
    ),
    WorkoutSensorDescription(
        key="active_calories",
        translation_key="active_calories",
        native_unit_of_measurement="kcal",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: (
            data.daily_summary.active_calories if data.daily_summary else None
        ),
    ),
    WorkoutSensorDescription(
        key="sleep_duration",
        translation_key="sleep_duration",
        native_unit_of_measurement="min",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            round(data.daily_summary.sleep_seconds / 60, 1)
            if data.daily_summary and data.daily_summary.sleep_seconds is not None
            else None
        ),
    ),
    WorkoutSensorDescription(
        key="deep_sleep_duration",
        translation_key="deep_sleep_duration",
        native_unit_of_measurement="min",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            round(data.daily_summary.deep_sleep_seconds / 60, 1)
            if data.daily_summary and data.daily_summary.deep_sleep_seconds is not None
            else None
        ),
    ),
    WorkoutSensorDescription(
        key="rem_sleep_duration",
        translation_key="rem_sleep_duration",
        native_unit_of_measurement="min",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            round(data.daily_summary.rem_sleep_seconds / 60, 1)
            if data.daily_summary and data.daily_summary.rem_sleep_seconds is not None
            else None
        ),
    ),
    WorkoutSensorDescription(
        key="light_sleep_duration",
        translation_key="light_sleep_duration",
        native_unit_of_measurement="min",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            round(data.daily_summary.light_sleep_seconds / 60, 1)
            if data.daily_summary and data.daily_summary.light_sleep_seconds is not None
            else None
        ),
    ),
    WorkoutSensorDescription(
        key="resting_heart_rate",
        translation_key="resting_heart_rate",
        native_unit_of_measurement="bpm",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data.daily_summary.resting_heart_rate if data.daily_summary else None
        ),
    ),
    WorkoutSensorDescription(
        key="oura_avg_hrv",
        translation_key="oura_avg_hrv",
        native_unit_of_measurement="ms",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.daily_summary.oura_avg_hrv if data.daily_summary else None,
    ),
)

#: "Last activity" sensors — meaningful for any source that reports activities
#: at all, including Apple Health (once at least one workout has been pushed).
LAST_ACTIVITY_SENSORS: tuple[WorkoutSensorDescription, ...] = (
    WorkoutSensorDescription(
        key="last_activity_name",
        translation_key="last_activity_name",
        value_fn=lambda data: data.activities[-1].name if data.activities else None,
    ),
    WorkoutSensorDescription(
        key="last_activity_type",
        translation_key="last_activity_type",
        value_fn=lambda data: (
            data.activities[-1].activity_type if data.activities else None
        ),
    ),
    WorkoutSensorDescription(
        key="last_activity_duration",
        translation_key="last_activity_duration",
        native_unit_of_measurement="min",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            round(data.activities[-1].duration_seconds / 60, 1) if data.activities else None
        ),
    ),
)

#: Running form / power / training-load sensors for the most recent
#: activity — currently only populated by Garmin's official Personal API
#: (see sources/garmin.py's Activity-parsing docstring and
#: SOURCES_WITH_ACTIVITY_DETAIL below). Each value_fn guards against both
#: "no activities yet" AND "most recent activity doesn't report this field"
#: (e.g. cadence stays None for a strength-training activity) the same way
#: LAST_ACTIVITY_SENSORS above does.
LAST_ACTIVITY_DETAIL_SENSORS: tuple[WorkoutSensorDescription, ...] = (
    WorkoutSensorDescription(
        key="last_activity_running_cadence",
        translation_key="last_activity_running_cadence",
        native_unit_of_measurement="spm",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data.activities[-1].avg_running_cadence_spm if data.activities else None
        ),
    ),
    WorkoutSensorDescription(
        key="last_activity_cycling_cadence",
        translation_key="last_activity_cycling_cadence",
        native_unit_of_measurement="rpm",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data.activities[-1].avg_cycling_cadence_rpm if data.activities else None
        ),
    ),
    WorkoutSensorDescription(
        key="last_activity_avg_power",
        translation_key="last_activity_avg_power",
        native_unit_of_measurement="W",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.activities[-1].avg_power_watts if data.activities else None,
    ),
    WorkoutSensorDescription(
        key="last_activity_max_power",
        translation_key="last_activity_max_power",
        native_unit_of_measurement="W",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.activities[-1].max_power_watts if data.activities else None,
    ),
    WorkoutSensorDescription(
        key="last_activity_normalized_power",
        translation_key="last_activity_normalized_power",
        native_unit_of_measurement="W",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data.activities[-1].normalized_power_watts if data.activities else None
        ),
    ),
    WorkoutSensorDescription(
        key="last_activity_vertical_oscillation",
        translation_key="last_activity_vertical_oscillation",
        native_unit_of_measurement="cm",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: (
            data.activities[-1].avg_vertical_oscillation_cm if data.activities else None
        ),
    ),
    WorkoutSensorDescription(
        key="last_activity_ground_contact_time",
        translation_key="last_activity_ground_contact_time",
        native_unit_of_measurement="ms",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: (
            data.activities[-1].avg_ground_contact_time_ms if data.activities else None
        ),
    ),
    WorkoutSensorDescription(
        key="last_activity_training_load",
        translation_key="last_activity_training_load",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data.activities[-1].training_load if data.activities else None,
    ),
    WorkoutSensorDescription(
        key="last_activity_aerobic_training_effect",
        translation_key="last_activity_aerobic_training_effect",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: (
            data.activities[-1].aerobic_training_effect if data.activities else None
        ),
    ),
    WorkoutSensorDescription(
        key="last_activity_anaerobic_training_effect",
        translation_key="last_activity_anaerobic_training_effect",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: (
            data.activities[-1].anaerobic_training_effect if data.activities else None
        ),
    ),
    # Deliberately NO "last activity fastest split" sensors here (previously
    # last_activity_fastest_5k/10k): Garmin's fastestSplit_* field is the
    # fastest CONTINUOUS split WITHIN that one activity, not a personal
    # best — a real, user-reported confusion (a slower run's "fastest 5K"
    # showing 24:08 while the user's genuine all-time 5K PB, sitting in an
    # activity from weeks earlier, was 19:17). See personal_bests.py and
    # PERSONAL_BEST_SENSORS below for the actual all-time-best tracking
    # this was replaced with.
)

#: Source types whose API exposes device-level daily stats (steps, HR, etc.).
SOURCES_WITH_DAILY_SUMMARY = {SOURCE_GARMIN}

#: Source types whose API can populate DailySummary's HRV fields — a
#: narrower set than SOURCES_WITH_DAILY_SUMMARY above, since Coros only ever
#: populates those three fields, never the rest (steps, body battery, etc.).
SOURCES_WITH_HRV = {SOURCE_GARMIN, SOURCE_COROS}

#: Source types whose API populates Activity's running-form/power/
#: training-load fields (see models.py's Activity docstring) — currently
#: Garmin only.
SOURCES_WITH_ACTIVITY_DETAIL = {SOURCE_GARMIN}

#: Source types that get OURA_SENSORS above — currently Oura only. See
#: that tuple's own docstring for why this is separate from
#: SOURCES_WITH_DAILY_SUMMARY/SOURCES_WITH_HRV rather than folded into them.
SOURCES_WITH_OURA = {SOURCE_OURA}


def _format_duration(total_seconds: float | None) -> str | None:
    """Format a duration in seconds as "M:SS" (or "H:MM:SS" past an hour) —
    the way a split/race time is actually read, not a raw seconds count.
    """
    if total_seconds is None:
        return None
    seconds = round(total_seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _format_pace(seconds_per_km: float | None) -> str | None:
    """Format a seconds-per-km figure as "M:SS /km" — the way every runner
    actually reads pace, not the raw number a source's API returns it as
    (a real, user-reported confusion: Coros's threshold pace sensor showing
    "285.0 s/km" instead of "4:45 /km").
    """
    if seconds_per_km is None:
        return None
    total_seconds = round(seconds_per_km)
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d} /km"


def _activity_duration_minutes(activity: Activity) -> float:
    return round(activity.duration_seconds / 60, 1)


def _activity_calories(activity: Activity) -> float:
    return activity.calories or 0


def _activity_distance_km(activity: Activity) -> float:
    return round((activity.distance_meters or 0) / 1000, 2)


@dataclass(frozen=True, kw_only=True)
class CumulativeSensorDescription(SensorEntityDescription):
    """Describes a lifetime-cumulative per-activity-type sensor.

    metric_value_fn extracts this metric's contribution from a single activity —
    the sensor sums this over whichever activities in each coordinator poll it
    hasn't already counted (tracked by Activity.source_id), and adds that as the
    delta to its running lifetime total (see CumulativeActivitySensor).

    Summing by not-yet-seen source_id, rather than assuming every activity in a
    poll happened "today" (the original design), is what lets this work for
    Apple Health too: unlike Garmin/Strava's daily poll, Apple Health workouts
    arrive via webhook whenever the user's Shortcut happens to run, which can be
    hours after the workout's real date (e.g. phone locked overnight) — an
    id-based delta attributes each workout correctly regardless of when it
    happens to be delivered, where a "today's total" delta would not.
    """

    activity_type: ActivityType
    metric: str
    metric_value_fn: Callable[[Activity], float]


def _build_activity_type_sensors() -> tuple[CumulativeSensorDescription, ...]:
    """One duration + calories sensor per ActivityType, plus distance for types that have it.

    Deliberately no state_class here (nor on CumulativeActivitySensor itself) —
    see that class's docstring for why. This entity is a live display only;
    its actual long-term statistics live under a separate external
    statistic_id (see statistics_import.py's statistic_id_slug), so there's
    nothing for state_class to usefully drive here — setting it would only
    make HA's recorder auto-compile a second, unused statistics sequence under
    this entity's own statistic_id. NOTE: SensorEntity resolves state_class
    from either _attr_state_class OR entity_description.state_class — omitting
    it from the entity class alone is NOT sufficient, it must also be absent
    here.
    """
    descriptions: list[CumulativeSensorDescription] = []
    for activity_type in ActivityType:
        descriptions.append(
            CumulativeSensorDescription(
                key=f"{activity_type.value}_duration_minutes",
                translation_key="activity_duration_minutes",
                translation_placeholders={"activity_type": activity_type.value},
                native_unit_of_measurement="min",
                activity_type=activity_type,
                metric="duration_minutes",
                metric_value_fn=_activity_duration_minutes,
            )
        )
        descriptions.append(
            CumulativeSensorDescription(
                key=f"{activity_type.value}_calories",
                translation_key="activity_calories",
                translation_placeholders={"activity_type": activity_type.value},
                native_unit_of_measurement="kcal",
                activity_type=activity_type,
                metric="calories",
                metric_value_fn=_activity_calories,
            )
        )
        if activity_type in DISTANCE_ACTIVITY_TYPES:
            descriptions.append(
                CumulativeSensorDescription(
                    key=f"{activity_type.value}_distance_km",
                    translation_key="activity_distance_km",
                    translation_placeholders={"activity_type": activity_type.value},
                    native_unit_of_measurement="km",
                    device_class=SensorDeviceClass.DISTANCE,
                    activity_type=activity_type,
                    metric="distance_km",
                    metric_value_fn=_activity_distance_km,
                )
            )
    return tuple(descriptions)


ACTIVITY_TYPE_SENSORS: tuple[CumulativeSensorDescription, ...] = _build_activity_type_sensors()


@callback
def _async_remove_stale_entities(
    hass: HomeAssistant, entry: ConfigEntry, current_unique_ids: set[str]
) -> None:
    """Remove any sensor entity this config entry has in the registry whose
    unique_id ISN'T one current_unique_ids says this run actually created.

    Every unique_id this integration ever generates is deterministic
    (f"{entry.entry_id}_{key}", for some static key) — see the various
    self._attr_unique_id assignments throughout this module — so a
    unique_id left in the registry but absent from current_unique_ids can
    only mean one thing: an OLDER version of this code created it and the
    current version no longer does (e.g. a WorkoutSensorDescription that
    was since removed, like the vo2_max/training_readiness_*/
    last_activity_fastest_5k/10k sensors removed earlier this project).
    Those entities then sit forever showing "unavailable" with no value_fn
    ever writing to them again — a real, repeatedly-hit annoyance this
    session alone (confirmed manually via the entity registry each time) —
    rather than being an intentional, temporarily-unpopulated entity.

    Called once per setup run, after every entity this run creates has
    actually been added (see async_setup_entry's _add_activity_type_sensors,
    the single completion point for both the Apple Health synchronous path
    and the backfill-gated async path other sources use) — calling this any
    earlier would incorrectly treat not-yet-created-but-legitimate entities
    (e.g. the activity-type/period-to-date sensors, which aren't added until
    the backfill finishes) as stale and delete them.
    """
    registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            registry_entry.platform == DOMAIN
            and registry_entry.domain == "sensor"
            and registry_entry.unique_id not in current_unique_ids
        ):
            _LOGGER.info(
                "Removing stale ha_workouts entity %s (no longer created by current code)",
                registry_entry.entity_id,
            )
            registry.async_remove(registry_entry.entity_id)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WorkoutDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entry_slug = coordinator.source.key
    source_type = entry.data.get(CONF_SOURCE_TYPE)

    summary_descriptions = list(LAST_ACTIVITY_SENSORS)
    if source_type in SOURCES_WITH_DAILY_SUMMARY:
        summary_descriptions = list(DAILY_SUMMARY_SENSORS) + summary_descriptions
    # SOURCES_WITH_DAILY_SUMMARY sources (Garmin) are always also in
    # SOURCES_WITH_HRV, so this always fires for them too — HRV_SENSORS is
    # additive to DAILY_SUMMARY_SENSORS above, not part of it, since Coros is
    # in SOURCES_WITH_HRV without being in SOURCES_WITH_DAILY_SUMMARY.
    if source_type in SOURCES_WITH_HRV:
        summary_descriptions = list(HRV_SENSORS) + summary_descriptions
    if source_type in SOURCES_WITH_OURA:
        summary_descriptions = list(OURA_SENSORS) + summary_descriptions
    if source_type in SOURCES_WITH_ACTIVITY_DETAIL:
        summary_descriptions = list(LAST_ACTIVITY_DETAIL_SENSORS) + summary_descriptions
    # Per-entry, not per-source-type: whether THIS Coros entry has MCP
    # connected (see config_flow.py's async_step_coros_mcp) — a user can
    # have a Coros entry with or without it.
    has_coros_mcp = (
        source_type == SOURCE_COROS and entry.data.get(CONF_COROS_MCP_ACCESS_TOKEN) is not None
    )
    if has_coros_mcp:
        summary_descriptions = list(COROS_MCP_SENSORS) + summary_descriptions

    entities: list[SensorEntity] = [
        WorkoutSensor(coordinator, entry, description) for description in summary_descriptions
    ]
    if has_coros_mcp:
        entities.append(CorosMcpDebugLogSensor(coordinator, entry))
    entities.append(
        BackfillStatusSensor(
            coordinator,
            entry,
            coordinator.backfill_progress,
            "backfill_status",
            "backfill_status",
        )
    )
    entities.append(LastUpdatedSensor(coordinator, entry))
    if source_type == SOURCE_GARMIN:
        entities.append(
            BackfillStatusSensor(
                coordinator,
                entry,
                coordinator.splits_backfill_progress,
                "splits_backfill_status",
                "splits_backfill_status",
            )
        )
    async_add_entities(entities)
    # Tracks every unique_id this run actually creates, across both this
    # immediate batch and the activity-type batch added later (see
    # _add_activity_type_sensors) — used by _async_remove_stale_entities
    # below to find and remove anything left over from an OLDER version of
    # this code that no longer creates it (e.g. a sensor description that
    # was since deleted). Entities carry their own unique_id as
    # self._attr_unique_id, so this just mirrors that same f-string shape
    # rather than reading it back off each constructed entity.
    current_unique_ids = {f"{entry.entry_id}_{description.key}" for description in summary_descriptions}
    if has_coros_mcp:
        current_unique_ids.add(f"{entry.entry_id}_coros_mcp_debug_log")
    current_unique_ids.add(f"{entry.entry_id}_backfill_status")
    current_unique_ids.add(f"{entry.entry_id}_last_updated")
    if source_type == SOURCE_GARMIN:
        current_unique_ids.add(f"{entry.entry_id}_splits_backfill_status")

    def _add_activity_type_sensors(_task: object | None = None) -> None:
        activity_entities = [
            CumulativeActivitySensor(
                coordinator,
                entry,
                description,
                explicit_entity_id=entity_id_slug(
                    entry_slug, description.activity_type, description.metric
                ),
                statistic_id=statistic_id_slug(
                    entry_slug, description.activity_type, description.metric
                ),
            )
            for description in ACTIVITY_TYPE_SENSORS
        ]
        activity_entities.extend(
            PeriodToDateSensor(
                coordinator,
                entry,
                entry_slug,
                activity_type,
                period,
                explicit_entity_id=f"sensor.{entry_slug}_{activity_type.value}_{period}td",
            )
            for activity_type in ActivityType
            for period in ("week", "month", "year")
        )
        async_add_entities(activity_entities)
        current_unique_ids.update(
            f"{entry.entry_id}_{description.key}" for description in ACTIVITY_TYPE_SENSORS
        )
        current_unique_ids.update(
            f"{entry.entry_id}_{activity_type.value}_{period}td"
            for activity_type in ActivityType
            for period in ("week", "month", "year")
        )
        _async_remove_stale_entities(hass, entry, current_unique_ids)

    if entry.data.get(CONF_SOURCE_TYPE) == SOURCE_APPLE_HEALTH:
        # Apple Health has no historical archive to backfill from (see
        # sources/apple_health.py) — every range-fetch would just return empty
        # after burning through the full configured pacing delay for nothing.
        # Create the activity-type sensors immediately; they'll seed at 0 (no
        # prior statistics exist) and grow only as workouts are pushed in.
        coordinator.backfill_progress.state = "complete"
        _add_activity_type_sensors()
    else:
        history_entities: list[SensorEntity] = [
            HistoryStartSensor(coordinator, entry, entry_slug, coordinator.backfill_progress)
        ]
        current_unique_ids.add(f"{entry.entry_id}_history_start")
        if source_type in SOURCES_WITH_ACTIVITY_DETAIL:
            history_entities.extend(
                PersonalBestSensor(
                    coordinator,
                    entry,
                    entry_slug,
                    coordinator.backfill_progress,
                    distance_key,
                    f"personal_best_{distance_key}",
                )
                for distance_key, _ in PERSONAL_BEST_DISTANCES
            )
            current_unique_ids.update(
                f"{entry.entry_id}_personal_best_{distance_key}"
                for distance_key, _ in PERSONAL_BEST_DISTANCES
            )
        async_add_entities(history_entities)
        # Backfill historical per-type statistics in the background so setup isn't
        # blocked by what can be a long-running, multi-year import. Progress is
        # exposed via coordinator.backfill_progress / the status sensor above.
        # Gap-aware: only fetches days not already covered, so this is cheap to
        # re-run after options change.
        #
        # entry.async_create_background_task, NOT hass.async_create_task: a
        # plain hass-level task is still tracked and AWAITED (with a
        # timeout) by HA's own startup bootstrap — confirmed as a real,
        # user-hit bug in production: a slow/large first-ever backfill hit
        # that bootstrap timeout, logged "Something is blocking Home
        # Assistant from wrapping up the start up phase... continue
        # anyway", and HA proceeding anyway while this task was still
        # running triggered a SECOND entity-platform setup pass, which then
        # logged "does not generate unique IDs... ignoring sensor.garmin_
        # yoga_monthtd" (and 54 others) — every duplicate entity from that
        # second pass silently dropped. A background task is explicitly
        # exempt from bootstrap's wait (see its own docstring: "Will not
        # block startup"), which is the correct semantic for a job that can
        # legitimately take minutes to hours, and ties its lifetime to this
        # config entry (auto-cancelled on unload/reload, not just HA
        # shutdown) rather than being a bare hass-level task.
        backfill_days = entry.options.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)
        backfill_task = entry.async_create_background_task(
            hass,
            async_backfill_activity_statistics(
                hass,
                entry_slug,
                coordinator.source,
                backfill_days,
                coordinator.backfill_progress,
                coordinator.request_lock,
            ),
            name=f"ha_workouts_{entry_slug}_statistics_backfill",
        )
        # The per-activity-type sensors are cumulative-total entities that seed
        # their starting value from the backfill's imported statistics on first
        # add (see CumulativeActivitySensor). They're only created once the
        # backfill finishes, so that seed always finds the final baseline rather
        # than racing it and permanently starting from 0 if the backfill is
        # still running at setup time.
        backfill_task.add_done_callback(_add_activity_type_sensors)

        if source_type == SOURCE_GARMIN:

            async def _splits_backfill_with_migration() -> None:
                # One-time, self-limiting migration: reset any activity
                # still carrying the old single-whole-activity-split bug's
                # signature back to splits=None, so the backfill below
                # naturally re-fetches real per-km data for it. Idempotent
                # without needing its own "has run" flag (unlike
                # personal_bests.py's scan) — once an activity is reset, its
                # new real splits will never again match the single-split/
                # full-distance signature, so a later run is just a cheap
                # no-op scan over already-correct data.
                reset_count = await async_reset_single_whole_activity_splits(
                    hass, entry_slug
                )
                if reset_count:
                    _LOGGER.info(
                        "Reset %d %s activities with stale single-split data "
                        "for re-backfill (see sources/garmin.py's FIT-splits fix)",
                        reset_count,
                        entry_slug,
                    )
                await async_backfill_activity_splits(
                    hass,
                    entry_slug,
                    coordinator.source,
                    backfill_days,
                    coordinator.splits_backfill_progress,
                    coordinator.request_lock,
                )

            # Independent of the activity/statistics backfill above (runs as
            # its own, separately-paced task, and doesn't gate sensor
            # creation on anything — see activity_log.async_backfill_activity_splits).
            # Shares the same backfill_days depth computed above rather than
            # having its own separate setting. Background task, not a plain
            # hass-level one — see the statistics backfill's own comment
            # above for why (HA bootstrap otherwise waits on and can time
            # out on this, triggering duplicate entity setup).
            entry.async_create_background_task(
                hass,
                _splits_backfill_with_migration(),
                name=f"ha_workouts_{entry_slug}_splits_backfill",
            )
        if source_type in SOURCES_WITH_ACTIVITY_DETAIL:
            # One-time (per source, until a full wipe) history scan feeding
            # the PersonalBestSensor entities created above — see
            # statistics_import.async_backfill_personal_bests's docstring
            # for why this must be a separate pass rather than piggybacking
            # on the statistics backfill above (which skips re-fetching
            # activities entirely once statistics are already covered,
            # which would otherwise leave personal bests permanently stuck
            # on an account that already had its statistics imported before
            # this feature existed). Background task — see the statistics
            # backfill's own comment above for why.
            entry.async_create_background_task(
                hass,
                async_backfill_personal_bests(
                    hass,
                    entry_slug,
                    coordinator.source,
                    backfill_days,
                    coordinator.request_lock,
                ),
                name=f"ha_workouts_{entry_slug}_personal_bests_backfill",
            )


class WorkoutSensor(CoordinatorEntity[WorkoutDataUpdateCoordinator], SensorEntity):
    """A single daily-summary, per-activity-type, or last-activity sensor."""

    entity_description: WorkoutSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WorkoutDataUpdateCoordinator,
        entry: ConfigEntry,
        description: WorkoutSensorDescription,
        explicit_entity_id: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        if explicit_entity_id is not None:
            self.entity_id = explicit_entity_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=coordinator.source.key.capitalize(),
        )

    @property
    def native_value(self) -> object | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)


class CumulativeActivitySensor(CoordinatorEntity[WorkoutDataUpdateCoordinator], SensorEntity):
    """A per-activity-type lifetime-cumulative sensor (e.g. total km ever run).

    Reports a genuinely non-decreasing running total rather than "today's total" —
    HA's Statistics Graph "Change" mode computes daily/weekly/monthly bars as a pure
    diff between period sums with no awareness of resets (see statistics_import.py's
    module docstring), so a value that resets each day produces large spurious
    negative spikes there. A lifetime-cumulative value avoids that entirely and is
    also what keeps this entity consistent with the backfill's cumulative sums.

    Tracks which activities (by Activity.source_id) have already been folded into
    the running total, so each coordinator poll adds only genuinely new activities
    as the delta — regardless of what day they're dated, which is what makes this
    work correctly for Apple Health's webhook delivery (can arrive hours after the
    workout's real date) as well as Garmin/Strava's same-day polling.

    This entity's own statistic_id (self.entity_id) carries no long-term
    statistics at all — self._statistic_id, a separate EXTERNAL statistic_id
    (see statistics_import.py's statistic_id_slug and module docstring for why),
    is what Statistics Graph cards should point at, and what
    async_apply_activity_deltas/the backfill functions write to. This entity
    exists purely to show the live running total as a normal display value.

    Deliberately does NOT set state_class: since the statistic_id above is used
    for nothing, there's nothing for state_class to usefully enable, and it
    would only make HA's recorder auto-compile a second, unused statistics
    sequence under this entity's own statistic_id. Every statistics write for
    this integration must go through async_apply_activity_deltas/the backfill
    functions, targeting the external statistic_id — never state_class-driven
    auto-compile of the entity's own.
    """

    entity_description: CumulativeSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WorkoutDataUpdateCoordinator,
        entry: ConfigEntry,
        description: CumulativeSensorDescription,
        explicit_entity_id: str,
        statistic_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self.entity_id = explicit_entity_id
        # The external statistic_id (statistics_import.statistic_id_slug) that
        # actually holds this sensor's long-term history — deliberately NOT
        # self.entity_id, see class docstring for why.
        self._statistic_id = statistic_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=coordinator.source.key.capitalize(),
        )
        self._cumulative_total: float = 0.0
        self._counted_source_ids: set[str] = set()

    async def async_added_to_hass(self) -> None:
        # Seed BEFORE calling super().async_added_to_hass() — that's what
        # registers this entity as a coordinator listener. Seeding after
        # registering left a real window where a concurrent coordinator update
        # (e.g. a webhook-triggered refresh, or just an unlucky poll timing)
        # could fire _handle_coordinator_update while _cumulative_total was
        # still its __init__ default of 0.0, computing a delta against the
        # wrong baseline and permanently writing a corrupted statistics row —
        # this was a real, repeatedly-hit bug, not a hypothetical one.
        #
        # The statistics table (written exclusively by this integration — see
        # the class docstring) is the ONLY source ever trusted for seeding.
        # Deliberately NOT using RestoreEntity/hass.states' cached last value as
        # a fallback: that cache is keyed by entity_id and, critically, is NOT
        # cleared when a config entry is deleted — so deleting and re-adding a
        # source (a normal thing to do, e.g. to reconfigure) could silently
        # resurrect an old, unrelated cumulative total from before the delete.
        # Statistics genuinely having nothing (a real first-ever boot for this
        # entity) is the only case where seeding at 0 is correct.
        seeded = await self._async_seed_from_statistics()
        self._cumulative_total = seeded if seeded is not None else 0.0
        await super().async_added_to_hass()
        self._handle_coordinator_update()

    async def _async_seed_from_statistics(self) -> float | None:
        def _query() -> float | None:
            rows = get_last_statistics(self.hass, 1, self._statistic_id, False, {"sum"})
            series = rows.get(self._statistic_id)
            if not series:
                return None
            return series[0]["sum"] or 0.0

        return await get_instance(self.hass).async_add_executor_job(_query)

    @callback
    def _handle_coordinator_update(self) -> None:
        data = self.coordinator.data
        if data is not None:
            new_activities = [
                a
                for a in data.activities
                if a.activity_type == self.entity_description.activity_type
                and a.source_id not in self._counted_source_ids
            ]
            # (source_id, value) pairs, not pre-summed — async_apply_activity_deltas
            # de-dupes by source_id against a persisted ledger before summing,
            # since this entity's own _counted_source_ids is per-instance,
            # in-memory state that can't protect against a second, independent
            # CumulativeActivitySensor instance for the same statistic_id (see
            # that function's docstring for the real bug this fixes).
            deltas_by_day: dict[date, list[tuple[str, float]]] = {}
            for activity in new_activities:
                value = self.entity_description.metric_value_fn(activity)
                if value:
                    day = activity.start.date()
                    deltas_by_day.setdefault(day, []).append((activity.source_id, value))
            if deltas_by_day:
                self._cumulative_total += sum(
                    value for entries in deltas_by_day.values() for _, value in entries
                )
                self.hass.async_create_task(
                    async_apply_activity_deltas(
                        self.hass,
                        self.coordinator.source.key,
                        self._statistic_id,
                        self.entity_description.activity_type,
                        self.entity_description.metric,
                        deltas_by_day,
                    )
                )
            self._counted_source_ids.update(a.source_id for a in new_activities)
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float:
        return round(self._cumulative_total, 2)


class PeriodToDateSensor(CoordinatorEntity[WorkoutDataUpdateCoordinator], SensorEntity):
    """Week/month/year-to-date total for one activity type.

    See period_sensors.py's module docstring for why this exists: HA core's
    own "change over a calendar period" statistics lookup produced a stuck,
    incorrect year-to-date figure in real-world testing, so these sensors
    compute the period sum directly from Activity records instead — no
    recorder statistics period-boundary math involved.

    Distance-capable activity types (running, cycling, etc.) use distance_km
    as the main state, matching the existing per-type cumulative sensors'
    split; other types (strength training, yoga, ...) use duration_minutes
    instead, since distance doesn't apply to them. The metric not used as the
    state, plus activity_count, are exposed as attributes rather than
    separate entities — this is one sensor per activity type per period
    (week/month/year), not one per metric, to avoid tripling the entity count.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WorkoutDataUpdateCoordinator,
        entry: ConfigEntry,
        entry_slug: str,
        activity_type: ActivityType,
        period: str,
        explicit_entity_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry_slug = entry_slug
        self._activity_type = activity_type
        self._period = period
        self._uses_distance = activity_type in DISTANCE_ACTIVITY_TYPES
        self._attr_unique_id = f"{entry.entry_id}_{activity_type.value}_{period}td"
        self._attr_translation_key = f"activity_{period}td"
        self._attr_translation_placeholders = {"activity_type": activity_type.value}
        self._attr_native_unit_of_measurement = "km" if self._uses_distance else "min"
        if self._uses_distance:
            self._attr_device_class = SensorDeviceClass.DISTANCE
        self.entity_id = explicit_entity_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=coordinator.source.key.capitalize(),
        )
        self._totals: PeriodTotals | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._async_refresh_totals()

    @callback
    def _handle_coordinator_update(self) -> None:
        # Recomputing here (rather than only reacting to new activities) is
        # deliberately simple: this reads already-local activity_log data (no
        # source API call), so there's no meaningful cost to just always
        # recomputing on every coordinator update rather than trying to track
        # "did anything actually change for this period" — and it's what
        # correctly rolls the value back to 0 the moment a new week/month/year
        # starts, with no separate reset logic needed.
        self.hass.async_create_task(self._async_refresh_totals())
        super()._handle_coordinator_update()

    async def _async_refresh_totals(self) -> None:
        week_start_day = self.coordinator.entry.options.get(
            CONF_WEEK_START_DAY, DEFAULT_WEEK_START_DAY
        )
        self._totals = await async_get_period_totals(
            self.hass,
            self._entry_slug,
            self._activity_type,
            self._period,
            dt_util.now().date(),
            week_start_day,
        )
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        if self._totals is None:
            return None
        value = self._totals.distance_km if self._uses_distance else self._totals.duration_minutes
        return round(value, 2)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        if self._totals is None:
            return {}
        attrs: dict[str, object] = {"activity_count": self._totals.activity_count}
        if self._uses_distance:
            attrs["duration_minutes"] = round(self._totals.duration_minutes, 2)
        else:
            attrs["distance_km"] = round(self._totals.distance_km, 2)
        attrs["calories"] = round(self._totals.calories, 2)
        return attrs


class BackfillStatusSensor(CoordinatorEntity[WorkoutDataUpdateCoordinator], SensorEntity):
    """Reports progress of a background historical-import task.

    Generic over WHICH BackfillProgress it reports — used both for the main
    activity/statistics backfill (coordinator.backfill_progress) and, for
    Garmin, the separate opt-in splits backfill
    (coordinator.splits_backfill_progress; see
    activity_log.async_backfill_activity_splits). State is one of
    idle/running/complete/error. Attributes expose how far back the import has
    reached so far, useful while a multi-year backfill is in progress.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WorkoutDataUpdateCoordinator,
        entry: ConfigEntry,
        progress: BackfillProgress,
        unique_id_suffix: str,
        translation_key: str,
    ) -> None:
        super().__init__(coordinator)
        self._progress = progress
        self._attr_unique_id = f"{entry.entry_id}_{unique_id_suffix}"
        self._attr_translation_key = translation_key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=coordinator.source.key.capitalize(),
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # progress updates independently of the coordinator's own refresh
        # cycle (it's mutated from the background backfill task), so push a
        # state write from there directly rather than polling for changes.
        self._progress.listeners.append(self._handle_progress_change)

    async def async_will_remove_from_hass(self) -> None:
        self._progress.listeners.remove(self._handle_progress_change)
        await super().async_will_remove_from_hass()

    @callback
    def _handle_progress_change(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        return self._progress.state

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        progress = self._progress
        return {
            "oldest_day_imported": (
                progress.oldest_day_imported.isoformat()
                if progress.oldest_day_imported
                else None
            ),
            "target_day": progress.target_day.isoformat() if progress.target_day else None,
            "days_imported_this_run": progress.days_imported_this_run,
            "error": progress.error,
        }


class HistoryStartSensor(CoordinatorEntity[WorkoutDataUpdateCoordinator], SensorEntity):
    """Reports the earliest day this integration has confirmed has real
    activity history, once the backfill's early-stop (see
    statistics_import.py's _CONSECUTIVE_EMPTY_CHUNKS_TO_STOP) has discovered
    it.

    Doubles as a user-visible confirmation of how far back the import
    actually reached — the same value is read back by
    async_backfill_activity_statistics on every future run so it doesn't
    needlessly re-walk a range it already confirmed is empty (see that
    function's docstring). Stays unknown until the backfill has actually hit
    the early-stop at least once; for a fixed (non-zero) backfill_days depth
    it never triggers, so this sensor stays unknown in that configuration.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.DATE
    _attr_translation_key = "history_start"

    def __init__(
        self,
        coordinator: WorkoutDataUpdateCoordinator,
        entry: ConfigEntry,
        entry_slug: str,
        progress: BackfillProgress,
    ) -> None:
        super().__init__(coordinator)
        self._entry_slug = entry_slug
        self._attr_unique_id = f"{entry.entry_id}_history_start"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=coordinator.source.key.capitalize(),
        )
        self._progress = progress
        self._earliest_day: date | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # The persisted value only changes when the backfill's early-stop
        # fires, which is signalled the same way BackfillStatusSensor learns
        # of progress — piggyback on that rather than polling.
        self._progress.listeners.append(self._handle_progress_change)
        await self._async_refresh_value()

    async def async_will_remove_from_hass(self) -> None:
        self._progress.listeners.remove(self._handle_progress_change)
        await super().async_will_remove_from_hass()

    @callback
    def _handle_progress_change(self) -> None:
        self.hass.async_create_task(self._async_refresh_value())

    async def _async_refresh_value(self) -> None:
        self._earliest_day = await async_get_earliest_known_activity_day(
            self.hass, self._entry_slug
        )
        self.async_write_ha_state()

    @property
    def native_value(self) -> date | None:
        return self._earliest_day


class PersonalBestSensor(CoordinatorEntity[WorkoutDataUpdateCoordinator], SensorEntity):
    """Reports the all-time fastest split ever seen for one named running
    distance (1K/5K/10K/half marathon/marathon) — see personal_bests.py's
    module docstring for why this is a genuinely different, more useful
    figure than a single activity's own "fastest split within itself"
    (which is what the old last_activity_fastest_5k/10k sensors showed,
    removed in favour of this).

    Two independent paths update the underlying persisted record: the live
    coordinator poll (today's new activities might set a new best) and the
    historical backfill (every activity ever fetched is folded in,
    chunk-by-chunk). This entity listens to both: the coordinator's own
    personal_best_update_listeners list for the live-poll path, and
    BackfillProgress.listeners for the backfill path — matching
    HistoryStartSensor's dual-source pattern for the same reason (the
    persisted value can change from either).
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WorkoutDataUpdateCoordinator,
        entry: ConfigEntry,
        entry_slug: str,
        progress: BackfillProgress,
        distance_key: str,
        translation_key: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry_slug = entry_slug
        self._progress = progress
        self._distance_key = distance_key
        self._attr_unique_id = f"{entry.entry_id}_personal_best_{distance_key}"
        self._attr_translation_key = translation_key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=coordinator.source.key.capitalize(),
        )
        self._best_seconds: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._progress.listeners.append(self._handle_external_change)
        self.coordinator.personal_best_update_listeners.append(self._handle_external_change)
        await self._async_refresh_value()

    async def async_will_remove_from_hass(self) -> None:
        self._progress.listeners.remove(self._handle_external_change)
        self.coordinator.personal_best_update_listeners.remove(self._handle_external_change)
        await super().async_will_remove_from_hass()

    @callback
    def _handle_external_change(self) -> None:
        self.hass.async_create_task(self._async_refresh_value())

    async def _async_refresh_value(self) -> None:
        bests = await async_get_personal_bests(self.hass, self._entry_slug)
        self._best_seconds = bests.get(self._distance_key)
        self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        # A "H:MM:SS"/"M:SS" string, not a number+unit — same reasoning as
        # fitness_threshold_pace's own docstring: nobody reads a split time
        # as raw seconds.
        return _format_duration(self._best_seconds)


class LastUpdatedSensor(CoordinatorEntity[WorkoutDataUpdateCoordinator], SensorEntity):
    """Reports when data was last actually fetched from the source.

    Works the same way for all three sources: Garmin/Strava's periodic poll and
    Apple Health's webhook-triggered refresh both update
    coordinator.last_data_update (see coordinator.py), so this entity doesn't
    need to know which kind of source it's attached to.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "last_updated"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: WorkoutDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_last_updated"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=coordinator.source.key.capitalize(),
        )

    @property
    def native_value(self) -> object | None:
        return self.coordinator.last_data_update


class CorosMcpDebugLogSensor(CoordinatorEntity[WorkoutDataUpdateCoordinator], SensorEntity):
    """Exposes the raw text of recent Coros MCP tool calls for troubleshooting.

    Coros's MCP API returns free-form prose (see sources/coros_mcp.py's
    module docstring), parsed by best-effort regex against wording captured
    from one real account. Anyone whose account phrases a response
    differently (different watch model, region, language, or just a Coros
    wording change) will silently get missing fields with no error — this
    integration's own dev/test account has no watch at all, so there's no
    way to notice or fix that locally. Enabled by default (still
    entity_category diagnostic, so it stays out of normal dashboards) while
    the MCP parsers are still being validated against real accounts with
    real watches — zero setup needed for someone willing to help
    troubleshoot to have raw_log content ready. Meant to be temporary: once
    the parsers are confirmed reliable across enough real accounts, this
    sensor can be removed entirely rather than kept forever. See
    sources/coros.py's CorosSource.mcp_debug_log for what populates it.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "coros_mcp_debug_log"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: WorkoutDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_coros_mcp_debug_log"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=coordinator.source.key.capitalize(),
        )

    @property
    def native_value(self) -> str | None:
        log = getattr(self.coordinator.source, "mcp_debug_log", None)
        if not log:
            return None
        last = log[-1]
        # Sensor states are capped at 255 chars by HA — the state is just a
        # "what/when" pointer; the full text of every recent call lives in
        # the raw_log attribute below instead.
        return f"{last['tool']} @ {last['time']}"

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        log = getattr(self.coordinator.source, "mcp_debug_log", None)
        return {"raw_log": list(log) if log else []}
