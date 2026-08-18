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
