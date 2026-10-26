import contextlib
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '', name TEXT NOT NULL,
 balance INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0), banned INTEGER NOT NULL DEFAULT 0,
 referrer_id INTEGER REFERENCES users(id), trial_used INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT '', state_data TEXT NOT NULL DEFAULT '{}',
 terms_at INTEGER, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS servers (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, location TEXT NOT NULL,
 url TEXT NOT NULL, credentials TEXT NOT NULL, inbound_id INTEGER NOT NULL,
 subscription_url TEXT NOT NULL, capacity INTEGER NOT NULL DEFAULT 1000,
 active INTEGER NOT NULL DEFAULT 1, trial INTEGER NOT NULL DEFAULT 1,
 created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
 id INTEGER PRIMARY KEY, server_id INTEGER NOT NULL REFERENCES servers(id),
 name TEXT NOT NULL, days INTEGER NOT NULL CHECK(days > 0),
 volume_gb INTEGER NOT NULL CHECK(volume_gb > 0), price INTEGER NOT NULL CHECK(price > 0),
 ip_limit INTEGER NOT NULL DEFAULT 2, active INTEGER NOT NULL DEFAULT 1,
 description TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS coupons (
 code TEXT PRIMARY KEY, percent INTEGER NOT NULL CHECK(percent BETWEEN 1 AND 100),
 max_uses INTEGER NOT NULL, expires_at INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS services (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
 plan_id INTEGER REFERENCES plans(id), server_id INTEGER NOT NULL REFERENCES servers(id),
 email TEXT NOT NULL UNIQUE, client_json TEXT NOT NULL, sub_id TEXT NOT NULL UNIQUE,
 expires_at INTEGER NOT NULL, total_bytes INTEGER NOT NULL, used_bytes INTEGER NOT NULL DEFAULT 0,
 enabled INTEGER NOT NULL DEFAULT 1, is_trial INTEGER NOT NULL DEFAULT 0,
 created_at INTEGER NOT NULL, synced_at INTEGER
);
CREATE TABLE IF NOT EXISTS orders (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
 plan_id INTEGER REFERENCES plans(id), server_id INTEGER NOT NULL REFERENCES servers(id),
 service_id TEXT REFERENCES services(id), kind TEXT NOT NULL DEFAULT 'purchase',
 price INTEGER NOT NULL CHECK(price >= 0), currency TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', coupon TEXT REFERENCES coupons(code),
 snapshot TEXT NOT NULL, target_json TEXT, payment_method TEXT,
 charge_id TEXT UNIQUE, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
 lease_until INTEGER NOT NULL DEFAULT 0, next_attempt INTEGER NOT NULL DEFAULT 0,
 created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, paid_at INTEGER,
 completed_at INTEGER
);
CREATE INDEX IF NOT EXISTS orders_queue ON orders(status, next_attempt, lease_until);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_renewal ON orders(service_id)
 WHERE kind='renew' AND status IN ('pending','paid','processing','failed','refund_pending');
CREATE TABLE IF NOT EXISTS ledger (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), amount INTEGER NOT NULL,
 kind TEXT NOT NULL, reference TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
 charge_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
 user_id INTEGER NOT NULL REFERENCES users(id), amount INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'settled', created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS referral_rewards (
 referred_id INTEGER PRIMARY KEY REFERENCES users(id), order_id TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
 amount INTEGER NOT NULL CHECK(amount > 0), file_id TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL DEFAULT 'pending', reviewed_by TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
 subject TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_messages (
 id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
 sender TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS gifts (
 code TEXT PRIMARY KEY, amount INTEGER NOT NULL CHECK(amount > 0),
 max_uses INTEGER NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS gift_claims (
 code TEXT REFERENCES gifts(code), user_id INTEGER REFERENCES users(id),
 PRIMARY KEY(code,user_id)
);
CREATE TABLE IF NOT EXISTS outbox (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, body TEXT NOT NULL,
 dedupe TEXT NOT NULL UNIQUE, attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt INTEGER NOT NULL DEFAULT 0, sent_at INTEGER, last_error TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
 entity TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_limits (
 key TEXT PRIMARY KEY, hits INTEGER NOT NULL, reset_at INTEGER NOT NULL
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=15000")
        return con

    def initialize(self):
        with contextlib.closing(self.connect()) as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)
            con.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version','1')")
            con.commit()
            version = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
            if version != "1":
                raise RuntimeError("Unsupported database schema")

    @contextlib.contextmanager
    def transaction(self):
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def all(self, sql, args=()):
        with contextlib.closing(self.connect()) as con:
            return [dict(row) for row in con.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def backup(self, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self.connect()) as source:
            with contextlib.closing(sqlite3.connect(destination)) as dest:
                source.backup(dest)
