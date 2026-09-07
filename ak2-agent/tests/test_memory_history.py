import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ak2_agent.cli import execute
from ak2_agent.memory import Memory
from ak2_agent.worlds import register, discover
from test_memory import lesson


class MemoryHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'memory.sqlite3'
        self.m = Memory(self.path)
        self.m.save({'id': 'r', 'status': 'running'})

    def tearDown(self):
        self.m.close()
        self.tmp.cleanup()

    def fact(self, identity, outcome='success'):
        self.m.observe('r', {'evidence': [{'identity': identity, 'outcome': outcome, 'kind': 'effect', 'detail': {'value': identity}}]})
        return self.m.facts('r')[0]['id']

    def learn(self, identity, verdict='supports', outcome='success'):
        fid = self.fact(identity, outcome)
        self.m.learn(lesson([fid], verdict, outcome), 'r', [fid], review={'id':'fixture-review','result':{'sufficient':True}})
        return fid

    def test_lifecycle_history_distinguishes_add_support_contradiction_and_rejection(self):
        self.learn('first')
        self.learn('second')
        fid = self.learn('failed', 'contradicts', 'failure')
        self.m.learn(lesson([fid], 'contradicts', 'failure'), 'r', [fid])
        rows = self.m.lesson_history()['entries']
        self.assertEqual([r['change'] for r in rows], ['added', 'reinforced', 'contradicted', 'rejected'])
        self.assertIsNone(rows[0]['before'])
        self.assertEqual(rows[0]['after']['body'], lesson([])['body'])
        self.assertEqual(rows[1]['before']['support'], 1)
        self.assertEqual(rows[1]['after']['support'], 2)
        self.assertEqual(rows[2]['after']['against'], 1)
        self.assertLess(rows[2]['after']['confidence'], rows[2]['before']['confidence'])
        self.assertEqual(rows[3]['before'], rows[3]['after'])
        self.assertEqual(rows[3]['reason'], 'evidence_already_counted_for_this_lesson')
        self.assertEqual(rows[0]['details']['review']['id'], 'fixture-review')
        self.assertTrue(all(r['run']=='r' and r['created_at'].endswith('+00:00') for r in rows))

    def test_deleted_lesson_text_and_facts_survive_run_cleanup_and_restart(self):
        self.learn('first')
        self.m.put('epoch', 5000)
        self.assertEqual(self.m.maintain(run='r'), ['check-before-action'])
        deleted = self.m.lesson_history(change='deleted')['entries'][0]
        self.assertEqual(deleted['reason'], 'weak_and_low_importance')
        self.assertEqual(deleted['before']['procedure'], lesson([])['procedure'])
        self.assertIsNone(deleted['after'])
        self.assertEqual(len(deleted['details']['confirmations']), 1)
        self.m.save({'id':'r','status':'finished','review_checkpoint':{'status':'reviewed'}})
        for i in range(100):
            self.m.save({'id':str(i),'status':'finished','review_checkpoint':{'status':'reviewed'}})
        self.m.trim()
        self.assertFalse(any(r['id']=='r' for r in self.m.runs()))
        self.assertEqual(self.m.facts('r'), [])
        self.m.close()
        self.m = Memory(self.path)
        rows = self.m.lesson_history(run='r')['entries']
        self.assertEqual([r['change'] for r in rows], ['added','deleted'])
        self.assertEqual(rows[0]['details']['evidence'][0]['detail'], '{"value":"first"}')
        self.assertEqual(rows[1], deleted)

    def test_capacity_removal_has_distinct_reason(self):
        with self.m.db:
            for i in range(81):
                self.m.db.execute('INSERT INTO lessons(id,title,body,tags,procedure,importance,touched) VALUES (?,?,?,?,?,?,?)',
                                  (str(i),str(i),'body','[]','[]',1,0))
        self.assertEqual(len(self.m.maintain()), 1)
        row = self.m.lesson_history(change='deleted')['entries'][0]
        self.assertEqual(row['reason'], 'capacity_limit')
        self.assertEqual(row['details']['rank'], 81)
        self.assertIsNone(row['run'])

    def test_audit_failure_rolls_back_mutation_and_forgetting(self):
        fid = self.fact('first')
        with patch.object(self.m, '_record_change', side_effect=RuntimeError('disk full')):
            with self.assertRaisesRegex(RuntimeError, 'disk full'):
                self.m.learn(lesson([fid]), 'r', [fid])
        self.assertEqual(self.m.lessons(), [])
        self.assertEqual(self.m.db.execute('SELECT count(*) FROM confirmations').fetchone()[0], 0)
        self.m.learn(lesson([fid]), 'r', [fid])
        self.m.put('epoch', 5000)
        with patch.object(self.m, '_record_change', side_effect=RuntimeError('disk full')):
            with self.assertRaises(RuntimeError):
                self.m.maintain(run='r')
        self.assertEqual(len(self.m.lessons()), 1)
        self.assertEqual(self.m.get('epoch'), 5000)
        self.assertEqual(self.m.lesson_history(change='deleted')['entries'], [])

    def test_existing_database_gets_one_baseline_not_fabricated_changes(self):
        self.learn('first')
        with self.m.db:
            self.m.db.execute('DROP TABLE lesson_changes')
            self.m.db.execute("DELETE FROM meta WHERE key='lesson_history_started_at'")
        for _ in range(2):
            self.m.close()
            self.m = Memory(self.path)
            rows = self.m.lesson_history()['entries']
            self.assertEqual([r['change'] for r in rows], ['snapshot'])
            self.assertEqual(rows[0]['after']['support'], 1)
            self.assertEqual(rows[0]['reason'], 'preexisting_state')

    def test_filtered_cursor_and_cli_json_do_not_start_brain(self):
        self.learn('one')
        self.learn('two')
        self.learn('three')
        page = self.m.lesson_history(change='reinforced', limit=1)
        rest = self.m.lesson_history(change='reinforced', limit=1, after_id=page['next_after_id'])
        self.assertNotEqual(page['entries'][0]['id'], rest['entries'][0]['id'])
        self.assertIsNone(rest['next_after_id'])
        with self.assertRaises(ValueError):
            self.m.lesson_history(limit=0)
        root = Path(self.tmp.name)/'project'
        register(root,'fixture','Synthetic world',"printf '{}'",True)
        world = discover(root)[0]
        world.memory.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(world.memory)
        try:
            self.m.db.backup(target)
        finally:
            target.close()
        output = io.StringIO()
        with patch('ak2_agent.cli.Brain', side_effect=AssertionError('No model call')), contextlib.redirect_stdout(output):
            self.assertEqual(execute(['memory-history','--root',str(root),'--world','fixture','--change','reinforced','--limit','1','--json']),0)
        value=json.loads(output.getvalue())
        self.assertEqual(value['entries'][0]['change'],'reinforced')
        self.assertIsNotNone(value['next_after_id'])

    def test_freeze_logs_import_without_copying_source_journal(self):
        self.learn('one')
        self.learn('two')
        target = Memory(Path(self.tmp.name)/'target.sqlite3')
        try:
            self.m.freeze_into(target)
            rows=target.lesson_history()['entries']
            self.assertEqual([r['change'] for r in rows], ['imported'])
            self.assertEqual(rows[0]['after']['support'],2)
            self.assertIsNone(rows[0]['run'])
        finally:
            target.close()
