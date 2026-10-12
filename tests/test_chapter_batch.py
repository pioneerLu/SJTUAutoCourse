import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from modules.chapter_batch import apply_review_batch
from modules.llm_types import LLMError
from modules.storage import write_json


class ChapterBatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        run = Path(self.temporary.name)
        self.first = run / 'course-2' / 'test-3'
        self.second = run / 'course-2' / 'test-4'
        self.targets = {self.first: {'b', 'a'}, self.second: None}
        self.config = Mock(username='private-user', password='private-pass')

    async def test_single_success_supports_no_callback_and_sorts_question_ids(self):
        with patch('modules.chapter_tests.apply_review', AsyncMock(return_value='saved')) as apply:
            status = await apply_review_batch(self.config, Mock(), 'unused', {self.first: {'b', 'a'}})
        self.assertEqual(status, 'saved')
        self.assertEqual(apply.call_args.kwargs['question_ids'], ['a', 'b'])

    async def test_completed_and_empty_chapters_are_counted_as_skips(self):
        events = []
        with patch('modules.chapter_tests.apply_review', AsyncMock(side_effect=['already_submitted', 'skipped_no_answers'])):
            result = await apply_review_batch(self.config, Mock(), 'unused', self.targets, callback=events.append)
        self.assertEqual(result, {'batch': True, 'total': 2, 'completed': 0, 'failed': 0, 'skipped': 2})
        self.assertEqual([(e['course'], e['test']) for e in events if e['kind'] == 'test_started'], [(2, 3), (2, 4)])

    async def test_failure_continues_and_redacts_user_credentials(self):
        events = []
        with patch('modules.chapter_tests.apply_review', AsyncMock(side_effect=[
                LLMError('private-user private-pass https://example.test/?ticket=private-ticket'), 'submitted'])):
            result = await apply_review_batch(self.config, Mock(), 'unused', self.targets, callback=events.append)
        self.assertEqual(result['completed'], 1)
        self.assertEqual(result['failed'], 1)
        text = str(events)
        for secret in ('private-user', 'private-pass', 'private-ticket'):
            self.assertNotIn(secret, text)

    async def test_failure_reported_by_backend_is_not_duplicated(self):
        async def failed(*args, callback, **kwargs):
            callback({'kind': 'test_finished', 'directory': str(args[3]), 'status': 'failed'})
            raise LLMError('fixture failure')
        events = []
        with patch('modules.chapter_tests.apply_review', side_effect=failed):
            result = await apply_review_batch(self.config, Mock(), 'unused', {self.first: None}, callback=events.append)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(sum(e['kind'] == 'test_finished' for e in events), 1)

    async def test_cancellation_stops_remaining_targets(self):
        with patch('modules.chapter_tests.apply_review', AsyncMock(side_effect=asyncio.CancelledError())) as apply:
            with self.assertRaises(asyncio.CancelledError):
                await apply_review_batch(self.config, Mock(), 'unused', self.targets)
        self.assertEqual(apply.await_count, 1)

    async def test_batch_of_completed_records_does_not_launch_browser(self):
        for directory in self.targets:
            directory.mkdir(parents=True)
            write_json(directory / 'result.json', {'status': 'submitted'})
        with patch('modules.chapter_tests.create_browser_session', AsyncMock()) as create:
            result = await apply_review_batch(self.config, Mock(), 'unused', self.targets)
        self.assertEqual(result['skipped'], 2)
        self.assertEqual(result['failed'], 0)
        create.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
