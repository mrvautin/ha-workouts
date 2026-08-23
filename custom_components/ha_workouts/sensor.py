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
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .activity_log import async_backfill_activity_splits
from .const import (
    CONF_BACKFILL_DAYS,
    CONF_SOURCE_TYPE,
    CONF_SPLITS_BACKFILL_DAYS,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_SPLITS_BACKFILL_DAYS,
    DOMAIN,
    SOURCE_APPLE_HEALTH,
    SOURCE_GARMIN,
)
from .coordinator import WorkoutDataUpdateCoordinator
from .models import Activity, ActivityType, WorkoutData
from .statistics_import import (
    DISTANCE_ACTIVITY_TYPES,
    BackfillProgress,
    async_apply_activity_deltas,
    async_backfill_activity_statistics,
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
    WorkoutSensorDescription(
        key="vo2_max",
        translation_key="vo2_max",
        native_unit_of_measurement="mL/kg/min",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: data.daily_summary.vo2_max if data.daily_summary else None,
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

#: Source types whose API exposes device-level daily stats (steps, HR, etc.).
SOURCES_WITH_DAILY_SUMMARY = {SOURCE_GARMIN}


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


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WorkoutDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entry_slug = coordinator.source.key
    source_type = entry.data.get(CONF_SOURCE_TYPE)

    summary_descriptions = list(LAST_ACTIVITY_SENSORS)
    if source_type in SOURCES_WITH_DAILY_SUMMARY:
        summary_descriptions = list(DAILY_SUMMARY_SENSORS) + summary_descriptions

    entities: list[SensorEntity] = [
        WorkoutSensor(coordinator, entry, description) for description in summary_descriptions
    ]
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
        async_add_entities(activity_entities)

    if entry.data.get(CONF_SOURCE_TYPE) == SOURCE_APPLE_HEALTH:
        # Apple Health has no historical archive to backfill from (see
        # sources/apple_health.py) — every range-fetch would just return empty
        # after burning through the full configured pacing delay for nothing.
        # Create the activity-type sensors immediately; they'll seed at 0 (no
        # prior statistics exist) and grow only as workouts are pushed in.
        coordinator.backfill_progress.state = "complete"
        _add_activity_type_sensors()
    else:
        # Backfill historical per-type statistics in the background so setup isn't
        # blocked by what can be a long-running, multi-year import. Progress is
        # exposed via coordinator.backfill_progress / the status sensor above.
        # Gap-aware: only fetches days not already covered, so this is cheap to
        # re-run after options change.
        backfill_days = entry.options.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)
        backfill_task = hass.async_create_task(
            async_backfill_activity_statistics(
                hass,
                entry_slug,
                coordinator.source,
                backfill_days,
                coordinator.backfill_progress,
                coordinator.request_lock,
            )
        )
        # The per-activity-type sensors are cumulative-total entities that seed
        # their starting value from the backfill's imported statistics on first
        # add (see CumulativeActivitySensor). They're only created once the
        # backfill finishes, so that seed always finds the final baseline rather
        # than racing it and permanently starting from 0 if the backfill is
        # still running at setup time.
        backfill_task.add_done_callback(_add_activity_type_sensors)

        if source_type == SOURCE_GARMIN:
            # Independent of the activity/statistics backfill above — see
            # activity_log.async_backfill_activity_splits for why this is
            # opt-in, separately paced, and doesn't block sensor creation on
            # anything (it only ever fills in already-recorded activities'
            # missing splits, it doesn't gate any entity's initial state).
            splits_backfill_days = entry.options.get(
                CONF_SPLITS_BACKFILL_DAYS, DEFAULT_SPLITS_BACKFILL_DAYS
            )
            hass.async_create_task(
                async_backfill_activity_splits(
                    hass,
                    entry_slug,
                    coordinator.source,
                    splits_backfill_days,
                    coordinator.splits_backfill_progress,
                    coordinator.request_lock,
                )
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
        self._progress.on_change = self._handle_progress_change

    async def async_will_remove_from_hass(self) -> None:
        self._progress.on_change = None
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
