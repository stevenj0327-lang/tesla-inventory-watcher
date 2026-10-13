"""Durable availability episodes and an email outbox in one SQLite transaction."""
import hashlib
import json
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS vehicles(
                vin TEXT PRIMARY KEY, episode INTEGER NOT NULL, missing INTEGER NOT NULL,
                active INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox(
                id TEXT PRIMARY KEY, vin TEXT, subject TEXT NOT NULL, body TEXT NOT NULL,
                created REAL NOT NULL, first_attempt REAL, next_attempt REAL NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
                provider_id TEXT);
        """)
        with self.db:
            self.db.execute("UPDATE outbox SET status='cancelled' WHERE vin IS NULL AND status='pending'")

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def enqueue(self, key, subject, body, now, vin=None):
        self.db.execute("INSERT OR IGNORE INTO outbox(id,vin,subject,body,created) VALUES(?,?,?,?,?)",
                        (key, vin, subject, body, now))

    def success(self, matches, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.put("last_attempt", now)
            self.put("last_success", now)
            self.put("failures", 0)
            self.put("last_error", None)
            for row in self.db.execute("SELECT * FROM vehicles").fetchall():
                if row["vin"] not in matches:
                    missing = row["missing"] + 1
                    self.db.execute("UPDATE vehicles SET missing=?,active=? WHERE vin=?",
                                    (missing, int(missing < 2 and row["active"]), row["vin"]))
                    # Do not email a stale match that disappeared before mail could be sent.
                    self.db.execute("UPDATE outbox SET status='cancelled' WHERE vin=? AND status='pending'",
                                    (row["vin"],))
            for vin, car in matches.items():
                previous = self.db.execute("SELECT * FROM vehicles WHERE vin=?", (vin,)).fetchone()
                episode = previous["episode"] if previous else 0
                is_new = previous is None or not previous["active"]
                if is_new:
                    episode += 1
                    title = "Tesla match: %s %s — $%s" % (car["model"], car["paint"], format(car["price"], ",.0f"))
                    seen = datetime.fromtimestamp(now, ZoneInfo("America/Los_Angeles")).strftime("%b %d, %Y %I:%M %p %Z")
                    body = ("%s %s\n%s | %s | %s\nListed cash price: $%s\n%s\n%s\n"
                            "Vehicle: %s\nChecked: %s\n\n%s\n\n"
                            "New, non-demo; no priced optional upgrades except requested Model Y paint. "
                            "Review Tesla's listing for final price, fees, delivery eligibility and availability. "
                            "This alert does not reserve the vehicle.\n") % (
                                car["model"], car["trim"], car["paint"], car["wheels"], car["interior"],
                                format(car["price"], ",.0f"), car["area"], car["location"] or "Location not listed",
                                vin, seen, car["url"])
                    self.enqueue("match:%s:%s" % (vin, episode), title, body, now, vin)
                elif previous["missing"]:
                    # A brief disappearance can cancel an unsent email, but must not lose it.
                    self.db.execute("UPDATE outbox SET status='pending' WHERE id=? AND status='cancelled'",
                                    ("match:%s:%s" % (vin, episode),))
                self.db.execute("INSERT OR REPLACE INTO vehicles VALUES(?,?,?,?,?)",
                                (vin, episode, 0, 1, json.dumps(car)))
            self.put("matches", len(matches))

    def failure(self, reason, now=None):
        now = time.time() if now is None else now
        with self.db:
            failures = self.get("failures", 0) + 1
            self.put("failures", failures)
            self.put("last_attempt", now)
            self.put("last_error", reason)

    def pending(self, include_matches=True, now=None):
        now = time.time() if now is None else now
        return self.db.execute("SELECT * FROM outbox WHERE status='pending' AND next_attempt<=? "
                               "AND ? AND vin IS NOT NULL ORDER BY created LIMIT 20", (now, include_matches)).fetchall()

    def attempt(self, key, now=None):
        now = time.time() if now is None else now
        with self.db:
            row = self.db.execute("SELECT first_attempt FROM outbox WHERE id=?", (key,)).fetchone()
            if row[0] is not None and now - row[0] >= 23 * 3600:
                # Resend's idempotency window is 24h. Never retry an uncertain send after it expires.
                self.db.execute("UPDATE outbox SET status='needs_review' WHERE id=?", (key,))
                self.put("mail_error", "An email's retry window expired; review provider logs")
                return False
            self.db.execute("UPDATE outbox SET first_attempt=COALESCE(first_attempt,?),attempts=attempts+1 WHERE id=?",
                            (now, key))
            return True

    def delivered(self, key, provider_id):
        with self.db:
            self.db.execute("UPDATE outbox SET status='accepted',provider_id=? WHERE id=?", (provider_id, key))
            self.put("mail_error", None)

    def mail_failed(self, key, reason, now=None):
        now = time.time() if now is None else now
        with self.db:
            attempts = self.db.execute("SELECT attempts FROM outbox WHERE id=?", (key,)).fetchone()[0]
            self.db.execute("UPDATE outbox SET next_attempt=? WHERE id=?", (now + min(900, 30 * 2 ** min(attempts, 5)), key))
            self.put("mail_error", reason)

    def summary(self):
        return {key: self.get(key) for key in ("last_attempt", "last_success", "matches", "failures", "last_error", "mail_error")}


def idempotency_key(event_id):
    return "tesla/" + hashlib.sha256(event_id.encode()).hexdigest()
