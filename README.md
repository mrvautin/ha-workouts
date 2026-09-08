# HA Workouts

A Home Assistant custom integration (HACS) that pulls workout and daily health
data from Garmin, Coros, Strava, and/or Apple Health and exposes it as
sensors, with long-term statistics for charting aggregated activity over
time — monthly running distance, year-to-date totals, year-over-year
comparisons, and so on.

You can add Garmin, Coros, Strava, Apple Health, or any combination — each is
a separate config entry with its own source-prefixed sensors
(`sensor.garmin_*`, `sensor.coros_*`, `sensor.strava_*`,
`sensor.apple_health_*`), so data from each source can be charted separately
or side by side.

## Supported sources

- **Garmin Connect** — email/password login (unofficial API, no developer
  account required).
- **Coros** — email/password login (unofficial API, no developer account
  required), plus an optional second connection for steps/sleep/HRV/recovery/
  fitness assessment/training load (see [Coros setup](#coros-setup) below).
- **Strava** — OAuth2, using your own Strava API application (see [Strava
  setup](#strava-setup) below). **As of Strava's June 2026 developer program
  change, this requires an active paid Strava subscription ($11.99/mo) on the
  account that owns the API application** — this is Strava's policy, not
  something this integration can work around. Without a subscription, Strava
  marks the application "Inactive" and rejects requests with a 403 error even
  after a successful sign-in.
- **Apple Health** — via a webhook pushed from an iOS Shortcut (see
  [Apple Health setup](#apple-health-setup) below). Apple doesn't offer a
  cloud API for Health data, so this works by receiving workouts from your
  phone rather than pulling them. There's no depth setting to choose like
  Garmin/Coros/Strava's backfill — instead, the Shortcut's "Get Workouts"
  action returns your full on-device workout history the first time it runs,
  so your existing history arrives all at once as soon as you run it.
- Google Fit / Fitbit — not yet supported.

## Installation

1. In HACS, add this repository as a custom repository (category:
   Integration), then install "HA Workouts".
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration**, search for
   "HA Workouts".
4. Choose a source (Garmin, Coros, Strava, or Apple Health) and follow the
   prompts — see [Garmin setup](#garmin-setup) / [Coros setup](#coros-setup) /
   [Strava setup](#strava-setup) / [Apple Health setup](#apple-health-setup)
   below.
5. For Garmin, Coros, and Strava: choose how far back to import history. This
   backfill runs in the background after setup finishes — for several years
   of history it can take several minutes (see
   [History backfill](#history-backfill) below) — and can be extended later
   without redoing it via the integration's **Configure** option. (Apple
   Health has no depth to choose — see
   [Apple Health setup](#apple-health-setup) below for how its history
   arrives instead.)
6. To add another source, repeat from step 3 (e.g. add Garmin, then run
   setup again and add Coros, Strava, and/or Apple Health).

### Garmin setup

Just enter your Garmin Connect email and password — no developer account or
API key needed. This uses the unofficial `garminconnect` library to sign in
the same way the Garmin Connect app does.

Garmin's API is unofficial and undocumented, so this integration is
deliberately conservative about request pacing to avoid tripping its rate
limits (see [Rate limits](#rate-limits--why-things-might-be-slow) below).

### Coros setup

Enter your Coros account email and password — no developer account or API key
needed. This uses the same unofficial API Coros's own `training.coros.com`
web dashboard uses to sign in.

**Region:** you'll be asked to pick Global (default), Europe, or China.
Almost every account, regardless of where you actually live, uses **Global**
— Europe and China are genuine exceptions for accounts specifically
registered there. If setup fails after picking one, try Global first, then
the other two.

**Important — logging in here signs you out of the Coros app.** Coros only
allows one active session per account, so connecting here will sign you out
of the Coros phone app/Training Hub website, and logging into either of
those later will sign this integration back out in turn. This isn't
something this integration can avoid — it's how Coros's own session system
works. If that's not acceptable, this Coros source may not be a good fit for
you; there's no bundled/shared workaround.

#### Optional: Coros health & fitness data (steps, sleep, HRV, recovery, training load)

Coros's activity data (distance, pace, splits — the part set up above) comes
from a completely different part of their systems than steps, sleep, HRV,
recovery status, fitness assessment, and training load. Those need a
**second, separate connection** — Coros's official "COROS MCP" program —
using the same email/password, but a genuinely different login mechanism
that does **not** sign you out of anything (no session conflict with either
the connection above or the Coros app).

You'll be offered this as an extra step right after entering your Coros
credentials during setup ("Connect Coros health & fitness data") — tick the
box to connect it there and then, or skip it and connect it later from
**Configure** on the Coros integration entry (a checkbox appears there for
as long as it isn't connected yet).

This unlocks these additional sensors, none of which any other source in
this integration provides:

- `sensor.coros_steps`, `sensor.coros_active_calories`
- `sensor.coros_sleep_score`, `sensor.coros_sleep_duration`
- `sensor.coros_recovery`, `sensor.coros_recovery_level`,
  `sensor.coros_estimated_full_recovery`
- `sensor.coros_training_load_short_term`, `sensor.coros_training_load_long_term`
- `sensor.coros_threshold_pace`

**Note on what's *not* included:** Coros's own phone app shows a VO2max
estimate and race-time predictions (5K/10K/half/full marathon) under
"Running Fitness." As of this integration's testing, Coros's own official
API doesn't return those specific figures yet, even though the app clearly
has them — this is a gap in Coros's API, not something this integration can
work around, so no sensors are offered for them. If Coros starts exposing
that data, sensors for it can be added without any other changes.

Any of these can legitimately show "unknown" rather than a number — e.g. HRV
if your specific watch model doesn't record it overnight, or steps if you
don't wear the watch day-to-day. That's Coros genuinely having nothing to
report, not a fault in the integration.

### Strava setup

Strava requires you to register your own API application — this integration
never uses a shared/bundled key:

1. Go to [strava.com/settings/api](https://www.strava.com/settings/api) and
   create an API application.
2. Set **Authorization Callback Domain** to your Home Assistant's domain
   (e.g. `homeassistant.local` or your external hostname — no `https://` and
   no path).
3. Copy the **Client ID** and **Client Secret** shown on that page.
4. In Home Assistant, go to **Settings → Devices & Services → Application
   Credentials**, add an entry for `ha_workouts`, and paste in the Client ID
   and Secret.
5. **Make sure the Strava account that owns this API application has an
   active Strava subscription.** Otherwise the app shows as "Inactive" and
   the integration fails to connect with a 403 error immediately after you
   authorize — this is the single most common setup failure, see
   [Troubleshooting](#troubleshooting).
6. Continue the HA Workouts config flow — you'll be redirected to Strava to
   authorize access, then back here to finish setup.

### Apple Health setup (needs paid app)

Apple Health/HealthKit has no cloud API, so there's nothing to poll — instead,
the integration generates a webhook URL during setup, and an iOS Shortcut on
your phone pushes each workout to it.

1. Start the config flow and choose **Apple Health**. HA Workouts generates a
   webhook URL and shows it to you — copy it (you can view it again later from
   the integration's **Configure** option if you need it, e.g. for a second
   phone).
2. Install [Toolbox Pro](https://apps.apple.com/app/id1476205977) (Paid App - Small amount) on your
   iPhone. Stock Shortcuts can only read raw daily quantity totals (e.g.
   "Walking + Running Distance"), which lump incidental walking in with real
   workouts and carry no per-session date — Toolbox Pro's **Get Workouts**
   action is what gets real structured workout records (type, distance,
   duration, calories) out of HealthKit.
3. Get the Shortcut itself — two options:
   - **Use the pre-built Shortcut (easiest):** open
     [this share link](https://www.icloud.com/shortcuts/afe46d04f4ce464c8ed76937cd865229)
     on your iPhone and tap **Add Shortcut**. The first time it runs, it'll
     ask you to paste in a webhook URL — paste the one from step 1. Skip to
     step 4.
   - **Or build it yourself:** create a new Shortcut with:
     - **Get Workouts** (from Toolbox Pro) to fetch your workout history.
     - For each result, build a dictionary with keys (all set to `Text`):
       - `id` — the workout's unique identifier (used to avoid
         double-counting if the Shortcut runs again over the same workout)
       - `type` — e.g. `Running`, `Cycling`, `Walking`, `Swimming`, `Hiking`
       - `startDate` / `endDate`
       - `duration` — in minutes
       - `distance` — with unit, e.g. `11.4 km` (also accepts `mi`)
       - `calories` — with unit, e.g. `724 kcal`
     - **Get Contents of URL**: method `POST`, URL = the webhook URL from
       step 1, request body = that dictionary, encoded as JSON.
4. Set the Shortcut to run automatically without confirmation prompts — e.g.
   a Shortcuts **Automation** (time of day, or "when app closes" for a
   fitness app) with **Ask Before Running** turned off, so it can post in the
   background.
5. Finish the config flow. The first time the Shortcut runs, matching
   per-activity-type sensors (e.g. `sensor.apple_health_running_distance_km`)
   appear and start reporting. Since Toolbox Pro's **Get Workouts** returns
   your full on-device workout history (not just new workouts), the first run
   typically posts your whole existing history in one go — there's no
   separate backfill step to configure like Garmin/Coros/Strava.
6. Optional - Create an automation to run every `x` days to run the shortcut to keep your data updated. Alternatively, create an automation to run the shortcut whenever you complete a workout.

Get the workouts

<img src="images/apple-shortcut.png" alt="Get the workouts" width="50%">

Setting up the data (make sure all fields are `Text`)

<img src="images/apple-shortcut1.png" alt="Setting up the data" width="50%">

## Data exposed

- **Daily summary sensors** (Garmin only — Coros/Strava/Apple Health have no
  equivalent): steps, resting heart rate, active calories, floors climbed,
  average stress, body battery, VO2 max, HRV (last night average, 7-day
  average, status), and Training Readiness (score, level, feedback).
- **Coros health & fitness sensors** (only if you connected the optional
  second MCP connection — see [Coros setup](#coros-setup)): steps, active
  calories, sleep score/duration, HRV (last night/7-day average), recovery
  (percent, level, estimated full recovery time), training load
  (short-term/long-term), threshold pace.
- **Per-activity-type sensors**, source-prefixed, e.g.
  `sensor.garmin_running_distance_km`, `sensor.coros_running_distance_km`,
  `sensor.strava_cycling_duration_minutes`,
  `sensor.apple_health_running_distance_km`: a lifetime-cumulative running
  total (like an odometer, not "today's total") for distance/duration/calories
  per activity type. Charting day/week/month/year totals from this is what
  the examples below show — the cumulative value itself isn't meant to be
  read directly.
- **Last activity**: name, type, duration of your most recent workout.
- **History import status** (`..._history_import_status`, Garmin/Coros/Strava
  only): shows backfill progress (`idle` / `running` / `backing_off` /
  `complete` / `error`) and how far back it's reached — useful to watch
  during a large first-time import. See
  [History backfill](#history-backfill). Apple Health doesn't have this
  sensor — its history arrives directly via the Shortcut rather than a
  paced background job, so there's no progress to report.

## Charting your data

All charting below uses Home Assistant's built-in **Statistics Graph** and
**Statistics** cards — no custom card or YAML template required.

### Monthly running distance (bar chart)

1. Edit a dashboard → **+ Add Card** → search **"Statistics Graph"**.
2. Set:
   - **Entities**: `sensor.garmin_running_distance_km`
   - **Period**: Month
   - **Stat type**: Change
   - **Graph type**: Bar
3. Save.

"Change" is what turns the cumulative sensor into a per-period bar chart —
it's the difference between consecutive period boundaries, not the raw
running total. This also works with `_duration_minutes` or `_calories`, and
with `Period: Day` or `Period: Week` for finer granularity.

### Running distance, year to date

1. Edit a dashboard → **+ Add Card** → search **"Statistics"** (the
   single-value stat card, not "Statistics Graph").
2. Set:
   - **Entity**: `sensor.garmin_running_distance_km`
   - **Period**: Year
   - **Stat type**: Change
3. Save. This shows how far you've run since Jan 1 of the current year,
   updating live as new activities come in.

### Comparing two sources, or two activity types

Add multiple entities to one Statistics Graph card — e.g.
`sensor.garmin_running_distance_km` and `sensor.apple_health_running_distance_km`
(if you use both), or `sensor.garmin_running_distance_km` and
`sensor.garmin_cycling_distance_km` — to overlay them on the same chart.

### Comparing this year to last year

Home Assistant's Statistics Graph card doesn't natively overlay two
different year ranges on one chart. The simplest working approach: add two
Statistics Graph cards to the same dashboard view, one showing `Period:
Month` for the current year and one for the previous year (use the card's
date-range picker to pin each to its respective year), stacked so you can
compare them side by side.

## History backfill

Applies to Garmin, Coros, and Strava only. Apple Health's existing history
arrives a different way — via the Shortcut's first run, which returns your
full on-device workout history in one batch (see
[Apple Health setup](#apple-health-setup)) — rather than this paced,
gap-aware background job.

On first setup (and whenever you increase the configured depth via
**Configure**), the integration fetches your past activity history and
imports it into Home Assistant's long-term statistics — this is what makes
the charts above useful immediately instead of starting from an empty graph.

- Depth options range from 90 days to "all available history." Longer
  ranges mean more API requests to your source, paced conservatively (see
  below), so a multi-year backfill can take several minutes.
- For "all available history," the backfill stops automatically once it
  finds several consecutive empty chunks — it doesn't walk all the way back
  to the 1970s for an account with only a few months of real data. The
  earliest day it actually found is shown on the `..._history_start_date`
  sensor.
- Progress is visible on the `..._history_import_status` sensor.
- It's gap-aware and self-healing: if Home Assistant restarts mid-backfill,
  the next run detects any incomplete range and re-fetches it rather than
  leaving a permanent hole in your history.
- Increasing the depth later only fetches the newly-uncovered older days —
  it doesn't redo the whole import.

## Rate limits / why things might be slow

Garmin's and Coros's APIs are both unofficial and undocumented, so this
integration paces backfill requests conservatively (20s between request
batches for both, plus a cooldown after Garmin login) to avoid triggering
rate limiting — which, if hit, can lock out _all_ API access for that
account for an extended period, not just the backfill. Strava's documented
limits are more generous so its pacing is lighter. If you see the history
import sensor show `backing_off`, this is expected behavior after a rate
limit, not an error — it will retry automatically.

## Troubleshooting

**Strava: "Strava rejected the request with a 403... status Inactive"** — the
Strava account that owns your API application doesn't have an active paid
Strava subscription. Subscribe on that account, reactivate the app at
strava.com/settings/api, then retry setup. See [Strava
setup](#strava-setup) above.

**Garmin: repeated "429 Too Many Requests" / integration stuck retrying** —
Garmin has rate-limited the account, usually from many rapid sign-in
attempts (e.g. repeatedly restarting Home Assistant in a short window).
Home Assistant retries automatically with increasing backoff; avoid
restarting Home Assistant repeatedly while this is happening, as each
restart re-triggers a fresh sign-in and can extend the lockout. It
typically clears within 30–60 minutes of being left alone.

**Coros: "Coros session expired or was invalidated" repeatedly** — most
often means you (or another device/app) logged into the Coros app or
Training Hub website, which silently ends this integration's session —
Coros only allows one active login at a time (see
[Coros setup](#coros-setup)). The integration will automatically log back in
on its next poll; this doesn't affect the separate optional MCP connection
for health/fitness data, if you have one connected.

**Coros: setup fails no matter which region I pick** — double check you're
actually entering your Coros account's email/password correctly first (a
wrong password can also manifest as a region-shaped failure). If you're
confident the credentials are right, try all three region options in turn —
this integration cannot detect your account's real region automatically
(see [Coros setup](#coros-setup)).

**A Statistics Graph card shows a big spike or drop on one day** — this
generally means the underlying statistics history has a gap or was
imported before a fix to this integration. For Garmin/Coros/Strava,
increasing then re-saving the backfill depth in **Configure** triggers a
fresh, self-healing import (see [History backfill](#history-backfill)). For
Apple Health, which doesn't have a backfill setting to re-trigger, re-run
the Shortcut to resend the affected workouts instead.

**Apple Health: the Shortcut runs but no data shows up** — first confirm the
webhook URL is actually reachable from your phone: open it directly in
Safari on the phone (not just in a browser on the same computer running HA).
A working webhook returns a plain-text `OK` or `Ignored (duplicate)` — Safari
may show this as a zero-byte "download," which is expected and means the
request succeeded, not a failure. If the URL doesn't load at all, check
**Settings → System → Network → Internal URL** in Home Assistant is set to
an address your phone can actually reach (e.g. your HA instance's LAN IP,
not `localhost`), and regenerate the Shortcut's URL from **Configure** on the
Apple Health integration entry afterwards.

**Apple Health: workouts appear twice, or a huge batch of history lands at
once** — the first time you run a "Get Workouts"-based Shortcut, Toolbox Pro
returns your _entire_ on-device workout history, not just new workouts, so
expect a large batch to post all at once on the first run. Re-running the
same Shortcut later is safe: each workout carries a stable `id`, and this
integration ignores anything it's already seen since the last Home Assistant
restart.

## Development

This repo includes a `docker-compose.dev.yml` for running a local Home
Assistant instance with the integration bind-mounted, so code changes are
picked up on restart without reinstalling anything:

```bash
docker compose -f docker-compose.dev.yml up -d
# HA available at http://localhost:8123
```

```bash
pip install -r requirements-dev.txt
ruff check custom_components/ha_workouts
```

### Architecture

- `models.py` — source-agnostic data model (`Activity`, `DailySummary`,
  `FitnessAssessment`, `BodyComposition`)
- `sources/base.py` — `WorkoutSource` interface each provider implements
- `sources/garmin.py` — Garmin Connect implementation (unofficial API)
- `sources/coros.py` — Coros implementation, against the same unofficial
  Training Hub API `training.coros.com` itself uses (activities, splits,
  and a rough HRV figure); optionally augmented by `sources/coros_mcp.py`
- `sources/coros_mcp.py` — client for Coros's official OAuth2 + MCP program
  ("COROS MCP"): Dynamic Client Registration, a fully server-to-server
  login flow (no browser/external step despite being real OAuth2 — see
  that module's docstring), token refresh, and JSON-RPC tool calls. Used
  only for the handful of health/fitness metrics Training Hub doesn't
  expose at all (steps, sleep, HRV assessment, recovery, fitness
  assessment, training load) — an entirely separate, optional login from
  `sources/coros.py`'s, with no session conflict between the two
- `sources/coros_mcp_parsers.py` — parses Coros MCP's free-form prose tool
  responses (not JSON — see `coros_mcp.py`'s docstring for why) into
  `DailySummary`/`FitnessAssessment` fields; each parser returns `None` for
  anything it can't find rather than raising, so an unrecognized wording
  degrades a sensor to "unknown" instead of crashing the daily poll
- `sources/strava.py` — Strava implementation (OAuth2 via Application Credentials)
- `sources/apple_health.py` — Apple Health implementation; a push receiver
  rather than a poller, parses webhook payloads from an iOS Shortcut into
  `Activity` objects and queues them for the coordinator
- `application_credentials.py` — declares Strava's OAuth2 endpoints
- `coordinator.py` — polling `DataUpdateCoordinator`, fetches today's data
  (or, for Apple Health, drains whatever's arrived via webhook since the
  last poll)
- `statistics_import.py` — gap-aware historical backfill into HA's long-term
  statistics tables for Garmin/Coros/Strava, plus `async_apply_activity_deltas`,
  which folds newly-seen activities into the same statistics tables keyed by
  each activity's own real date — used for every live update, and the only
  path Apple Health ever writes through, since it has no separate backfill
- `config_flow.py` — source picker, Garmin form, Coros form (plus its
  optional MCP-connect sub-step), Strava OAuth2 flow, Apple Health webhook
  generation, backfill depth selection for Garmin/Coros/Strava (initial +
  reconfigurable via Options, which is also where Coros MCP can be
  connected after the fact if skipped during setup)
- `__init__.py` — registers the Apple Health webhook endpoint and its
  request handler, alongside general entry setup/teardown
- `sensor.py` — daily summary, per-activity-type, HRV, Coros MCP, and
  backfill status entities

Adding a new source means implementing `WorkoutSource` in `sources/` and
wiring it into `config_flow.py` and `__init__.py`'s `_build_source`.
