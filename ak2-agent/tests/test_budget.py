import unittest
from unittest.mock import Mock, patch
from ak2_agent.budget import set_budget, budget_status, TimeBudgetExpired
from ak2_agent.brain import Brain
from ak2_agent.cli import parser


class BudgetTests(unittest.TestCase):
    def test_absolute_deadline_and_default_resume_preserves_it(self):
        state={}
        with patch('ak2_agent.budget.time.time',return_value=100): set_budget(state,3600)
        with patch('ak2_agent.budget.time.time',return_value=150):
            set_budget(state,None)
            self.assertEqual(budget_status(state)['remaining_seconds'],3550)
            set_budget(state,60)
        self.assertEqual(state['deadline_at'],210)

    def test_invalid_budget_and_cli_values(self):
        for value in [0,-1,1.5,True]:
            with self.assertRaises(ValueError):set_budget({},value)
        for command in ['run','resume','adapt','train','evaluate','stream']:
            args=parser().parse_args([command,'--time-budget-seconds','3600'] + (['tasks.jsonl'] if command == 'stream' else ['--seeds','1'] if command in ('train','evaluate') else []))
            self.assertEqual(args.time_budget_seconds,3600)
        with patch('sys.stderr'),self.assertRaises(SystemExit):
            parser().parse_args(['run','--time-budget-seconds','0'])

    def test_expired_brain_never_starts_sdk_call(self):
        brain=Brain.__new__(Brain);brain.deadline_at=1;brain._call_once=Mock()
        with self.assertRaises(TimeBudgetExpired):brain.call('decide',{})
        brain._call_once.assert_not_called()
