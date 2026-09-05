"""One SQLite notebook: passport, working state, episodes, reinforced lessons."""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from pathlib import Path


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def terms(value):
    return set(re.findall(r"[\w.-]{3,}", str(value).lower()))


class Memory:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS core(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, state TEXT NOT NULL, updated REAL);
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY, run TEXT, kind TEXT, data TEXT, created REAL);
            CREATE INDEX IF NOT EXISTS event_run ON events(run,id);
            CREATE TABLE IF NOT EXISTS requests(
                id TEXT PRIMARY KEY, run TEXT, payload TEXT, response TEXT);
            CREATE TABLE IF NOT EXISTS lessons(
                id TEXT PRIMARY KEY, title TEXT, body TEXT, tags TEXT, procedure TEXT,
                support INTEGER DEFAULT 0, against INTEGER DEFAULT 0,
                importance REAL, touched INTEGER, uses INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS evidence(
                lesson TEXT REFERENCES lessons(id) ON DELETE CASCADE,
                event INTEGER, run TEXT, verdict TEXT, explanation TEXT,
                PRIMARY KEY(lesson,event));
            CREATE TABLE IF NOT EXISTS confirmations(
                lesson TEXT REFERENCES lessons(id) ON DELETE CASCADE,
                identity TEXT, PRIMARY KEY(lesson,identity));
            CREATE TABLE IF NOT EXISTS training_seeds(seed INTEGER PRIMARY KEY);
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def put(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO core VALUES (?,?)", (key, dumps(value)))

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM core WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def save_run(self, run, state):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?)", (run, dumps(state), time.time()))

    def run(self, run=None):
        row = self.db.execute("SELECT state FROM runs WHERE id=?", (run,)).fetchone() if run else self.db.execute(
            "SELECT state FROM runs ORDER BY updated DESC LIMIT 1").fetchone()
        if not row:
            raise ValueError("Нет сохранённого запуска. Сначала выполните run --seed 42.")
        return json.loads(row[0])

    def runs(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT state FROM runs ORDER BY updated DESC")]

    def event(self, run, kind, data):
        with self.db:
            cur = self.db.execute("INSERT INTO events(run,kind,data,created) VALUES (?,?,?,?)",
                                  (run, kind, dumps(data), time.time()))
        return cur.lastrowid

    def recent(self, run, limit=12):
        rows = self.db.execute("SELECT id,kind,data FROM events WHERE run=? ORDER BY id DESC LIMIT ?", (run, limit))
        return list(reversed([dict(id=r[0], kind=r[1], data=json.loads(r[2])) for r in rows]))

    def prepare(self, request_id, run, payload):
        """Write ahead: even a lost response can be replayed with the same identity."""
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO requests VALUES (?,?,?,NULL)",
                            (request_id, run, dumps(payload)))
        row = self.db.execute("SELECT payload,response FROM requests WHERE id=?", (request_id,)).fetchone()
        if json.loads(row[0]) != payload:
            raise ValueError("Local idempotency conflict")
        return json.loads(row[1]) if row[1] is not None else None

    def received(self, request_id, response):
        with self.db:
            self.db.execute("UPDATE requests SET response=? WHERE id=?", (dumps(response), request_id))

    def learn(self, lesson, run, allowed_ids):
        """LLM interprets evidence; deterministic bookkeeping prevents fake repetition."""
        evidence_ids = set(lesson.get("evidence_ids", [])) & set(allowed_ids)
        verdict = lesson.get("verdict")
        if not evidence_ids or verdict not in ("supports", "contradicts"):
            return None
        key = re.sub(r"[^\w.-]+", "-", lesson["key"].strip().lower())[:100]
        if not key:
            return None
        existing = self.db.execute("SELECT id FROM lessons WHERE id=?", (key,)).fetchone()
        if not existing and verdict == "contradicts":
            return None
        # A claim may refer only to this run's actual action/observation episodes.
        verified = []
        identities = []
        for eid in evidence_ids:
            row = self.db.execute("SELECT kind,data FROM events WHERE id=? AND run=?", (eid, run)).fetchone()
            if not row:
                continue
            data = json.loads(row[1])
            if row[0] == "outcome":
                response = data.get("response", {})
                # Read-only polling and 202 acceptance are NOT successful execution evidence.
                if response.get("operation_id") and response.get("status") != "succeeded":
                    continue
                status = response.get("http_status", 0)
                failed = status >= 400 or bool(response.get("error"))
                if verdict == "supports" and lesson.get("outcome", "success") == "success" and (status not in (200, 201) or failed):
                    continue
                if lesson.get("outcome") == "failure" and not failed:
                    continue
                action = data.get("action", {})
                if action.get("kind") == "read":
                    continue
                verified.append(eid)
                identities.append(run + ":" + data.get("request_id", str(eid)))
            elif row[0] == "observation":
                # Completed async operations are strong evidence, keyed by operation, not poll.
                for op in data.get("operations", []):
                    expected = "failed" if lesson.get("outcome") == "failure" else "succeeded"
                    if op.get("status") == expected:
                        verified.append(eid)
                        identities.append(run + ":operation:" + op["operation_id"])
                if verdict == "contradicts" or lesson.get("outcome") == "failure":
                    # A control command can succeed yet make the service worse.
                    # Recognize actual request failures, not only HTTP errors of the panel.
                    errors = data.get("recent_errors", data.get("recent_logs", {}))
                    for record in errors.get("last_records", []):
                        if record.get("error") and record.get("request_id"):
                            verified.append(eid)
                            identities.append(run + ":failure:" + record["request_id"])
            elif row[0] == "completed":
                verified.append(eid)
                identities.append(run + ":completed")
        if not verified:
            return None
        epoch = self.get("epoch", 0)
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO lessons
                (id,title,body,tags,procedure,importance,touched) VALUES (?,?,?,?,?,?,?)""",
                (key, lesson["title"][:200], lesson["body"][:2000], dumps(lesson["tags"][:12]),
                 dumps(lesson.get("procedure", [])[:12]), min(1, max(0, lesson["importance"])), epoch))
            # One reflection is one confirmation, even if it cites five log lines.
            # Distinct request/operation identity survives multiple reflections and polls.
            fresh = []
            for identity in identities:
                cur = self.db.execute("INSERT OR IGNORE INTO confirmations VALUES (?,?)", (key, identity))
                if cur.rowcount:
                    fresh.append(identity)
            if not fresh:
                return None
            eid = max(verified)
            cur = self.db.execute("INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?)",
                (key, eid, run, verdict, lesson["explanation"][:1500]))
            if cur.rowcount:
                col = "support" if verdict == "supports" else "against"
                self.db.execute(f"UPDATE lessons SET {col}={col}+1,touched=? WHERE id=?", (epoch, key))
                self.db.execute("UPDATE lessons SET title=?,body=?,procedure=?,tags=? WHERE id=?", (
                    lesson["title"][:200], lesson["body"][:2000],
                    dumps(lesson["procedure"][:12]) if lesson.get("procedure") else self.db.execute("SELECT procedure FROM lessons WHERE id=?", (key,)).fetchone()[0],
                    dumps(lesson["tags"][:12]), key))
        return key

    def lesson_rows(self):
        epoch = self.get("epoch", 0)
        rows = []
        for r in self.db.execute("SELECT * FROM lessons"):
            x = dict(r)
            x["tags"], x["procedure"] = json.loads(x["tags"]), json.loads(x["procedure"])
            x["confidence"] = (x["support"] + 1) / (x["support"] + x["against"] + 2)
            half_life = 30 + 20 * x["support"] + 100 * x["importance"]
            x["strength"] = round(x["confidence"] * math.pow(0.5, max(0, epoch-x["touched"])/half_life), 4)
            x["level"] = "skill" if x["procedure"] and x["support"] >= 3 and x["confidence"] >= .75 else (
                "long_term" if x["support"] >= 3 and x["confidence"] >= .75 else "candidate")
            x["evidence"] = [dict(e) for e in self.db.execute(
                "SELECT event,run,verdict,explanation FROM evidence WHERE lesson=? ORDER BY event DESC LIMIT 4", (x["id"],))]
            rows.append(x)
        return rows

    def recall(self, query, limit=8):
        query_terms = terms(query)
        rows = self.lesson_rows()
        for x in rows:
            overlap = len(query_terms & terms(dumps([x["tags"], x["title"], x["body"]])))
            x["relevance"] = overlap
            x["score"] = overlap * 3 + x["strength"] + x["importance"]
        # Include one important general lesson, plus situation-specific memories.
        return sorted(rows, key=lambda x: x["score"], reverse=True)[:limit]

    def used(self, ids):
        # Recalling is not evidence and does NOT boost confidence or reset decay.
        with self.db:
            for mid in set(ids):
                self.db.execute("UPDATE lessons SET uses=uses+1 WHERE id=?", (mid,))

    def maintain(self):
        with self.db:
            epoch = int(self.db.execute("""INSERT INTO core(key,value) VALUES ('epoch','1')
                ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1 RETURNING value""").fetchone()[0])
        rows = self.lesson_rows()
        ranked = sorted(rows, key=lambda x: x["strength"] + .3*x["importance"], reverse=True)
        forget = [x for i,x in enumerate(ranked) if i >= 80 or (x["strength"] < .12 and x["importance"] < .9)]
        with self.db:
            for x in forget:
                self.db.execute("DELETE FROM lessons WHERE id=?", (x["id"],))
            for x in ranked:
                self.db.execute("DELETE FROM evidence WHERE lesson=? AND rowid NOT IN (SELECT rowid FROM evidence WHERE lesson=? ORDER BY event DESC LIMIT 8)", (x["id"], x["id"]))
                self.db.execute("DELETE FROM confirmations WHERE lesson=? AND rowid NOT IN (SELECT rowid FROM confirmations WHERE lesson=? ORDER BY rowid DESC LIMIT 300)", (x["id"], x["id"]))
        self.trim_history()
        self.put("forgetting", {"epoch": epoch, "last_forgotten": [x["id"] for x in forget],
                              "total": self.get("forgetting", {}).get("total", 0) + len(forget)})
        return [x["id"] for x in forget]

    def trim_history(self):
        """Bound checkpoints and journals without changing frozen evaluation lessons."""
        with self.db:
            # Active runs keep their write-ahead journal and recent episodes.
            for state in self.runs():
                run = state["local_id"]
                self.db.execute("DELETE FROM events WHERE run=? AND id NOT IN (SELECT id FROM events WHERE run=? ORDER BY id DESC LIMIT 150)", (run, run))
            done = [s for s in self.runs() if s.get("status") in ("completed", "failed")]
            for state in done:
                self.db.execute("DELETE FROM requests WHERE run=?", (state["local_id"],))
            for state in done[100:]:
                self.db.execute("DELETE FROM events WHERE run=?", (state["local_id"],))
                self.db.execute("DELETE FROM runs WHERE id=?", (state["local_id"],))
            # A long interrupted run cannot grow an unlimited journal of completed calls.
            self.db.execute("DELETE FROM requests WHERE response IS NOT NULL AND id NOT IN (SELECT id FROM requests ORDER BY rowid DESC LIMIT 2000)")

    def snapshot(self):
        lessons = sorted(self.lesson_rows(), key=lambda x: (x["uses"],x["support"]), reverse=True)
        clouds = {}
        for x in lessons:
            for tag in x["tags"]:
                clouds[tag] = clouds.get(tag, 0) + x["support"]
        return {"lessons": lessons, "tags": clouds, "forgetting": self.get("forgetting", {}),
                "limits": {"lessons": 80, "episodes_per_run": 150, "completed_runs": 100}}
