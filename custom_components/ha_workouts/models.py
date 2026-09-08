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
    #: Last night's average HRV reading, in milliseconds (RMSSD) — Garmin's
    #: own "last night" figure, not a same-day-so-far average.
    hrv_last_night_avg: float | None = None
    hrv_weekly_avg: float | None = None
    #: Garmin's own qualitative bucket for the HRV reading above, e.g.
    #: "BALANCED"/"UNBALANCED"/"LOW" — kept as the source's raw string rather
    #: than normalized, since there's no other source to normalize against yet.
    hrv_status: str | None = None
    training_readiness_score: int | None = None
    #: Garmin's own qualitative bucket, e.g. "PRIME"/"HIGH"/"MODERATE"/"LOW"/
    #: "POOR" — kept as the source's raw string, same reasoning as hrv_status.
    training_readiness_level: str | None = None
    training_readiness_feedback: str | None = None
    #: 0-100, from Coros's own MCP querySleepData tool — a genuine numeric
    #: score, unlike hrv_status above which has no Coros equivalent.
    sleep_score: int | None = None
    #: Coros's own qualitative bucket, e.g. "Heavy training allowed" — kept
    #: as the source's raw string, same reasoning as hrv_status/
    #: training_readiness_level above.
    recovery_level: str | None = None
    recovery_percent: int | None = None
    recovery_estimated_full_hours: float | None = None
    #: Coros's own MCP queryTrainingLoadAssessment tool — short-term
    #: (roughly 7-day) and long-term (roughly 28-day) training load, and
    #: their ratio (ACWR-style; >1 means recent load is rising relative to
    #: the longer baseline). No Garmin equivalent field exists to share
    #: this with; Garmin's closest concept (acuteLoad) already only ever
    #: surfaces inside training_readiness_* above, not as its own number.
    training_load_short_term: float | None = None
    training_load_long_term: float | None = None


@dataclass(slots=True)
class FitnessAssessment:
    """A point-in-time fitness/race-readiness snapshot, normalized across
    sources — currently only produced by Coros's MCP
    queryFitnessAssessmentOverview tool (see sources/coros_mcp.py).

    Deliberately separate from DailySummary: unlike steps/HRV/sleep, which
    are genuinely per-day figures, these are Coros's current best estimate
    given ALL of a user's training history — there's no meaningful "today's
    VO2max" the way there's a "today's step count". Fields are individually
    optional since Coros's own tool only returns whichever of these it has
    enough data to compute (confirmed: an account with little training
    history got only threshold_pace_seconds_per_km, with vo2_max and every
    race prediction absent).
    """

    source: str
    vo2_max: float | None = None
    running_performance: float | None = None
    threshold_pace_seconds_per_km: float | None = None
    race_prediction_5k_seconds: float | None = None
    race_prediction_10k_seconds: float | None = None
    race_prediction_half_marathon_seconds: float | None = None
    race_prediction_marathon_seconds: float | None = None


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
    fitness_assessment: FitnessAssessment | None = None
