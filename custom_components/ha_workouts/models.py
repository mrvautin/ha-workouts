"""Source-agnostic data models shared by all workout data providers."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class ActivityType(StrEnum):
    """Normalized activity types, mapped from each source's native vocabulary."""

    RUNNING = "running"
    CYCLING = "cycling"
    SWIMMING = "swimming"
    WALKING = "walking"
    STRENGTH_TRAINING = "strength_training"
    HIKING = "hiking"
    YOGA = "yoga"
    OTHER = "other"


@dataclass(slots=True)
class ActivitySplit:
    """One auto-lap split within an activity (e.g. one km of a run).

    cumulative_distance_meters/cumulative_elapsed_seconds are the running
    totals AT THE END of this split — i.e. "how far/how long in, by the point
    this split finished" — which is what answers "how long did it take to
    reach the Nth km" directly, without the caller needing to sum prior splits
    themselves.
    """

    index: int
    distance_meters: float
    duration_seconds: float
    elapsed_seconds: float
    cumulative_distance_meters: float
    cumulative_elapsed_seconds: float
    avg_pace_seconds_per_km: float | None = None
    avg_heart_rate: int | None = None
    max_heart_rate: int | None = None
    elevation_gain_meters: float | None = None


@dataclass(slots=True)
class Activity:
    """A single logged workout/activity, normalized across sources."""

    source: str
    source_id: str
    activity_type: ActivityType
    start: datetime
    duration_seconds: float
    distance_meters: float | None = None
    calories: float | None = None
    avg_heart_rate: int | None = None
    max_heart_rate: int | None = None
    elevation_gain_meters: float | None = None
    name: str | None = None
    #: Per-km (or per-mile, per the source's own auto-lap config) splits, if
    #: the source provides them and we've fetched them for this activity.
    #: None means "not fetched/not available" — distinct from an empty list,
    #: which would mean "fetched, but the activity genuinely had no laps".
    splits: list[ActivitySplit] | None = None


@dataclass(slots=True)
class DailySummary:
    """Aggregated per-day stats, normalized across sources."""

    source: str
    day: date
    steps: int | None = None
    resting_heart_rate: int | None = None
    sleep_seconds: float | None = None
    stress_avg: int | None = None
    body_battery_max: int | None = None
    body_battery_min: int | None = None
    active_calories: float | None = None
    floors_climbed: int | None = None
    vo2_max: float | None = None


@dataclass(slots=True)
class BodyComposition:
    """A single body composition / weigh-in reading, normalized across sources."""

    source: str
    timestamp: datetime
    weight_kg: float
    body_fat_percent: float | None = None
    muscle_mass_kg: float | None = None
    bmi: float | None = None


@dataclass(slots=True)
class WorkoutData:
    """Full payload fetched by a source on each coordinator refresh."""

    activities: list[Activity] = field(default_factory=list)
    daily_summary: DailySummary | None = None
    body_composition: BodyComposition | None = None
