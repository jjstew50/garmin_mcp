"""
Persistent tracker module for Garmin MCP Server.

Provides SQLite-backed storage for activities, health metrics, and weight data
so Claude can access historical Garmin data across conversations without
re-fetching from the API every time.

Tools:
  Sync:  sync_activities, sync_health_metrics, sync_weight
  Query: get_tracked_activities, get_activity_summary,
         get_health_history, get_weight_history, get_sync_status
"""

import json
import os
from datetime import datetime, timezone, date as Date, timedelta

import aiosqlite

garmin_client = None
_db_initialized = False

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key TEXT NOT NULL,
    data_type TEXT NOT NULL,
    last_synced_at TEXT NOT NULL,
    earliest_date TEXT,
    latest_date TEXT,
    records_synced INTEGER DEFAULT 0,
    UNIQUE(user_key, data_type)
);

CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key TEXT NOT NULL,
    activity_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    start_time TEXT,
    activity_type TEXT,
    activity_name TEXT,
    distance_meters REAL,
    duration_seconds REAL,
    moving_duration_seconds REAL,
    calories REAL,
    avg_hr_bpm REAL,
    max_hr_bpm REAL,
    steps INTEGER,
    avg_power_watts REAL,
    training_load REAL,
    training_effect TEXT,
    synced_at TEXT NOT NULL,
    UNIQUE(user_key, activity_id)
);

CREATE INDEX IF NOT EXISTS idx_activities_user_date
    ON activities(user_key, date);
CREATE INDEX IF NOT EXISTS idx_activities_user_type
    ON activities(user_key, activity_type);

CREATE TABLE IF NOT EXISTS health_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key TEXT NOT NULL,
    date TEXT NOT NULL,
    total_steps INTEGER,
    resting_hr_bpm INTEGER,
    avg_stress_level INTEGER,
    max_stress_level INTEGER,
    body_battery_highest INTEGER,
    body_battery_lowest INTEGER,
    avg_spo2_percent REAL,
    sleep_seconds INTEGER,
    deep_sleep_seconds INTEGER,
    light_sleep_seconds INTEGER,
    rem_sleep_seconds INTEGER,
    awake_seconds INTEGER,
    sleep_score INTEGER,
    avg_overnight_hrv REAL,
    total_calories INTEGER,
    active_calories INTEGER,
    distance_meters REAL,
    moderate_intensity_minutes INTEGER,
    vigorous_intensity_minutes INTEGER,
    synced_at TEXT NOT NULL,
    UNIQUE(user_key, date)
);

CREATE INDEX IF NOT EXISTS idx_health_metrics_user_date
    ON health_metrics(user_key, date);

CREATE TABLE IF NOT EXISTS weight_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key TEXT NOT NULL,
    date TEXT NOT NULL,
    timestamp_gmt TEXT,
    weight_kg REAL,
    bmi REAL,
    body_fat_percent REAL,
    body_water_percent REAL,
    muscle_mass_grams REAL,
    bone_mass_grams REAL,
    source_type TEXT,
    synced_at TEXT NOT NULL,
    UNIQUE(user_key, timestamp_gmt)
);

CREATE INDEX IF NOT EXISTS idx_weight_entries_user_date
    ON weight_entries(user_key, date);
"""

_DB_FILENAME = "garmin_tracker.db"


def _db_path() -> str:
    base = os.environ.get("GARMIN_DATA_PATH", "/app/data")
    return os.path.join(base, _DB_FILENAME)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_user_key(client) -> str:
    # In SSE mode the Bearer token (API key) is the canonical per-user key — it's
    # guaranteed unique and set by the ASGI middleware before any tool runs.
    # Fall back to display_name only in stdio/single-user mode where the ContextVar is unset.
    from garmin_mcp.context import _active_user_key
    key = _active_user_key.get(None)
    if not key:
        key = getattr(client, "username", None) or getattr(client, "display_name", None)
    if not key:
        raise RuntimeError("Cannot determine user identity: no active user key and display_name unavailable")
    return key


async def _ensure_db():
    global _db_initialized
    if _db_initialized:
        return
    path = _db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript(SCHEMA_DDL)
        await db.commit()
    _db_initialized = True


def configure(client):
    global garmin_client
    garmin_client = client


def register_tools(app):

    @app.tool()
    async def sync_activities(limit: int = 50) -> str:
        """Sync the most recent Garmin activities into the local tracker database.

        Call this to populate or refresh the local activity history. After syncing
        you can query with get_tracked_activities or get_activity_summary without
        hitting the Garmin API again.

        Args:
            limit: Number of most-recent activities to fetch and store (default 50, max 500).
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)
        limit = min(max(1, limit), 500)

        raw = garmin_client.get_activities(0, limit)
        if not raw:
            return json.dumps({"status": "no_data", "message": "No activities returned from Garmin"})

        synced_at = _now_utc()
        upserted = 0

        async with aiosqlite.connect(_db_path()) as db:
            for a in raw:
                start_time = a.get("startTimeLocal", "")
                date = start_time[:10] if start_time else None

                activity_type_obj = a.get("activityType") or {}
                activity_type = activity_type_obj.get("typeKey") if isinstance(activity_type_obj, dict) else None

                await db.execute(
                    """
                    INSERT INTO activities
                        (user_key, activity_id, date, start_time, activity_type, activity_name,
                         distance_meters, duration_seconds, moving_duration_seconds, calories,
                         avg_hr_bpm, max_hr_bpm, steps, avg_power_watts, training_load,
                         training_effect, synced_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(user_key, activity_id) DO UPDATE SET
                        date=excluded.date,
                        start_time=excluded.start_time,
                        activity_type=excluded.activity_type,
                        activity_name=excluded.activity_name,
                        distance_meters=excluded.distance_meters,
                        duration_seconds=excluded.duration_seconds,
                        moving_duration_seconds=excluded.moving_duration_seconds,
                        calories=excluded.calories,
                        avg_hr_bpm=excluded.avg_hr_bpm,
                        max_hr_bpm=excluded.max_hr_bpm,
                        steps=excluded.steps,
                        avg_power_watts=excluded.avg_power_watts,
                        training_load=excluded.training_load,
                        training_effect=excluded.training_effect,
                        synced_at=excluded.synced_at
                    """,
                    (
                        user_key,
                        a.get("activityId"),
                        date,
                        start_time,
                        activity_type,
                        a.get("activityName"),
                        a.get("distance"),
                        a.get("duration"),
                        a.get("movingDuration"),
                        a.get("calories"),
                        a.get("averageHR"),
                        a.get("maxHR"),
                        a.get("steps"),
                        a.get("avgPower"),
                        a.get("activityTrainingLoad"),
                        a.get("trainingEffectLabel"),
                        synced_at,
                    ),
                )
                upserted += 1

            dates = [a.get("startTimeLocal", "")[:10] for a in raw if a.get("startTimeLocal")]
            earliest = min(dates) if dates else None
            latest = max(dates) if dates else None

            await db.execute(
                """
                INSERT INTO sync_log (user_key, data_type, last_synced_at, earliest_date, latest_date, records_synced)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(user_key, data_type) DO UPDATE SET
                    last_synced_at=excluded.last_synced_at,
                    earliest_date=MIN(sync_log.earliest_date, excluded.earliest_date),
                    latest_date=MAX(sync_log.latest_date, excluded.latest_date),
                    records_synced=excluded.records_synced
                """,
                (user_key, "activities", synced_at, earliest, latest, upserted),
            )
            await db.commit()

        return json.dumps(
            {
                "status": "ok",
                "user": user_key,
                "synced": upserted,
                "date_range": {"earliest": earliest, "latest": latest},
            },
            indent=2,
        )

    @app.tool()
    async def sync_health_metrics(start_date: str, end_date: str) -> str:
        """Sync daily health metrics for a date range into the local tracker database.

        Fetches steps, sleep, resting HR, stress, body battery, SpO2, and calories
        for each day and stores them locally. After syncing, query with get_health_history.

        Limited to 90 days per call to avoid excessive Garmin API usage.

        Args:
            start_date: Start date in YYYY-MM-DD format.
            end_date: End date in YYYY-MM-DD format (inclusive).
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)
        synced_at = _now_utc()

        start = Date.fromisoformat(start_date)
        end = Date.fromisoformat(end_date)

        if (end - start).days > 90:
            return json.dumps(
                {
                    "status": "error",
                    "message": "Date range exceeds 90 days. Split into smaller ranges to avoid API rate limits.",
                }
            )

        all_dates = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]

        # Bulk fetches where the API supports date ranges
        steps_by_date: dict = {}
        try:
            steps_data = garmin_client.get_daily_steps(start_date, end_date)
            for s in steps_data or []:
                steps_by_date[s.get("calendarDate")] = s
        except Exception:
            pass

        bb_by_date: dict = {}
        try:
            bb_data = garmin_client.get_body_battery(start_date, end_date)
            for bb in bb_data or []:
                bb_by_date[bb.get("date")] = bb
        except Exception:
            pass

        upserted = 0
        errors = []

        async with aiosqlite.connect(_db_path()) as db:
            for date_str in all_dates:
                sleep_seconds = deep_sleep = light_sleep = rem_sleep = awake_seconds = None
                sleep_score = avg_overnight_hrv = avg_spo2 = None
                try:
                    sd = garmin_client.get_sleep_data(date_str)
                    daily = (sd or {}).get("dailySleepDTO", {})
                    sleep_seconds = daily.get("sleepTimeSeconds")
                    deep_sleep = daily.get("deepSleepSeconds")
                    light_sleep = daily.get("lightSleepSeconds")
                    rem_sleep = daily.get("remSleepSeconds")
                    awake_seconds = daily.get("awakeSleepSeconds")
                    scores = daily.get("sleepScores") or {}
                    sleep_score = scores.get("overall", {}).get("value") if isinstance(scores, dict) else None
                    avg_overnight_hrv = sd.get("avgOvernightHrv")
                    spo2_summary = (sd or {}).get("wellnessSpO2SleepSummaryDTO") or {}
                    avg_spo2 = spo2_summary.get("averageSpo2")
                except Exception as e:
                    errors.append(f"sleep {date_str}: {e}")

                rhr = total_calories = active_calories = distance_m = None
                avg_stress = max_stress = mod_intensity = vig_intensity = None
                bb_highest = bb_lowest = None
                try:
                    stats = garmin_client.get_stats(date_str)
                    rhr = stats.get("restingHeartRate")
                    total_calories = stats.get("totalKilocalories")
                    active_calories = stats.get("activeKilocalories")
                    distance_m = stats.get("totalDistanceMeters")
                    avg_stress = stats.get("averageStressLevel")
                    max_stress = stats.get("maxStressLevel")
                    mod_intensity = stats.get("moderateIntensityMinutes")
                    vig_intensity = stats.get("vigorousIntensityMinutes")
                    bb_highest = stats.get("bodyBatteryHighestValue")
                    bb_lowest = stats.get("bodyBatteryLowestValue")
                except Exception as e:
                    errors.append(f"stats {date_str}: {e}")

                steps_row = steps_by_date.get(date_str) or {}
                total_steps = steps_row.get("totalSteps")

                # Fall back to body battery from bulk fetch if stats didn't have it
                if bb_highest is None:
                    bb_row = bb_by_date.get(date_str) or {}
                    bb_highest = bb_row.get("charged")
                    bb_lowest = bb_row.get("drained")

                await db.execute(
                    """
                    INSERT INTO health_metrics
                        (user_key, date, total_steps, resting_hr_bpm, avg_stress_level,
                         max_stress_level, body_battery_highest, body_battery_lowest,
                         avg_spo2_percent, sleep_seconds, deep_sleep_seconds,
                         light_sleep_seconds, rem_sleep_seconds, awake_seconds,
                         sleep_score, avg_overnight_hrv, total_calories, active_calories,
                         distance_meters, moderate_intensity_minutes,
                         vigorous_intensity_minutes, synced_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(user_key, date) DO UPDATE SET
                        total_steps=excluded.total_steps,
                        resting_hr_bpm=excluded.resting_hr_bpm,
                        avg_stress_level=excluded.avg_stress_level,
                        max_stress_level=excluded.max_stress_level,
                        body_battery_highest=excluded.body_battery_highest,
                        body_battery_lowest=excluded.body_battery_lowest,
                        avg_spo2_percent=excluded.avg_spo2_percent,
                        sleep_seconds=excluded.sleep_seconds,
                        deep_sleep_seconds=excluded.deep_sleep_seconds,
                        light_sleep_seconds=excluded.light_sleep_seconds,
                        rem_sleep_seconds=excluded.rem_sleep_seconds,
                        awake_seconds=excluded.awake_seconds,
                        sleep_score=excluded.sleep_score,
                        avg_overnight_hrv=excluded.avg_overnight_hrv,
                        total_calories=excluded.total_calories,
                        active_calories=excluded.active_calories,
                        distance_meters=excluded.distance_meters,
                        moderate_intensity_minutes=excluded.moderate_intensity_minutes,
                        vigorous_intensity_minutes=excluded.vigorous_intensity_minutes,
                        synced_at=excluded.synced_at
                    """,
                    (
                        user_key, date_str, total_steps, rhr, avg_stress, max_stress,
                        bb_highest, bb_lowest, avg_spo2, sleep_seconds, deep_sleep,
                        light_sleep, rem_sleep, awake_seconds, sleep_score,
                        avg_overnight_hrv, total_calories, active_calories,
                        distance_m, mod_intensity, vig_intensity, synced_at,
                    ),
                )
                upserted += 1

            await db.execute(
                """
                INSERT INTO sync_log (user_key, data_type, last_synced_at, earliest_date, latest_date, records_synced)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(user_key, data_type) DO UPDATE SET
                    last_synced_at=excluded.last_synced_at,
                    earliest_date=MIN(sync_log.earliest_date, excluded.earliest_date),
                    latest_date=MAX(sync_log.latest_date, excluded.latest_date),
                    records_synced=excluded.records_synced
                """,
                (user_key, "health_metrics", synced_at, start_date, end_date, upserted),
            )
            await db.commit()

        result: dict = {
            "status": "ok",
            "user": user_key,
            "days_synced": upserted,
            "date_range": {"start": start_date, "end": end_date},
        }
        if errors:
            result["warnings"] = errors[:10]
        return json.dumps(result, indent=2)

    @app.tool()
    async def sync_weight(start_date: str, end_date: str) -> str:
        """Sync weight and body composition entries for a date range into the local tracker database.

        After syncing, query with get_weight_history.

        Args:
            start_date: Start date in YYYY-MM-DD format.
            end_date: End date in YYYY-MM-DD format (inclusive).
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)
        synced_at = _now_utc()

        data = garmin_client.get_weigh_ins(start_date, end_date)
        daily_summaries = (data or {}).get("dailyWeightSummaries", [])

        upserted = 0
        async with aiosqlite.connect(_db_path()) as db:
            for day in daily_summaries:
                for w in day.get("allWeightMetrics", []):
                    weight_g = w.get("weight")
                    weight_kg = round(weight_g / 1000, 3) if weight_g else None
                    ts_gmt = w.get("timestampGMT") or w.get("calendarDate")

                    await db.execute(
                        """
                        INSERT INTO weight_entries
                            (user_key, date, timestamp_gmt, weight_kg, bmi,
                             body_fat_percent, body_water_percent, muscle_mass_grams,
                             bone_mass_grams, source_type, synced_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(user_key, timestamp_gmt) DO UPDATE SET
                            weight_kg=excluded.weight_kg,
                            bmi=excluded.bmi,
                            body_fat_percent=excluded.body_fat_percent,
                            body_water_percent=excluded.body_water_percent,
                            muscle_mass_grams=excluded.muscle_mass_grams,
                            bone_mass_grams=excluded.bone_mass_grams,
                            source_type=excluded.source_type,
                            synced_at=excluded.synced_at
                        """,
                        (
                            user_key,
                            w.get("calendarDate"),
                            ts_gmt,
                            weight_kg,
                            w.get("bmi"),
                            w.get("bodyFat"),
                            w.get("bodyWater"),
                            w.get("muscleMass"),
                            w.get("boneMass"),
                            w.get("sourceType"),
                            synced_at,
                        ),
                    )
                    upserted += 1

            await db.execute(
                """
                INSERT INTO sync_log (user_key, data_type, last_synced_at, earliest_date, latest_date, records_synced)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(user_key, data_type) DO UPDATE SET
                    last_synced_at=excluded.last_synced_at,
                    earliest_date=MIN(sync_log.earliest_date, excluded.earliest_date),
                    latest_date=MAX(sync_log.latest_date, excluded.latest_date),
                    records_synced=excluded.records_synced
                """,
                (user_key, "weight", synced_at, start_date, end_date, upserted),
            )
            await db.commit()

        return json.dumps(
            {
                "status": "ok",
                "user": user_key,
                "entries_synced": upserted,
                "date_range": {"start": start_date, "end": end_date},
            },
            indent=2,
        )

    @app.tool()
    async def get_tracked_activities(
        start_date: str = None,
        end_date: str = None,
        activity_type: str = None,
        limit: int = 50,
    ) -> str:
        """Query locally stored activities from the tracker database.

        Returns activities that have been previously synced via sync_activities.
        All filters are optional — omit to get the most recent activities.

        Args:
            start_date: Only return activities on or after this date (YYYY-MM-DD). Optional.
            end_date: Only return activities on or before this date (YYYY-MM-DD). Optional.
            activity_type: Filter by activity type, e.g. 'running', 'cycling', 'strength_training'. Optional.
            limit: Maximum number of results to return (default 50).
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)

        sql = "SELECT * FROM activities WHERE user_key = ?"
        params: list = [user_key]

        if start_date:
            sql += " AND date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND date <= ?"
            params.append(end_date)
        if activity_type:
            sql += " AND activity_type = ?"
            params.append(activity_type)

        sql += " ORDER BY date DESC, start_time DESC LIMIT ?"
        params.append(limit)

        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()

        activities = []
        for r in rows:
            a = dict(r)
            a.pop("id", None)
            a.pop("user_key", None)
            a.pop("synced_at", None)
            a = {k: v for k, v in a.items() if v is not None}
            activities.append(a)

        return json.dumps(
            {
                "count": len(activities),
                "filters": {
                    k: v
                    for k, v in {
                        "start_date": start_date,
                        "end_date": end_date,
                        "activity_type": activity_type,
                    }.items()
                    if v is not None
                },
                "activities": activities,
            },
            indent=2,
        )

    @app.tool()
    async def get_activity_summary(start_date: str, end_date: str) -> str:
        """Get aggregate activity stats from the local tracker database for a date range.

        Returns overall totals and a per-activity-type breakdown including count,
        total distance, total hours, average HR, and total calories.

        Requires activities to be synced first via sync_activities.

        Args:
            start_date: Start date in YYYY-MM-DD format.
            end_date: End date in YYYY-MM-DD format (inclusive).
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)

        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row

            async with db.execute(
                """
                SELECT
                    COUNT(*) as total_activities,
                    ROUND(SUM(distance_meters) / 1000.0, 2) as total_distance_km,
                    ROUND(SUM(duration_seconds) / 3600.0, 2) as total_hours,
                    ROUND(AVG(avg_hr_bpm), 1) as avg_hr_bpm,
                    CAST(SUM(calories) AS INTEGER) as total_calories,
                    CAST(SUM(steps) AS INTEGER) as total_steps
                FROM activities
                WHERE user_key = ? AND date >= ? AND date <= ?
                """,
                (user_key, start_date, end_date),
            ) as cursor:
                overall = dict(await cursor.fetchone())

            async with db.execute(
                """
                SELECT
                    activity_type,
                    COUNT(*) as count,
                    ROUND(SUM(distance_meters) / 1000.0, 2) as total_distance_km,
                    ROUND(SUM(duration_seconds) / 3600.0, 2) as total_hours,
                    ROUND(AVG(avg_hr_bpm), 1) as avg_hr_bpm,
                    CAST(SUM(calories) AS INTEGER) as total_calories
                FROM activities
                WHERE user_key = ? AND date >= ? AND date <= ?
                GROUP BY activity_type
                ORDER BY count DESC
                """,
                (user_key, start_date, end_date),
            ) as cursor:
                by_type = [dict(r) for r in await cursor.fetchall()]

        overall = {k: v for k, v in overall.items() if v is not None}
        by_type = [{k: v for k, v in row.items() if v is not None} for row in by_type]

        return json.dumps(
            {
                "date_range": {"start": start_date, "end": end_date},
                "overall": overall,
                "by_activity_type": by_type,
            },
            indent=2,
        )

    @app.tool()
    async def get_health_history(start_date: str, end_date: str) -> str:
        """Query locally stored daily health metrics from the tracker database.

        Returns per-day data including steps, sleep, resting HR, stress, body battery,
        SpO2, and calories, plus averages across the entire date range.

        Requires data to be synced first via sync_health_metrics.

        Args:
            start_date: Start date in YYYY-MM-DD format.
            end_date: End date in YYYY-MM-DD format (inclusive).
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)

        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM health_metrics
                WHERE user_key = ? AND date >= ? AND date <= ?
                ORDER BY date DESC
                """,
                (user_key, start_date, end_date),
            ) as cursor:
                rows = await cursor.fetchall()

        metrics = []
        for r in rows:
            m = dict(r)
            m.pop("id", None)
            m.pop("user_key", None)
            m.pop("synced_at", None)
            if m.get("sleep_seconds"):
                m["sleep_hours"] = round(m["sleep_seconds"] / 3600, 2)
            m = {k: v for k, v in m.items() if v is not None}
            metrics.append(m)

        averages: dict = {}
        if metrics:
            def _safe_avg(field: str):
                vals = [m[field] for m in metrics if m.get(field) is not None]
                return round(sum(vals) / len(vals), 1) if vals else None

            sleep_total = sum((m.get("sleep_seconds") or 0) for m in metrics)
            averages = {
                "avg_steps": _safe_avg("total_steps"),
                "avg_sleep_hours": round(sleep_total / len(metrics) / 3600, 2),
                "avg_resting_hr": _safe_avg("resting_hr_bpm"),
                "avg_stress": _safe_avg("avg_stress_level"),
                "avg_sleep_score": _safe_avg("sleep_score"),
                "avg_spo2": _safe_avg("avg_spo2_percent"),
                "avg_body_battery_high": _safe_avg("body_battery_highest"),
            }
            averages = {k: v for k, v in averages.items() if v is not None}

        return json.dumps(
            {
                "date_range": {"start": start_date, "end": end_date},
                "days_with_data": len(metrics),
                "averages": averages,
                "daily_metrics": metrics,
            },
            indent=2,
        )

    @app.tool()
    async def get_weight_history(start_date: str, end_date: str) -> str:
        """Query locally stored weight and body composition entries from the tracker database.

        Returns individual weigh-in entries plus summary stats (min, max, avg, change).

        Requires data to be synced first via sync_weight.

        Args:
            start_date: Start date in YYYY-MM-DD format.
            end_date: End date in YYYY-MM-DD format (inclusive).
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)

        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT date, weight_kg, bmi, body_fat_percent, body_water_percent,
                       muscle_mass_grams, bone_mass_grams, source_type, timestamp_gmt
                FROM weight_entries
                WHERE user_key = ? AND date >= ? AND date <= ?
                ORDER BY date DESC, timestamp_gmt DESC
                """,
                (user_key, start_date, end_date),
            ) as cursor:
                rows = await cursor.fetchall()

        entries = [{k: v for k, v in dict(r).items() if v is not None} for r in rows]

        weights = [e["weight_kg"] for e in entries if e.get("weight_kg")]
        stats: dict = {}
        if weights:
            stats = {
                "min_weight_kg": min(weights),
                "max_weight_kg": max(weights),
                "avg_weight_kg": round(sum(weights) / len(weights), 2),
                "change_kg": round(weights[0] - weights[-1], 2),  # most recent minus oldest
                "entry_count": len(weights),
            }

        return json.dumps(
            {
                "date_range": {"start": start_date, "end": end_date},
                "stats": stats,
                "entries": entries,
            },
            indent=2,
        )

    @app.tool()
    async def get_sync_status() -> str:
        """Show the local tracker database sync status.

        Returns when each data type (activities, health_metrics, weight) was last synced,
        what date range is covered in the local DB, and total row counts.
        Use this to know whether data needs to be refreshed before querying.
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)

        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT data_type, last_synced_at, earliest_date, latest_date, records_synced
                FROM sync_log WHERE user_key = ?
                ORDER BY data_type
                """,
                (user_key,),
            ) as cursor:
                rows = await cursor.fetchall()

            counts: dict = {}
            for table, key in [
                ("activities", "activities"),
                ("health_metrics", "health_metrics"),
                ("weight_entries", "weight"),
            ]:
                async with db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE user_key = ?", (user_key,)
                ) as c:
                    counts[key] = (await c.fetchone())[0]

        sync_info: dict = {}
        for row in rows:
            d = dict(row)
            dtype = d.pop("data_type")
            d["total_rows_in_db"] = counts.get(dtype, 0)
            sync_info[dtype] = d

        for key in ["activities", "health_metrics", "weight"]:
            if key not in sync_info:
                sync_info[key] = {"total_rows_in_db": counts.get(key, 0), "last_synced_at": None}

        return json.dumps(
            {
                "user": user_key,
                "db_path": _db_path(),
                "sync_status": sync_info,
            },
            indent=2,
        )

    return app
