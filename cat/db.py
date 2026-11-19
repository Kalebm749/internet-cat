"""SQLite schema and helpers. The engine is the only writer; the web app opens read-only."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS state(
  id INTEGER PRIMARY KEY CHECK(id=1),
  sim_time TEXT, area TEXT, needs JSON, mood JSON, energy REAL, weather TEXT,
  tendencies JSON, is_asleep INT, brain_online INT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS objects(
  id TEXT PRIMARY KEY, name TEXT, area TEXT,           -- area = 'inventory' when carried
  tags JSON, state JSON, created_by TEXT,              -- 'world' | 'event' | 'creature'
  created_at TEXT);

CREATE TABLE IF NOT EXISTS ticks(
  id INTEGER PRIMARY KEY, kind TEXT DEFAULT 'tick',    -- 'tick' | 'doze' | 'asleep' | 'catchup'
  sim_time TEXT, real_time TEXT, area TEXT,
  reaction TEXT, thought TEXT, action TEXT, intent TEXT,
  outcome TEXT, gains JSON, effort TEXT, duration INT,
  changes JSON, rejected_changes JSON, needs_after JSON, mood_after JSON);

CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, tick_id INT, sim_time TEXT, area TEXT, text TEXT);

CREATE TABLE IF NOT EXISTS memories(
  id INTEGER PRIMARY KEY, kind TEXT,                   -- 'short' | 'long'
  text TEXT, importance REAL, created_sim_time TEXT,
  last_recalled TEXT, embedding BLOB, embed_model TEXT);

CREATE TABLE IF NOT EXISTS preferences(
  id INTEGER PRIMARY KEY, subject TEXT, valence REAL,  -- -1..1
  reason TEXT, updated_at TEXT);

-- what the entity currently thinks it is: short text lines, learned during sleep, revisable
CREATE TABLE IF NOT EXISTS self_image(
  id INTEGER PRIMARY KEY, text TEXT, confidence REAL,  -- 0..1
  formed_day TEXT,                                     -- sim date the line was formed (YYYY-MM-DD)
  updated_at TEXT);

CREATE TABLE IF NOT EXISTS sleep_log(
  id INTEGER PRIMARY KEY, sim_date TEXT, summary TEXT, tick_id INT,   -- tick at which the sleep began
  tendency_changes JSON, new_memories JSON, new_preferences JSON);

-- ---- tracing (phase 2, part B): what the engine did and why. See cat/trace.py ----
CREATE TABLE IF NOT EXISTS tick_traces(
  tick_id INTEGER PRIMARY KEY,                 -- negative = a tick that raised before it was recorded
  started_at TEXT, finished_at TEXT, total_ms REAL,
  stage_timings JSON,                          -- {"world": ms, "recall": ms, "brain": ms, "narrator": ms, "apply": ms, "record": ms, "sleep": ms}
  status TEXT,                                 -- ok | doze | error
  error TEXT);

CREATE TABLE IF NOT EXISTS llm_calls(
  id INTEGER PRIMARY KEY, tick_id INT,
  kind TEXT,                                   -- brain | narrator | sleep_consolidate | sleep_tendencies | sleep_preferences | sleep_self_image | other
  model TEXT, options JSON, system_prompt TEXT, user_prompt TEXT, schema JSON,
  raw_response TEXT, parsed JSON, valid_json INT, attempt INT, error TEXT,
  prompt_tokens INT, completion_tokens INT,
  load_ms INT, prompt_eval_ms INT, eval_ms INT, total_ms INT, started_at TEXT);

CREATE TABLE IF NOT EXISTS embed_calls(
  id INTEGER PRIMARY KEY, tick_id INT,
  purpose TEXT,                                -- memory_write | recall_query | repetition_check
  model TEXT, input_text TEXT, dims INT, total_ms INT, error TEXT, started_at TEXT);

CREATE TABLE IF NOT EXISTS recall_logs(
  id INTEGER PRIMARY KEY, tick_id INT, query_text TEXT,
  candidates JSON);                            -- [{memory_id, text, recency, importance, relevance, score, selected}]

CREATE TABLE IF NOT EXISTS effect_logs(
  id INTEGER PRIMARY KEY, tick_id INT, op TEXT, args JSON, accepted INT, reason TEXT);

CREATE TABLE IF NOT EXISTS need_math(
  id INTEGER PRIMARY KEY, tick_id INT, need TEXT, "before" REAL,
  decay REAL, bucket TEXT, gain_raw REAL, repetition_mult REAL,
  effort_cost REAL, clamp_adjust REAL, "after" REAL);

CREATE TABLE IF NOT EXISTS mood_math(
  tick_id INTEGER PRIMARY KEY, inputs JSON, valence REAL, arousal REAL, word TEXT);

CREATE TABLE IF NOT EXISTS event_rolls(
  id INTEGER PRIMARY KEY, tick_id INT, event_text TEXT, chance REAL, roll REAL,
  fired INT, filtered_reason TEXT);

CREATE TABLE IF NOT EXISTS sleep_traces(
  id INTEGER PRIMARY KEY, sleep_log_id INT, step TEXT,
  input JSON, output JSON, proposed JSON, clamped JSON);

CREATE INDEX IF NOT EXISTS idx_llm_tick ON llm_calls(tick_id);
CREATE INDEX IF NOT EXISTS idx_llm_started ON llm_calls(started_at);
CREATE INDEX IF NOT EXISTS idx_embed_tick ON embed_calls(tick_id);
CREATE INDEX IF NOT EXISTS idx_recall_tick ON recall_logs(tick_id);
CREATE INDEX IF NOT EXISTS idx_effect_tick ON effect_logs(tick_id);
CREATE INDEX IF NOT EXISTS idx_need_tick ON need_math(tick_id);
CREATE INDEX IF NOT EXISTS idx_roll_tick ON event_rolls(tick_id);
CREATE INDEX IF NOT EXISTS idx_sleeptr_log ON sleep_traces(sleep_log_id);
CREATE INDEX IF NOT EXISTS idx_ticks_kind ON ticks(kind);
CREATE INDEX IF NOT EXISTS idx_events_tick ON events(tick_id);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind);
"""


def connect(path: Path | str, readonly: bool = False) -> sqlite3.Connection:
    """Open the DB. isolation_level=None: we manage transactions explicitly via `transaction()`."""
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None, timeout=10)
    else:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), isolation_level=None, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Tiny migration helper: add a column to an existing table if it is not there yet."""
    if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _add_column(conn, "memories", "tick_id", "INT")      # which tick wrote this memory (for tracing)
    if get_meta(conn, "schema_version") is None:
        set_meta(conn, "schema_version", SCHEMA_VERSION)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """All-or-nothing block. A crash or exception leaves the previous state intact."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def get_state(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM state WHERE id=1").fetchone()
    if row is None:
        return None
    d = dict(row)
    for k in ("needs", "mood", "tendencies"):
        d[k] = json.loads(d[k]) if d[k] else None
    return d


def save_state(conn: sqlite3.Connection, s: dict, updated_at: str) -> None:
    conn.execute(
        "INSERT INTO state(id,sim_time,area,needs,mood,energy,weather,tendencies,is_asleep,"
        "brain_online,updated_at) VALUES(1,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET sim_time=excluded.sim_time, area=excluded.area, "
        "needs=excluded.needs, mood=excluded.mood, energy=excluded.energy, "
        "weather=excluded.weather, tendencies=excluded.tendencies, is_asleep=excluded.is_asleep, "
        "brain_online=excluded.brain_online, updated_at=excluded.updated_at",
        (s["sim_time"], s["area"], dumps(s["needs"]), dumps(s["mood"]), s["energy"],
         s["weather"], dumps(s["tendencies"]), int(s["is_asleep"]), int(s["brain_online"]),
         updated_at))
