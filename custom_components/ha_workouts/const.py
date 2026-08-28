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

#: Pause between each activity's splits request during the historical splits
#: backfill — far more conservative than the main backfill's per-CHUNK pause
#: (statistics_import._CHUNK_DAYS's pacing), since this job makes one request
#: PER ACTIVITY rather than per multi-month chunk; hundreds of historical
#: activities at one request every 45s still finishes within a few hours
#: without ever bursting Garmin's rate limits.
SPLITS_BACKFILL_PAUSE_SECONDS = 45.0

CONF_WEEK_START_DAY = "week_start_day"

#: HA has no system-wide "first day of week" setting an integration can read
#: (it's only a per-card option in Statistics Graph/Statistic card YAML, e.g.
#: period.calendar.first_weekday — see
#: homeassistant/components/recorder/util.py). So week-to-date sensors
#: (period_sensors.py) need their own config. Values are Python's
#: date.weekday() convention (Monday=0 .. Sunday=6).
WEEK_START_DAY_OPTIONS: dict[str, int] = {
    "Monday": 0,
    "Sunday": 6,
}
DEFAULT_WEEK_START_DAY = 0  # Monday
