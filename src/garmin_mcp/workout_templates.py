"""
Workout template resources for Garmin MCP Server

Provides MCP resources with valid workout JSON structures that clients can
read and use as templates for creating custom workouts via upload_workout.
"""
import json

# =============================================================================
# WORKOUT TEMPLATES
# =============================================================================

SIMPLE_RUN_TEMPLATE = {
    "workoutName": "Simple Run",
    "description": "Basic run workout: warmup, run, cooldown",
    "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
    "workoutSegments": [{
        "segmentOrder": 1,
        "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
        "workoutSteps": [
            {
                "type": "ExecutableStepDTO",
                "stepOrder": 1,
                "stepType": {"stepTypeId": 1, "stepTypeKey": "warmup"},
                "description": "Warmup 5 min",
                "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                "endConditionValue": 300.0,
                "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
            },
            {
                "type": "ExecutableStepDTO",
                "stepOrder": 2,
                "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
                "description": "Run 20 min",
                "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                "endConditionValue": 1200.0,
                "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
            },
            {
                "type": "ExecutableStepDTO",
                "stepOrder": 3,
                "stepType": {"stepTypeId": 2, "stepTypeKey": "cooldown"},
                "description": "Cooldown 5 min",
                "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                "endConditionValue": 300.0,
                "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
            }
        ]
    }]
}

INTERVAL_RUNNING_TEMPLATE = {
    "workoutName": "Interval Run",
    "description": "Interval workout with repeat groups: warmup, 6x(400m fast + 2min recovery), cooldown",
    "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
    "workoutSegments": [{
        "segmentOrder": 1,
        "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
        "workoutSteps": [
            {
                "type": "ExecutableStepDTO",
                "stepOrder": 1,
                "stepType": {"stepTypeId": 1, "stepTypeKey": "warmup"},
                "description": "Warmup 10 min",
                "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                "endConditionValue": 600.0,
                "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
            },
            {
                "type": "RepeatGroupDTO",
                "stepOrder": 2,
                "numberOfIterations": 6,
                "workoutSteps": [
                    {
                        "type": "ExecutableStepDTO",
                        "stepOrder": 1,
                        "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
                        "description": "Fast 400m",
                        "endCondition": {"conditionTypeId": 3, "conditionTypeKey": "distance"},
                        "endConditionValue": 400.0,
                        "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
                    },
                    {
                        "type": "ExecutableStepDTO",
                        "stepOrder": 2,
                        "stepType": {"stepTypeId": 4, "stepTypeKey": "recovery"},
                        "description": "Recovery 2 min",
                        "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                        "endConditionValue": 120.0,
                        "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
                    }
                ]
            },
            {
                "type": "ExecutableStepDTO",
                "stepOrder": 3,
                "stepType": {"stepTypeId": 2, "stepTypeKey": "cooldown"},
                "description": "Cooldown 10 min",
                "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                "endConditionValue": 600.0,
                "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
            }
        ]
    }]
}

TEMPO_RUN_TEMPLATE = {
    "workoutName": "Tempo Run",
    "description": "Tempo workout: warmup, 20min at tempo pace (HR zone 4), cooldown",
    "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
    "workoutSegments": [{
        "segmentOrder": 1,
        "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
        "workoutSteps": [
            {
                "type": "ExecutableStepDTO",
                "stepOrder": 1,
                "stepType": {"stepTypeId": 1, "stepTypeKey": "warmup"},
                "description": "Warmup 10 min",
                "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                "endConditionValue": 600.0,
                "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
            },
            {
                "type": "ExecutableStepDTO",
                "stepOrder": 2,
                "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
                "description": "Tempo 20 min - HR Zone 4",
                "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                "endConditionValue": 1200.0,
                "targetType": {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"},
                "zoneNumber": 4
            },
            {
                "type": "ExecutableStepDTO",
                "stepOrder": 3,
                "stepType": {"stepTypeId": 2, "stepTypeKey": "cooldown"},
                "description": "Cooldown 10 min",
                "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                "endConditionValue": 600.0,
                "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
            }
        ]
    }]
}

STRENGTH_CIRCUIT_TEMPLATE = {
    "workoutName": "Strength Circuit",
    "description": "Strength training circuit: warmup, 3x circuit (work + rest), cooldown",
    "sportType": {"sportTypeId": 4, "sportTypeKey": "strength_training"},
    "workoutSegments": [{
        "segmentOrder": 1,
        "sportType": {"sportTypeId": 4, "sportTypeKey": "strength_training"},
        "workoutSteps": [
            {
                "type": "ExecutableStepDTO",
                "stepOrder": 1,
                "stepType": {"stepTypeId": 1, "stepTypeKey": "warmup"},
                "description": "Warmup 5 min",
                "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                "endConditionValue": 300.0,
                "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
            },
            {
                "type": "RepeatGroupDTO",
                "stepOrder": 2,
                "numberOfIterations": 3,
                "workoutSteps": [
                    {
                        "type": "ExecutableStepDTO",
                        "stepOrder": 1,
                        "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
                        "description": "Circuit work 10 min",
                        "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                        "endConditionValue": 600.0,
                        "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
                    },
                    {
                        "type": "ExecutableStepDTO",
                        "stepOrder": 2,
                        "stepType": {"stepTypeId": 4, "stepTypeKey": "recovery"},
                        "description": "Rest 2 min",
                        "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                        "endConditionValue": 120.0,
                        "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
                    }
                ]
            },
            {
                "type": "ExecutableStepDTO",
                "stepOrder": 3,
                "stepType": {"stepTypeId": 2, "stepTypeKey": "cooldown"},
                "description": "Cooldown stretch 5 min",
                "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                "endConditionValue": 300.0,
                "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
            }
        ]
    }]
}

TRIDOT_RUN_TEMPLATE = {
    "description": "Example upload_running_workout() call — Threshold Repeats (4x6min @ threshold pace with 2min recoveries)",
    "tool": "upload_running_workout",
    "pace_reference_mps": {
        "2.51": "10:41/mile (easy/recovery)",
        "2.68": "10:00/mile",
        "2.87": "9:21/mile",
        "3.21": "8:21/mile (threshold lower)",
        "3.50": "7:40/mile (threshold upper)",
    },
    "example_call": {
        "name": "2025-11-12 Threshold Repeats",
        "steps": [
            {"duration_seconds": 600, "target": {"type": "heart_rate", "min_bpm": 108, "max_bpm": 141},
             "description": "Easy warmup"},
            {"duration_seconds": 360, "target": {"type": "speed", "min_mps": 3.21, "max_mps": 3.50},
             "description": "Threshold"},
            {"duration_seconds": 120, "target": {"type": "heart_rate", "min_bpm": 108, "max_bpm": 141},
             "description": "Recovery"},
            {"duration_seconds": 360, "target": {"type": "speed", "min_mps": 3.21, "max_mps": 3.50},
             "description": "Threshold"},
            {"duration_seconds": 120, "target": {"type": "heart_rate", "min_bpm": 108, "max_bpm": 141},
             "description": "Recovery"},
            {"duration_seconds": 360, "target": {"type": "speed", "min_mps": 3.21, "max_mps": 3.50},
             "description": "Threshold"},
            {"duration_seconds": 120, "target": {"type": "heart_rate", "min_bpm": 108, "max_bpm": 141},
             "description": "Recovery"},
            {"duration_seconds": 360, "target": {"type": "speed", "min_mps": 3.21, "max_mps": 3.50},
             "description": "Threshold"},
            {"duration_seconds": 1200, "target": {"type": "heart_rate", "min_bpm": 108, "max_bpm": 141},
             "description": "Cooldown"},
        ],
    },
    "notes": [
        "Steps are flat (no nesting/repeat groups) — write out each interval explicitly",
        "All steps are time-based; distance-based steps are not supported",
        "speed target uses absolute m/s range (not pace zones); use pace_to_mps() to convert",
        "heart_rate target uses absolute BPM range (not zone numbers)",
    ],
}

TRIDOT_CYCLING_TEMPLATE = {
    "description": "Example upload_cycling_workout() call — Threshold Intervals with power targets",
    "tool": "upload_cycling_workout",
    "power_zone_reference_ftp280": {
        "174-214W": "Easy/recovery (~62-76% FTP)",
        "217-257W": "Tempo/sweet spot (~77-92% FTP)",
        "259-299W": "Threshold (~92-107% FTP)",
        "302-342W": "Above threshold (~108-122% FTP)",
    },
    "ftp_conversion": "Use ftp_percent_to_watts(ftp, low_pct, high_pct), e.g. ftp_percent_to_watts(280, 0.92, 1.07) -> (258, 300)",
    "example_call": {
        "name": "2025-11-10 Threshold Intervals",
        "steps": [
            {"duration_seconds": 240, "target": {"type": "heart_rate", "min_bpm": 101, "max_bpm": 122}},
            {"duration_seconds": 30, "target": {"type": "heart_rate", "min_bpm": 101, "max_bpm": 122},
             "description": "Spinup!"},
            {"duration_seconds": 30, "target": {"type": "heart_rate", "min_bpm": 101, "max_bpm": 122},
             "description": "Easy Spin"},
            {"duration_seconds": 960, "target": {"type": "power", "min_watts": 259, "max_watts": 299},
             "description": "Cadence @ 80 rpm"},
            {"duration_seconds": 120, "target": {"type": "power", "min_watts": 174, "max_watts": 214}},
            {"duration_seconds": 960, "target": {"type": "power", "min_watts": 259, "max_watts": 299}},
            {"duration_seconds": 120, "target": {"type": "power", "min_watts": 174, "max_watts": 214}},
            {"duration_seconds": 960, "target": {"type": "power", "min_watts": 259, "max_watts": 299}},
            {"duration_seconds": 600, "target": {"type": "power", "min_watts": 174, "max_watts": 214},
             "description": "Cooldown spin"},
        ],
    },
    "notes": [
        "Steps are flat (no nesting/repeat groups) — write out each interval explicitly",
        "All steps are time-based; distance-based steps are not supported",
        "power target uses absolute watts range (not FTP zone numbers)",
        "heart_rate target uses absolute BPM range (not zone numbers)",
        "Short spinup steps (30s) are common in Tridot cycling workouts before main blocks",
    ],
}

# Reference documentation for workout structure
WORKOUT_STRUCTURE_REFERENCE = {
    "description": "Reference guide for Garmin workout JSON structure",
    "step_types": {
        "ExecutableStepDTO": "Regular workout step (warmup, interval, cooldown, recovery, rest)",
        "RepeatGroupDTO": "Repeat group containing nested steps with numberOfIterations"
    },
    "stepType_values": {
        "1": {"stepTypeKey": "warmup", "description": "Warmup phase"},
        "2": {"stepTypeKey": "cooldown", "description": "Cooldown phase"},
        "3": {"stepTypeKey": "interval", "description": "Work/effort interval"},
        "4": {"stepTypeKey": "recovery", "description": "Recovery between intervals"},
        "5": {"stepTypeKey": "rest", "description": "Complete rest"}
    },
    "endCondition_values": {
        "1": {"conditionTypeKey": "lap.button", "description": "Manual lap press"},
        "2": {"conditionTypeKey": "time", "description": "Duration in seconds"},
        "3": {"conditionTypeKey": "distance", "description": "Distance in meters"}
    },
    "targetType_values": {
        "1": {"workoutTargetTypeKey": "no.target", "description": "No specific target"},
        "4": {"workoutTargetTypeKey": "heart.rate.zone", "description": "Heart rate zone (use zoneNumber 1-5)"},
        "2": {"workoutTargetTypeKey": "power.zone", "description": "Power zone (use targetValueOne/targetValueTwo for absolute watts range)"},
        "5": {"workoutTargetTypeKey": "speed.zone", "description": "Speed zone (use targetValueOne/targetValueTwo for absolute m/s range)"},
        "6": {"workoutTargetTypeKey": "pace.zone", "description": "Pace zone by number (use zoneNumber 1-5)"}
    },
    "sportType_values": {
        "1": {"sportTypeKey": "running"},
        "2": {"sportTypeKey": "cycling"},
        "4": {"sportTypeKey": "strength_training"},
        "5": {"sportTypeKey": "cardio"},
        "11": {"sportTypeKey": "walking"}
    }
}


def register_resources(app):
    """Register workout template resources with the MCP server app"""

    @app.resource("workout://templates/simple-run")
    async def get_simple_run_template() -> str:
        """Simple run workout template (warmup, run, cooldown)

        A basic running workout structure suitable for easy runs.
        Modify the endConditionValue to adjust durations.
        """
        return json.dumps(SIMPLE_RUN_TEMPLATE, indent=2)

    @app.resource("workout://templates/interval-running")
    async def get_interval_template() -> str:
        """Interval running workout template with repeat groups

        Demonstrates RepeatGroupDTO for interval training.
        Includes 6x400m intervals with 2min recovery.
        """
        return json.dumps(INTERVAL_RUNNING_TEMPLATE, indent=2)

    @app.resource("workout://templates/tempo-run")
    async def get_tempo_template() -> str:
        """Tempo run workout template with heart rate zone target

        Demonstrates targeting a specific heart rate zone.
        20min tempo block at HR zone 4.
        """
        return json.dumps(TEMPO_RUN_TEMPLATE, indent=2)

    @app.resource("workout://templates/strength-circuit")
    async def get_strength_template() -> str:
        """Strength training circuit template

        Circuit-style strength workout with repeat groups.
        3 rounds of 10min work + 2min rest.
        """
        return json.dumps(STRENGTH_CIRCUIT_TEMPLATE, indent=2)

    @app.resource("workout://templates/tridot-run")
    async def get_tridot_run_template() -> str:
        """Tridot-style running workout template using upload_running_workout()

        Demonstrates a Threshold Repeats pattern (4x6min @ threshold pace).
        Uses flat step structure with absolute speed (m/s) and HR (BPM) targets.
        Includes pace reference table for m/s ↔ min/mile conversion.
        """
        return json.dumps(TRIDOT_RUN_TEMPLATE, indent=2)

    @app.resource("workout://templates/tridot-cycling")
    async def get_tridot_cycling_template() -> str:
        """Tridot-style cycling workout template using upload_cycling_workout()

        Demonstrates a Threshold Intervals pattern with power and HR targets.
        Uses flat step structure with absolute power (watts) and HR (BPM) targets.
        Includes FTP% conversion reference.
        """
        return json.dumps(TRIDOT_CYCLING_TEMPLATE, indent=2)

    @app.resource("workout://reference/structure")
    async def get_structure_reference() -> str:
        """Reference guide for workout JSON structure

        Documents valid values for step types, conditions, targets, and sports.
        Use this to understand what values are valid in workout definitions.
        """
        return json.dumps(WORKOUT_STRUCTURE_REFERENCE, indent=2)

    return app
