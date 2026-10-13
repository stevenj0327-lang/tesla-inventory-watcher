import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock

from tracker.github_runner import GitHub, run
from tracker.state import State
from test_tracker import match


class Source:
    result = {}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def scan(self): return self.result


class GitHubTests(unittest.TestCase):
    def test_receipt_prevents_duplicate_after_interrupted_save(self):
        with tempfile.TemporaryDirectory() as folder:
            state = State(Path(folder) / 'state')
            state.success(match())
            event = state.pending()[0]
            api = GitHub('owner/repo', 'test')
            api.api = Mock(return_value={'number': 42})
            receipts = {}
            self.assertEqual(api.notify(event, receipts), '42')
            self.assertEqual(api.notify(event, receipts), '42')
            self.assertEqual(api.api.call_count, 1)
            state.db.close()

    def test_empty_scan_does_not_create_notifications(self):
        with tempfile.TemporaryDirectory() as folder:
            api = Mock()
            self.assertTrue(run(api, Path(folder) / 'state', Source))
            api.notify.assert_not_called()
            api.receipts.assert_not_called()

    def test_failed_scan_does_not_create_notifications(self):
        class Failed(Source):
            def scan(self): raise RuntimeError('blocked')
        with tempfile.TemporaryDirectory() as folder:
            api = Mock()
            self.assertFalse(run(api, Path(folder) / 'state', Failed))
            api.notify.assert_not_called()
            api.save.assert_called_once()

    def test_match_state_saved_before_notification(self):
        class Found(Source):
            result = match()
        with tempfile.TemporaryDirectory() as folder:
            api = Mock()
            api.notify.return_value = '42'
            self.assertTrue(run(api, Path(folder) / 'state', Found))
            actions = [entry[0] for entry in api.mock_calls]
            self.assertLess(actions.index('save'), actions.index('notify'))
            self.assertEqual(api.save.call_count, 2)

    def test_system_event_rejected(self):
        api = GitHub('owner/repo','test')
        api.api = Mock()
        with self.assertRaises(RuntimeError): api.notify({'vin': None}, {})
        api.api.assert_not_called()
