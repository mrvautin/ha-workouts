"""Parsers for Coros MCP's free-form prose tool responses (see coros_mcp.py's
module docstring for why these responses are prose, not JSON).

Each parser is regex/line-based against real captured output from a live
account (see this integration's PR/commit history for the exact repr()'d
strings used to write these) and returns None for any field it can't find,
rather than raising — a Coros wording change should degrade a sensor to
"unknown", not crash the whole daily poll. None of these parsers are backed
by a published schema; treat them as best-effort.
"""
from __future__ import annotations

import re
from datetime import date

from ..models import DailySummary, FitnessAssessment

# Matches "Label: 123" / "Label: 4:45" / "Label: 12.3%" style lines — the
# consistent shape every Coros MCP tool response uses for its data points.
_LABEL_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 /\-]*?):\s*(.+)$")


def _label_value_map(text: str) -> dict[str, str]:
    """Collect every "Label: value" line in the text into a dict, keyed by
    the label text exactly as it appears (e.g. "Steps", "Short-Term Load").

    Callers look up by the specific label(s) they need; this doesn't
    interpret values itself since different tools use the same label names
    (e.g. "Comment") for different things.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _LABEL_RE.match(line.strip())
        if match:
            values[match.group(1).strip()] = match.group(2).strip()
    return values


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.search(r"-?\d+", value)
    return int(match.group()) if match else None


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    return float(match.group()) if match else None


def _parse_hms_to_seconds(value: str) -> float | None:
    """Parse a colon-separated duration like "4:45" (min:sec) or "1:23:45"
    (h:min:sec) into total seconds. Used for pace and race-prediction
    fields — Coros formats both this way in its prose responses.
    """
    match = re.search(r"\d+(?::\d+){1,2}", value)
    if not match:
        return None
    parts = [int(p) for p in match.group().split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return float(minutes * 60 + seconds)
    hours, minutes, seconds = parts
    return float(hours * 3600 + minutes * 60 + seconds)


def parse_daily_health_data(source: str, day: date, text: str) -> DailySummary | None:
    """queryDailyHealthData: one or more "--- YYYYMMDD ---" day blocks, each
    followed by "Steps: N | Calories: N kcal | Exercise: N min" — returns
    the block matching `day`, or the most recent block if `day` isn't found
    (e.g. today's data hasn't synced from the watch yet, same as
    querySleepData's own explicit "may not be synced" note).
    """
    day_str = day.strftime("%Y%m%d")
    blocks = re.split(r"^---\s*(\d{8})\s*---$", text, flags=re.MULTILINE)
    # re.split with a capturing group interleaves: [preamble, day1, body1, day2, body2, ...]
    day_bodies = {blocks[i]: blocks[i + 1] for i in range(1, len(blocks) - 1, 2)}
    body = day_bodies.get(day_str)
    if body is None and day_bodies:
        body = day_bodies[max(day_bodies)]
    if body is None:
        return None
    values = _label_value_map(body)
    return DailySummary(
        source=source,
        day=day,
        steps=_parse_int(values.get("Steps")),
        active_calories=_parse_float(values.get("Calories")),
    )


def parse_sleep_data(source: str, day: date, text: str) -> DailySummary | None:
    """querySleepData: a "YYYY-MM-DD" date header per night, followed by
    "Label: value" lines including a sleep score when present. Same
    most-recent-available fallback as parse_daily_health_data, since a
    night's sleep can legitimately not be synced yet.
    """
    day_str = day.isoformat()
    blocks = re.split(r"^(\d{4}-\d{2}-\d{2})$", text, flags=re.MULTILINE)
    day_bodies = {blocks[i]: blocks[i + 1] for i in range(1, len(blocks) - 1, 2)}
    body = day_bodies.get(day_str)
    if body is None and day_bodies:
        body = day_bodies[max(day_bodies)]
    if body is None:
        return None
    values = _label_value_map(body)
    sleep_minutes = _parse_int(values.get("Total Sleep") or values.get("Main Sleep"))
    return DailySummary(
        source=source,
        day=day,
        sleep_score=_parse_int(values.get("Sleep Score")),
        sleep_seconds=(sleep_minutes * 60) if sleep_minutes is not None else None,
    )


def parse_sleep_hrv(source: str, day: date, text: str) -> DailySummary | None:
    """querySleepHrv: same per-night structure as parse_sleep_data, with
    an "Average" or "HRV" value line per night. "No data found..." is a
    normal, expected response (e.g. no watch, or nothing synced yet) —
    returns None rather than treating it as a parse failure.
    """
    if "no data found" in text.lower() or "no sleep hrv" in text.lower():
        return None
    day_str = day.isoformat()
    blocks = re.split(r"^(\d{4}-\d{2}-\d{2})$", text, flags=re.MULTILINE)
    day_bodies = {blocks[i]: blocks[i + 1] for i in range(1, len(blocks) - 1, 2)}
    body = day_bodies.get(day_str)
    if body is None and day_bodies:
        body = day_bodies[max(day_bodies)]
    if body is None:
        return None
    values = _label_value_map(body)
    avg = _parse_float(values.get("Average") or values.get("HRV") or values.get("Avg HRV"))
    if avg is None:
        return None
    return DailySummary(source=source, day=day, hrv_last_night_avg=avg)


def parse_recovery_status(source: str, day: date, text: str) -> DailySummary | None:
    """queryRecoveryStatus: "Recovery: N%", "Level: <text>",
    "Estimated Full Recovery: Nh" — a single current snapshot, not
    per-day, so `day` is only used to stamp the returned DailySummary.
    """
    values = _label_value_map(text)
    percent = _parse_int(values.get("Recovery"))
    level = values.get("Level")
    hours = _parse_float(values.get("Estimated Full Recovery"))
    if percent is None and level is None and hours is None:
        return None
    return DailySummary(
        source=source,
        day=day,
        recovery_percent=percent,
        recovery_level=level,
        recovery_estimated_full_hours=hours,
    )


def parse_fitness_assessment(source: str, text: str) -> FitnessAssessment | None:
    """queryFitnessAssessmentOverview: whichever of "VO2max", "Running
    Performance", "Threshold Pace", and the four race predictions Coros has
    enough training history to compute — confirmed on a real account that
    fields can be individually absent (e.g. only Threshold Pace present),
    which is expected, not a parse failure.
    """
    values = _label_value_map(text)
    threshold_pace = values.get("Threshold Pace")
    result = FitnessAssessment(
        source=source,
        vo2_max=_parse_float(values.get("VO2max") or values.get("VO2 Max")),
        running_performance=_parse_float(values.get("Running Performance")),
        threshold_pace_seconds_per_km=(
            _parse_hms_to_seconds(threshold_pace) if threshold_pace else None
        ),
        race_prediction_5k_seconds=_race_seconds(values, "5K"),
        race_prediction_10k_seconds=_race_seconds(values, "10K"),
        race_prediction_half_marathon_seconds=_race_seconds(values, "Half Marathon"),
        race_prediction_marathon_seconds=_race_seconds(values, "Marathon"),
    )
    if all(
        v is None
        for v in (
            result.vo2_max,
            result.running_performance,
            result.threshold_pace_seconds_per_km,
            result.race_prediction_5k_seconds,
            result.race_prediction_10k_seconds,
            result.race_prediction_half_marathon_seconds,
            result.race_prediction_marathon_seconds,
        )
    ):
        return None
    return result


def _race_seconds(values: dict[str, str], label: str) -> float | None:
    raw = values.get(label)
    return _parse_hms_to_seconds(raw) if raw else None


def parse_training_load(source: str, day: date, text: str) -> DailySummary | None:
    """queryTrainingLoadAssessment: a "YYYY-MM-DD" date header per day,
    followed by "Comment: ...", "Short-Term Load: N", "Long-Term Load: N".
    Same most-recent-available fallback as the other per-day parsers.
    """
    day_str = day.isoformat()
    blocks = re.split(r"^(\d{4}-\d{2}-\d{2})$", text, flags=re.MULTILINE)
    day_bodies = {blocks[i]: blocks[i + 1] for i in range(1, len(blocks) - 1, 2)}
    body = day_bodies.get(day_str)
    if body is None and day_bodies:
        body = day_bodies[max(day_bodies)]
    if body is None:
        return None
    values = _label_value_map(body)
    short_term = _parse_float(values.get("Short-Term Load"))
    long_term = _parse_float(values.get("Long-Term Load"))
    if short_term is None and long_term is None:
        return None
    return DailySummary(
        source=source,
        day=day,
        training_load_short_term=short_term,
        training_load_long_term=long_term,
    )
