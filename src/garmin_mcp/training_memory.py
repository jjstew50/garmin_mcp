"""
Training Memory module for Garmin MCP Server.

Enabled via GARMIN_TRAINING_MEMORY=true environment variable.

Provides persistent memory for training plans, phases, qualitative notes,
and planned-vs-actual workout tracking. Designed to work alongside the
tracker module (raw Garmin data sync) but can be used independently.

When Claude discusses a training plan with the user, it should save that
context here so future conversations pick up where the last left off
without requiring the user to re-explain their situation.

Tools:
  Training phases:   set_training_phase, get_training_phase, end_training_phase
  Training plans:    create_training_plan, get_training_plan, get_active_training_plan
  Planned workouts:  add_planned_workout, update_planned_workout, get_planned_workouts
  Auto-matching:     match_workouts_to_activities
  Notes:             save_training_note, get_training_notes
  Summary:           get_training_summary
  Config:            get_user_config, set_user_config
"""

import json
import os
from datetime import datetime, timezone, date as Date, timedelta

import aiosqlite

garmin_client = None
_db_initialized = False

TRAINING_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS training_phases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key TEXT NOT NULL,
    name TEXT NOT NULL,
    goal TEXT,
    start_date TEXT NOT NULL,
    end_date TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_phases_user_active
    ON training_phases(user_key, is_active);

CREATE TABLE IF NOT EXISTS training_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key TEXT NOT NULL,
    phase_id INTEGER,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    title TEXT,
    overview TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plans_user_date
    ON training_plans(user_key, start_date);

CREATE TABLE IF NOT EXISTS planned_workouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key TEXT NOT NULL,
    plan_id INTEGER,
    planned_date TEXT NOT NULL,
    activity_type TEXT,
    title TEXT NOT NULL,
    description TEXT,
    target_duration_minutes INTEGER,
    target_intensity TEXT,
    status TEXT DEFAULT 'planned',
    actual_activity_id INTEGER,
    match_confidence REAL,
    feedback TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_planned_user_date
    ON planned_workouts(user_key, planned_date);
CREATE INDEX IF NOT EXISTS idx_planned_user_status
    ON planned_workouts(user_key, status);

CREATE TABLE IF NOT EXISTS training_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key TEXT NOT NULL,
    date TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT,
    content TEXT NOT NULL,
    planned_workout_id INTEGER,
    activity_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_user_date
    ON training_notes(user_key, date);
CREATE INDEX IF NOT EXISTS idx_notes_user_category
    ON training_notes(user_key, category);

CREATE TABLE IF NOT EXISTS user_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key TEXT NOT NULL,
    config_key TEXT NOT NULL,
    config_value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_key, config_key)
);
"""

# Activity type compatibility map for fuzzy matching
_COMPATIBLE_TYPES: dict[str, set[str]] = {
    "running": {"running", "trail_running", "treadmill_running", "road_running", "track_running", "virtual_run"},
    "cycling": {"cycling", "road_biking", "mountain_biking", "indoor_cycling", "virtual_ride"},
    "strength_training": {"strength_training", "fitness_equipment", "weight_training", "functional_strength_training"},
    "swimming": {"swimming", "open_water_swimming", "lap_swimming"},
    "walking": {"walking", "hiking"},
    "cardio": {"cardio", "elliptical", "rowing", "stair_climbing"},
}

# Build reverse map: specific type → canonical group
_TYPE_TO_GROUP: dict[str, str] = {}
for _group, _types in _COMPATIBLE_TYPES.items():
    for _t in _types:
        _TYPE_TO_GROUP[_t] = _group


def _db_path() -> str:
    base = os.environ.get("GARMIN_DATA_PATH", "/app/data")
    return os.path.join(base, "garmin_tracker.db")


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return Date.today().isoformat()


def _get_user_key(client) -> str:
    key = getattr(client, "username", None) or getattr(client, "display_name", None)
    if not key:
        raise RuntimeError("Cannot determine user identity")
    return key


def _auto_match_threshold() -> float:
    """Global auto-match threshold from env (per-user config can override)."""
    try:
        return float(os.environ.get("GARMIN_AUTO_MATCH_THRESHOLD", "0.75"))
    except ValueError:
        return 0.75


def _auto_match_enabled() -> bool:
    return os.environ.get("GARMIN_AUTO_MATCH_ENABLED", "true").lower() in ("true", "1", "yes")


def _type_match_confidence(planned_type: str | None, actual_type: str | None) -> float:
    """Return 0.0–1.0 confidence that planned and actual activity types match."""
    if not planned_type or not actual_type:
        return 0.5  # unknown type, moderate confidence
    if planned_type.lower() == actual_type.lower():
        return 1.0
    pg = _TYPE_TO_GROUP.get(planned_type.lower())
    ag = _TYPE_TO_GROUP.get(actual_type.lower())
    if pg and ag and pg == ag:
        return 0.85
    return 0.2


async def _ensure_db():
    global _db_initialized
    if _db_initialized:
        return
    path = _db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript(TRAINING_SCHEMA_DDL)
        await db.commit()
    _db_initialized = True


def configure(client):
    global garmin_client
    garmin_client = client


def register_tools(app):

    # ------------------------------------------------------------------
    # Training phases
    # ------------------------------------------------------------------

    @app.tool()
    async def set_training_phase(name: str, goal: str = None, start_date: str = None) -> str:
        """Start a new training phase (e.g. 'High Rep Block', 'Marathon Prep', 'Deload Week').

        Automatically ends any currently active phase. Call this when the user
        signals a shift in training focus or goals.

        Args:
            name: Short name for the phase, e.g. 'High Rep Block'.
            goal: What the user is trying to achieve this phase. Optional.
            start_date: Start date YYYY-MM-DD. Defaults to today.
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)
        date = start_date or _today()
        now = _now_utc()

        async with aiosqlite.connect(_db_path()) as db:
            # End any active phase
            await db.execute(
                "UPDATE training_phases SET is_active=0, end_date=? WHERE user_key=? AND is_active=1",
                (date, user_key),
            )
            await db.execute(
                "INSERT INTO training_phases (user_key, name, goal, start_date, is_active, created_at) VALUES (?,?,?,?,1,?)",
                (user_key, name, goal, date, now),
            )
            await db.commit()

        return json.dumps({"status": "ok", "phase": name, "start_date": date, "goal": goal}, indent=2)

    @app.tool()
    async def get_training_phase() -> str:
        """Get the current active training phase and recent phase history.

        Returns what training block the user is currently in, the goal,
        and how many days in. Also shows the last 2 completed phases for context.
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)

        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row

            async with db.execute(
                "SELECT * FROM training_phases WHERE user_key=? AND is_active=1 ORDER BY start_date DESC LIMIT 1",
                (user_key,),
            ) as cur:
                active_row = await cur.fetchone()

            async with db.execute(
                "SELECT * FROM training_phases WHERE user_key=? AND is_active=0 ORDER BY start_date DESC LIMIT 2",
                (user_key,),
            ) as cur:
                past = [dict(r) for r in await cur.fetchall()]

        if not active_row:
            return json.dumps({"active_phase": None, "recent_phases": past, "message": "No active training phase. Use set_training_phase to start one."})

        active = dict(active_row)
        start = Date.fromisoformat(active["start_date"])
        active["days_in"] = (Date.today() - start).days

        return json.dumps({"active_phase": active, "recent_phases": past}, indent=2)

    @app.tool()
    async def end_training_phase(end_date: str = None) -> str:
        """Mark the current training phase as complete.

        Args:
            end_date: End date YYYY-MM-DD. Defaults to today.
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)
        date = end_date or _today()

        async with aiosqlite.connect(_db_path()) as db:
            async with db.execute(
                "SELECT name FROM training_phases WHERE user_key=? AND is_active=1", (user_key,)
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return json.dumps({"status": "no_active_phase", "message": "No active training phase to end."})
            await db.execute(
                "UPDATE training_phases SET is_active=0, end_date=? WHERE user_key=? AND is_active=1",
                (date, user_key),
            )
            await db.commit()

        return json.dumps({"status": "ok", "phase_ended": row[0], "end_date": date}, indent=2)

    # ------------------------------------------------------------------
    # Training plans
    # ------------------------------------------------------------------

    @app.tool()
    async def create_training_plan(
        start_date: str,
        end_date: str,
        title: str = None,
        overview: str = None,
    ) -> str:
        """Create a training plan for a date range (typically a week).

        After creating the plan, add individual sessions with add_planned_workout.
        The overview should describe the overall intent: e.g. '3x strength, 2x cardio,
        focus on posterior chain'.

        Args:
            start_date: Plan start date YYYY-MM-DD (usually Monday).
            end_date: Plan end date YYYY-MM-DD (usually Sunday).
            title: Optional plan name, e.g. 'Week 3 - High Rep Block'.
            overview: Free-text description of the week's goals and structure.
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)
        now = _now_utc()

        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row

            # Link to active phase if one exists
            async with db.execute(
                "SELECT id FROM training_phases WHERE user_key=? AND is_active=1 LIMIT 1", (user_key,)
            ) as cur:
                phase_row = await cur.fetchone()
            phase_id = phase_row["id"] if phase_row else None

            await db.execute(
                "INSERT INTO training_plans (user_key, phase_id, start_date, end_date, title, overview, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (user_key, phase_id, start_date, end_date, title, overview, now, now),
            )
            plan_id = (await (await db.execute("SELECT last_insert_rowid()")).fetchone())[0]
            await db.commit()

        return json.dumps(
            {"status": "ok", "plan_id": plan_id, "start_date": start_date, "end_date": end_date, "title": title},
            indent=2,
        )

    @app.tool()
    async def get_active_training_plan() -> str:
        """Get the current or most upcoming training plan and all its planned workouts.

        Returns the plan covering today, or the next upcoming plan if today
        falls outside any plan. Use this at the start of a conversation to
        load the current week's context.
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)
        today = _today()

        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row

            # Plan covering today
            async with db.execute(
                "SELECT * FROM training_plans WHERE user_key=? AND start_date<=? AND end_date>=? ORDER BY start_date DESC LIMIT 1",
                (user_key, today, today),
            ) as cur:
                plan_row = await cur.fetchone()

            # If no current plan, get the next upcoming one
            if not plan_row:
                async with db.execute(
                    "SELECT * FROM training_plans WHERE user_key=? AND start_date>? ORDER BY start_date ASC LIMIT 1",
                    (user_key, today),
                ) as cur:
                    plan_row = await cur.fetchone()

            if not plan_row:
                return json.dumps({"plan": None, "message": "No active or upcoming training plan. Use create_training_plan to set one up."})

            plan = dict(plan_row)
            plan_id = plan["id"]

            async with db.execute(
                "SELECT * FROM planned_workouts WHERE user_key=? AND plan_id=? ORDER BY planned_date ASC",
                (user_key, plan_id),
            ) as cur:
                workouts = [dict(r) for r in await cur.fetchall()]

        return json.dumps({"plan": plan, "workouts": workouts}, indent=2)

    @app.tool()
    async def get_training_plan(start_date: str, end_date: str) -> str:
        """Get all training plans and their planned workouts for a date range.

        Args:
            start_date: Start date YYYY-MM-DD.
            end_date: End date YYYY-MM-DD.
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)

        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row

            async with db.execute(
                """SELECT * FROM training_plans WHERE user_key=?
                   AND start_date<=? AND end_date>=?
                   ORDER BY start_date ASC""",
                (user_key, end_date, start_date),
            ) as cur:
                plans = [dict(r) for r in await cur.fetchall()]

            result = []
            for plan in plans:
                async with db.execute(
                    "SELECT * FROM planned_workouts WHERE user_key=? AND plan_id=? ORDER BY planned_date ASC",
                    (user_key, plan["id"]),
                ) as cur:
                    plan["workouts"] = [dict(r) for r in await cur.fetchall()]
                result.append(plan)

        return json.dumps({"plans": result, "date_range": {"start": start_date, "end": end_date}}, indent=2)

    # ------------------------------------------------------------------
    # Planned workouts
    # ------------------------------------------------------------------

    @app.tool()
    async def add_planned_workout(
        planned_date: str,
        title: str,
        activity_type: str = None,
        description: str = None,
        target_duration_minutes: int = None,
        target_intensity: str = None,
        plan_id: int = None,
    ) -> str:
        """Add a planned workout session for a specific date.

        Use this when the user agrees to a workout (e.g. 'Let's do 5x5 squats on Thursday').
        Once added, the system will auto-match it to actual Garmin activities after the date passes.

        Args:
            planned_date: Date the workout is planned for, YYYY-MM-DD.
            title: Short name, e.g. 'Lower Body - 5x5', 'Easy 5K Run'.
            activity_type: Type matching Garmin types: 'strength_training', 'running', 'cycling', etc.
            description: Details: sets, reps, distances, notes. E.g. '5x5 squat @ 80%, 3x8 RDL'.
            target_duration_minutes: Expected duration in minutes.
            target_intensity: 'easy', 'moderate', 'hard', or 'max'.
            plan_id: Link to a training plan ID if applicable.
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)
        now = _now_utc()

        async with aiosqlite.connect(_db_path()) as db:
            await db.execute(
                """INSERT INTO planned_workouts
                   (user_key, plan_id, planned_date, activity_type, title, description,
                    target_duration_minutes, target_intensity, status, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,'planned',?,?)""",
                (user_key, plan_id, planned_date, activity_type, title, description,
                 target_duration_minutes, target_intensity, now, now),
            )
            workout_id = (await (await db.execute("SELECT last_insert_rowid()")).fetchone())[0]
            await db.commit()

        return json.dumps(
            {"status": "ok", "workout_id": workout_id, "planned_date": planned_date, "title": title},
            indent=2,
        )

    @app.tool()
    async def update_planned_workout(
        workout_id: int,
        status: str,
        feedback: str = None,
        actual_activity_id: int = None,
    ) -> str:
        """Update a planned workout with what actually happened.

        Call this when the user reports on a workout — whether they did it, skipped it,
        or modified it. Store their qualitative feedback here.

        Args:
            workout_id: ID of the planned workout to update.
            status: One of 'done', 'skipped', 'modified', 'planned'.
            feedback: Qualitative notes, e.g. 'felt strong', 'struggled with squats',
                      'cut it short due to fatigue', 'added an extra set'.
            actual_activity_id: Garmin activity ID if this maps to a specific activity.
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)
        now = _now_utc()

        async with aiosqlite.connect(_db_path()) as db:
            async with db.execute(
                "SELECT title, planned_date FROM planned_workouts WHERE id=? AND user_key=?",
                (workout_id, user_key),
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return json.dumps({"status": "not_found", "message": f"No planned workout with id={workout_id} for this user."})

            await db.execute(
                """UPDATE planned_workouts SET status=?, feedback=?, actual_activity_id=?, updated_at=?
                   WHERE id=? AND user_key=?""",
                (status, feedback, actual_activity_id, now, workout_id, user_key),
            )
            await db.commit()

        return json.dumps(
            {"status": "ok", "workout_id": workout_id, "title": row[0], "planned_date": row[1],
             "new_status": status},
            indent=2,
        )

    @app.tool()
    async def get_planned_workouts(
        start_date: str = None,
        end_date: str = None,
        status: str = None,
    ) -> str:
        """Get planned workouts with optional filters.

        Use this to check what was planned for a period and compare against
        what actually happened.

        Args:
            start_date: Only return workouts on or after this date YYYY-MM-DD. Optional.
            end_date: Only return workouts on or before this date YYYY-MM-DD. Optional.
            status: Filter by status: 'planned', 'done', 'skipped', 'modified'. Optional.
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)

        sql = "SELECT * FROM planned_workouts WHERE user_key=?"
        params: list = [user_key]
        if start_date:
            sql += " AND planned_date>=?"
            params.append(start_date)
        if end_date:
            sql += " AND planned_date<=?"
            params.append(end_date)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY planned_date ASC"

        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = [dict(r) for r in await cur.fetchall()]

        return json.dumps(
            {"count": len(rows), "workouts": rows,
             "filters": {k: v for k, v in {"start_date": start_date, "end_date": end_date, "status": status}.items() if v}},
            indent=2,
        )

    # ------------------------------------------------------------------
    # Auto-matching
    # ------------------------------------------------------------------

    @app.tool()
    async def match_workouts_to_activities(days: int = 14) -> str:
        """Auto-match planned workouts to actual Garmin activities.

        For each 'planned' workout in the last N days, looks for a Garmin
        activity on the same date with a compatible type. If confidence
        exceeds the threshold (GARMIN_AUTO_MATCH_THRESHOLD env var, default 0.75),
        automatically marks the workout as 'done' and links the activity.

        Returns a list of matches made and any planned workouts that remain unmatched.

        Args:
            days: How many days back to look for planned workouts to match (default 14).
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)
        threshold = _auto_match_threshold()
        today = _today()
        since = (Date.today() - timedelta(days=days)).isoformat()
        now = _now_utc()

        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row

            # Planned workouts in range that haven't been matched yet
            async with db.execute(
                """SELECT * FROM planned_workouts
                   WHERE user_key=? AND status='planned'
                   AND planned_date>=? AND planned_date<=?
                   ORDER BY planned_date ASC""",
                (user_key, since, today),
            ) as cur:
                planned = [dict(r) for r in await cur.fetchall()]

            if not planned:
                return json.dumps({"matches_made": 0, "unmatched": [], "message": "No unmatched planned workouts in range."})

            # Load Garmin activities for those dates
            date_set = {p["planned_date"] for p in planned}
            date_list = sorted(date_set)
            async with db.execute(
                f"""SELECT activity_id, date, activity_type, activity_name, duration_seconds
                    FROM activities
                    WHERE user_key=? AND date IN ({','.join('?' * len(date_list))})""",
                [user_key] + date_list,
            ) as cur:
                activities_by_date: dict[str, list] = {}
                for r in await cur.fetchall():
                    d = dict(r)
                    activities_by_date.setdefault(d["date"], []).append(d)

            matched = []
            unmatched = []

            for pw in planned:
                date = pw["planned_date"]
                candidates = activities_by_date.get(date, [])

                best_activity = None
                best_conf = 0.0

                for act in candidates:
                    conf = _type_match_confidence(pw.get("activity_type"), act.get("activity_type"))
                    if conf > best_conf:
                        best_conf = conf
                        best_activity = act

                if best_activity and best_conf >= threshold:
                    await db.execute(
                        """UPDATE planned_workouts
                           SET status='done', actual_activity_id=?, match_confidence=?, updated_at=?
                           WHERE id=? AND user_key=?""",
                        (best_activity["activity_id"], best_conf, now, pw["id"], user_key),
                    )
                    matched.append({
                        "planned_workout_id": pw["id"],
                        "planned_title": pw["title"],
                        "planned_date": date,
                        "matched_activity_id": best_activity["activity_id"],
                        "matched_activity_name": best_activity.get("activity_name"),
                        "match_confidence": round(best_conf, 2),
                    })
                else:
                    low_conf = best_conf if best_activity else None
                    unmatched.append({
                        "planned_workout_id": pw["id"],
                        "planned_title": pw["title"],
                        "planned_date": date,
                        "best_match_confidence": round(low_conf, 2) if low_conf is not None else None,
                        "candidates_on_date": len(candidates),
                    })

            await db.commit()

        return json.dumps(
            {
                "threshold_used": threshold,
                "matches_made": len(matched),
                "still_unmatched": len(unmatched),
                "matched": matched,
                "unmatched": unmatched,
            },
            indent=2,
        )

    # ------------------------------------------------------------------
    # Training notes
    # ------------------------------------------------------------------

    @app.tool()
    async def save_training_note(
        content: str,
        category: str = "general",
        date: str = None,
        title: str = None,
        planned_workout_id: int = None,
        activity_id: int = None,
    ) -> str:
        """Save a qualitative note from the conversation to long-term memory.

        Use this proactively whenever the user shares something meaningful about
        their training — feelings, soreness, struggles, breakthroughs, lifestyle
        context, or any insight that should inform future recommendations.

        Categories: 'feeling', 'feedback', 'goal', 'soreness', 'nutrition',
                    'lifestyle', 'observation', 'general'

        Args:
            content: The note content. Be specific — include numbers, body parts,
                     exercise names where relevant.
            category: Category tag for retrieval. Default 'general'.
            date: Date this applies to YYYY-MM-DD. Defaults to today.
            title: Optional short title for the note.
            planned_workout_id: Link to a planned workout if relevant.
            activity_id: Link to a Garmin activity ID if relevant.
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)
        note_date = date or _today()
        now = _now_utc()

        async with aiosqlite.connect(_db_path()) as db:
            await db.execute(
                """INSERT INTO training_notes
                   (user_key, date, category, title, content, planned_workout_id, activity_id, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (user_key, note_date, category, title, content, planned_workout_id, activity_id, now),
            )
            note_id = (await (await db.execute("SELECT last_insert_rowid()")).fetchone())[0]
            await db.commit()

        return json.dumps({"status": "ok", "note_id": note_id, "date": note_date, "category": category}, indent=2)

    @app.tool()
    async def get_training_notes(
        start_date: str = None,
        end_date: str = None,
        category: str = None,
        limit: int = 50,
    ) -> str:
        """Retrieve saved training notes.

        Use at the start of conversations to recall context: what the user
        has been experiencing, what goals were set, recent feedback.

        Args:
            start_date: Only notes on or after this date YYYY-MM-DD. Optional.
            end_date: Only notes on or before this date YYYY-MM-DD. Optional.
            category: Filter by category. Optional.
            limit: Max notes to return (default 50).
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)

        sql = "SELECT * FROM training_notes WHERE user_key=?"
        params: list = [user_key]
        if start_date:
            sql += " AND date>=?"
            params.append(start_date)
        if end_date:
            sql += " AND date<=?"
            params.append(end_date)
        if category:
            sql += " AND category=?"
            params.append(category)
        sql += " ORDER BY date DESC, created_at DESC LIMIT ?"
        params.append(limit)

        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = [dict(r) for r in await cur.fetchall()]

        return json.dumps({"count": len(rows), "notes": rows}, indent=2)

    # ------------------------------------------------------------------
    # Holistic training summary
    # ------------------------------------------------------------------

    @app.tool()
    async def get_training_summary(start_date: str, end_date: str) -> str:
        """Get a comprehensive training summary combining all tracked data.

        Pulls together: current training phase, planned vs actual workouts,
        completion rate, training notes, and raw activity stats from the tracker DB.
        Use this to answer 'how am I doing?' or to prepare for a planning conversation.

        Requires activities to be synced via sync_activities first for the
        planned-vs-actual comparison to be meaningful.

        Args:
            start_date: Start date YYYY-MM-DD.
            end_date: End date YYYY-MM-DD.
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)

        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row

            # Active phase
            async with db.execute(
                "SELECT name, goal, start_date FROM training_phases WHERE user_key=? AND is_active=1 LIMIT 1",
                (user_key,),
            ) as cur:
                phase_row = await cur.fetchone()

            # Planned workout breakdown
            async with db.execute(
                """SELECT status, COUNT(*) as count FROM planned_workouts
                   WHERE user_key=? AND planned_date>=? AND planned_date<=?
                   GROUP BY status""",
                (user_key, start_date, end_date),
            ) as cur:
                status_counts: dict[str, int] = {r["status"]: r["count"] for r in await cur.fetchall()}

            total_planned = sum(status_counts.values())
            done_count = status_counts.get("done", 0) + status_counts.get("modified", 0)
            completion_rate = round(done_count / total_planned * 100) if total_planned else None

            # Recent notes
            async with db.execute(
                """SELECT date, category, title, content FROM training_notes
                   WHERE user_key=? AND date>=? AND date<=?
                   ORDER BY date DESC LIMIT 10""",
                (user_key, start_date, end_date),
            ) as cur:
                notes = [dict(r) for r in await cur.fetchall()]

            # Activity stats (from tracker)
            async with db.execute(
                """SELECT COUNT(*) as total, activity_type,
                          ROUND(SUM(distance_meters)/1000.0, 1) as km,
                          ROUND(SUM(duration_seconds)/3600.0, 1) as hours
                   FROM activities WHERE user_key=? AND date>=? AND date<=?
                   GROUP BY activity_type ORDER BY total DESC""",
                (user_key, start_date, end_date),
            ) as cur:
                activities_by_type = [dict(r) for r in await cur.fetchall()]

            # Health snapshot averages
            async with db.execute(
                """SELECT ROUND(AVG(total_steps),0) as avg_steps,
                          ROUND(AVG(resting_hr_bpm),0) as avg_rhr,
                          ROUND(AVG(sleep_seconds)/3600.0, 1) as avg_sleep_hours,
                          ROUND(AVG(avg_stress_level),0) as avg_stress,
                          ROUND(AVG(sleep_score),0) as avg_sleep_score,
                          ROUND(AVG(body_battery_highest),0) as avg_bb_high
                   FROM health_metrics WHERE user_key=? AND date>=? AND date<=?""",
                (user_key, start_date, end_date),
            ) as cur:
                health_row = await cur.fetchone()

        health = {k: v for k, v in dict(health_row).items() if v is not None} if health_row else {}

        result = {
            "date_range": {"start": start_date, "end": end_date},
            "training_phase": dict(phase_row) if phase_row else None,
            "workout_compliance": {
                "total_planned": total_planned,
                "completed": done_count,
                "skipped": status_counts.get("skipped", 0),
                "unaccounted": status_counts.get("planned", 0),
                "completion_rate_pct": completion_rate,
            },
            "activities_logged": activities_by_type,
            "health_averages": health,
            "recent_notes": notes,
        }

        return json.dumps(result, indent=2)

    # ------------------------------------------------------------------
    # Per-user config
    # ------------------------------------------------------------------

    @app.tool()
    async def set_user_config(key: str, value: str) -> str:
        """Set a per-user configuration value in the tracker database.

        Useful for storing user preferences that Claude should remember,
        like preferred workout days, rest day rules, or auto-match behavior.

        Common keys:
          auto_match_threshold — float 0.0-1.0, default from GARMIN_AUTO_MATCH_THRESHOLD env
          preferred_rest_days  — e.g. 'Sunday,Wednesday'
          weekly_workout_target — integer, e.g. '4'
          notes               — any free-text preference

        Args:
            key: Config key name.
            value: Config value (always stored as string).
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)
        now = _now_utc()

        async with aiosqlite.connect(_db_path()) as db:
            await db.execute(
                """INSERT INTO user_config (user_key, config_key, config_value, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(user_key, config_key) DO UPDATE SET config_value=excluded.config_value, updated_at=excluded.updated_at""",
                (user_key, key, value, now),
            )
            await db.commit()

        return json.dumps({"status": "ok", "key": key, "value": value}, indent=2)

    @app.tool()
    async def get_user_config() -> str:
        """Get all per-user configuration values stored in the tracker database.

        Returns the user's stored preferences and settings. Also shows
        the current environment-level defaults for comparison.
        """
        await _ensure_db()
        user_key = _get_user_key(garmin_client)

        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT config_key, config_value, updated_at FROM user_config WHERE user_key=? ORDER BY config_key",
                (user_key,),
            ) as cur:
                rows = {r["config_key"]: {"value": r["config_value"], "updated_at": r["updated_at"]} for r in await cur.fetchall()}

        return json.dumps(
            {
                "user_key": user_key,
                "stored_config": rows,
                "env_defaults": {
                    "GARMIN_AUTO_MATCH_THRESHOLD": os.environ.get("GARMIN_AUTO_MATCH_THRESHOLD", "0.75"),
                    "GARMIN_AUTO_MATCH_ENABLED": os.environ.get("GARMIN_AUTO_MATCH_ENABLED", "true"),
                },
            },
            indent=2,
        )

    return app
