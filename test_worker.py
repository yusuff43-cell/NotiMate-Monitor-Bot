import unittest
from unittest.mock import Mock, patch

from event_store import EventJob, retry_delay_seconds
from worker import run_once


class WorkerTests(unittest.TestCase):
    def test_retry_delay_is_bounded(self):
        self.assertEqual(retry_delay_seconds(1), 5)
        self.assertEqual(retry_delay_seconds(3), 20)
        self.assertEqual(retry_delay_seconds(20), 300)

    def test_no_job_returns_false(self):
        store = Mock()
        store.claim_next.return_value = None
        self.assertFalse(run_once(store, Mock()))

    def test_success_marks_event_completed(self):
        store = Mock()
        job = EventJob('evt-1', 'bot-1', {'type': 'message'}, 1)
        store.claim_next.return_value = job
        processor = Mock()
        self.assertTrue(run_once(store, processor))
        processor.assert_called_once_with('bot-1', job.payload)
        store.mark_completed.assert_called_once_with('evt-1')
        store.mark_retry.assert_not_called()

    def test_failure_is_retried_before_limit(self):
        store = Mock()
        store.claim_next.return_value = EventJob('evt-2', 'bot-1', {}, 2)
        processor = Mock(side_effect=RuntimeError('temporary'))
        with patch('worker.MAX_ATTEMPTS', 5), self.assertLogs(level='ERROR'):
            run_once(store, processor)
        store.mark_retry.assert_called_once_with('evt-2', 'RuntimeError: temporary', 2)
        store.mark_failed.assert_not_called()

    def test_failure_becomes_terminal_at_limit(self):
        store = Mock()
        store.claim_next.return_value = EventJob('evt-3', 'bot-1', {}, 5)
        processor = Mock(side_effect=ValueError('bad payload'))
        with patch('worker.MAX_ATTEMPTS', 5), self.assertLogs(level='ERROR'):
            run_once(store, processor)
        store.mark_failed.assert_called_once_with('evt-3', 'ValueError: bad payload')
        store.mark_retry.assert_not_called()


if __name__ == '__main__':
    unittest.main()
