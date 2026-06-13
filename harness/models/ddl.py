"""DDL strings for the two SQLite databases. No execution here."""

from __future__ import annotations

PRAGMA_FOREIGN_KEYS = "PRAGMA foreign_keys = ON"


CORE_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS sessions (
      id                  TEXT PRIMARY KEY,
      status              TEXT NOT NULL,
      current_stage       TEXT NOT NULL,
      iter_since_approval INTEGER NOT NULL DEFAULT 0,
      created_at          TEXT NOT NULL,
      updated_at          TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS spend_log (
      id          TEXT PRIMARY KEY,
      ts          TEXT NOT NULL,
      session_id  TEXT NOT NULL REFERENCES sessions(id),
      model       TEXT NOT NULL,
      is_fallback INTEGER NOT NULL DEFAULT 0,
      tokens_in   INTEGER NOT NULL,
      tokens_out  INTEGER NOT NULL,
      cost_usd    REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_spend_day ON spend_log(ts)",
    "CREATE INDEX IF NOT EXISTS idx_spend_session ON spend_log(session_id)",
]


# Order is load-bearing: events references material/checkpoints/alarms, so those
# must exist first. alarms.triggered_by_event_id is logical (not FK) to avoid a
# cycle with events.alarm_id.
SESSION_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS material (
      id         TEXT PRIMARY KEY,
      direction  TEXT NOT NULL,
      stage      TEXT NOT NULL,
      type       TEXT NOT NULL,
      content    TEXT NOT NULL,
      pending    INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS checkpoints (
      id               TEXT PRIMARY KEY,
      name             TEXT NOT NULL,
      stage            TEXT NOT NULL,
      status           TEXT NOT NULL,
      criteria_results TEXT NOT NULL,
      material_id      TEXT NULL REFERENCES material(id),
      created_at       TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ckpt_name_created ON checkpoints(name, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_ckpt_material ON checkpoints(material_id)",
    """
    CREATE TABLE IF NOT EXISTS alarms (
      id                    TEXT PRIMARY KEY,
      type                  TEXT NOT NULL,
      severity              TEXT NOT NULL,
      context               TEXT NOT NULL,
      recommended_action    TEXT NOT NULL,
      stage                 TEXT NOT NULL,
      triggered_by_event_id TEXT NULL,
      resolved              INTEGER NOT NULL DEFAULT 0,
      created_at            TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_alarms_event ON alarms(triggered_by_event_id)",
    """
    CREATE TABLE IF NOT EXISTS events (
      id            TEXT PRIMARY KEY,
      ts            TEXT NOT NULL,
      type          TEXT NOT NULL,
      stage         TEXT NOT NULL,
      payload       TEXT NOT NULL,
      material_id   TEXT NULL REFERENCES material(id),
      checkpoint_id TEXT NULL REFERENCES checkpoints(id),
      alarm_id      TEXT NULL REFERENCES alarms(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_material ON events(material_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_checkpoint ON events(checkpoint_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_alarm ON events(alarm_id)",
]
