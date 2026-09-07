import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ak2_agent.learning import learn_checked
from ak2_agent.memory import Memory
from test_memory import lesson


def approved(context):
    return {"sufficient": True, "reason": "Fixture evidence substantiates the claim",
            "limitations": [], "evidence_ids": [f["id"] for f in context["evidence"]]}


class LearningReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.tmp.name)/'memory.sqlite3')
        self.state = {'id': 'r', 'llm_seconds': 0}
        self.memory.observe('r', {'evidence': [{'identity': 'x', 'outcome': 'success', 'kind': 'effect', 'detail': {'measured': 7}}]})
        self.eid = self.memory.facts('r')[0]['id']
        self.brain = SimpleNamespace(deadline_at=None, usage={'seconds': .1}, verify_lesson=Mock(side_effect=approved))

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def apply(self, candidate=None, **kwargs):
        return learn_checked(self.memory, self.state, self.brain, candidate or lesson([self.eid]), [self.eid], **kwargs)

    def test_reviewer_rejects_without_changing_existing_confidence(self):
        self.apply()
        self.brain.verify_lesson.side_effect = None
        self.brain.verify_lesson.return_value = {'sufficient': False, 'reason': 'Filter predetermines the observed group',
                                                'limitations': ['Excluded clients not measured'], 'evidence_ids': []}
        context = {'query': {'rule_id': 'restricted-rule'}, 'groups': ['one']}
        self.memory.observe('r', {'evidence': [{'identity': 'fresh', 'outcome': 'success', 'kind': 'action', 'detail': 'accepted'}]})
        self.eid = self.memory.facts('r')[0]['id']
        self.assertIsNone(self.apply(context=context))
        self.assertEqual(self.memory.lessons()[0]['support'], 1)
        self.assertEqual(self.memory.learning_feedback['reason'], 'insufficient_evidence')
        entry = self.memory.lesson_history(change='rejected')['entries'][-1]
        self.assertEqual(entry['reason'], 'insufficient_evidence')
        self.assertEqual(entry['details']['proposal']['body'], lesson([])['body'])
        self.assertEqual(entry['details']['feedback']['review']['reason'], 'Filter predetermines the observed group')
        self.assertEqual(entry['before'], entry['after'])
        self.assertEqual(self.brain.verify_lesson.call_args.args[0]['context'], context)

    def test_unavailable_malformed_or_expired_verifier_never_learns(self):
        for failure in (RuntimeError('offline'), {'sufficient': True}, None):
            with self.subTest(failure=failure):
                self.brain.verify_lesson.side_effect = failure if isinstance(failure, Exception) else None
                self.brain.verify_lesson.return_value = failure
                if failure is None:
                    self.brain.deadline_at = 1
                self.assertIsNone(self.apply())
                self.assertEqual(self.memory.lessons(), [])
                self.assertEqual(self.memory.learning_feedback['reason'], 'verification_unavailable')
                self.assertEqual(self.memory.lesson_history()['entries'][-1]['reason'], 'verification_unavailable')
        self.assertEqual(self.brain.deadline_at, 1)

    def test_unseen_facts_and_reviewer_invented_ids_rejected(self):
        self.assertIsNone(self.apply(lesson([self.eid+100])))
        self.brain.verify_lesson.assert_not_called()
        self.brain.verify_lesson.side_effect = lambda c: {**approved(c), 'evidence_ids': [self.eid+100]}
        self.assertIsNone(self.apply())
        self.assertEqual(self.memory.lessons(), [])

    def test_review_receipt_survives_crash_and_same_event_is_not_reinforced_twice(self):
        with patch.object(self.memory, 'learn', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.apply()
        self.assertIsNotNone(self.apply())
        self.assertIsNone(self.apply())
        self.brain.verify_lesson.assert_called_once()
        self.assertEqual(self.memory.lessons()[0]['support'], 1)
        self.assertIsNone(self.brain.deadline_at)

    def test_verifier_can_only_confirm_selected_relevant_facts(self):
        self.memory.observe('r', {'evidence': [{'identity': 'y', 'outcome': 'success', 'kind': 'action', 'detail': 'accepted'}]})
        other = self.memory.facts('r')[0]['id']
        self.brain.verify_lesson.side_effect = lambda c: {**approved(c), 'evidence_ids': [self.eid]}
        learn_checked(self.memory, self.state, self.brain, lesson([self.eid, other]), [self.eid, other])
        evidence = self.memory.lessons()[0]['evidence']
        self.assertEqual([e['identity'] for e in evidence], ['r:x'])
