import asyncio
import configparser
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch, Mock, AsyncMock

from modules.gui_support import SettingsStore, TaskController
from modules.gui_app import AutoCourseApp, configure_dpi
from modules.configs import Config
from modules.llm import LLMSettings, LLMError
from modules.runtime_env import project_python, using_project_python
from modules.browser import _launch_args


TEMPLATE = '''[user-account]
username=
password=
[browser-option]
driver=Chrome
EXE_PATH=
[course-option]
limitMaxTime=0
limitSpeed=1
soundOff=True
[script-option]
enableHideWindow=False
enableAutoCaptcha=False
[course-url]
URL1=https://wisdom-mooc.zhihuishu.com/study/index?recruitAndCourseId=Course_42&x=%E4%B8%AD
[llm-option]
url=https://llm.test/v1
key=fixture-secret
model=glm-5.3-flash
model_ds=deepseek-v4-flash-vision-exp
timeout=90
minConfidence=0.8
retryDelay=0
requestInterval=0
[llm-model-1]
model=glm-5.3-flash
url=
key=
[llm-model-2]
model=deepseek-v4-flash-vision-exp
url=
key=
[custom]
value=preserved
'''


class SettingsTests(unittest.TestCase):
    def test_store_and_runtime_use_the_same_course_normalization(self):
        from modules.course_urls import normalize_course_urls
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = TEMPLATE.replace('URL1=https://', 'URL1=课程： http://')
            (root / 'config.example').write_text(template, encoding='utf-8')
            (root / 'config').write_text(template, encoding='utf-8')
            store = SettingsStore(root)
            config = Config.__new__(Config)
            config._config = store.parser
            expected = normalize_course_urls(store.courses())
            self.assertEqual(config.get_course_urls(), expected)
            store.save({}, store.courses())
            self.assertEqual(store.courses(), expected)
            self.assertEqual(store.get('llm-option', 'key'), 'fixture-secret')

    def test_source_launcher_prefers_project_venv_and_keeps_frozen_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / '.venv' / 'Scripts/python.exe'
            executable.parent.mkdir(parents=True)
            executable.touch()
            with patch('modules.runtime_env.sys.frozen', False, create=True):
                self.assertEqual(project_python(directory, 'system-python'), str(executable))
                with patch('modules.runtime_env.sys.executable', str(executable)):
                    self.assertTrue(using_project_python(str(executable)))
            with patch('modules.runtime_env.sys.frozen', True, create=True):
                self.assertEqual(project_python(directory, 'frozen-app'), 'frozen-app')

    def test_source_launcher_without_project_venv_uses_current_python(self):
        with tempfile.TemporaryDirectory() as directory, patch('modules.runtime_env.sys.frozen', False, create=True):
            self.assertEqual(project_python(directory, 'current-python'), 'current-python')

    def test_video_worker_uses_the_project_interpreter(self):
        controller = TaskController()
        process = Mock(stdout=[])
        process.wait.return_value = 0
        with patch('modules.gui_support.project_python', return_value='project-venv-python'), \
                patch('modules.gui_support.subprocess.Popen', return_value=process) as launch:
            controller.start_video('fixture-root')
            deadline = time.monotonic() + 2
            while controller.running and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(controller.running)
        self.assertEqual(launch.call_args.args[0][0], 'project-venv-python')

    def test_save_preserves_secret_unknown_options_and_encoded_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config.example').write_text(TEMPLATE, encoding='utf-8')
            (root / 'config').write_text(TEMPLATE, encoding='utf-8')
            store = SettingsStore(root)
            store.save({'llm-option': {'modelProfile': '2'}}, store.courses())
            self.assertEqual(store.get('llm-option', 'key'), 'fixture-secret')
            self.assertEqual(store.get('custom', 'value'), 'preserved')
            self.assertIn('%E4%B8%AD', store.courses()[0])
            config = Config.__new__(Config)
            config._config = store.parser
            with patch.dict('os.environ', {}, clear=True):
                settings = LLMSettings.from_config(config)
            self.assertEqual(settings.model, 'deepseek-v4-flash-vision-exp')
            self.assertEqual(settings.url, 'https://llm.test/v1')
            self.assertEqual(settings.key, 'fixture-secret')
            config._config.set('llm-model-2', 'url', 'https://second.test/v1')
            with patch.dict('os.environ', {}, clear=True):
                self.assertEqual(LLMSettings.from_config(config, '2').url, 'https://second.test/v1')

    def test_task_cancellation_runs_cleanup(self):
        controller = TaskController()
        started = threading.Event()
        cleaned = threading.Event()
        async def job(callback):
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                cleaned.set()
        controller.start('cancel-test', job)
        self.assertTrue(started.wait(2))
        controller.stop()
        self.assertTrue(cleaned.wait(2))
        deadline = time.monotonic() + 2
        while controller.running and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(controller.running)
        self.assertEqual(controller.events.get(timeout=1)['kind'], 'cancelled')


class GuiTests(unittest.TestCase):
    def setUp(self):
        configure_dpi()
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        (self.path / 'config.example').write_text(TEMPLATE, encoding='utf-8')
        (self.path / 'config').write_text(TEMPLATE, encoding='utf-8')
        self.root = tk.Tk()
        self.app = AutoCourseApp(self.root, self.path)
        self.root.update()

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_browser_choice_is_saved_and_restored_for_both_browsers(self):
        self.assertEqual(tuple(self.app.browser_select.cget('values')), ('Chrome', 'Edge'))
        self.assertEqual(str(self.app.browser_select.cget('state')), 'readonly')
        for browser, channel in (('Edge', 'msedge'), ('Chrome', 'chrome')):
            with self.subTest(browser=browser):
                self.app.browser_select.set(browser)
                self.assertTrue(self.app.save_config(quiet=True))
                config = Config(self.app.store.path, Path(__file__).resolve().parents[1] / 'data/mirrors.json')
                self.assertEqual(config.driver, browser.lower())
                self.assertEqual(_launch_args(config)['channel'], channel)
                self.app.close()
                self.root = tk.Tk()
                self.root.withdraw()
                self.app = AutoCourseApp(self.root, self.path)
                self.root.update()
                self.assertEqual(self.app.browser_select.get(), browser)

    def test_switching_browser_clears_old_executable_but_keeps_current_browser_path(self):
        executable = self.app.variable('browser-option', 'EXE_PATH')
        executable.set(r'C:\custom\chrome.exe')
        self.app.browser_select.set('Chrome')
        self.assertEqual(executable.get(), r'C:\custom\chrome.exe')
        self.app.browser_select.set('Edge')
        self.assertEqual(executable.get(), '')
        executable.set(r'C:\custom\msedge.exe')
        self.assertTrue(self.app.save_config(quiet=True))
        self.assertEqual(self.app.store.get('browser-option', 'EXE_PATH'), r'C:\custom\msedge.exe')
        self.app.browser_select.set('Chrome')
        self.assertEqual(executable.get(), '')

    def test_browser_selection_and_video_start_remain_visible_at_minimum_window(self):
        self.app.tabs.select(self.app.video_tab)
        self.root.geometry('980x700')
        self.root.update()
        tab = self.app.video_tab
        for widget in (self.app.browser_select, self.app.action_buttons[0]):
            self.assertTrue(widget.winfo_ismapped())
            self.assertGreaterEqual(widget.winfo_rooty(), tab.winfo_rooty())
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                 tab.winfo_rooty() + tab.winfo_height())

    def test_switching_browser_stops_reusing_previous_cdp_connection(self):
        self.app.store.save({'browser-option': {'attachExistingChrome': 'True'}}, self.app.store.courses())
        self.app.browser_select.set('Chrome')
        self.assertTrue(self.app.save_config(quiet=True))
        config = Config(self.app.store.path, Path(__file__).resolve().parents[1] / 'data/mirrors.json')
        self.assertTrue(config.attach_existing_chrome)
        self.app.browser_select.set('Edge')
        self.assertTrue(self.app.save_config(quiet=True))
        config = Config(self.app.store.path, Path(__file__).resolve().parents[1] / 'data/mirrors.json')
        self.assertEqual(config.driver, 'edge')
        self.assertFalse(config.attach_existing_chrome)
        self.assertEqual(_launch_args(config)['channel'], 'msedge')

    def test_existing_edge_config_and_custom_path_are_restored_without_reset(self):
        template = TEMPLATE.replace('driver=Chrome', 'driver= eDgE ').replace('EXE_PATH=', r'EXE_PATH=C:\custom\msedge.exe')
        (self.path / 'config').write_text(template, encoding='utf-8')
        self.app.close()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = AutoCourseApp(self.root, self.path)
        self.root.update()
        self.assertEqual(self.app.browser_select.get(), 'Edge')
        self.assertEqual(self.app.variable('browser-option', 'EXE_PATH').get(), r'C:\custom\msedge.exe')
        self.assertTrue(self.app.save_config(quiet=True))
        self.assertEqual(self.app.store.get('browser-option', 'driver'), 'Edge')
        self.assertEqual(self.app.store.get('browser-option', 'EXE_PATH'), r'C:\custom\msedge.exe')

    def test_invalid_browser_does_not_write_config(self):
        previous = (self.path / 'config').read_bytes()
        self.app.browser_select.set('Firefox')
        with patch('modules.gui_app.messagebox.showerror') as error:
            self.assertFalse(self.app.save_config(quiet=True))
        self.assertIn('Chrome', error.call_args.args[1])
        self.assertIn('Edge', error.call_args.args[1])
        self.assertEqual((self.path / 'config').read_bytes(), previous)

    def test_formatted_course_input_is_rewritten_without_clearing_same_course(self):
        url = self.app.store.courses()[0]
        self.app.chapters['fixture'] = {'name': 'keep this selection'}
        self.app.course_text.delete('1.0', 'end')
        self.app.course_text.insert('1.0', '课程：[' + url + '](' + url.removeprefix('https://') + ')。')
        self.assertTrue(self.app.save_config(quiet=True))
        self.assertEqual(self.app.course_text.get('1.0', 'end').strip(), url)
        self.assertIn('fixture', self.app.chapters)

    def test_invalid_course_input_does_not_write_any_settings(self):
        previous = (self.path / 'config').read_bytes()
        self.app.course_text.delete('1.0', 'end')
        self.app.course_text.insert('1.0', 'https://wisdom-mooc.zhihuishu.com/')
        with patch('modules.gui_app.messagebox.showerror') as error:
            self.assertFalse(self.app.save_config(quiet=True))
        self.assertIn('recruitAndCourseId', error.call_args.args[1])
        self.assertEqual((self.path / 'config').read_bytes(), previous)

    def result_fixture(self, stamp, course=1, test=1, name='第一章', mode='preview'):
        run = self.path / '.autovisor' / 'chapter-tests' / stamp
        directory = run / f'course-{course}' / f'test-{test}'
        directory.mkdir(parents=True)
        (run / 'run.json').write_text(json.dumps({'mode': mode, 'model': 'fixture-model', 'status': 'completed'}), encoding='utf-8')
        (directory / 'context.json').write_text(json.dumps({'course': course, 'index': test - 1,
            'chapter_name': name, 'url': f'https://chapter.test/course/{course}'}), encoding='utf-8')
        q = {'number': 1, 'question_id': 'same-question-id', 'type_and_score': '单选题', 'stem_dom_text': '',
             'stem_screenshot': str(directory / 'question-1.png'),
             'options': [{'id': 'a', 'text': 'A. fixture'}, {'id': 'b', 'text': 'B. other'}]}
        record = {'question': q, 'status': 'answered', 'model': 'fixture-model',
                  'answer': {'option_ids': ['a'], 'confidence': 0.9, 'explanation': 'fixture'}}
        (directory / 'review.json').write_text(json.dumps([record]), encoding='utf-8')
        (directory / 'questions.json').write_text(json.dumps([q]), encoding='utf-8')
        (directory / 'result.json').write_text(json.dumps({'status': 'preview'}), encoding='utf-8')
        return run.resolve(), directory.resolve(), record

    def test_runs_and_tests_with_same_question_id_remain_distinct_and_filterable(self):
        first, d1, _ = self.result_fixture('20261009-140000-123456', test=1, name='第一章')
        _, d2, _ = self.result_fixture('20261009-140000-123456', test=2, name='第二章')
        second, d3, _ = self.result_fixture('20261009-140000-234567', course=2, name='另一门课')
        self.app.refresh_results()
        self.assertEqual(self.app.view_run, second)
        self.assertEqual(len(self.app.result_runs), 2)
        self.app.select_result_scope(first)
        self.assertEqual(len(self.app.result_rows), 3)
        self.assertEqual({directory for _, directory in self.app.result_rows.values()}, {d1, d2, d3})
        self.assertEqual(self.app.answer_tree.get_children(), (self.app.result_runs[first]['iid'],))
        self.app.select_result_scope(first, d2)
        tests = self.app.answer_tree.get_children(self.app.result_runs[first]['iid'])
        self.assertEqual(tests, (self.app.result_tests[d2]['iid'],))
        self.assertIn('第二章', self.app.answer_tree.item(tests[0], 'text'))
        self.assertIn('测试 2', self.app.test_filter.get())
        self.assertNotEqual(self.app.result_runs[first]['label'], self.app.result_runs[second]['label'])

    def test_switching_run_clears_hidden_selection_and_apply_uses_selected_test(self):
        first, d1, r1 = self.result_fixture('20261009-140000-123456')
        second, d2, r2 = self.result_fixture('20261009-150000-123456', test=2)
        self.app.update_answer(r1, d1)
        self.app.update_answer(r2, d2)
        self.app.answer_tree.selection_set('r0')
        self.app.select_result_scope(second, d2)
        self.assertFalse(self.app.answer_tree.selection())
        self.app.answer_tree.selection_set(self.app.result_tests[d2]['iid'])
        with patch.object(self.app, 'begin') as begin, patch.object(self.app, 'save_config', return_value=True), \
                patch.object(self.app, 'config', return_value=Mock()):
            self.app.apply_answers(False)
        self.assertEqual(begin.call_args.args[2], d2)
        with patch('modules.chapter_tests.apply_review', AsyncMock(return_value='saved')) as apply:
            asyncio.run(begin.call_args.args[1](lambda event: None))
        self.assertEqual(apply.call_args.args[3], d2)
        self.app.answer_tree.item(self.app.result_runs[second]['iid'], open=False)
        with patch.object(self.app, 'begin') as begin, patch('modules.gui_app.messagebox.showinfo'):
            self.app.apply_answers(False)
        begin.assert_not_called()
        self.app.answer_tree.selection_set('r0')  # Even a stale programmatic selection is guarded.
        with patch.object(self.app, 'begin') as begin, patch('modules.gui_app.messagebox.showinfo'):
            self.app.apply_answers(False)
        begin.assert_not_called()

    def test_multi_selection_groups_questions_by_test_and_group_selects_whole_test(self):
        run, d1, r1 = self.result_fixture('20261009-140000-123456')
        _, d2, r2 = self.result_fixture('20261009-140000-123456', test=2)
        r3 = dict(r1, question=dict(r1['question'], question_id='second-id', number=2))
        self.app.update_answer(r1, d1)
        self.app.update_answer(r2, d2)
        self.app.update_answer(r3, d1)
        self.app.select_result_scope(run)
        self.app.answer_tree.item(self.app.result_tests[d1]['iid'], open=True)
        self.app.answer_tree.item(self.app.result_tests[d2]['iid'], open=True)
        self.assertEqual(str(self.app.answer_tree.cget('selectmode')), 'extended')
        self.app.answer_tree.selection_set(('r0', 'r1', 'r2'))
        with patch.object(self.app, 'begin') as begin, patch.object(self.app, 'save_config', return_value=True), \
                patch.object(self.app, 'config', return_value=Mock()):
            self.app.apply_answers(False)
        with patch('modules.chapter_tests.apply_review', AsyncMock(return_value='saved')) as apply:
            asyncio.run(begin.call_args.args[1](lambda event: None))
        calls = {call.args[3]: call.kwargs['question_ids'] for call in apply.call_args_list}
        self.assertEqual(calls, {d1: ['same-question-id', 'second-id'], d2: ['same-question-id']})
        self.app.answer_tree.selection_set(('r0', self.app.result_tests[d1]['iid']))
        with patch.object(self.app, 'begin') as begin, patch.object(self.app, 'save_config', return_value=True), \
                patch.object(self.app, 'config', return_value=Mock()):
            self.app.apply_answers(False)
        with patch('modules.chapter_tests.apply_review', AsyncMock(return_value='saved')) as apply:
            asyncio.run(begin.call_args.args[1](lambda event: None))
        apply.assert_awaited_once()
        self.assertIsNone(apply.call_args.kwargs['question_ids'])

    def test_batch_apply_continues_after_one_test_fails_and_reports_summary(self):
        run, d1, r1 = self.result_fixture('20261009-140000-123456')
        _, d2, r2 = self.result_fixture('20261009-140000-123456', test=2)
        self.app.update_answer(r1, d1)
        self.app.update_answer(r2, d2)
        self.app.select_result_scope(run)
        self.app.answer_tree.selection_set([self.app.result_tests[d1]['iid'], self.app.result_tests[d2]['iid']])
        with patch.object(self.app, 'begin') as begin, patch.object(self.app, 'save_config', return_value=True), \
                patch.object(self.app, 'config', return_value=Mock()):
            self.app.apply_answers(True)
        events = []
        with patch('modules.chapter_tests.apply_review', AsyncMock(side_effect=[LLMError('fixture failure'), 'submitted'])) as apply:
            result = asyncio.run(begin.call_args.args[1](events.append))
        self.assertEqual(apply.await_count, 2)
        self.assertEqual(result, {'batch': True, 'total': 2, 'completed': 1, 'failed': 1, 'skipped': 0})
        self.assertTrue(any(e['kind'] == 'test_started' and e['test'] == 2 for e in events))
        self.assertTrue(any(e['kind'] == 'test_finished' and e['status'] == 'failed' for e in events))
        self.app.controller.events.put({'kind': 'done', 'name': '交卷所选章节', 'result': result})
        self.app.poll()
        self.assertIn('完成 1 个章节', self.app.status.get())
        self.assertIn('失败 1 个', self.app.status.get())
        with patch('modules.chapter_tests.apply_review', AsyncMock(side_effect=asyncio.CancelledError())) as apply:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(begin.call_args.args[1](lambda event: None))
        self.assertEqual(apply.await_count, 1)

    def test_restart_discovers_local_runs_and_restores_latest_results(self):
        first, d1, _ = self.result_fixture('20261008-230000-123456')
        second, d2, _ = self.result_fixture('20261009-080000-123456', course=2, test=3, name='第三章')
        self.app.close()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = AutoCourseApp(self.root, self.path)
        self.root.update()
        self.assertEqual(set(self.app.result_runs), {first, second})
        self.assertEqual(self.app.view_run, second)
        self.assertEqual({directory for _, directory in self.app.result_rows.values()}, {d2})
        self.assertIn('课程 2', self.app.result_tests[d2]['label'])
        self.app.select_result_scope(first, d1)
        self.assertIn(d1, {directory for _, directory in self.app.result_rows.values()})

    def test_new_run_keeps_old_results_and_marks_its_own_test_status(self):
        first, d1, _ = self.result_fixture('20261009-140000-123456')
        second, d2, _ = self.result_fixture('20261009-150000-123456', test=2)
        self.app.refresh_results()
        self.app.select_result_scope(first)
        self.app.controller.events.put({'kind': 'artifact', 'path': str(second)})
        self.app.controller.events.put({'kind': 'test_started', 'directory': str(d2), 'course': 1, 'test': 2, 'name': '第二章'})
        self.app.controller.events.put({'kind': 'test_finished', 'directory': str(d2), 'course': 1, 'test': 2, 'status': 'saved'})
        self.app.controller.events.put({'kind': 'done', 'name': '回填并暂存', 'result': 0})
        self.app.poll()
        self.assertEqual(self.app.view_run, second)
        self.assertFalse(self.app.answer_tree.item(self.app.result_runs[first]['iid'], 'open'))
        self.assertEqual(self.app.answer_tree.set(self.app.result_tests[d2]['iid'], 'state'), '已暂存')
        self.assertEqual(self.app.answer_tree.set(self.app.result_tests[d1]['iid'], 'state'), '预览完成')
        log = self.app.log_text.get('1.0', 'end')
        self.assertIn('2026-10-09 15:00:00.123456', log)
        self.assertIn('测试 2', log)
        self.assertIn('第二章', self.app.result_tests[d2]['label'])
        self.app.select_result_scope(None, d1)
        self.assertTrue(self.app.answer_tree.item(self.app.result_runs[first]['iid'], 'open'))

    def test_failed_single_question_retry_preserves_review_and_saves_response_diagnostics(self):
        run, directory, record = self.result_fixture('20261009-160000-123456')
        original = (directory / 'review.json').read_bytes()
        self.app.update_answer(record, directory)
        self.app.answer_tree.selection_set('r0')
        failure = LLMError('fixture format failure')
        failure.response_diagnostics = [{'attempt': 1, 'content': '{"option_ids":[]}'}]
        settings = LLMSettings('https://llm.test/v1', 'fixture-key', 'fixture', retry_delay=0)
        with patch.object(self.app, 'begin') as begin, patch.object(self.app, 'save_config', return_value=True), \
                patch.object(self.app, 'config', return_value=Mock()), \
                patch('modules.gui_app.LLMSettings.from_config', return_value=settings), \
                patch('modules.gui_app.VisionLLM') as model:
            model.return_value.answer = AsyncMock(side_effect=failure)
            self.app.retry_answer()
            with self.assertRaisesRegex(LLMError, 'fixture format failure'):
                asyncio.run(begin.call_args.args[1](lambda event: None))
        self.assertEqual((directory / 'review.json').read_bytes(), original)
        diagnostics = list((directory / 'response-errors').glob('*-question-1.json'))
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(json.loads(diagnostics[0].read_text(encoding='utf-8'))['responses'], failure.response_diagnostics)

    def test_defaults_and_two_model_selection_persist(self):
        self.assertEqual(self.app.mode.get(), '生成答案预览')
        self.assertIn('glm-5.3-flash', self.app.profile_select.cget('values')[0])
        self.assertIn('deepseek-v4-flash-vision-exp', self.app.profile_select.cget('values')[1])
        self.app.active_profile.set('2')
        self.assertTrue(self.app.save_config(quiet=True))
        self.assertEqual(self.app.store.get('llm-option', 'modelProfile'), '2')
        self.assertEqual(self.app.store.get('llm-option', 'key'), 'fixture-secret')
        self.assertGreater(self.app.log_text.winfo_height(), 30)

    def test_overwrite_is_off_by_default_and_persists_when_enabled(self):
        toggle = self.app.variable('llm-option', 'overwriteAnswers')
        self.assertEqual(toggle.get(), 'False')
        toggle.set('True')
        self.assertTrue(self.app.save_config(quiet=True))
        self.assertEqual(self.app.store.get('llm-option', 'overwriteAnswers'), 'True')
        config = Config.__new__(Config)
        config._config = self.app.store.parser
        self.assertTrue(config.get_bool_field('llm-option', 'overwriteAnswers'))

    def test_result_select_all_respects_filters_and_can_clear_selection(self):
        run, d1, r1 = self.result_fixture('20261009-140000-123456')
        _, d2, r2 = self.result_fixture('20261009-140000-123456', test=2)
        other, d3, r3 = self.result_fixture('20261009-150000-123456', test=3)
        for record, directory in ((r1, d1), (r2, d2), (r3, d3)):
            self.app.update_answer(record, directory)
        self.app.select_result_scope(run)
        self.app.answer_tree.item(self.app.result_runs[run]['iid'], open=False)
        self.app.results_select_all.invoke()
        self.assertEqual(set(self.app.answer_tree.selection()),
            {self.app.result_tests[d1]['iid'], self.app.result_tests[d2]['iid']})
        self.assertTrue(self.app.results_all_selected.get())
        self.assertTrue(self.app.answer_tree.item(self.app.result_runs[run]['iid'], 'open'))
        self.assertNotIn(self.app.result_tests[d3]['iid'], self.app.answer_tree.selection())
        self.app.results_select_all.invoke()
        self.assertFalse(self.app.answer_tree.selection())
        self.assertFalse(self.app.results_all_selected.get())
        self.app.select_result_scope(run, d2)
        self.app.results_select_all.invoke()
        self.assertEqual(self.app.answer_tree.selection(), (self.app.result_tests[d2]['iid'],))
        self.app.select_result_scope(other, d3)
        self.assertFalse(self.app.results_all_selected.get())
        self.app.results_select_all.invoke()
        self.assertEqual(self.app.answer_tree.selection(), (self.app.result_tests[d3]['iid'],))

    def test_select_all_excludes_completed_and_question_only_records(self):
        run, ready, _ = self.result_fixture('20261009-140000-123456')
        _, completed, _ = self.result_fixture('20261009-140000-123456', test=2)
        _, question_only, _ = self.result_fixture('20261009-140000-123456', test=3)
        (completed / 'result.json').write_text(json.dumps({'status': 'already_submitted'}), encoding='utf-8')
        (completed / 'review.json').unlink()
        (completed / 'questions.json').unlink()
        (question_only / 'review.json').unlink()
        self.app.refresh_results()
        self.app.select_result_scope(run)
        self.app.results_select_all.invoke()
        self.assertEqual(self.app.answer_tree.selection(), (self.app.result_tests[ready]['iid'],))
        self.app.answer_tree.selection_set(self.app.result_tests[question_only]['iid'])
        with patch.object(self.app, 'begin') as begin, patch('modules.gui_app.messagebox.showinfo') as info:
            self.app.apply_answers(False)
        begin.assert_not_called()
        self.assertEqual(info.call_args.args[0], '尚无完整答案')
        self.assertIn('生成答案预览', info.call_args.args[1])
        self.app.select_result_scope(run, completed)
        self.app.results_select_all.invoke()
        self.assertFalse(self.app.answer_tree.selection())
        self.assertIn('没有可回填', self.app.result_detail.get())

    def test_result_toolbar_remains_visible_when_chapters_are_hidden(self):
        self.root.geometry('980x700')
        self.app.focus_results()
        self.root.update()
        self.assertFalse(self.app.show_chapters.get())
        for widget in (self.app.results_select_all, self.app.overwrite_checkbox):
            self.assertTrue(widget.winfo_ismapped())
            self.assertEqual(widget.master, self.app.results_toolbar)
            x = widget.winfo_rootx() - self.root.winfo_rootx()
            y = widget.winfo_rooty() - self.root.winfo_rooty()
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + widget.winfo_width(), self.root.winfo_width())
            self.assertLessEqual(y + widget.winfo_height(), self.root.winfo_height())
        self.assertEqual(self.app.variable('llm-option', 'overwriteAnswers').get(), 'False')
        self.app.overwrite_checkbox.invoke()
        self.assertEqual(self.app.variable('llm-option', 'overwriteAnswers').get(), 'True')

    def prepare_visible_result(self):
        run, directory, record = self.result_fixture('20261010-140000-123456')
        self.app.update_answer(record, directory)
        self.app.select_result_scope(run, directory)
        self.app.answer_tree.item(self.app.result_runs[run]['iid'], open=True)
        self.app.answer_tree.item(self.app.result_tests[directory]['iid'], open=True)
        self.app.chapter_tree.insert('', 'end', iid='fixture-chapter', values=('✓', '1', '示例章节', '就绪'))
        self.app.tabs.select(self.app.tests_tab)
        self.root.update()

    def assert_result_visible(self):
        for widget in (self.app.results_toolbar, self.app.answer_tree, self.app.results_actions):
            self.assertTrue(widget.winfo_ismapped())
            self.assertGreater(widget.winfo_height(), 1)
            self.assertGreaterEqual(widget.winfo_rooty(), self.app.tests_tab.winfo_rooty())
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                 self.app.tests_tab.winfo_rooty() + self.app.tests_tab.winfo_height())
        self.app.answer_tree.see('r0')
        self.root.update()
        bbox = self.app.answer_tree.bbox('r0')
        self.assertTrue(bbox, '答案行应有可见位置，不能只有表头')
        self.assertLessEqual(bbox[1] + bbox[3], self.app.answer_tree.winfo_height())

    def test_answer_rows_and_actions_remain_visible_when_window_is_resized(self):
        self.prepare_visible_result()
        self.assertFalse(self.app.show_courses.get())
        for size in ('980x700', '1200x800', '1320x900', '980x700'):
            with self.subTest(size=size):
                self.root.geometry(size)
                self.root.update()
                self.assert_result_visible()
                self.assertTrue(self.app.chapter_tree.bbox('fixture-chapter'))

    def test_toggling_chapters_repeatedly_preserves_visible_answers(self):
        self.prepare_visible_result()
        self.root.geometry('980x700')
        self.root.update()
        with_chapters = self.app.answer_tree.winfo_height()
        for shown in (False, True, False, True):
            self.app.show_chapters.set(shown)
            self.app.toggle_chapter_list()
            self.root.update()
            self.assertEqual(bool(self.app.chapter_tree.winfo_ismapped()), shown)
            self.assert_result_visible()
            if shown:
                bbox = self.app.chapter_tree.bbox('fixture-chapter')
                self.assertTrue(bbox)
                self.assertLessEqual(bbox[1] + bbox[3], self.app.chapter_tree.winfo_height())
            else:
                self.assertGreater(self.app.answer_tree.winfo_height(), with_chapters)

    def test_progress_events_and_answer_table(self):
        directory = self.path / 'test-1'
        question = {'number': 1, 'question_id': 'q1', 'type_and_score': '单选题', 'stem_dom_text': '',
                    'stem_screenshot': 'missing.png', 'options': [{'id': 'a', 'text': 'A. answer'}]}
        record = {'question': question, 'status': 'answered', 'model': 'glm-5.3-flash',
                  'answer': {'option_ids': ['a'], 'confidence': 0.9, 'explanation': 'fixture'}, 'meets_confidence': True}
        self.app.controller.events.put({'kind': 'test_started', 'directory': str(directory), 'course': 1, 'test': 1})
        self.app.controller.events.put({'kind': 'answer_started', 'number': 1, 'total': 3, 'model': 'glm-5.3-flash'})
        self.app.controller.events.put({'kind': 'answer_ready', 'result': record})
        self.app.poll()
        self.assertEqual(len(self.app.answer_tree.get_children()), 1)
        self.assertIn('A. answer', self.app.answer_tree.item('r0', 'values'))
        self.assertEqual(self.app.result_rows['r0'][1], directory.resolve())

    def test_save_model_error_is_visible(self):
        self.app.controller.events.put({'kind': 'error', 'name': '回填并暂存',
            'message': '模型接口返回 HTTP 503；本次未回填或暂存。'})
        with patch('modules.gui_app.messagebox.showerror') as show_error:
            self.app.poll()
        self.assertIn('HTTP 503', self.app.status.get())
        show_error.assert_called_once()

    def test_verification_wait_and_resume_are_visible_in_result_record(self):
        directory = self.path / 'test-1'
        self.app.controller.events.put({'kind': 'test_started', 'directory': str(directory), 'course': 1, 'test': 1})
        self.app.controller.events.put({'kind': 'test_navigation', 'state': '等待验证', 'message': '请在 Chrome 完成安全验证'})
        self.app.poll()
        source = self.app.result_tests[directory.resolve()]
        self.assertEqual(self.app.answer_tree.set(source['iid'], 'state'), '等待验证')
        self.assertIn('安全验证', self.app.status.get())
        self.app.controller.events.put({'kind': 'test_navigation', 'state': '重新核对', 'message': '验证完成，重新核对题目'})
        self.app.poll()
        self.assertEqual(self.app.answer_tree.set(source['iid'], 'state'), '重新核对')

    def test_submitted_chapter_is_shown_as_skipped_without_error(self):
        self.app.controller.events.put({'kind': 'chapter', 'chapter': {
            'course': 1, 'index': 0, 'name': 'fixture chapter', 'url': 'https://chapter.test/'}})
        self.app.controller.events.put({'kind': 'test_finished', 'course': 1, 'test': 1,
            'status': 'already_submitted', 'directory': str(self.path)})
        self.app.controller.events.put({'kind': 'done', 'name': '回填并暂存该章节', 'result': 'already_submitted'})
        with patch('modules.gui_app.messagebox.showerror') as error:
            self.app.poll()
        error.assert_not_called()
        self.assertEqual(self.app.chapter_tree.set('c1-t0', 'state'), '已交卷（跳过）')
        self.assertIn('已跳过', self.app.status.get())

    def test_saving_new_threshold_refreshes_answer_status(self):
        q = {'number': 1, 'question_id': 'q1', 'type_and_score': '单选题', 'stem_dom_text': '',
             'stem_screenshot': 'missing.png', 'options': [{'id': 'a', 'text': 'A. fixture'}]}
        record = {'question': q, 'status': 'answered', 'meets_confidence': True,
                  'answer': {'option_ids': ['a'], 'confidence': 0.9, 'explanation': 'fixture'}}
        self.app.update_answer(record, self.path)
        self.app.variable('llm-option', 'minConfidence').set('0.95')
        self.assertTrue(self.app.save_config(quiet=True))
        self.assertFalse(record['meets_confidence'])
        self.assertEqual(self.app.answer_tree.item('r0', 'values')[-1], '低置信度')
        record.update(status='manual', answer={'option_ids': ['a'], 'confidence': 0, 'explanation': 'manual'})
        self.app.update_answer(record, self.path)
        self.assertEqual(self.app.answer_tree.item('r0', 'values')[-1], '手动修改')

    def test_load_legacy_results_without_question_ids(self):
        q = {'number': 1, 'type_and_score': '单选题', 'stem_dom_text': '',
             'stem_screenshot': str(self.path / 'stem.png'),
             'options': [{'id': 'a', 'text': 'A. fixture'}, {'id': 'b', 'text': 'B. fixture'}]}
        questions_path = self.path / 'questions.json'
        questions_path.write_text(json.dumps([q]), encoding='utf-8')
        original = questions_path.read_bytes()
        result_path = self.path / 'results.json'
        result_path.write_text(json.dumps([{'question_number': 1, 'question_id': 'legacy-q1',
            'stem_screenshot': q['stem_screenshot'], 'status': 'valid_response', 'model': 'fixture',
            'answer': {'option_ids': ['a'], 'confidence': 0.9, 'explanation': 'fixture'},
            'meets_configured_confidence': False}]), encoding='utf-8')
        with patch('modules.gui_app.filedialog.askopenfilename', return_value=str(result_path)), \
                patch('modules.gui_app.messagebox.showerror') as error:
            self.app.load_history()
        error.assert_not_called()
        record, directory = self.app.result_rows['r0']
        self.assertEqual(record['question']['question_id'], 'legacy-q1')
        self.assertTrue(record['meets_confidence'])
        self.assertEqual(directory, self.path.resolve())
        self.assertEqual(questions_path.read_bytes(), original)

    def test_history_does_not_match_wrong_id_by_number_or_partially_load(self):
        q = {'number': 1, 'question_id': 'q1', 'type_and_score': '单选题', 'stem_dom_text': '',
             'stem_screenshot': str(self.path / 'stem.png'),
             'options': [{'id': 'a', 'text': 'A. fixture'}]}
        (self.path / 'questions.json').write_text(json.dumps([q]), encoding='utf-8')
        first = {'question': q, 'status': 'manual',
                 'answer': {'option_ids': ['a'], 'confidence': 0, 'explanation': 'manual'}}
        second = {'question_number': 1, 'question_id': 'different-id',
                  'stem_screenshot': q['stem_screenshot'], 'status': 'valid_response'}
        result_path = self.path / 'results.json'
        result_path.write_text(json.dumps([first, second]), encoding='utf-8')
        with patch('modules.gui_app.filedialog.askopenfilename', return_value=str(result_path)), \
                patch('modules.gui_app.messagebox.showerror') as error:
            self.app.load_history()
        error.assert_called_once()
        self.assertEqual(len(self.app.answer_tree.get_children()), 0)
