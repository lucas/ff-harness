"""Closed enum sets for the v1 harness. Values are the on-disk strings."""

from __future__ import annotations

from enum import Enum


class EventType(str, Enum):
    WORKER_INPUT = "worker_input"
    WORKER_OUTPUT = "worker_output"
    MODEL_SWAPPED = "model_swapped"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    POST_HOOK_RUN = "post_hook_run"
    CHECKPOINT_RESULT = "checkpoint_result"
    ALARM_RAISED = "alarm_raised"
    AWAITING_HUMAN = "awaiting_human"
    HUMAN_RESUMED = "human_resumed"
    REWOUND = "rewound"


class MaterialType(str, Enum):
    BUSINESS_BRIEF = "business_brief"
    PENDING_QUESTION = "pending_question"
    USER_ANSWER = "user_answer"
    USER_APPROVAL = "user_approval"
    LAYOUT_SPEC = "layout_spec"
    MOCKUP = "mockup"
    SITE_FILE = "site_file"
    VALIDATION_RESULT = "validation_result"


class AlarmType(str, Enum):
    ITERATION_LIMIT_REACHED = "iteration_limit_reached"
    SPEND_CAP_REACHED = "spend_cap_reached"
    OUTPUT_SCHEMA_VIOLATION = "output_schema_violation"
    TOOL_FAILED = "tool_failed"


class CheckpointName(str, Enum):
    BUSINESS_BRIEF_CONFIRMED = "business_brief_confirmed"
    MOCKUP_RENDERS = "mockup_renders"
    MOCKUP_APPROVED = "mockup_approved"
    SITE_VALID = "site_valid"
    SEO_ARTIFACTS_PRESENT = "seo_artifacts_present"


class Severity(str, Enum):
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Stage(str, Enum):
    BOOTSTRAP = "bootstrap"
    MOCKUP = "mockup"
    BUILD = "build"
    DONE = "done"


class SessionStatus(str, Enum):
    ACTIVE = "active"
    AWAITING_HUMAN = "awaiting_human"
    COMPLETED = "completed"
    FAILED = "failed"


class Direction(str, Enum):
    IN = "in"
    OUT = "out"


class CheckpointStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
