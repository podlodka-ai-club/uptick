import tempfile
import unittest
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path

from ak2_agent.memory import Memory


def lesson(ids, verdict="supports", outcome="success"):
    return {"key": "check-before-action", "title": "Проверять условие", "body": "При сигнале проверь состояние перед действием.",
        "tags": ["signal"], "procedure": ["Check", "Act", "Verify"], "importance": .4,
        "evidence_ids": ids, "verdict": verdict, "outcome": outcome, "explanation": "Observed outcome"}


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = Memory(Path(self.tmp.name)/"memory.sqlite3")

    def tearDown(self):
        self.m.close()
        self.tmp.cleanup()

    def fact(self, identity="op1", run="r", outcome="success"):
        self.m.observe(run, {"evidence": [{"identity": identity, "outcome": outcome, "kind": "operation", "detail": "Finished"}]})
        return self.m.facts(run)[0]["id"]

    def test_only_new_completed_evidence_reinforces(self):
        eid = self.fact()
        self.assertIsNotNone(self.m.learn(lesson([eid]), "r", [eid]))
        self.fact()
        self.assertIsNone(self.m.learn(lesson([eid]), "r", [eid]))
        self.assertEqual(self.m.lessons()[0]["support"], 1)
        self.assertEqual(len(self.m.facts("r")), 1)

    def test_cross_run_and_unseen_evidence_rejected(self):
        eid = self.fact(run="other")
        self.assertIsNone(self.m.learn(lesson([eid]), "r", [eid]))
        self.assertIsNone(self.m.learn(lesson([eid]), "other", []))
        self.assertEqual(self.m.lessons(), [])

    def test_failures_cannot_prove_success(self):
        eid = self.fact(outcome="failure")
        self.assertIsNone(self.m.learn(lesson([eid]), "r", [eid]))
        self.assertEqual(self.m.learning_feedback["reason"], "no_matching_evidence")
        self.assertEqual(self.m.learning_feedback["cited_fact_outcomes"], [{"id":eid,"outcome":"failure"}])
        self.assertIsNotNone(self.m.learn(lesson([eid], outcome="failure"), "r", [eid]))

    def test_successful_action_can_contradict_a_lesson_without_relabeling_its_fact(self):
        eid = self.fact()
        self.m.learn(lesson([eid]),"r",[eid])
        later = self.fact("different-result")
        self.assertIsNone(self.m.learn(lesson([later],"contradicts","failure"),"r",[later]))
        self.assertEqual(self.m.learning_feedback["cited_fact_outcomes"],[{"id":later,"outcome":"success"}])
        self.assertIsNotNone(self.m.learn(lesson([later],"contradicts","success"),"r",[later]))
        self.assertEqual(self.m.lessons()[0]["against"],1)
        self.assertTrue(self.m.learning_feedback["accepted"])

    def test_structured_evidence_is_normalized_without_losing_fields(self):
        self.m.observe("r", {"evidence": [{"identity":"x", "outcome":"success", "kind":"action", "detail":{"measured":7}}]})
        self.assertEqual(self.m.facts("r")[0]["detail"], '{"measured":7}')

    def test_several_facts_are_one_confirmation(self):
        ids = [self.fact("one"), self.fact("two")]
        self.m.learn(lesson(ids), "r", ids)
        self.assertEqual(self.m.lessons()[0]["support"], 1)

    def test_repetition_promotes_and_contradiction_demotes(self):
        for i in range(3):
            eid = self.fact(str(i))
            self.m.learn(lesson([eid]), "r", [eid])
        self.assertEqual(self.m.lessons()[0]["level"], "skill")
        eid = self.fact("failed", outcome="failure")
        self.m.learn(lesson([eid], "contradicts", "failure"), "r", [eid])
        self.assertEqual(self.m.lessons()[0]["level"], "candidate")
        self.assertEqual(self.m.lessons()[0]["against"], 1)

    def test_revised_claim_cannot_inherit_confidence_from_old_claim(self):
        for i in range(3):
            eid=self.fact(str(i));self.m.learn(lesson([eid]),"r",[eid])
        eid=self.fact("new-condition")
        changed=lesson([eid]);changed["body"]="A different conditional claim."
        self.assertIsNone(self.m.learn(changed,"r",[eid]))
        self.assertEqual(self.m.learning_feedback["reason"],"existing_claim_changed")
        self.assertEqual(self.m.lessons()[0]["body"],lesson([])["body"])
        changed["key"]="revised-claim"
        self.assertIsNotNone(self.m.learn(changed,"r",[eid]))
        rows={x["id"]:x for x in self.m.lessons()}
        self.assertEqual(rows["check-before-action"]["support"],3)
        self.assertEqual(rows["revised-claim"]["support"],1)
        self.assertEqual(rows["revised-claim"]["level"],"candidate")

    def test_recall_does_not_rehearse_and_weak_lessons_are_forgotten(self):
        eid = self.fact()
        self.m.learn(lesson([eid]), "r", [eid])
        before = self.m.lessons()[0]
        self.m.recall("signal")
        self.m.used([before["id"]])
        after = self.m.lessons()[0]
        self.assertEqual(before["strength"], after["strength"])
        self.assertEqual(before["support"], after["support"])
        self.m.put("epoch", 5000)
        self.assertEqual(self.m.maintain(), [before["id"]])
        self.assertEqual(self.m.lessons(), [])

    def test_restart_and_freeze_preserve_experience_only(self):
        eid = self.fact()
        self.m.learn(lesson([eid]), "r", [eid])
        self.m.save({"id": "r", "status": "running", "plan": "private-run-plan"})
        self.m.close()
        self.m = Memory(Path(self.tmp.name)/"memory.sqlite3")
        other = Memory(Path(self.tmp.name)/"frozen.sqlite3")
        try:
            self.m.freeze_into(other)
            self.assertEqual(len(other.lessons()), 1)
            self.assertEqual(other.runs(), [])
            self.assertEqual(other.facts("r"), [])
        finally:
            other.close()

    def legacy_database(self):
        path = Path(self.tmp.name)/"legacy.sqlite3"
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE lessons(id TEXT PRIMARY KEY,title TEXT,body TEXT,tags TEXT,procedure TEXT,
                    support INTEGER DEFAULT 0,against INTEGER DEFAULT 0,importance REAL,touched INTEGER,uses INTEGER DEFAULT 0);
                CREATE TABLE confirmations(lesson TEXT REFERENCES lessons(id) ON DELETE CASCADE,
                    identity TEXT,verdict TEXT,detail TEXT,PRIMARY KEY(lesson,identity));
                CREATE TABLE facts(id INTEGER PRIMARY KEY,run TEXT,identity TEXT,outcome TEXT,kind TEXT,detail TEXT,
                    UNIQUE(run,identity));
                CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT INTO meta VALUES ('epoch','5000');
            """)
            candidate = lesson([])
            db.execute("INSERT INTO lessons VALUES (?,?,?,?,?,?,?,?,?,?)", (candidate["key"],candidate["title"],
                candidate["body"],json.dumps(candidate["tags"]),json.dumps(candidate["procedure"]),3,1,.4,0,8))
            fact = {"id":1,"run":"r","identity":"old-result","outcome":"success","kind":"effect","detail":"Observed before wording changed"}
            db.execute("INSERT INTO facts VALUES (?,?,?,?,?,?)", tuple(fact.values()))
            db.execute("INSERT INTO confirmations VALUES (?,?,?,?)", (candidate["key"],"r:old-result","supports",
                json.dumps({"fact":fact,"explanation":"Historical assessment"})))
        return path

    def test_legacy_confidence_is_preserved_as_history_across_restart_and_freeze(self):
        path = self.legacy_database()
        for _ in range(2):
            migrated = Memory(path)
            try:
                row = migrated.lessons()[0]
                self.assertEqual((row["support"], row["against"]), (0,0))
                self.assertEqual((row["legacy_support"], row["legacy_against"]), (3,1))
                self.assertEqual(row["body"], lesson([])["body"])
                self.assertEqual(row["level"], "candidate")
                self.assertEqual(row["confidence"], .5)
                self.assertGreater(row["strength"], .49)
                self.assertEqual(migrated.maintain(), [])
                self.assertEqual(json.loads(row["evidence"][0]["detail"])["explanation"], "Historical assessment")
            finally:
                migrated.close()
        migrated, frozen = Memory(path), Memory(Path(self.tmp.name)/"frozen-legacy.sqlite3")
        try:
            migrated.freeze_into(frozen)
            row = frozen.lessons()[0]
            self.assertEqual(row["support"], 0)
            self.assertEqual(row["legacy_support"], 3)
            self.assertEqual(row["uses"], 0)
            self.assertEqual(frozen.facts("r"), [])
        finally:
            migrated.close()
            frozen.close()

    def test_legacy_evidence_can_be_reassessed_once_for_current_claim(self):
        migrated = Memory(self.legacy_database())
        try:
            self.assertIsNotNone(migrated.learn(lesson([1]), "r", [1]))
            self.assertIsNone(migrated.learn(lesson([1]), "r", [1]))
            row = migrated.lessons()[0]
            self.assertEqual(row["support"], 1)
            self.assertEqual(row["legacy_support"], 3)
            evidence = json.loads(row["evidence"][0]["detail"])
            self.assertTrue(evidence["claim_hash"])
            self.assertEqual(evidence["legacy_confirmation"]["detail"]["explanation"], "Historical assessment")
            self.assertEqual(len(row["evidence"]), 1)
        finally:
            migrated.close()

    def test_parallel_incompatible_claims_cannot_share_confidence(self):
        ids = [self.fact("parallel-one"), self.fact("parallel-two")]
        path = self.m.path
        barrier = Barrier(2)
        def propose(i):
            memory = Memory(path)
            try:
                candidate = lesson([ids[i]])
                candidate["body"] += " Variant " + str(i)
                barrier.wait(timeout=5)
                return memory.learn(candidate, "r", [ids[i]])
            finally:
                memory.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(propose, range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        row = self.m.lessons()[0]
        self.assertEqual(row["support"], 1)
        self.assertEqual(len(row["evidence"]), 1)

    def test_archive_survives_trim_restart_and_pages_only_its_run(self):
        self.m.save({"id": "r", "status": "running"})
        ids = []
        for i in range(320):
            ids.append(self.m.event("r", "decision", {"assessment": str(i)}))
        self.fact("early", run="r")
        self.m.event("other", "decision", {"assessment": "private-other"})
        cutoff = self.m.archive_info("r")["through_id"]
        self.m.trim()
        self.m.close()
        self.m = Memory(Path(self.tmp.name)/"memory.sqlite3")
        self.assertTrue(self.m.archive_info("r")["complete_from_creation"])
        self.m.event("r", "decision", {"assessment": "after-cutoff"})
        params = {"limit": 37, "kind": "decision"}
        seen = []
        while params is not None:
            page = self.m.archive_page("r", params, cutoff)
            seen.extend(e["id"] for e in page["events"])
            params = page["next_parameters"]
        self.assertEqual(seen, ids)
        with self.assertRaises(ValueError):
            self.m.archive_page("r", {"run": "other"})
        with self.assertRaises(ValueError):
            self.m.archive_page("r", {"limit": 100000})

    def test_old_run_archive_is_explicitly_incomplete(self):
        self.m.save({"id":"old", "status":"paused"})
        with self.m.db:
            self.m.db.execute("DELETE FROM run_archives WHERE run='old'")
        self.m.close()
        self.m = Memory(Path(self.tmp.name)/"memory.sqlite3")
        self.assertFalse(self.m.archive_info("old")["complete_from_creation"])
        self.m.save({"id":"old", "status":"finished"})
        self.assertFalse(self.m.archive_info("old")["complete_from_creation"])

    def test_pending_reviews_are_not_deleted_by_completed_run_retention(self):
        for i in range(102):
            self.m.save({"id": str(i), "status": "finished", "review_checkpoint": {"status": "pending"}})
            self.m.event(str(i), "decision", {"assessment": "retained"})
        self.m.trim()
        self.assertEqual(len(self.m.runs()), 102)
        self.assertEqual(self.m.archive_info("0")["count"], 1)

    def test_full_evidence_is_not_silently_truncated(self):
        detail = {"value": "x"*10000, "last_field": "keep"}
        self.m.observe("r", {"evidence": [{"identity":"full", "outcome":"success", "kind":"effect", "detail":detail}]})
        self.assertEqual(json.loads(self.m.facts("r")[0]["detail"]), detail)

    def test_request_replay_and_ambiguity(self):
        self.assertIsNone(self.m.prepare("a", "r", {"action": 1}, False))
        with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
            self.m.prepare("a", "r", {"action": 1}, False)
        self.m.received("a", {"done": True})
        self.assertEqual(self.m.prepare("a", "r", {"action": 1}, False), {"done": True})
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.m.prepare("a", "r", {"action": 2}, True)

    def test_retention_keeps_unacknowledged_queue_replay(self):
        self.m.save({"id":"r", "status":"paused", "bootstrapped":True, "queue":[{"request_id":"keep"}], "queue_index":0})
        self.m.prepare("keep","r",{},False)
        self.m.received("keep",{"verified":True})
        with self.m.db:
            self.m.db.executemany("INSERT INTO requests VALUES (?,?,?,?)", [(f"x{i}","r","{}","{}") for i in range(2100)])
        self.m.trim()
        self.assertEqual(self.m.prepare("keep","r",{},False), {"verified":True})
        self.assertLessEqual(self.m.db.execute("SELECT count(*) FROM requests").fetchone()[0],2001)


if __name__ == "__main__":
    unittest.main()
