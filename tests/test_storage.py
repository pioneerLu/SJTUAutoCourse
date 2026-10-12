import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modules.gui_support import SettingsStore
from modules.storage import read_json, read_json_safely, write_json, write_text_atomic


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'review.json'
        write_json(self.path, {'old': '原记录'})
        self.original = self.path.read_bytes()

    def assert_preserved(self):
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_unicode_replacement_and_no_temporary_files_left(self):
        write_json(self.path, [{'explanation': '大学生活', 'option_ids': ['123']}])
        self.assertEqual(read_json(self.path)[0]['explanation'], '大学生活')
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_serialization_failure_keeps_original(self):
        with self.assertRaises(TypeError):
            write_json(self.path, {'bad': object()})
        self.assert_preserved()

    def test_disk_flush_failure_keeps_original_and_cleans_temporary(self):
        with patch('modules.storage.os.fsync', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                write_json(self.path, {'new': True})
        self.assert_preserved()

    def test_locked_target_keeps_original_and_cleans_temporary(self):
        with patch('modules.storage.os.replace', side_effect=PermissionError('locked target')):
            with self.assertRaises(PermissionError):
                write_json(self.path, {'new': True})
        self.assert_preserved()

    def test_target_is_unchanged_until_complete_replacement_is_ready(self):
        replace = os.replace
        def observe(temporary, target):
            self.assertEqual(self.path.read_bytes(), self.original)
            self.assertEqual(read_json(temporary), {'complete': True})
            replace(temporary, target)
        with patch('modules.storage.os.replace', side_effect=observe):
            write_json(self.path, {'complete': True})
        self.assertEqual(read_json(self.path), {'complete': True})

    def test_display_reads_tolerate_damage_but_business_reads_fail(self):
        for content in ('not json', '\ufffd', '[]'):
            with self.subTest(content=content):
                self.path.write_text(content, encoding='utf-8')
                self.assertEqual(read_json_safely(self.path, {}), {})
        self.path.write_bytes(b'\xff')
        self.assertEqual(read_json_safely(self.path, {}), {})
        with self.assertRaises(ValueError):
            read_json(self.path)
        self.assertEqual(read_json_safely(self.root / 'missing.json', []), [])

    def test_missing_parent_is_not_created(self):
        with self.assertRaises(FileNotFoundError):
            write_text_atomic(self.root / 'missing' / 'config', 'config')
        self.assertFalse((self.root / 'missing').exists())

    def test_settings_failure_preserves_disk_and_in_memory_configuration(self):
        path = self.root / 'config'
        path.write_text('[custom]\nvalue = old\n', encoding='utf-8')
        store = SettingsStore(self.root)
        original = path.read_bytes()
        with patch('modules.storage.os.replace', side_effect=PermissionError('locked config')):
            with self.assertRaises(PermissionError):
                store.save({'custom': {'value': 'new'}}, [
                    'https://wisdom-mooc.zhihuishu.com/study/index?recruitAndCourseId=Fixture_42'])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(store.get('custom', 'value'), 'old')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['config', 'review.json'])


if __name__ == '__main__':
    unittest.main()
