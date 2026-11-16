"""SQLite persistence. One connection per request; WAL mode so readers never block the writer."""
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  REAL NOT NULL,
    t0          REAL,                     -- case clock start (epoch seconds), set once
    is_demo     INTEGER NOT NULL DEFAULT 0,
    time_scale  REAL                      -- per-player clock speed (demo sessions); NULL = server default
);
CREATE TABLE IF NOT EXISTS puzzle_opens (
    participant_id TEXT NOT NULL REFERENCES participants(id),
    puzzle_id      TEXT NOT NULL,
    opened_at      REAL NOT NULL,
    PRIMARY KEY (participant_id, puzzle_id)
);
CREATE TABLE IF NOT EXISTS submissions (
    participant_id TEXT NOT NULL REFERENCES participants(id),
    puzzle_id      TEXT NOT NULL,
    raw_answer     TEXT NOT NULL,         -- exactly what the player typed
    normalized     TEXT NOT NULL,
    correct        INTEGER NOT NULL,      -- never returned to players before results are released
    recorded_at    REAL NOT NULL,
    PRIMARY KEY (participant_id, puzzle_id)   -- one submission per puzzle, enforced by the database
);
CREATE TABLE IF NOT EXISTS hints (
    participant_id TEXT NOT NULL REFERENCES participants(id),
    puzzle_id      TEXT NOT NULL,
    used_at        REAL NOT NULL,
    PRIMARY KEY (participant_id, puzzle_id)
);
CREATE TABLE IF NOT EXISTS accusations (
    participant_id TEXT PRIMARY KEY REFERENCES participants(id),
    suspect_id     TEXT NOT NULL,
    correct        INTEGER NOT NULL,
    recorded_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def init_db(path: str) -> None:
    with sqlite3.connect(path) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        # Upgrade databases created before demo sessions existed.
        cols = {r[1] for r in con.execute("PRAGMA table_info(participants)")}
        if "is_demo" not in cols:
            con.execute("ALTER TABLE participants ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0")
        if "time_scale" not in cols:
            con.execute("ALTER TABLE participants ADD COLUMN time_scale REAL")


@contextmanager
def connect(path: str):
    con = sqlite3.connect(path, timeout=10, isolation_level=None)  # autocommit; explicit BEGIN where needed
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
    finally:
        con.close()
