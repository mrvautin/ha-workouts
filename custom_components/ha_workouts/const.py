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
