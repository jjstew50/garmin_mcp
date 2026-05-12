"""
Workout-related functions for Garmin Connect MCP Server
"""
import json
import datetime
from typing import Any, Dict, List, Optional, Union

# The garmin_client will be set by the main file
garmin_client = None


def configure(client):
    """Configure the module with the Garmin client instance"""
    global garmin_client
    garmin_client = client


def _fix_hr_zone_step(step: dict) -> None:
    """Fix a common mistake where HR zone targets use targetValueOne instead of zoneNumber.

    When targetType is heart.rate.zone and a named zone is intended, Garmin expects
    zoneNumber (1-5). If targetValueOne is set to a small integer (1-5) and zoneNumber
    is missing, this is almost certainly a zone number, not an absolute HR value.

    Custom HR bpm ranges (e.g. targetValueOne=105, targetValueTwo=143) are left
    unchanged — these are legitimate custom heart rate targets in Garmin Connect.
    """
    target_type = step.get('targetType', {})
    target_key = target_type.get('workoutTargetTypeKey', '')

    if target_key == 'heart.rate.zone' and 'zoneNumber' not in step:
        zone = step.get('targetValueOne')
        if zone is not None and 1 <= zone <= 5:
            step['zoneNumber'] = int(zone)
            step.pop('targetValueOne', None)
            step.pop('targetValueTwo', None)

    # Recurse into nested steps (RepeatGroupDTO)
    for nested in step.get('workoutSteps', []):
        _fix_hr_zone_step(nested)


def _fix_hr_zone_steps(workout_data: dict) -> None:
    """Walk all workout steps and fix HR zone target mistakes."""
    for segment in workout_data.get('workoutSegments', []):
        for step in segment.get('workoutSteps', []):
            _fix_hr_zone_step(step)


# =============================================================================
# TRIDOT-STYLE WORKOUT HELPERS
# =============================================================================

# Garmin speed zone percentages relative to lactate threshold speed.
# Zone N covers [SPEED_ZONE_FLOORS[N-1], SPEED_ZONE_FLOORS[N]) of LT speed,
# with zone 5 having no upper bound.
_SPEED_ZONE_PCT_FLOORS = [0.0, 0.77, 0.87, 0.94, 1.00]  # lower bound of zones 1-5


def _get_speed_zones_mps() -> list[dict]:
    """Derive running speed zone boundaries (m/s) from the user's lactate threshold speed.

    Fetches lactateThresholdSpeed from the user profile (stored as sec/m in Garmin's API)
    and computes zone floors using Garmin's standard LT-based percentages:
      Zone 1: < 77% LT speed   (easy/recovery)
      Zone 2: 77-87% LT speed  (aerobic base)
      Zone 3: 87-94% LT speed  (tempo)
      Zone 4: 94-100% LT speed (threshold)
      Zone 5: > 100% LT speed  (VO2max/interval)

    Returns list of 5 dicts: [{"zone": N, "min_mps": float, "max_mps": float|None}, ...]
    """
    profile = garmin_client.get_user_profile()
    lt_sec_per_m = profile.get("userData", {}).get("lactateThresholdSpeed")
    if not lt_sec_per_m or lt_sec_per_m <= 0:
        raise ValueError("Lactate threshold speed not available in user profile")
    lt_mps = 1.0 / lt_sec_per_m

    zones = []
    for i, floor_pct in enumerate(_SPEED_ZONE_PCT_FLOORS):
        min_mps = round(lt_mps * floor_pct, 4)
        max_pct = _SPEED_ZONE_PCT_FLOORS[i + 1] if i + 1 < len(_SPEED_ZONE_PCT_FLOORS) else None
        max_mps = round(lt_mps * max_pct, 4) if max_pct is not None else None
        zones.append({"zone": i + 1, "min_mps": min_mps, "max_mps": max_mps})
    return zones

def pace_to_mps(minutes: float, seconds: float = 0) -> float:
    """Convert pace (min/mile) to speed in m/s.

    Args:
        minutes: Whole minutes per mile (e.g. 8 for 8:30/mile)
        seconds: Additional seconds (e.g. 30 for 8:30/mile)

    Returns:
        Speed in meters per second

    Example:
        pace_to_mps(8, 30)  -> ~3.175  (8:30/mile)
        pace_to_mps(9, 0)   -> ~2.987  (9:00/mile)
    """
    return 1609.344 / (minutes * 60 + seconds)


def pace_km_to_mps(minutes: float, seconds: float = 0) -> float:
    """Convert pace (min/km) to speed in m/s.

    Args:
        minutes: Whole minutes per km
        seconds: Additional seconds

    Returns:
        Speed in meters per second
    """
    return 1000.0 / (minutes * 60 + seconds)


def ftp_percent_to_watts(ftp: int, low_pct: float, high_pct: float) -> tuple:
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
    target_low: float = 0.0,
    target_high: float = 0.0,
    description: str = None,
    step_type_id: int = 7,
    step_type_key: str = "other",
    zone_number: int = None,
) -> dict:
    """Build a single ExecutableStepDTO for a Garmin workout.

    Used internally by upload_running_workout() and upload_cycling_workout()
    to build each flat time-based step.

    For zone-based targets (pace_zone, heart_rate_zone), pass zone_number instead
    of target_low/target_high — Garmin uses zoneNumber (1-5) for these.
    """
    step = {
        "type": "ExecutableStepDTO",
        "stepOrder": step_order,
        "stepType": {"stepTypeId": step_type_id, "stepTypeKey": step_type_key},
        "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
        "endConditionValue": float(duration_seconds),
        "targetType": {
            "workoutTargetTypeId": target_type_id,
            "workoutTargetTypeKey": target_type_key,
        },
    }
    if zone_number is not None:
        step["zoneNumber"] = zone_number
    else:
        step["targetValueOne"] = float(target_low)
        step["targetValueTwo"] = float(target_high)
    if description:
        step["description"] = description
    return step


def _curate_workout_summary(workout: dict) -> dict:
    """Extract essential workout metadata for list views"""
    sport_type = workout.get('sportType', {})

    summary = {
        "id": workout.get('workoutId'),
        "name": workout.get('workoutName'),
        "sport": sport_type.get('sportTypeKey'),
        "provider": workout.get('workoutProvider'),
        "created_date": workout.get('createdDate'),
        "updated_date": workout.get('updatedDate'),
    }

    # Add optional fields if present
    if workout.get('description'):
        summary['description'] = workout.get('description')

    if workout.get('estimatedDuration'):
        summary['estimated_duration_seconds'] = workout.get('estimatedDuration')

    if workout.get('estimatedDistance'):
        summary['estimated_distance_meters'] = workout.get('estimatedDistance')

    # Remove None values
    return {k: v for k, v in summary.items() if v is not None}


def _curate_step_target(
    curated: dict,
    step: dict,
    target_field: str,
    value_one_field: str,
    value_two_field: str,
    zone_field: str,
    prefix: str = "",
) -> None:
    """Curate a workout target block, handling Garmin null target payloads safely."""
    target_type = step.get(target_field)
    if not isinstance(target_type, dict):
        target_type = {}
    target_key = target_type.get('workoutTargetTypeKey')

    if not target_key or target_key == 'no.target':
        return

    curated[f'{prefix}target_type'] = target_key

    if step.get(value_one_field) is not None:
        curated[f'{prefix}target_value_low'] = step.get(value_one_field)
    if step.get(value_two_field) is not None:
        curated[f'{prefix}target_value_high'] = step.get(value_two_field)
    if step.get(zone_field) is not None:
        curated[f'{prefix}target_zone'] = step.get(zone_field)


def _curate_workout_step(step: dict) -> dict:
    """Extract essential workout step information"""
    step_type = step.get('stepType') or {}
    end_condition = step.get('endCondition') or {}

    curated = {
        "order": step.get('stepOrder'),
        "type": step_type.get('stepTypeKey'),  # warmup, interval, cooldown, rest, recover
    }

    # Description
    if step.get('description'):
        curated['description'] = step.get('description')

    # End condition (duration/distance/lap press)
    if end_condition.get('conditionTypeKey'):
        curated['end_condition'] = end_condition.get('conditionTypeKey')
    if step.get('endConditionValue'):
        # Value meaning depends on condition type (seconds for time, meters for distance)
        curated['end_condition_value'] = step.get('endConditionValue')

    # Primary target (heart rate, pace, power, etc.)
    _curate_step_target(
        curated,
        step,
        target_field='targetType',
        value_one_field='targetValueOne',
        value_two_field='targetValueTwo',
        zone_field='zoneNumber',
    )

    # Swim workouts often store pace prescriptions as secondary targets.
    _curate_step_target(
        curated,
        step,
        target_field='secondaryTargetType',
        value_one_field='secondaryTargetValueOne',
        value_two_field='secondaryTargetValueTwo',
        zone_field='secondaryZoneNumber',
        prefix='secondary_',
    )

    # Strength training exercise info
    if step.get('category'):
        curated['category'] = step.get('category')
    if step.get('exerciseName'):
        curated['exercise_name'] = step.get('exerciseName')
    if step.get('weightValue') is not None:
        curated['weight_value'] = step.get('weightValue')
        weight_unit = step.get('weightUnit', {})
        if weight_unit and weight_unit.get('unitKey'):
            curated['weight_unit'] = weight_unit.get('unitKey')

    # Repeat info for repeat steps
    if step.get('type') == 'RepeatGroupDTO':
        curated['repeat_count'] = step.get('numberOfIterations')
        nested_steps = step.get('workoutSteps', [])
        if nested_steps:
            curated['steps'] = [_curate_workout_step(s) for s in nested_steps]
            curated['step_count'] = len(nested_steps)

    return {k: v for k, v in curated.items() if v is not None}


def _curate_workout_segment(segment: dict) -> dict:
    """Extract essential segment information including workout steps"""
    sport_type = segment.get('sportType', {})

    curated = {
        "order": segment.get('segmentOrder'),
        "sport": sport_type.get('sportTypeKey'),
    }

    # Estimated metrics
    if segment.get('estimatedDurationInSecs'):
        curated['estimated_duration_seconds'] = segment.get('estimatedDurationInSecs')
    if segment.get('estimatedDistanceInMeters'):
        curated['estimated_distance_meters'] = segment.get('estimatedDistanceInMeters')

    # Workout steps - the actual content of the segment
    steps = segment.get('workoutSteps', [])
    if steps:
        curated['steps'] = [_curate_workout_step(s) for s in steps]
        curated['step_count'] = len(steps)

    return {k: v for k, v in curated.items() if v is not None}


def _curate_workout_details(workout: dict) -> dict:
    """Extract detailed workout information with segments

    Handles both regular workouts (from get_workout_by_id) and training plan workouts
    (from fbt-adaptive endpoint) which use slightly different field names.
    """
    sport_type = workout.get('sportType') or {}

    details = {
        "id": workout.get('workoutId'),
        "uuid": workout.get('workoutUuid'),
        "name": workout.get('workoutName'),
        "sport": sport_type.get('sportTypeKey') if sport_type else None,
        "provider": workout.get('workoutProvider'),
        "created_date": workout.get('createdDate'),
        "updated_date": workout.get('updatedDate'),
    }

    # Optional fields
    if workout.get('description'):
        details['description'] = workout.get('description')

    # Handle both field name variants (regular vs training plan workouts)
    duration = workout.get('estimatedDuration') or workout.get('estimatedDurationInSecs')
    if duration:
        details['estimated_duration_seconds'] = duration

    distance = workout.get('estimatedDistance') or workout.get('estimatedDistanceInMeters')
    if distance:
        details['estimated_distance_meters'] = distance

    if workout.get('avgTrainingSpeed'):
        details['avg_training_speed_mps'] = workout.get('avgTrainingSpeed')

    # Training plan specific fields
    if workout.get('workoutPhrase'):
        details['workout_type'] = workout.get('workoutPhrase')

    if workout.get('trainingEffectLabel'):
        details['training_effect_label'] = workout.get('trainingEffectLabel')

    if workout.get('estimatedTrainingEffect'):
        details['estimated_training_effect'] = workout.get('estimatedTrainingEffect')

    # Curate segments with workout steps
    segments = workout.get('workoutSegments', [])
    if segments:
        details['segments'] = [_curate_workout_segment(seg) for seg in segments]
        details['segment_count'] = len(segments)

    # Remove None values
    return {k: v for k, v in details.items() if v is not None}


def _curate_scheduled_workout(scheduled: dict) -> dict:
    """Extract essential scheduled workout information from GraphQL response"""
    # GraphQL response has workout data at top level (not nested)
    # Completed is determined by presence of associatedActivityId
    is_completed = scheduled.get('associatedActivityId') is not None

    summary = {
        "date": scheduled.get('scheduleDate'),
        "workout_uuid": scheduled.get('workoutUuid'),
        "workout_id": scheduled.get('workoutId'),
        "name": scheduled.get('workoutName'),
        "sport": scheduled.get('workoutType'),
        "completed": is_completed,
    }

    # Training plan info
    if scheduled.get('tpPlanName'):
        summary['training_plan'] = scheduled.get('tpPlanName')

    # Workout type description (e.g., "AEROBIC_LOW_SHORTAGE_BASE", "ANAEROBIC_SPEED", "LONG_WORKOUT")
    # This describes the intent/type of the workout from Garmin Coach
    if scheduled.get('workoutPhrase'):
        summary['workout_type'] = scheduled.get('workoutPhrase')

    # Rest day and race day flags
    if scheduled.get('isRestDay'):
        summary['is_rest_day'] = True
    if scheduled.get('race'):
        summary['is_race_day'] = True

    # Optional fields
    if scheduled.get('estimatedDurationInSecs'):
        summary['estimated_duration_seconds'] = scheduled.get('estimatedDurationInSecs')

    if scheduled.get('estimatedDistanceInMeters'):
        summary['estimated_distance_meters'] = scheduled.get('estimatedDistanceInMeters')

    # If completed, include the activity ID
    if is_completed:
        summary['activity_id'] = scheduled.get('associatedActivityId')

    # Remove None values
    return {k: v for k, v in summary.items() if v is not None}


def register_tools(app):
    """Register all workout-related tools with the MCP server app"""

    @app.tool()
    async def get_workouts() -> str:
        """Get all workouts with curated summary list

        Returns a count and list of workout summaries with essential metadata only.
        For detailed workout information including segments, use get_workout_by_id.
        """
        try:
            workouts = garmin_client.get_workouts()
            if not workouts:
                return "No workouts found."

            # Curate the workout list
            curated = {
                "count": len(workouts),
                "workouts": [_curate_workout_summary(w) for w in workouts]
            }

            return json.dumps(curated, indent=2)
        except Exception as e:
            return f"Error retrieving workouts: {str(e)}"

    @app.tool()
    async def get_workout_by_id(workout_id: Union[int, str]) -> str:
        """Get detailed information for a specific workout

        Returns workout details including segments and step structure.

        Accepts either:
        - Numeric workout ID (from get_workouts or get_scheduled_workouts)
        - Workout UUID (from get_training_plan_workouts for Garmin Coach workouts)

        Args:
            workout_id: Workout ID (numeric) or UUID (for training plan workouts)
        """
        try:
            workout_id_str = str(workout_id)
            # Detect if this is a UUID (contains dashes) or numeric ID
            is_uuid = '-' in workout_id_str

            if is_uuid:
                # Training plan / Garmin Coach workout - use fbt-adaptive endpoint
                url = f"workout-service/fbt-adaptive/{workout_id_str}"
                workout = garmin_client.connectapi(url)
            else:
                # Regular workout - use standard endpoint
                workout = garmin_client.get_workout_by_id(int(workout_id_str))

            if not workout:
                return f"No workout found with ID {workout_id_str}."

            # Return curated details with segments
            curated = _curate_workout_details(workout)
            return json.dumps(curated, indent=2)
        except Exception as e:
            return f"Error retrieving workout: {str(e)}"

    @app.tool()
    async def download_workout(workout_id: int) -> str:
        """Download a workout as a FIT file

        Downloads the workout in FIT format. The binary data cannot be returned
        directly through the MCP interface, but this confirms the workout is available.

        Args:
            workout_id: ID of the workout to download
        """
        try:
            workout_data = garmin_client.download_workout(workout_id)
            if not workout_data:
                return f"No workout data found for workout with ID {workout_id}."

            # Return information about the download
            data_size = len(workout_data) if isinstance(workout_data, (bytes, bytearray)) else 0
            return json.dumps({
                "workout_id": workout_id,
                "format": "FIT",
                "size_bytes": data_size,
                "message": "Workout data is available in FIT format. Use Garmin Connect API to save to file."
            }, indent=2)
        except Exception as e:
            return f"Error downloading workout: {str(e)}"

    @app.tool()
    async def upload_workout(workout_data: dict) -> str:
        """Upload a workout from JSON data

        Creates a new workout in Garmin Connect from structured workout data.

        IMPORTANT: Step types must use Garmin's DTO format:
        - Use "ExecutableStepDTO" for regular steps (warmup, interval, cooldown, recovery)
        - Use "RepeatGroupDTO" for repeat/interval groups with numberOfIterations

        IMPORTANT: Heart rate targets come in two forms:
        - Named zone (e.g. Zone 2): set targetType to "heart.rate.zone" and use "zoneNumber" (1-5).
          Do NOT put the zone number in targetValueOne.
        - Custom HR range (e.g. 105-143 bpm): set targetType to "heart.rate.zone" and use
          "targetValueOne" (low bpm) / "targetValueTwo" (high bpm). Do NOT set "zoneNumber".
          This matches Garmin Connect's "Custom" heart rate target.
        For non-HR targets (pace, power, cadence), use targetValueOne/targetValueTwo directly.

        Note: a safety check converts targetValueOne 1-5 to zoneNumber when zoneNumber is missing,
        to catch the common mistake of putting a zone index in targetValueOne. Typical bpm values
        (e.g. 105, 143) are not affected.

        IMPORTANT: Sport type IDs for workouts (different from activity API!):
        - 1 = running, 2 = cycling, 5 = strength_training, 6 = cardio, 11 = walking

        **Available Templates:**
        Instead of building workout JSON from scratch, you can use these MCP resources as starting points:
        - workout://templates/simple-run - Basic warmup/run/cooldown structure
        - workout://templates/interval-running - Interval training with repeat groups
        - workout://templates/tempo-run - Tempo run with heart rate zone targets
        - workout://templates/strength-circuit - Strength training with exercises, reps, rest
        - workout://reference/structure - Complete JSON structure reference with all fields

        Access these resources using your MCP client's resource reading capability, modify the template
        as needed, and pass the resulting JSON as the workout_data parameter.

        **Strength training workouts** require these additional fields on each exercise step:
        - "category": exercise category (e.g. "BENCH_PRESS", "PULL_UP", "CURL", "SHOULDER_PRESS",
          "ROW", "SQUAT", "DEADLIFT", "TRICEPS_EXTENSION", "PLANK", "LUNGE", "CARDIO")
        - "exerciseName": specific exercise (e.g. "BARBELL_BENCH_PRESS", "PULL_UP",
          "DUMBBELL_BICEPS_CURL", "DUMBBELL_SHOULDER_PRESS", "BENT_OVER_ROW_WITH_DUMBELL",
          "BODY_WEIGHT_DIP", "BARBELL_SQUAT", "BARBELL_DEADLIFT")
        - "weightValue" (optional): weight as number (e.g. 24.0)
        - "weightUnit" (optional): {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}
        Use endCondition reps (conditionTypeId: 10) for exercises, rest (stepTypeId: 5) between sets.

        Example strength exercise step:
        {
            "type": "ExecutableStepDTO",
            "stepOrder": 1,
            "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
            "endCondition": {"conditionTypeId": 10, "conditionTypeKey": "reps"},
            "endConditionValue": 10.0,
            "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
            "category": "BENCH_PRESS",
            "exerciseName": "BARBELL_BENCH_PRESS",
            "weightValue": 60.0,
            "weightUnit": {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}
        }

        Example running workout with HR zone target:
        {
            "workoutName": "My Workout",
            "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
            "workoutSegments": [{
                "segmentOrder": 1,
                "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
                "workoutSteps": [{
                    "type": "ExecutableStepDTO",
                    "stepOrder": 1,
                    "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
                    "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                    "endConditionValue": 1200.0,
                    "targetType": {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"},
                    "zoneNumber": 3
                }]
            }]
        }

        Args:
            workout_data: Dictionary containing workout structure (name, sport type, segments, etc.)
        """
        try:
            # Fix common mistake: HR zone targets using targetValueOne instead of zoneNumber
            _fix_hr_zone_steps(workout_data)

            # Pass dict directly - library handles conversion
            result = garmin_client.upload_workout(workout_data)

            # Curate the response
            if isinstance(result, dict):
                curated = {
                    "status": "success",
                    "workout_id": result.get('workoutId'),
                    "name": result.get('workoutName'),
                    "message": "Workout uploaded successfully"
                }
                # Remove None values
                curated = {k: v for k, v in curated.items() if v is not None}
                return json.dumps(curated, indent=2)

            return json.dumps(result, indent=2)
        except Exception as e:
            return f"Error uploading workout: {str(e)}"

    @app.tool()
    async def upload_running_workout(
        name: str,
        steps: list,
        description: str = None,
        estimated_duration_seconds: int = None,
    ) -> str:
        """Upload a structured running workout to Garmin Connect.

        Higher-level helper than upload_workout(). Accepts a simplified flat step list
        with typed targets and builds the correct Garmin API payload automatically.
        Steps are always time-based (no distance-based steps).

        Each step in `steps` is a dict with these fields:
          - duration_seconds (int, required): Length of step in seconds
          - target (dict, required): One of:
              {"type": "speed", "min_mps": float, "max_mps": float}      — absolute speed range
              {"type": "speed_zone", "zone": int}                        — personal speed zone 1-5 (auto-fetches LT speed)
              {"type": "heart_rate", "min_bpm": float, "max_bpm": float} — absolute BPM range
              {"type": "heart_rate_zone", "zone": int}                   — Garmin HR zone 1-5
              {"type": "none"}
          - description (str, optional): Label shown on watch (e.g. "Threshold", "Recovery")

        HR zone reference:
          Zone 1: easy/recovery   Zone 2: aerobic   Zone 3: tempo
          Zone 4: threshold       Zone 5: max effort

        Absolute speed reference (m/s ↔ min/mile):
          2.51 m/s ≈ 10:41/mile (easy)    3.21 m/s ≈ 8:21/mile (threshold lower)
          2.68 m/s ≈ 10:00/mile           3.50 m/s ≈ 7:40/mile (threshold upper)
          2.87 m/s ≈  9:21/mile

        Use pace_to_mps(minutes, seconds) to convert, e.g. pace_to_mps(8, 30) -> 3.175 m/s

        **Templates:**
        - workout://templates/tridot-run — example Threshold Repeats workout

        Example (mixing zone and absolute targets):
            steps = [
                {"duration_seconds": 600, "target": {"type": "heart_rate_zone", "zone": 2}},
                {"duration_seconds": 360, "target": {"type": "speed", "min_mps": 3.21, "max_mps": 3.50},
                 "description": "Threshold"},
                {"duration_seconds": 120, "target": {"type": "heart_rate_zone", "zone": 1},
                 "description": "Recovery"},
            ]

        Args:
            name: Workout name (e.g. "2025-11-12 Threshold Repeats")
            steps: List of step dicts (see above)
            description: Optional workout description text
            estimated_duration_seconds: Optional total duration hint; auto-calculated from steps if omitted
        """
        try:
            SPEED_TARGET_TYPE_ID = 5    # workoutTargetTypeId for "speed.zone" (absolute m/s range)
            SPEED_TARGET_TYPE_KEY = "speed.zone"
            HR_TARGET_TYPE_ID = 4       # workoutTargetTypeId for "heart.rate.zone"
            NO_TARGET_TYPE_ID = 1       # workoutTargetTypeId for "no.target"
            STEP_TYPE_ID_OTHER = 7      # stepTypeId for "other"

            sport_type = {"sportTypeId": 1, "sportTypeKey": "running"}

            # Fetch speed zones once if any step uses speed_zone
            speed_zones_cache = None
            if any(s.get("target", {}).get("type") == "speed_zone" for s in steps):
                speed_zones_cache = {z["zone"]: z for z in _get_speed_zones_mps()}

            workout_steps = []
            for i, step in enumerate(steps):
                target = step.get("target", {"type": "none"})
                target_type = target.get("type", "none")
                zone_number = None

                if target_type == "speed":
                    target_id = SPEED_TARGET_TYPE_ID
                    target_key = SPEED_TARGET_TYPE_KEY
                    target_low = target["min_mps"]
                    target_high = target["max_mps"]
                elif target_type == "speed_zone":
                    z = speed_zones_cache[target["zone"]]
                    target_id = SPEED_TARGET_TYPE_ID
                    target_key = SPEED_TARGET_TYPE_KEY
                    target_low = z["min_mps"]
                    target_high = z["max_mps"] if z["max_mps"] is not None else z["min_mps"] * 1.15
                elif target_type == "heart_rate":
                    target_id = HR_TARGET_TYPE_ID
                    target_key = "heart.rate.zone"
                    target_low = target["min_bpm"]
                    target_high = target["max_bpm"]
                elif target_type == "heart_rate_zone":
                    target_id = HR_TARGET_TYPE_ID
                    target_key = "heart.rate.zone"
                    zone_number = target["zone"]
                    target_low = target_high = 0.0
                else:
                    target_id = NO_TARGET_TYPE_ID
                    target_key = "no.target"
                    target_low = target_high = 0.0

                workout_steps.append(_make_step(
                    step_order=i + 1,
                    duration_seconds=step["duration_seconds"],
                    target_type_id=target_id,
                    target_type_key=target_key,
                    target_low=target_low,
                    target_high=target_high,
                    description=step.get("description"),
                    step_type_id=STEP_TYPE_ID_OTHER,
                    step_type_key="other",
                    zone_number=zone_number,
                ))

            workout_data = {
                "workoutName": name,
                "sportType": sport_type,
                "workoutSegments": [{
                    "segmentOrder": 1,
                    "sportType": sport_type,
                    "workoutSteps": workout_steps,
                }],
                "estimatedDuration": estimated_duration_seconds if estimated_duration_seconds
                    else sum(s["duration_seconds"] for s in steps),
            }

            if description:
                workout_data["description"] = description

            result = garmin_client.upload_workout(workout_data)

            if isinstance(result, dict):
                return json.dumps({
                    "status": "success",
                    "workout_id": result.get("workoutId"),
                    "name": result.get("workoutName"),
                    "steps_uploaded": len(steps),
                    "message": "Running workout uploaded successfully",
                }, indent=2)

            return json.dumps(result, indent=2)
        except Exception as e:
            return f"Error uploading running workout: {str(e)}"

    @app.tool()
    async def upload_cycling_workout(
        name: str,
        steps: list,
        description: str = None,
        estimated_duration_seconds: int = None,
    ) -> str:
        """Upload a structured cycling workout to Garmin Connect.

        Higher-level helper than upload_workout(). Accepts a simplified flat step list
        with typed targets and builds the correct Garmin API payload automatically.
        Steps are always time-based (no distance-based steps).

        Each step in `steps` is a dict with these fields:
          - duration_seconds (int, required): Length of step in seconds
          - target (dict, required): One of:
              {"type": "power", "min_watts": float, "max_watts": float}   — absolute watts range
              {"type": "heart_rate", "min_bpm": float, "max_bpm": float}  — absolute BPM range
              {"type": "heart_rate_zone", "zone": int}                    — Garmin HR zone 1-5
              {"type": "none"}
          - description (str, optional): Label shown on watch (e.g. "Spinup!", "Cadence @ 90 rpm")

        Power zone reference (example based on FTP ~280W):
          Easy/recovery:   174–214W  (~62–76% FTP)
          Tempo:           217–257W  (~77–92% FTP)
          Threshold:       259–299W  (~92–107% FTP)
          Above threshold: 302–342W  (~108–122% FTP)

        Use ftp_percent_to_watts(ftp, low_pct, high_pct) to calculate, e.g.
        ftp_percent_to_watts(280, 0.92, 1.07) -> (258, 300)

        **Templates:**
        - workout://templates/tridot-cycling — example Threshold Intervals workout

        Example:
            steps = [
                {"duration_seconds": 300, "target": {"type": "heart_rate", "min_bpm": 101, "max_bpm": 122},
                 "description": "Spinup!"},
                {"duration_seconds": 960, "target": {"type": "power", "min_watts": 259, "max_watts": 299},
                 "description": "Cadence @ 80 rpm"},
                {"duration_seconds": 600, "target": {"type": "power", "min_watts": 174, "max_watts": 214}},
            ]

        Args:
            name: Workout name (e.g. "2025-11-10 Threshold Intervals")
            steps: List of step dicts (see above)
            description: Optional workout description text
            estimated_duration_seconds: Optional total duration hint; auto-calculated from steps if omitted
        """
        try:
            POWER_TARGET_TYPE_ID = 2    # workoutTargetTypeId for "power.zone" (absolute watts range)
            HR_TARGET_TYPE_ID = 4       # workoutTargetTypeId for "heart.rate.zone"
            NO_TARGET_TYPE_ID = 1       # workoutTargetTypeId for "no.target"
            STEP_TYPE_ID_OTHER = 7      # stepTypeId for "other"

            sport_type = {"sportTypeId": 2, "sportTypeKey": "cycling"}

            workout_steps = []
            for i, step in enumerate(steps):
                target = step.get("target", {"type": "none"})
                target_type = target.get("type", "none")
                zone_number = None

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
                elif target_type == "heart_rate_zone":
                    target_id = HR_TARGET_TYPE_ID
                    target_key = "heart.rate.zone"
                    zone_number = target["zone"]
                    target_low = target_high = 0.0
                else:
                    target_id = NO_TARGET_TYPE_ID
                    target_key = "no.target"
                    target_low = target_high = 0.0

                workout_steps.append(_make_step(
                    step_order=i + 1,
                    duration_seconds=step["duration_seconds"],
                    target_type_id=target_id,
                    target_type_key=target_key,
                    target_low=target_low,
                    target_high=target_high,
                    description=step.get("description"),
                    step_type_id=STEP_TYPE_ID_OTHER,
                    step_type_key="other",
                    zone_number=zone_number,
                ))

            workout_data = {
                "workoutName": name,
                "sportType": sport_type,
                "workoutSegments": [{
                    "segmentOrder": 1,
                    "sportType": sport_type,
                    "workoutSteps": workout_steps,
                }],
                "estimatedDuration": estimated_duration_seconds if estimated_duration_seconds
                    else sum(s["duration_seconds"] for s in steps),
            }

            if description:
                workout_data["description"] = description

            result = garmin_client.upload_workout(workout_data)

            if isinstance(result, dict):
                return json.dumps({
                    "status": "success",
                    "workout_id": result.get("workoutId"),
                    "name": result.get("workoutName"),
                    "steps_uploaded": len(steps),
                    "message": "Cycling workout uploaded successfully",
                }, indent=2)

            return json.dumps(result, indent=2)
        except Exception as e:
            return f"Error uploading cycling workout: {str(e)}"

    @app.tool()
    async def upload_workouts(workouts: list[dict]) -> str:
        """Upload multiple workouts from JSON data in a single call

        Creates multiple new workouts in Garmin Connect. Each item in the list
        uses the same structure as upload_workout.

        IMPORTANT: Step types must use Garmin's DTO format:
        - Use "ExecutableStepDTO" for regular steps (warmup, interval, cooldown, recovery)
        - Use "RepeatGroupDTO" for repeat/interval groups with numberOfIterations

        IMPORTANT: For heart rate zone targets, use "zoneNumber" (1-5), NOT targetValueOne/targetValueTwo.

        Args:
            workouts: List of workout dictionaries, each containing workout structure
                      (name, sport type, segments, etc.) — same format as upload_workout.
        """
        results = []
        for workout_data in workouts:
            try:
                _fix_hr_zone_steps(workout_data)
                result = garmin_client.upload_workout(workout_data)
                if isinstance(result, dict):
                    entry = {
                        "status": "success",
                        "workout_id": result.get('workoutId'),
                        "name": result.get('workoutName'),
                        "message": "Workout uploaded successfully"
                    }
                    results.append({k: v for k, v in entry.items() if v is not None})
                else:
                    results.append({"status": "success", "message": "Workout uploaded successfully"})
            except Exception as e:
                results.append({
                    "status": "error",
                    "name": workout_data.get('workoutName'),
                    "message": f"Error uploading workout: {str(e)}"
                })

        total = len(results)
        succeeded = sum(1 for r in results if r["status"] == "success")
        return json.dumps({
            "total": total,
            "succeeded": succeeded,
            "failed": total - succeeded,
            "results": results
        }, indent=2)

    @app.tool()
    async def delete_workout(workout_id: int) -> str:
        """Delete a workout from Garmin Connect

        Permanently removes a workout from your Garmin Connect workout library.

        Args:
            workout_id: ID of the workout to delete (get IDs from get_workouts)
        """
        try:
            url = f"{garmin_client.garmin_workouts}/workout/{workout_id}"
            response = garmin_client.client.delete("connectapi", url, api=True)

            if response.status_code == 204 or response.status_code == 200:
                return json.dumps({
                    "status": "success",
                    "workout_id": workout_id,
                    "message": f"Workout {workout_id} deleted successfully"
                }, indent=2)
            else:
                return json.dumps({
                    "status": "failed",
                    "workout_id": workout_id,
                    "http_status": response.status_code,
                    "message": f"Failed to delete workout: HTTP {response.status_code}"
                }, indent=2)
        except Exception as e:
            return f"Error deleting workout: {str(e)}"

    @app.tool()
    async def delete_workouts(workout_ids: list[int]) -> str:
        """Delete multiple workouts from Garmin Connect in a single call

        Permanently removes multiple workouts from your Garmin Connect workout library.

        Args:
            workout_ids: List of workout IDs to delete (get IDs from get_workouts)
        """
        results = []
        for workout_id in workout_ids:
            try:
                url = f"{garmin_client.garmin_workouts}/workout/{workout_id}"
                response = garmin_client.client.delete("connectapi", url, api=True)

                if response.status_code in (200, 204):
                    results.append({
                        "status": "success",
                        "workout_id": workout_id,
                        "message": f"Workout {workout_id} deleted successfully"
                    })
                else:
                    results.append({
                        "status": "failed",
                        "workout_id": workout_id,
                        "http_status": response.status_code,
                        "message": f"Failed to delete workout: HTTP {response.status_code}"
                    })
            except Exception as e:
                results.append({
                    "status": "error",
                    "workout_id": workout_id,
                    "message": f"Error deleting workout: {str(e)}"
                })

        total = len(results)
        succeeded = sum(1 for r in results if r["status"] == "success")
        return json.dumps({
            "total": total,
            "succeeded": succeeded,
            "failed": total - succeeded,
            "results": results
        }, indent=2)

    @app.tool()
    async def get_scheduled_workouts(start_date: str, end_date: str) -> str:
        """Get scheduled workouts between two dates with curated summary list

        Returns workouts that have been scheduled on the Garmin Connect calendar,
        including their scheduled dates and completion status.

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
        """
        try:
            # Query for scheduled workouts using GraphQL
            query = {
                "query": f'query{{workoutScheduleSummariesScalar(startDate:"{start_date}", endDate:"{end_date}")}}'
            }
            result = garmin_client.query_garmin_graphql(query)

            if not result or "data" not in result:
                return "No scheduled workouts found or error querying data."

            scheduled = result.get("data", {}).get("workoutScheduleSummariesScalar", [])

            if not scheduled:
                return f"No workouts scheduled between {start_date} and {end_date}."

            # Curate the scheduled workout list
            curated = {
                "count": len(scheduled),
                "date_range": {"start": start_date, "end": end_date},
                "scheduled_workouts": [_curate_scheduled_workout(s) for s in scheduled]
            }

            return json.dumps(curated, indent=2)
        except Exception as e:
            return f"Error retrieving scheduled workouts: {str(e)}"

    @app.tool()
    async def get_training_plan_workouts(calendar_date: str) -> str:
        """Get training plan workouts for the week containing the given date

        Returns workouts from your active training plan for the week containing
        the specified date. The API returns approximately 7 days of scheduled
        workouts anchored around the given date.

        Training plan workouts have workout_uuid (not workout_id). Use the
        workout_uuid with get_workout_by_id to get detailed step information.

        Args:
            calendar_date: Reference date in YYYY-MM-DD format (returns week's workouts)
        """
        try:
            # Query for training plan workouts using GraphQL
            query = {
                "query": f'query{{trainingPlanScalar(calendarDate:"{calendar_date}", lang:"en-US", firstDayOfWeek:"monday")}}'
            }
            result = garmin_client.query_garmin_graphql(query)

            if not result or "data" not in result:
                return "No training plan data found or error querying data."

            plan_data = result.get("data", {}).get("trainingPlanScalar", {})
            training_plans = plan_data.get("trainingPlanWorkoutScheduleDTOS", [])

            if not training_plans:
                return f"No training plan workouts scheduled for {calendar_date}."

            # Collect all workouts from all training plans
            all_workouts = []
            plan_names = []

            for plan in training_plans:
                plan_name = plan.get('planName')
                if plan_name and plan_name not in plan_names:
                    plan_names.append(plan_name)

                # workoutScheduleSummaries has same structure as scheduled workouts
                workout_summaries = plan.get('workoutScheduleSummaries', [])
                for workout in workout_summaries:
                    # Reuse the scheduled workout curation since structure is identical
                    all_workouts.append(_curate_scheduled_workout(workout))

            # Curate training plan data
            curated = {
                "date": calendar_date,
                "training_plans": plan_names if plan_names else None,
                "count": len(all_workouts),
                "workouts": all_workouts
            }

            # Remove None values from top level
            curated = {k: v for k, v in curated.items() if v is not None}

            return json.dumps(curated, indent=2)
        except Exception as e:
            return f"Error retrieving training plan workouts: {str(e)}"

    @app.tool()
    async def schedule_workout(workout_id: int, calendar_date: str) -> str:
        """Schedule a workout to a specific calendar date

        This adds an existing workout from your Garmin workout library
        to your Garmin Connect calendar on the specified date.

        Args:
            workout_id: ID of the workout to schedule (get IDs from get_workouts)
            calendar_date: Date to schedule the workout in YYYY-MM-DD format
        """
        try:
            url = f"workout-service/schedule/{workout_id}"
            response = garmin_client.client.post("connectapi", url, json={"date": calendar_date})

            if response.status_code == 200:
                return json.dumps({
                    "status": "success",
                    "workout_id": workout_id,
                    "scheduled_date": calendar_date,
                    "message": f"Successfully scheduled workout {workout_id} for {calendar_date}"
                }, indent=2)
            else:
                return json.dumps({
                    "status": "failed",
                    "workout_id": workout_id,
                    "scheduled_date": calendar_date,
                    "http_status": response.status_code,
                    "message": f"Failed to schedule workout: HTTP {response.status_code}"
                }, indent=2)
        except Exception as e:
            return f"Error scheduling workout: {str(e)}"

    @app.tool()
    async def get_user_zones() -> str:
        """Get your personal HR and running speed zones from Garmin Connect.

        Returns:
        - Heart rate zones (floors in BPM) from your Garmin profile
        - Running speed zones derived from your lactate threshold speed
          (also shown as min/mile and min/km pace for reference)

        Speed zones are computed from your lactate threshold speed using
        Garmin's standard LT-based percentages:
          Zone 1 (Easy/Recovery): < 77% LT speed
          Zone 2 (Aerobic Base):  77-87% LT speed
          Zone 3 (Tempo):         87-94% LT speed
          Zone 4 (Threshold):     94-100% LT speed
          Zone 5 (VO2max):       > 100% LT speed

        These zones can be used directly with upload_running_workout() via
        the "speed_zone" target type: {"type": "speed_zone", "zone": 1-5}
        """
        try:
            # HR zones
            hr_zones_raw = garmin_client.garth.get(
                "connectapi", "biometric-service/heartRateZones"
            ).json()
            default_hr = next(
                (z for z in hr_zones_raw if z.get("sport") == "DEFAULT"), hr_zones_raw[0]
            )
            hr_floors = [
                default_hr["zone1Floor"],
                default_hr["zone2Floor"],
                default_hr["zone3Floor"],
                default_hr["zone4Floor"],
                default_hr["zone5Floor"],
            ]
            max_hr = default_hr["maxHeartRateUsed"]
            hr_zones = []
            for i, floor in enumerate(hr_floors):
                ceiling = hr_floors[i + 1] - 1 if i + 1 < len(hr_floors) else max_hr
                hr_zones.append({"zone": i + 1, "min_bpm": floor, "max_bpm": ceiling})

            # Speed zones from LT
            speed_zones_mps = _get_speed_zones_mps()
            speed_zones = []
            for z in speed_zones_mps:
                entry = {"zone": z["zone"], "min_mps": z["min_mps"]}
                if z["max_mps"] is not None:
                    entry["max_mps"] = z["max_mps"]
                # Add pace reference
                if z["min_mps"] > 0:
                    min_pace_mile = 1609.344 / z["min_mps"] / 60
                    m, s = divmod(min_pace_mile * 60, 60)
                    entry["min_pace_per_mile"] = f"{int(m)}:{int(s):02d}"
                if z["max_mps"]:
                    max_pace_mile = 1609.344 / z["max_mps"] / 60
                    m, s = divmod(max_pace_mile * 60, 60)
                    entry["max_pace_per_mile"] = f"{int(m)}:{int(s):02d}"
                speed_zones.append(entry)

            profile = garmin_client.get_user_profile()
            lt_sec_per_m = profile.get("userData", {}).get("lactateThresholdSpeed")
            lt_mps = round(1.0 / lt_sec_per_m, 3) if lt_sec_per_m else None
            lt_hr = profile.get("userData", {}).get("lactateThresholdHeartRate")

            return json.dumps({
                "lactate_threshold": {
                    "speed_mps": lt_mps,
                    "heart_rate_bpm": lt_hr,
                },
                "heart_rate_zones": hr_zones,
                "speed_zones": speed_zones,
                "note": "Speed zones are derived from LT speed using Garmin standard percentages. "
                        "Use {'type': 'speed_zone', 'zone': N} in upload_running_workout().",
            }, indent=2)
        except Exception as e:
            return f"Error retrieving user zones: {str(e)}"

    @app.tool()
    async def schedule_workouts(schedules: list[dict]) -> str:
        """Schedule multiple workouts to specific calendar dates

        This adds workouts to your Garmin Connect calendar in a single call.
        Each item can either reference an existing workout by ID, or provide
        inline workout_data to upload-and-schedule in one step.

        Args:
            schedules: List of workout schedules, each with:
                - calendar_date (str): Date to schedule the workout in YYYY-MM-DD format (required)
                - workout_id (int): ID of an existing workout to schedule (required unless workout_data is provided)
                - workout_data (dict): Inline workout JSON to upload first, then schedule (optional).
                  When provided, workout_id is not required. Uses the same structure as upload_workout.

        Examples:
            Schedule existing workouts by ID:
            [{"workout_id": 123456, "calendar_date": "2024-01-15"},
             {"workout_id": 789012, "calendar_date": "2024-01-17"}]

            Upload and schedule inline:
            [{"calendar_date": "2024-01-15", "workout_data": {"workoutName": "Easy Run", ...}},
             {"workout_id": 789012, "calendar_date": "2024-01-17"}]
        """
        results = []
        for item in schedules:
            workout_id = item.get("workout_id")
            calendar_date = item.get("calendar_date")
            workout_data = item.get("workout_data")

            if calendar_date is None:
                results.append({
                    "status": "failed",
                    "workout_id": workout_id,
                    "scheduled_date": calendar_date,
                    "message": "Missing required field: calendar_date"
                })
                continue

            if workout_id is None and workout_data is None:
                results.append({
                    "status": "failed",
                    "workout_id": None,
                    "scheduled_date": calendar_date,
                    "message": "Missing required fields: provide either workout_id or workout_data"
                })
                continue

            try:
                workout_name = None

                if workout_data is not None:
                    # Upload the workout first, then use the returned ID to schedule
                    _fix_hr_zone_steps(workout_data)
                    upload_result = garmin_client.upload_workout(workout_data)
                    if not isinstance(upload_result, dict) or upload_result.get('workoutId') is None:
                        results.append({
                            "status": "failed",
                            "scheduled_date": calendar_date,
                            "message": "Upload succeeded but no workout_id returned"
                        })
                        continue
                    workout_id = upload_result['workoutId']
                    workout_name = upload_result.get('workoutName')

                url = f"workout-service/schedule/{workout_id}"
                response = garmin_client.client.post("connectapi", url, json={"date": calendar_date})

                if response.status_code == 200:
                    entry = {
                        "status": "success",
                        "workout_id": workout_id,
                        "scheduled_date": calendar_date,
                        "message": f"Successfully scheduled workout {workout_id} for {calendar_date}"
                    }
                    if workout_name:
                        entry["workout_name"] = workout_name
                    results.append(entry)
                else:
                    results.append({
                        "status": "failed",
                        "workout_id": workout_id,
                        "scheduled_date": calendar_date,
                        "http_status": response.status_code,
                        "message": f"Failed to schedule workout: HTTP {response.status_code}"
                    })
            except Exception as e:
                results.append({
                    "status": "error",
                    "workout_id": workout_id,
                    "scheduled_date": calendar_date,
                    "message": f"Error scheduling workout: {str(e)}"
                })

        total = len(results)
        succeeded = sum(1 for r in results if r["status"] == "success")
        return json.dumps({
            "total": total,
            "succeeded": succeeded,
            "failed": total - succeeded,
            "results": results
        }, indent=2)

    return app
