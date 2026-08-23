"""Constants for the ha_workouts integration."""
from datetime import timedelta

DOMAIN = "ha_workouts"

CONF_SOURCE_TYPE = "source_type"
CONF_BACKFILL_DAYS = "backfill_days"

SOURCE_GARMIN = "garmin"
SOURCE_STRAVA = "strava"
SOURCE_APPLE_HEALTH = "apple_health"
SOURCE_TYPES = [SOURCE_GARMIN, SOURCE_STRAVA, SOURCE_APPLE_HEALTH]

CONF_WEBHOOK_ID = "webhook_id"

#: Strava OAuth scopes needed to read activity list, activity detail (calories),
#: and athlete stats.
STRAVA_OAUTH_SCOPES = "activity:read_all,read"

DEFAULT_UPDATE_INTERVAL = timedelta(minutes=15)

#: How often to run the background statistics consistency check (see
#: statistics_import.async_check_statistics_consistency) — deliberately much
#: less frequent than the data poll above, since it's a safety net for rare
#: corruption, not something that needs near-real-time detection.
CONSISTENCY_CHECK_INTERVAL = timedelta(hours=12)

#: Options offered for how far back to import history. "all" is stored as 0 and
#: means "no lower bound" — fetch back to whatever Garmin has.
BACKFILL_DAYS_OPTIONS: dict[str, int] = {
    "90 days": 90,
    "1 year": 365,
    "2 years": 730,
    "5 years": 1825,
    "All available history": 0,
}
DEFAULT_BACKFILL_DAYS = 365

CONF_SPLITS_BACKFILL_DAYS = "splits_backfill_days"

#: Options for the opt-in historical splits backfill (Garmin only — see
#: activity_log.async_backfill_activity_splits). Separate from
#: BACKFILL_DAYS_OPTIONS/CONF_BACKFILL_DAYS above: unlike the main
#: activity/statistics backfill, Garmin's API has no batch endpoint for
#: splits — it's strictly one extra request per historical activity, so this
#: is opt-in, defaults to Off, and is paced far more slowly (see
#: SPLITS_BACKFILL_PAUSE_SECONDS).
SPLITS_BACKFILL_DAYS_OPTIONS: dict[str, int] = {
    "Off": -1,
    "Last 30 days": 30,
    "Last 90 days": 90,
    "Last year": 365,
    "All available history": 0,
}
DEFAULT_SPLITS_BACKFILL_DAYS = -1

#: Pause between each activity's splits request during the historical splits
#: backfill — far more conservative than the main backfill's per-CHUNK pause
#: (statistics_import._CHUNK_DAYS's pacing), since this job makes one request
#: PER ACTIVITY rather than per multi-month chunk; hundreds of historical
#: activities at one request every 45s still finishes within a few hours
#: without ever bursting Garmin's rate limits.
SPLITS_BACKFILL_PAUSE_SECONDS = 45.0
