import os
import sqlite3

from flask import g

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DATABASE_PATH") or os.path.join(BASE_DIR, "getitdone.db")


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    """One connection per request, closed in teardown."""
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        # Columns added after a database may already exist; CREATE TABLE IF NOT EXISTS won't add them.
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'users'").fetchone():
            cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
            if "feed_token" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN feed_token TEXT")
        with open(os.path.join(BASE_DIR, "schema.sql")) as f:
            conn.executescript(f.read())
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_feed_token ON users(feed_token)")
        conn.commit()
    finally:
        conn.close()
