"""One world, one notebook; evidence bookkeeping never interprets its domain."""
from __future__ import annotations

import json
import math
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .files import digest, dumps, inside, lock, redact


def terms(value):
    return set(re.findall(r"[\w.-]{3,}", str(value).lower()))


class Memory:
    def __init__(self, path, *, world_local=None):
        self.path = Path(path).resolve()
        self.world_local = Path(world_local).resolve() if world_local else None
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,state TEXT NOT NULL,updated REAL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,run TEXT,kind TEXT,data TEXT,created REAL);
            CREATE INDEX IF NOT EXISTS event_run ON events(run,id);
            CREATE TABLE IF NOT EXISTS facts(id INTEGER PRIMARY KEY,run TEXT,identity TEXT,outcome TEXT,kind TEXT,detail TEXT,
                UNIQUE(run,identity));
            CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,run TEXT,payload TEXT,response TEXT);
            CREATE TABLE IF NOT EXISTS lessons(id TEXT PRIMARY KEY,title TEXT,body TEXT,tags TEXT,procedure TEXT,
                support INTEGER DEFAULT 0,against INTEGER DEFAULT 0,importance REAL,touched INTEGER,uses INTEGER DEFAULT 0,
                legacy_support INTEGER DEFAULT 0,legacy_against INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS confirmations(lesson TEXT REFERENCES lessons(id) ON DELETE CASCADE,
                identity TEXT,verdict TEXT,detail TEXT,PRIMARY KEY(lesson,identity));
            CREATE TABLE IF NOT EXISTS trained(seed INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS run_archives(run TEXT PRIMARY KEY,complete INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS learning_reviews(id TEXT PRIMARY KEY,run TEXT,result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS lesson_changes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,created REAL NOT NULL,run TEXT,lesson TEXT NOT NULL,
                change TEXT NOT NULL,reason TEXT NOT NULL,before_json TEXT,after_json TEXT,details_json TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS lesson_changes_lesson ON lesson_changes(lesson,id);
            CREATE INDEX IF NOT EXISTS lesson_changes_run ON lesson_changes(run,id);
            INSERT OR IGNORE INTO run_archives SELECT id,0 FROM runs;
        """)
        self.db.commit()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(lessons)")}
            if "legacy_support" not in columns:
                self.db.execute("ALTER TABLE lessons ADD COLUMN legacy_support INTEGER DEFAULT 0")
                self.db.execute("ALTER TABLE lessons ADD COLUMN legacy_against INTEGER DEFAULT 0")
                # Old confirmations did not identify the claim they assessed.
                # Preserve the history, but do not assign its confidence to today's wording.
                self.db.execute("UPDATE lessons SET legacy_support=support,legacy_against=against,support=0,against=0")
            if self.get("claim_versions_initialized") is None:
                # Give reclassified candidates a normal review window before decay;
                # the migration itself must not immediately erase old experience.
                self.db.execute("UPDATE lessons SET touched=? WHERE legacy_support+legacy_against>0 AND support=0 AND against=0",
                                (self.get("epoch", 0),))
                self.db.execute("INSERT INTO meta VALUES ('claim_versions_initialized','1')")
            if self.get("lesson_history_started_at") is None:
                for item in self.lessons():
                    self._record_change(item["id"], "snapshot", "preexisting_state", after=item,
                        details={"note": "Initial snapshot; earlier changes are not reconstructed"})
                self.db.execute("INSERT INTO meta VALUES ('lesson_history_started_at',?)", (dumps(time.time()),))

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, dumps(value)))

    def save(self, state):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO run_archives VALUES (?,1)", (state["id"],))
            self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?)", (state["id"], dumps(state), time.time()))

    def runs(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT state FROM runs ORDER BY updated DESC")]

    def run(self, run_id=None):
        matches = [s for s in self.runs() if run_id is None or run_id in (s["id"], s.get("external_id"))]
        if not matches:
            raise ValueError("Saved run not found in selected world's memory")
        return matches[0]

    def event(self, run, kind, data):
        with self.db:
            cur = self.db.execute("INSERT INTO events(run,kind,data,created) VALUES (?,?,?,?)",
                (run, kind, dumps(redact(data)), time.time()))
        return cur.lastrowid

    def recent(self, run, limit=12):
        rows = self.db.execute("SELECT id,kind,data FROM events WHERE run=? ORDER BY id DESC LIMIT ?", (run, limit))
        return list(reversed([{"id": r[0], "kind": r[1], "data": json.loads(r[2])} for r in rows]))

    def archive_info(self, run, through_id=None):
        row = self.db.execute("SELECT complete FROM run_archives WHERE run=?", (run,)).fetchone()
        through_id = through_id if through_id is not None else self.db.execute(
            "SELECT coalesce(max(id),0) FROM events WHERE run=?", (run,)).fetchone()[0]
        groups = [dict(r) for r in self.db.execute(
            "SELECT kind,count(*) AS count,min(id) AS first_id,max(id) AS last_id FROM events WHERE run=? AND id<=? GROUP BY kind", (run, through_id))]
        # A compact, evenly spaced navigation index covers the entire retained run.
        ids = [r[0] for r in self.db.execute("SELECT id FROM events WHERE run=? AND id<=? ORDER BY id", (run, through_id))]
        stride = max(1, (len(ids)+31)//32)
        return {"source_id": "__local_events__", "complete_from_creation": bool(row and row[0]),
                "coverage_note": "Full event archive" if row and row[0] else
                    "Legacy history may have been trimmed; missing decisions cannot be reconstructed",
                "count": len(ids), "through_id": through_id, "kinds": groups,
                "navigation": [{"after_id": ids[i-1] if i else 0, "first_id": ids[i],
                                "last_id": ids[min(i+stride, len(ids))-1]} for i in range(0, len(ids), stride)],
                "parameters": "after_id: nonnegative event ID (default 0), limit: 1..50 (default 20), kind: optional exact event kind"}

    def archive_page(self, run, parameters, through_id=None):
        if set(parameters) - {"after_id", "limit", "kind"}:
            raise ValueError("Unknown local archive parameter")
        after, limit, kind = parameters.get("after_id", 0), parameters.get("limit", 20), parameters.get("kind")
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Invalid local archive cursor/limit")
        if kind is not None and (not isinstance(kind, str) or not 1 <= len(kind) <= 100):
            raise ValueError("Invalid archive event kind")
        through_id = through_id if through_id is not None else self.db.execute(
            "SELECT coalesce(max(id),0) FROM events WHERE run=?", (run,)).fetchone()[0]
        rows = self.db.execute("SELECT id,kind,data,created FROM events WHERE run=? AND id>? AND id<=?" +
            (" AND kind=?" if kind is not None else "") + " ORDER BY id LIMIT ?",
            (run, after, through_id, kind, limit+1) if kind is not None else (run, after, through_id, limit+1)).fetchall()
        events = [{**dict(r), "data": json.loads(r["data"])} for r in rows[:limit]]
        next_parameters = {**parameters, "after_id": events[-1]["id"]} if len(rows)>limit else None
        # Map existing facts, never re-import events as new evidence.
        identities = set()
        for event in events:
            data = event["data"]
            observation = data.get("response", {}) if event["kind"] == "outcome" else data
            identities.update(f["identity"] for f in observation.get("evidence", []) if "identity" in f)
        facts = self.facts_by_identity(run, identities)
        return {"events": events, "facts": facts, "next_parameters": next_parameters}

    def facts_by_identity(self, run, identities):
        return [dict(r) for identity in sorted(identities) for r in self.db.execute(
            "SELECT * FROM facts WHERE run=? AND identity=?", (run, identity))]

    def selected_facts(self, run, ids):
        return [dict(r) for fid in sorted(set(ids)) for r in self.db.execute(
            "SELECT * FROM facts WHERE run=? AND id=?", (run, fid))]

    def _lesson_snapshot(self, key):
        return next((x for x in self.lessons() if x["id"] == key), None)

    def _record_change(self, key, change, reason, *, run=None, before=None, after=None, details=None):
        """Caller owns the transaction: audit and mutation must commit together.

        No foreign keys or retention cleanup: deletion of a run/lesson cannot erase this journal.
        """
        self.db.execute("INSERT INTO lesson_changes(created,run,lesson,change,reason,before_json,after_json,details_json) VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), run, key, change, reason, dumps(redact(before)) if before is not None else None,
             dumps(redact(after)) if after is not None else None, dumps(redact(details or {}))))

    def _record_rejection(self, lesson, run, feedback):
        key = re.sub(r"[^\w.-]+", "-", lesson["key"].strip().lower())[:100]
        current = self._lesson_snapshot(key)
        self._record_change(key, "rejected", feedback["reason"], run=run, before=current, after=current,
            details={"proposal": lesson, "feedback": feedback,
                     "evidence": self.selected_facts(run, lesson["evidence_ids"])})

    def reject_lesson(self, lesson, run, feedback):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._record_rejection(lesson, run, feedback)
        self.learning_feedback = feedback

    def lesson_history(self, *, after_id=0, limit=50, lesson=None, run=None, change=None):
        if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("History requires after_id >= 0 and limit 1..1000")
        where, args = ["id> ?"], [after_id]
        for column, value in (("lesson", lesson), ("run", run), ("change", change)):
            if value is not None:
                where.append(column+"=?")
                args.append(value)
        rows = self.db.execute("SELECT * FROM lesson_changes WHERE "+" AND ".join(where)+" ORDER BY id LIMIT ?",
                               (*args, limit+1)).fetchall()
        entries = []
        for row in rows[:limit]:
            item = dict(row)
            item["created_at"] = datetime.fromtimestamp(item["created"], timezone.utc).isoformat()
            for field in ("before", "after", "details"):
                value = item.pop(field+"_json")
                item[field] = json.loads(value) if value is not None else None
            entries.append(item)
        return {"started_at": self.get("lesson_history_started_at"), "entries": entries,
                "next_after_id": entries[-1]["id"] if len(rows)>limit else None}

    def observe(self, run, response):
        # The adapter reports domain semantics. Re-polling an operation keeps the same identity.
        with self.db:
            for fact in response.get("evidence", []):
                self.db.execute("INSERT OR IGNORE INTO facts(run,identity,outcome,kind,detail) VALUES (?,?,?,?,?)",
                    (run, fact["identity"], fact["outcome"], fact["kind"],
                     (redact(fact["detail"]) if isinstance(fact["detail"], str) else dumps(redact(fact["detail"])))))
        self.event(run, "observation", response)

    def facts(self, run, limit=30):
        return [dict(r) for r in self.db.execute("SELECT * FROM facts WHERE run=? ORDER BY id DESC LIMIT ?", (run, limit))]

    def prepare(self, request_id, run, payload, replay_safe):
        row = self.db.execute("SELECT payload,response FROM requests WHERE id=?", (request_id,)).fetchone()
        if row:
            if json.loads(row[0]) != payload:
                raise ValueError("Idempotency conflict: request payload changed")
            if row[1] is not None:
                return json.loads(row[1])
            if not replay_safe:
                raise RuntimeError("Ambiguous non-idempotent action: reconcile its result before resuming")
            return None
        with self.db:
            self.db.execute("INSERT INTO requests VALUES (?,?,?,NULL)", (request_id, run, dumps(payload)))
        return None

    def received(self, request_id, response):
        with self.db:
            self.db.execute("UPDATE requests SET response=? WHERE id=?", (dumps(response), request_id))

    def learn(self, lesson, run, allowed_ids, *, review=None):
        ids = set(lesson["evidence_ids"]) & set(allowed_ids)
        cited = self.selected_facts(run, ids)
        facts = [f for f in cited if f["outcome"] == lesson["outcome"]]
        key = re.sub(r"[^\w.-]+", "-", lesson["key"].strip().lower())[:100]
        self.learning_feedback = {"key": key, "accepted": False,
            "reason": "no_matching_evidence",
            "cited_fact_outcomes": [{"id": f["id"], "outcome": f["outcome"]} for f in cited]}
        if not facts or not key:
            self.reject_lesson(lesson, run, self.learning_feedback)
            return None
        verdict = lesson["verdict"]
        epoch = self.get("epoch", 0)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            before = self._lesson_snapshot(key)
            exists = self.db.execute("SELECT id,body,procedure FROM lessons WHERE id=?", (key,)).fetchone()
            if not exists and verdict == "contradicts":
                self.learning_feedback["reason"] = "cannot_contradict_missing_lesson"
                self._record_rejection(lesson, run, self.learning_feedback)
                return None
            normalized = lambda text: " ".join(text.split())
            if exists and (normalized(exists["body"]) != normalized(lesson["body"][:2500]) or
                    [normalized(x) for x in json.loads(exists["procedure"])] !=
                    [normalized(x) for x in lesson["procedure"][:12]]):
                self.learning_feedback.update(reason="existing_claim_changed",
                    current_body=exists["body"], current_procedure=json.loads(exists["procedure"]))
                self._record_rejection(lesson, run, self.learning_feedback)
                return None
            self.db.execute("INSERT OR IGNORE INTO lessons(id,title,body,tags,procedure,importance,touched) VALUES (?,?,?,?,?,?,?)",
                (key, lesson["title"][:200], lesson["body"][:2500], dumps(lesson["tags"][:12]),
                 dumps(lesson["procedure"][:12]), min(1, max(0, lesson["importance"])), epoch))
            claim_hash = digest({"body": normalized(lesson["body"][:2500]),
                                 "procedure": [normalized(x) for x in lesson["procedure"][:12]]})
            fresh = 0
            for f in facts:
                identity = run + ":" + f["identity"]
                detail = {"fact": f, "explanation": lesson["explanation"][:1500], "claim_hash": claim_hash}
                previous = self.db.execute("SELECT verdict,detail FROM confirmations WHERE lesson=? AND identity=?",
                                           (key, identity)).fetchone()
                if previous and "claim_hash" not in json.loads(previous["detail"]):
                    # The model has now assessed this historical fact against the
                    # immutable current claim. Retain its original assessment as history.
                    detail["legacy_confirmation"] = {"verdict": previous["verdict"], "detail": json.loads(previous["detail"])}
                    self.db.execute("UPDATE confirmations SET verdict=?,detail=? WHERE lesson=? AND identity=?",
                                    (verdict, dumps(detail), key, identity))
                    fresh += 1
                    continue
                cur = self.db.execute("INSERT OR IGNORE INTO confirmations VALUES (?,?,?,?)",
                    (key, identity, verdict, dumps(detail)))
                fresh += cur.rowcount
            if not fresh:
                self.learning_feedback["reason"] = "evidence_already_counted_for_this_lesson"
                self._record_rejection(lesson, run, self.learning_feedback)
                return None
            # Five cited facts in one reflection count as ONE reinforcement.
            column = "support" if verdict == "supports" else "against"
            self.db.execute(f"UPDATE lessons SET {column}={column}+1,touched=? WHERE id=?", (epoch, key))
            self._record_change(key, "added" if before is None else ("reinforced" if verdict == "supports" else "contradicted"),
                "new_evidence", run=run, before=before, after=self._lesson_snapshot(key),
                details={"proposal": lesson, "evidence": facts, "review": review})
        self.learning_feedback = {"key": key, "accepted": True}
        return key

    def lessons(self):
        epoch = self.get("epoch", 0)
        rows = []
        for row in self.db.execute("SELECT * FROM lessons"):
            x = dict(row)
            x["tags"], x["procedure"] = json.loads(x["tags"]), json.loads(x["procedure"])
            x["confidence"] = (x["support"] + 1) / (x["support"] + x["against"] + 2)
            x["strength"] = round(x["confidence"] * math.pow(.5, max(0, epoch-x["touched"]) / (30+20*x["support"]+100*x["importance"])), 4)
            mature = x["support"] >= 3 and x["confidence"] >= .75
            x["level"] = ("skill" if x["procedure"] else "long_term") if mature else "candidate"
            x["evidence"] = [dict(r) for r in self.db.execute("SELECT identity,verdict,detail FROM confirmations WHERE lesson=? ORDER BY rowid DESC LIMIT 4", (x["id"],))]
            rows.append(x)
        return rows

    def recall(self, query, limit=8):
        query = terms(query)
        rows = self.lessons()
        for x in rows:
            x["score"] = 3 * len(query & terms(dumps([x["tags"], x["title"], x["body"]]))) + x["strength"] + x["importance"]
        return sorted(rows, key=lambda x: x["score"], reverse=True)[:limit]

    def used(self, ids):
        with self.db:
            for key in set(ids):
                self.db.execute("UPDATE lessons SET uses=uses+1 WHERE id=?", (key,))

    def maintain(self, run=None):
        forgotten = []
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("INSERT INTO meta VALUES ('epoch','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")
            rows = sorted(self.lessons(), key=lambda x: x["strength"]+.3*x["importance"], reverse=True)
            for i, item in enumerate(rows):
                reasons = []
                if i >= 80:
                    reasons.append("capacity_limit")
                if item["strength"] < .12 and item["importance"] < .9:
                    reasons.append("weak_and_low_importance")
                if not reasons:
                    continue
                forgotten.append(item["id"])
                self._record_change(item["id"], "deleted", "+".join(reasons), run=run, before=item,
                    details={"epoch": self.get("epoch", 0), "rank": i+1, "capacity_limit": 80,
                             "strength_threshold": .12, "importance_threshold": .9,
                             "confirmations": [dict(r) for r in self.db.execute(
                                 "SELECT * FROM confirmations WHERE lesson=?", (item["id"],))]})
                self.db.execute("DELETE FROM lessons WHERE id=?", (item["id"],))
            self.db.execute("DELETE FROM confirmations WHERE identity NOT IN (SELECT run || ':' || identity FROM facts) AND rowid NOT IN (SELECT rowid FROM confirmations ORDER BY rowid DESC LIMIT 24000)")
            summary = {"epoch": self.get("epoch"), "last_forgotten": forgotten,
                       "total": self.get("forgetting", {}).get("total", 0)+len(forgotten)}
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('forgetting',?)", (dumps(summary),))
        self.trim()
        return forgotten

    def trim(self):
        states = self.runs()
        completed = [s for s in states if s.get("status") == "finished" and
                     s.get("review_checkpoint", {}).get("status") == "reviewed"]
        for s in completed[100:]:
            if self.world_local:
                folder = inside(self.world_local, "runs/" + s["id"])
                if folder.exists():
                    try:
                        with lock(folder / "run.lock"):
                            shutil.rmtree(folder)
                    except ValueError:
                        continue
            with self.db:
                for table in ("events", "facts", "requests", "run_archives", "learning_reviews"):
                    self.db.execute(f"DELETE FROM {table} WHERE run=?", (s["id"],))
                self.db.execute("DELETE FROM runs WHERE id=?", (s["id"],))
        with self.db:
            for s in self.runs():
                if s.get("status") == "finished":
                    self.db.execute("DELETE FROM requests WHERE run=?", (s["id"],))
            # Keep the active request queue even during very long runs, but bound
            # completed acknowledgements. Queue replay uses the same external ID.
            protected = {item["request_id"] for s in states for item in s.get("queue", [])[s.get("queue_index", 0):]}
            protected |= {s["id"]+"-bootstrap" for s in states if not s.get("bootstrapped")}
            for row in self.db.execute("SELECT id FROM requests WHERE response IS NOT NULL ORDER BY rowid DESC LIMIT -1 OFFSET 2000").fetchall():
                if row[0] not in protected:
                    self.db.execute("DELETE FROM requests WHERE id=?", (row[0],))

    def freeze_into(self, target):
        # No run history, working plans, private bootstrap or facts enter a new evaluation arm.
        with target.db:
            for row in self.db.execute("SELECT * FROM lessons"):
                values = dict(row)
                values["uses"] = 0
                target.db.execute("INSERT INTO lessons(" + ",".join(values) + ") VALUES (" + ",".join("?" for _ in values) + ")",
                                  tuple(values.values()))
            for row in self.db.execute("SELECT * FROM confirmations"):
                target.db.execute("INSERT INTO confirmations VALUES (?,?,?,?)", tuple(row))
            target.db.execute("INSERT OR REPLACE INTO meta VALUES ('epoch',?)", (dumps(self.get("epoch", 0)),))
            for item in target.lessons():
                target._record_change(item["id"], "imported", "frozen_experience", after=item)

    def snapshot(self):
        lessons = self.lessons()
        tags = {}
        for x in lessons:
            for tag in x["tags"]:
                tags[tag] = tags.get(tag, 0) + x["support"]
        return {"lessons": lessons, "tags": tags, "forgetting": self.get("forgetting", {}),
                "limits": {"lessons": 80, "events_per_run": None, "facts_per_run": None, "reviewed_completed_runs": 100}}
