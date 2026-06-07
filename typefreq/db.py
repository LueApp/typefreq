"""SQLite storage for typefreq.

Thread model: each thread that talks to the DB gets its own connection via
`connect()`. SQLite handles concurrent readers natively; writes are serialized
by SQLite itself with WAL mode.
"""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS word_counts (
    word    TEXT PRIMARY KEY,
    count   INTEGER NOT NULL DEFAULT 0,
    last_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS hourly_buckets (
    hour_ts INTEGER NOT NULL,   -- unix epoch of hour start (UTC)
    words   INTEGER NOT NULL DEFAULT 0,
    typos   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hour_ts)
);

CREATE TABLE IF NOT EXISTS typos (
    ts          INTEGER NOT NULL,
    word        TEXT NOT NULL,
    suggestion  TEXT
);
CREATE INDEX IF NOT EXISTS idx_typos_ts ON typos(ts DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS custom_words (
    word     TEXT PRIMARY KEY,
    added_ts INTEGER NOT NULL
);

-- Per-day per-word counts. Powers the week/month/year/today leaderboards
-- by GROUP BY over an arbitrary day range. day_ts is the unix-epoch start
-- of the *local* day (see day_start_utc).
CREATE TABLE IF NOT EXISTS daily_word_counts (
    word    TEXT NOT NULL,
    day_ts  INTEGER NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (word, day_ts)
);
CREATE INDEX IF NOT EXISTS idx_daily_word_counts_day_ts ON daily_word_counts(day_ts DESC);
"""


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    # check_same_thread=False: callers must serialize their own access (the
    # Engine wraps every call in a Lock). Flask request handlers run in worker
    # threads, so without this flag every API call would raise.
    conn = sqlite3.connect(
        str(path), timeout=5.0, isolation_level=None, check_same_thread=False,
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(path: Path | str = DB_PATH) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def _hour_floor(ts: int) -> int:
    return ts - (ts % 3600)


def _day_floor(ts: int) -> int:
    """Start of the local day containing `ts`, expressed as a unix epoch.

    Mirrors `day_start_utc(ts)` but kept inline for hot-path use (record_word).
    """
    dt = datetime.fromtimestamp(ts).astimezone()
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def record_word(conn: sqlite3.Connection, word: str, ts: int | None = None) -> None:
    """Increment count for `word` and bump its hourly + daily buckets."""
    if ts is None:
        ts = int(time.time())
    bucket = _hour_floor(ts)
    day_bucket = _day_floor(ts)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO word_counts(word, count, last_ts)
            VALUES (?, 1, ?)
            ON CONFLICT(word) DO UPDATE SET
                count = count + 1,
                last_ts = excluded.last_ts
            """,
            (word, ts),
        )
        conn.execute(
            """
            INSERT INTO hourly_buckets(hour_ts, words, typos)
            VALUES (?, 1, 0)
            ON CONFLICT(hour_ts) DO UPDATE SET words = words + 1
            """,
            (bucket,),
        )
        conn.execute(
            """
            INSERT INTO daily_word_counts(word, day_ts, count)
            VALUES (?, ?, 1)
            ON CONFLICT(word, day_ts) DO UPDATE SET count = count + 1
            """,
            (word, day_bucket),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def record_typo(
    conn: sqlite3.Connection,
    word: str,
    suggestion: str | None,
    ts: int | None = None,
) -> None:
    if ts is None:
        ts = int(time.time())
    bucket = _hour_floor(ts)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO typos(ts, word, suggestion) VALUES (?, ?, ?)",
            (ts, word, suggestion),
        )
        conn.execute(
            """
            INSERT INTO hourly_buckets(hour_ts, words, typos)
            VALUES (?, 0, 1)
            ON CONFLICT(hour_ts) DO UPDATE SET typos = typos + 1
            """,
            (bucket,),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def top_words(conn: sqlite3.Connection, limit: int = 25, since: int | None = None) -> list[dict]:
    if since is None:
        cur = conn.execute(
            "SELECT word, count FROM word_counts ORDER BY count DESC LIMIT ?",
            (limit,),
        )
    else:
        # "Since" filter is approximate — we filter by last_ts as a proxy.
        cur = conn.execute(
            """
            SELECT word, count FROM word_counts
            WHERE last_ts >= ?
            ORDER BY count DESC LIMIT ?
            """,
            (since, limit),
        )
    return [{"word": r[0], "count": r[1]} for r in cur]


def hourly_activity(conn: sqlite3.Connection, since: int) -> list[dict]:
    cur = conn.execute(
        """
        SELECT hour_ts, words, typos FROM hourly_buckets
        WHERE hour_ts >= ?
        ORDER BY hour_ts ASC
        """,
        (since,),
    )
    return [{"hour": r[0], "words": r[1], "typos": r[2]} for r in cur]


def recent_typos(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    cur = conn.execute(
        "SELECT ts, word, suggestion FROM typos ORDER BY ts DESC LIMIT ?",
        (limit,),
    )
    return [{"ts": r[0], "word": r[1], "suggestion": r[2]} for r in cur]


def totals(conn: sqlite3.Connection, since: int | None = None) -> dict:
    if since is None:
        words = conn.execute("SELECT COALESCE(SUM(count), 0) FROM word_counts").fetchone()[0]
        uniq = conn.execute("SELECT COUNT(*) FROM word_counts").fetchone()[0]
        typos = conn.execute("SELECT COUNT(*) FROM typos").fetchone()[0]
    else:
        words = conn.execute(
            "SELECT COALESCE(SUM(words), 0) FROM hourly_buckets WHERE hour_ts >= ?",
            (since,),
        ).fetchone()[0]
        uniq = conn.execute(
            "SELECT COUNT(*) FROM word_counts WHERE last_ts >= ?",
            (since,),
        ).fetchone()[0]
        typos = conn.execute(
            "SELECT COUNT(*) FROM typos WHERE ts >= ?",
            (since,),
        ).fetchone()[0]
    return {"words": int(words), "unique_words": int(uniq), "typos": int(typos)}


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def day_start_utc(now_ts: int | None = None) -> int:
    """Return the unix timestamp at the start of today (local time, expressed as UTC epoch)."""
    if now_ts is None:
        now_ts = int(time.time())
    return _day_floor(now_ts)


def week_start_utc(now_ts: int | None = None) -> int:
    """Start of this week (Monday 00:00 local time), expressed as a unix epoch."""
    if now_ts is None:
        now_ts = int(time.time())
    dt = datetime.fromtimestamp(now_ts).astimezone()
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    monday = midnight - timedelta(days=dt.weekday())
    return int(monday.timestamp())


def month_start_utc(now_ts: int | None = None) -> int:
    """Start of this calendar month (day 1, 00:00 local), expressed as a unix epoch."""
    if now_ts is None:
        now_ts = int(time.time())
    dt = datetime.fromtimestamp(now_ts).astimezone()
    first = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(first.timestamp())


def year_start_utc(now_ts: int | None = None) -> int:
    """Start of this calendar year (Jan 1, 00:00 local), expressed as a unix epoch."""
    if now_ts is None:
        now_ts = int(time.time())
    dt = datetime.fromtimestamp(now_ts).astimezone()
    first = dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(first.timestamp())


def top_words_in_period(
    conn: sqlite3.Connection, since: int, limit: int = 25,
) -> list[dict]:
    """Top words by count summed over per-day buckets with day_ts >= `since`.

    Only counts events recorded after the `daily_word_counts` table started
    being populated; older history lives only in `word_counts`. For "all-time"
    leaderboards, use `top_words(conn, limit=...)` instead.
    """
    cur = conn.execute(
        """
        SELECT word, SUM(count) AS c FROM daily_word_counts
        WHERE day_ts >= ?
        GROUP BY word
        ORDER BY c DESC
        LIMIT ?
        """,
        (since, limit),
    )
    return [{"word": r[0], "count": int(r[1])} for r in cur]


# --- custom words (user-whitelisted) ----------------------------------------

def list_custom_words(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        "SELECT word, added_ts FROM custom_words ORDER BY added_ts DESC"
    )
    return [{"word": r[0], "added_ts": r[1]} for r in cur]


def add_custom_word(conn: sqlite3.Connection, word: str, ts: int | None = None) -> bool:
    """Insert `word` into custom_words. Returns True if newly added, False if already present."""
    if ts is None:
        ts = int(time.time())
    cur = conn.execute(
        "INSERT OR IGNORE INTO custom_words(word, added_ts) VALUES (?, ?)",
        (word, ts),
    )
    return cur.rowcount > 0


def remove_custom_word(conn: sqlite3.Connection, word: str) -> bool:
    """Delete `word` from custom_words. Returns True if a row was removed."""
    cur = conn.execute("DELETE FROM custom_words WHERE word = ?", (word,))
    return cur.rowcount > 0


def delete_typos_for_word(conn: sqlite3.Connection, word: str) -> int:
    """Remove all `typos` rows whose `word` column matches. Returns count deleted.

    Used when whitelisting a typo: we don't want it to keep haunting the
    'recent typos' panel. We also do NOT touch hourly_buckets.typos counts
    — those reflect historical activity, not active misspellings.
    """
    cur = conn.execute("DELETE FROM typos WHERE word = ?", (word,))
    return cur.rowcount


def retract_typo(
    conn: sqlite3.Connection,
    ts: int,
    word: str,
    suggestion: str,
) -> bool:
    """Undo a record_word(suggestion) + record_typo(word, suggestion) pair.

    `ts` must match the unix timestamp used when the pair was recorded — this
    is how we identify the specific `typos` row to delete (handles the case
    of the same typo appearing more than once in the same second).

    Returns True iff the matching typo row existed and was removed. All four
    side effects (typos row, word_counts decrement, hourly_buckets.words and
    .typos decrements) happen together in a single transaction.
    """
    bucket = _hour_floor(ts)
    day_bucket = _day_floor(ts)
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Delete the specific typo row. Use LIMIT 1 in case the same word
        # was logged twice within one second.
        cur = conn.execute(
            "DELETE FROM typos WHERE rowid IN ("
            "SELECT rowid FROM typos WHERE ts = ? AND word = ? AND suggestion = ? LIMIT 1"
            ")",
            (ts, word, suggestion),
        )
        if cur.rowcount == 0:
            conn.execute("ROLLBACK")
            return False
        # Decrement the suggestion's count in word_counts. If it would drop
        # to zero, delete the row entirely so top-words doesn't show empties.
        conn.execute(
            "UPDATE word_counts SET count = count - 1 WHERE word = ? AND count > 0",
            (suggestion,),
        )
        conn.execute("DELETE FROM word_counts WHERE word = ? AND count <= 0", (suggestion,))
        # Decrement both halves of the hourly bucket.
        conn.execute(
            "UPDATE hourly_buckets SET words = MAX(words - 1, 0), typos = MAX(typos - 1, 0) "
            "WHERE hour_ts = ?",
            (bucket,),
        )
        # Decrement the per-day count for the suggestion. Drop empty rows so
        # period leaderboards stay clean.
        conn.execute(
            "UPDATE daily_word_counts SET count = count - 1 "
            "WHERE word = ? AND day_ts = ? AND count > 0",
            (suggestion, day_bucket),
        )
        conn.execute(
            "DELETE FROM daily_word_counts WHERE word = ? AND day_ts = ? AND count <= 0",
            (suggestion, day_bucket),
        )
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise
