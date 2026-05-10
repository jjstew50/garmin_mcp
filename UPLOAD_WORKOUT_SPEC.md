# Implementation Spec: `upload_running_workout()` & `upload_cycling_workout()`

## Background: What the Tridot Workouts Reveal

After pulling ~100 Tridot workouts from your Garmin account and inspecting 6 in detail (3 running, 3 cycling), the following patterns are clear and must be replicated exactly.

---

## 1. What Tridot Actually Sends to Garmin

### Flat Step Structure (No Repeat Groups)
Tridot never uses `RepeatGroupDTO`. Every step is written out individually as a flat list of `ExecutableStepDTO` objects with sequential `stepOrder`. For example, "4 x 6-min threshold repeats with 2-min recoveries" becomes 8 explicit steps — it does not use a repeat group.

### Step Type: Always `"other"`
All Tridot steps use:
```json
"stepType": {"stepTypeId": 7, "stepTypeKey": "other"}
```
> **⚠️ Verify `stepTypeId` for `"other"`:** The existing `workouts.py` curation strips the raw IDs. Before implementing, make one raw API call to verify the `stepTypeId` for the `"other"` key. Add this debug helper temporarily:
> ```python
> workout = garmin_client.get_workout_by_id(1382310261)
> print(workout['workoutSegments'][0]['workoutSteps'][0]['stepType'])
> ```

### End Conditions: Always Time-Based
Every step uses:
```json
"endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
"endConditionValue": 600.0   // seconds, always a float
```
Tridot never uses `distance` or `lap.button` end conditions.

### Target Types and IDs

This is the most critical part. The curated output strips `workoutTargetTypeId`, so you must verify these by inspecting raw API output. The three target types observed in your workouts are:

| Sport | Target Key | Likely ID | Value Fields |
|-------|-----------|-----------|--------------|
| Running | `speed.zone` | **verify** (likely `6`) | `targetValueOne` = min m/s, `targetValueTwo` = max m/s |
| Running & Cycling | `heart.rate.zone` | `4` (confirmed in templates) | `targetValueOne` = min BPM, `targetValueTwo` = max BPM |
| Cycling | `power.zone` | **verify** (likely `8`) | `targetValueOne` = min watts, `targetValueTwo` = max watts |

> **⚠️ Critical: Verify `workoutTargetTypeId` values before implementing:**
> ```python
> # Use a known running workout with speed target (Threshold Repeats)
> workout = garmin_client.get_workout_by_id(1382310261)
> step = workout['workoutSegments'][0]['workoutSteps'][0]
> print(step['targetType'])  # will show workoutTargetTypeId and workoutTargetTypeKey
>
> # Use a known cycling workout with power target (Threshold Intervals)
> workout = garmin_client.get_workout_by_id(1381357838)
> step = workout['workoutSegments'][0]['workoutSteps'][5]  # a high-power step
> print(step['targetType'])
> ```

> **⚠️ Important: `heart.rate.zone` uses absolute BPMs, NOT zone numbers**
> Your Tridot workouts use absolute BPM ranges (e.g. `targetValueOne: 108.0, targetValueTwo: 141.0`), NOT the `zoneNumber` field. The existing `_fix_hr_zone_step()` in `workouts.py` will incorrectly try to convert these if the values are 1-5, but since Tridot BPM values are always >5, this is not a problem. Still, be aware of this distinction.

### Sport Type IDs
```json
// Running
"sportType": {"sportTypeId": 1, "sportTypeKey": "running"}

// Cycling
"sportType": {"sportTypeId": 2, "sportTypeKey": "cycling"}
```

---

## 2. Observed Workout Structures (Real Examples)

### Running: Easy Run (HR target)
**ID:** 1381494369 — 20 min total, 2 flat steps both targeting HR 108–141 BPM

```
Step 1: 600s @ HR 108–141 BPM
Step 2: 600s @ HR 108–141 BPM
```

### Running: Threshold Repeats (speed target)
**ID:** 1382310261 — 60 min total, 9 flat steps alternating between easy pace (2.51–2.86 m/s) and threshold pace (3.21–3.50 m/s)

```
Step 1: 600s @ 2.51–2.86 m/s  (warmup/easy)
Step 2: 360s @ 3.21–3.50 m/s  (threshold)
Step 3: 120s @ 2.51–2.86 m/s  (recovery)
Step 4: 360s @ 3.21–3.50 m/s  (threshold)
Step 5: 120s @ 2.51–2.86 m/s  (recovery)
Step 6: 360s @ 3.21–3.50 m/s  (threshold)
Step 7: 120s @ 2.51–2.86 m/s  (recovery)
Step 8: 360s @ 3.21–3.50 m/s  (threshold)
Step 9: 1200s @ 2.51–2.86 m/s (cooldown)
```

### Running: Fartleks (speed target)
**ID:** 1373978410 — 55 min, 17 steps alternating easy/fast in 3-min/1-min bursts

### Cycling: Easy Ride (HR target)
**ID:** 1370199097 — 60 min total, 14 steps, all targeting HR 101–122 BPM. Many short spin-up steps (30s each) with descriptions like "Spinup!", "Easy Spin", "Cadence @ 90 rpm"

### Cycling: Threshold Intervals (power target)
**ID:** 1381357838 — 60 min, 15 steps alternating easy power (174–214W) and threshold power (259–299W), including warmup spin-ups and long threshold blocks

### Cycling: Step Ups (power target)
**ID:** 1378100727 — 90 min, 30 steps with progressively harder power blocks (217–257W, 259–299W, 302–342W) and recoveries

---

## 3. Helper Conversion Functions to Implement

These go in `workouts.py` (or a new `workout_helpers.py`):

```python
def pace_to_mps(minutes: float, seconds: float = 0) -> float:
    """Convert pace (min/mile) to speed in m/s.

    Args:
        minutes: Whole minutes per mile (e.g. 8 for 8:30/mile)
        seconds: Additional seconds (e.g. 30 for 8:30/mile)

    Returns:
        Speed in meters per second

    Example:
        pace_to_mps(8, 30)  -> 3.175  (8:30/mile -> ~3.18 m/s)
        pace_to_mps(9, 0)   -> 2.987  (9:00/mile)
    """
    total_seconds_per_mile = minutes * 60 + seconds
    meters_per_mile = 1609.344
    return meters_per_mile / total_seconds_per_mile


def pace_km_to_mps(minutes: float, seconds: float = 0) -> float:
    """Convert pace (min/km) to speed in m/s.

    Args:
        minutes: Whole minutes per km
        seconds: Additional seconds

    Returns:
        Speed in meters per second
    """
    total_seconds_per_km = minutes * 60 + seconds
    return 1000.0 / total_seconds_per_km


def ftp_percent_to_watts(ftp: int, low_pct: float, high_pct: float) -> tuple[int, int]:
    """Convert FTP percentages to absolute watts.

    Args:
        ftp: Functional Threshold Power in watts
        low_pct: Lower bound as percentage (e.g. 0.76 for 76%)
        high_pct: Upper bound as percentage (e.g. 0.90 for 90%)

    Returns:
        Tuple of (low_watts, high_watts)

    Example:
        ftp_percent_to_watts(280, 0.76, 0.90) -> (213, 252)
    """
    return (round(ftp * low_pct), round(ftp * high_pct))


def _make_step(
    step_order: int,
    duration_seconds: float,
    target_type_id: int,
    target_type_key: str,
    target_low: float,
    target_high: float,
    description: str = None,
    step_type_id: int = 7,      # "other" — verify this value
    step_type_key: str = "other"
) -> dict:
    """Build a single ExecutableStepDTO for a Garmin workout.

    This is the internal step builder. Both upload_running_workout() and
    upload_cycling_workout() use this to build each step.
    """
    step = {
        "type": "ExecutableStepDTO",
        "stepOrder": step_order,
        "stepType": {"stepTypeId": step_type_id, "stepTypeKey": step_type_key},
        "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
        "endConditionValue": float(duration_seconds),
        "targetType": {
            "workoutTargetTypeId": target_type_id,
            "workoutTargetTypeKey": target_type_key
        },
        "targetValueOne": float(target_low),
        "targetValueTwo": float(target_high)
    }
    if description:
        step["description"] = description
    return step
```

---

## 4. The Two Functions to Implement

These go inside `register_tools(app)` in `workouts.py`, after the existing `upload_workout` tool.

### 4a. `upload_running_workout()`

```python
@app.tool()
async def upload_running_workout(
    name: str,
    steps: list,
    description: str = None,
    estimated_duration_seconds: int = None,
) -> str:
    """Upload a structured running workout to Garmin Connect.

    This is a higher-level helper than upload_workout(), designed for
    Tridot-style flat step workouts using absolute pace or HR targets.

    Each step in `steps` is a dict with these fields:
      - duration_seconds (int, required): Length of step in seconds
      - target (dict, required): One of:
          {"type": "speed", "min_mps": float, "max_mps": float}
          {"type": "heart_rate", "min_bpm": float, "max_bpm": float}
          {"type": "none"}
      - description (str, optional): Label shown on watch (e.g. "Spinup!", "Threshold block")

    For pace-based targets, pre-convert using pace_to_mps():
      2.51 m/s ≈ 10:41/mile (easy)
      3.21 m/s ≈ 8:21/mile (threshold)
      3.50 m/s ≈ 7:40/mile (threshold upper)

    Example:
        steps = [
            {"duration_seconds": 600, "target": {"type": "heart_rate", "min_bpm": 108, "max_bpm": 141}},
            {"duration_seconds": 360, "target": {"type": "speed", "min_mps": 3.21, "max_mps": 3.50},
             "description": "Threshold"},
            {"duration_seconds": 120, "target": {"type": "heart_rate", "min_bpm": 108, "max_bpm": 141},
             "description": "Recovery"},
            {"duration_seconds": 1200, "target": {"type": "heart_rate", "min_bpm": 108, "max_bpm": 141}},
        ]

    Args:
        name: Workout name (e.g. "2025-11-12 Threshold Repeats")
        steps: List of step dicts (see above)
        description: Optional workout description text
        estimated_duration_seconds: Optional total duration hint for Garmin calendar display
    """
    try:
        # TARGET TYPE IDs — verify these before shipping (see spec)
        SPEED_TARGET_TYPE_ID = 6      # workoutTargetTypeId for "speed.zone" — VERIFY
        HR_TARGET_TYPE_ID = 4         # workoutTargetTypeId for "heart.rate.zone" — confirmed
        NO_TARGET_TYPE_ID = 1         # workoutTargetTypeId for "no.target" — confirmed
        STEP_TYPE_ID_OTHER = 7        # stepTypeId for "other" — VERIFY

        sport_type = {"sportTypeId": 1, "sportTypeKey": "running"}

        workout_steps = []
        for i, step in enumerate(steps):
            target = step.get("target", {"type": "none"})
            target_type = target.get("type", "none")

            if target_type == "speed":
                target_id = SPEED_TARGET_TYPE_ID
                target_key = "speed.zone"
                target_low = target["min_mps"]
                target_high = target["max_mps"]
            elif target_type == "heart_rate":
                target_id = HR_TARGET_TYPE_ID
                target_key = "heart.rate.zone"
                target_low = target["min_bpm"]
                target_high = target["max_bpm"]
            else:
                target_id = NO_TARGET_TYPE_ID
                target_key = "no.target"
                target_low = 0.0
                target_high = 0.0

            workout_steps.append(_make_step(
                step_order=i + 1,
                duration_seconds=step["duration_seconds"],
                target_type_id=target_id,
                target_type_key=target_key,
                target_low=target_low,
                target_high=target_high,
                description=step.get("description"),
                step_type_id=STEP_TYPE_ID_OTHER,
                step_type_key="other"
            ))

        workout_data = {
            "workoutName": name,
            "sportType": sport_type,
            "workoutSegments": [{
                "segmentOrder": 1,
                "sportType": sport_type,
                "workoutSteps": workout_steps
            }]
        }

        if description:
            workout_data["description"] = description

        if estimated_duration_seconds:
            workout_data["estimatedDuration"] = estimated_duration_seconds
        else:
            # Auto-calculate from steps
            workout_data["estimatedDuration"] = sum(
                s["duration_seconds"] for s in steps
            )

        # Reuse existing upload logic
        result = garmin_client.upload_workout(workout_data)

        if isinstance(result, dict):
            return json.dumps({
                "status": "success",
                "workout_id": result.get("workoutId"),
                "name": result.get("workoutName"),
                "steps_uploaded": len(steps),
                "message": "Running workout uploaded successfully"
            }, indent=2)

        return json.dumps(result, indent=2)

    except Exception as e:
        return f"Error uploading running workout: {str(e)}"
```

---

### 4b. `upload_cycling_workout()`

```python
@app.tool()
async def upload_cycling_workout(
    name: str,
    steps: list,
    description: str = None,
    estimated_duration_seconds: int = None,
) -> str:
    """Upload a structured cycling workout to Garmin Connect.

    This is a higher-level helper than upload_workout(), designed for
    Tridot-style flat step workouts using absolute power or HR targets.

    Each step in `steps` is a dict with these fields:
      - duration_seconds (int, required): Length of step in seconds
      - target (dict, required): One of:
          {"type": "power", "min_watts": float, "max_watts": float}
          {"type": "heart_rate", "min_bpm": float, "max_bpm": float}
          {"type": "none"}
      - description (str, optional): Label shown on watch (e.g. "Spinup!", "Cadence @ 80 rpm")

    Common power zone examples (from your Tridot workouts, FTP ~280W):
      Easy/recovery:  174–214W  (~62–76% FTP)
      Tempo/sweet spot: 217–257W (~77–92% FTP)
      Threshold:      259–299W  (~92–107% FTP)
      Above threshold: 302–342W (~108–122% FTP)

    Example:
        steps = [
            {"duration_seconds": 240, "target": {"type": "heart_rate", "min_bpm": 101, "max_bpm": 122}},
            {"duration_seconds": 30, "target": {"type": "heart_rate", "min_bpm": 101, "max_bpm": 122},
             "description": "Spinup!"},
            {"duration_seconds": 960, "target": {"type": "power", "min_watts": 259, "max_watts": 299},
             "description": "Cadence @ 80 rpm"},
            {"duration_seconds": 120, "target": {"type": "power", "min_watts": 174, "max_watts": 214}},
            {"duration_seconds": 960, "target": {"type": "power", "min_watts": 259, "max_watts": 299}},
            {"duration_seconds": 960, "target": {"type": "power", "min_watts": 174, "max_watts": 214}},
        ]

    Args:
        name: Workout name (e.g. "2025-11-10 Threshold Intervals")
        steps: List of step dicts (see above)
        description: Optional workout description text
        estimated_duration_seconds: Optional total duration hint for Garmin calendar display
    """
    try:
        # TARGET TYPE IDs — verify these before shipping (see spec)
        POWER_TARGET_TYPE_ID = 8      # workoutTargetTypeId for "power.zone" — VERIFY
        HR_TARGET_TYPE_ID = 4         # workoutTargetTypeId for "heart.rate.zone" — confirmed
        NO_TARGET_TYPE_ID = 1         # workoutTargetTypeId for "no.target" — confirmed
        STEP_TYPE_ID_OTHER = 7        # stepTypeId for "other" — VERIFY

        sport_type = {"sportTypeId": 2, "sportTypeKey": "cycling"}

        workout_steps = []
        for i, step in enumerate(steps):
            target = step.get("target", {"type": "none"})
            target_type = target.get("type", "none")

            if target_type == "power":
                target_id = POWER_TARGET_TYPE_ID
                target_key = "power.zone"
                target_low = target["min_watts"]
                target_high = target["max_watts"]
            elif target_type == "heart_rate":
                target_id = HR_TARGET_TYPE_ID
                target_key = "heart.rate.zone"
                target_low = target["min_bpm"]
                target_high = target["max_bpm"]
            else:
                target_id = NO_TARGET_TYPE_ID
                target_key = "no.target"
                target_low = 0.0
                target_high = 0.0

            workout_steps.append(_make_step(
                step_order=i + 1,
                duration_seconds=step["duration_seconds"],
                target_type_id=target_id,
                target_type_key=target_key,
                target_low=target_low,
                target_high=target_high,
                description=step.get("description"),
                step_type_id=STEP_TYPE_ID_OTHER,
                step_type_key="other"
            ))

        workout_data = {
            "workoutName": name,
            "sportType": sport_type,
            "workoutSegments": [{
                "segmentOrder": 1,
                "sportType": sport_type,
                "workoutSteps": workout_steps
            }]
        }

        if description:
            workout_data["description"] = description

        if estimated_duration_seconds:
            workout_data["estimatedDuration"] = estimated_duration_seconds
        else:
            workout_data["estimatedDuration"] = sum(
                s["duration_seconds"] for s in steps
            )

        result = garmin_client.upload_workout(workout_data)

        if isinstance(result, dict):
            return json.dumps({
                "status": "success",
                "workout_id": result.get("workoutId"),
                "name": result.get("workoutName"),
                "steps_uploaded": len(steps),
                "message": "Cycling workout uploaded successfully"
            }, indent=2)

        return json.dumps(result, indent=2)

    except Exception as e:
        return f"Error uploading cycling workout: {str(e)}"
```

---

## 5. Where Things Go in the Codebase

### `src/garmin_mcp/workouts.py`
Add in this order:
1. The `_make_step()` helper function at module level (alongside `_fix_hr_zone_step`, etc.)
2. `upload_running_workout()` inside `register_tools(app)`
3. `upload_cycling_workout()` inside `register_tools(app)`

The two conversion helpers (`pace_to_mps`, `pace_km_to_mps`, `ftp_percent_to_watts`) can go at module level in `workouts.py` or in a new `src/garmin_mcp/workout_helpers.py` if you want to keep things clean and import them into `workouts.py`.

### `src/garmin_mcp/workout_templates.py`
Add two new template resources that demonstrate the new functions:
- `workout://templates/tridot-run` — example calling `upload_running_workout()`
- `workout://templates/tridot-cycling` — example calling `upload_cycling_workout()`

### No changes needed to `__init__.py`
The functions register themselves inside `workouts.register_tools(app)`, which is already called.

---

## 6. Pre-Implementation Verification Steps

Before writing a line of code, run these two quick checks to confirm the unknown IDs:

### Step 1: Verify `stepTypeId` for `"other"`
```python
workout = garmin_client.get_workout_by_id(1382310261)  # Threshold Repeats (running)
step = workout['workoutSegments'][0]['workoutSteps'][0]
print("stepType:", step['stepType'])
# Expected something like: {'stepTypeId': 7, 'stepTypeKey': 'other'}
```

### Step 2: Verify `workoutTargetTypeId` for `speed.zone` and `power.zone`
```python
# Running speed target
workout = garmin_client.get_workout_by_id(1382310261)
step = workout['workoutSegments'][0]['workoutSteps'][1]  # a threshold step
print("running target:", step['targetType'])
print("targetValueOne:", step.get('targetValueOne'))
print("targetValueTwo:", step.get('targetValueTwo'))

# Cycling power target
workout = garmin_client.get_workout_by_id(1381357838)
step = workout['workoutSegments'][0]['workoutSteps'][5]  # a power step
print("cycling target:", step['targetType'])
print("targetValueOne:", step.get('targetValueOne'))
print("targetValueTwo:", step.get('targetValueTwo'))
```

---

## 7. Validation Test

After implementing, upload a small test workout and verify it appears correctly in Garmin Connect:

```python
# Test running
test_run = await upload_running_workout(
    name="TEST Running - Delete Me",
    steps=[
        {"duration_seconds": 300, "target": {"type": "heart_rate", "min_bpm": 108, "max_bpm": 141}},
        {"duration_seconds": 360, "target": {"type": "speed", "min_mps": 3.21, "max_mps": 3.50},
         "description": "Threshold"},
        {"duration_seconds": 600, "target": {"type": "heart_rate", "min_bpm": 108, "max_bpm": 141}},
    ]
)

# Test cycling
test_ride = await upload_cycling_workout(
    name="TEST Cycling - Delete Me",
    steps=[
        {"duration_seconds": 300, "target": {"type": "heart_rate", "min_bpm": 101, "max_bpm": 122},
         "description": "Spinup!"},
        {"duration_seconds": 600, "target": {"type": "power", "min_watts": 259, "max_watts": 299},
         "description": "Cadence @ 80 rpm"},
        {"duration_seconds": 600, "target": {"type": "power", "min_watts": 174, "max_watts": 214}},
    ]
)
```

Then confirm in Garmin Connect web/app that:
- The workouts appear in your library
- The sport icons are correct (running shoe / bike)
- The step targets and durations look correct
- Delete the test workouts after confirming

---

## 8. MCP Tool Description Best Practices

The docstrings of both tools (which become the MCP tool descriptions visible to AI clients) should:
- Clearly state this is **time-based steps only** (no distance-based steps)
- List the target type options with actual example values from your Tridot workouts
- Include the pace-to-m/s equivalents so an AI can reason about them without computing
- Explain that steps are flat (no nesting/repeat groups)

---

## Summary

| Thing | Running | Cycling |
|-------|---------|---------|
| `sportTypeId` | `1` | `2` |
| `sportTypeKey` | `"running"` | `"cycling"` |
| Primary target type key | `"speed.zone"` | `"power.zone"` |
| Primary target unit | m/s (absolute range) | watts (absolute range) |
| Secondary target type key | `"heart.rate.zone"` | `"heart.rate.zone"` |
| Secondary target unit | BPM absolute range | BPM absolute range |
| Step structure | Flat `ExecutableStepDTO` list | Flat `ExecutableStepDTO` list |
| Step type key | `"other"` | `"other"` |
| End condition | time (seconds) | time (seconds) |
| `workoutTargetTypeId` (speed) | **VERIFY** (likely `6`) | — |
| `workoutTargetTypeId` (power) | — | **VERIFY** (likely `8`) |
| `workoutTargetTypeId` (HR) | `4` | `4` |
| `workoutTargetTypeId` (none) | `1` | `1` |
