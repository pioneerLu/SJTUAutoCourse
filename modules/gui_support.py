"""GUI 配置保存和可取消后台任务；不依赖 Tk，便于单独测试。"""
import asyncio
import configparser
from io import StringIO
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading

from modules.chapter_records import read_result_metadata, answer_source, discover_result_runs, load_answer_history
from modules.storage import write_text_atomic
from modules.runtime_env import project_python
from modules.course_urls import normalize_course_urls


class SettingsStore:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'config'
        self.parser = configparser.ConfigParser(interpolation=None)
        self.parser.read(self.root / 'config.example', encoding='utf-8-sig')
        self.parser.read(self.path, encoding='utf-8-sig')

    def get(self, section, option, default=''):
        return self.parser.get(section, option, raw=True, fallback=str(default))

    def courses(self):
        return [self.get('course-url', k).strip() for k in self.parser.options('course-url')
                if self.get('course-url', k).strip()]

    def save(self, values, courses):
        courses = normalize_course_urls(courses)
        # 重新读取磁盘：仅覆盖 GUI 管理的字段，保留用户修改的其他配置。
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(self.root / 'config.example', encoding='utf-8-sig')
        parser.read(self.path, encoding='utf-8-sig')
        for section, options in values.items():
            if not parser.has_section(section):
                parser.add_section(section)
            for option, value in options.items():
                parser.set(section, option, str(value))
        if not parser.has_section('course-url'):
            parser.add_section('course-url')
        for option in parser.options('course-url'):
            parser.remove_option('course-url', option)
        for i, url in enumerate(courses, 1):
            parser.set('course-url', f'URL{i}', url)
        buffer = StringIO()
        parser.write(buffer)
        write_text_atomic(self.path, buffer.getvalue())
        self.parser = parser


class GuiLogger:
    def __init__(self, events):
        from modules.logger import Logger
        self.delegate = Logger()
        self.events = events

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def info(self, text, **kwargs):
        self.delegate.info(text, **kwargs)
        self.events.put({'kind': 'log', 'message': str(text)})

    def warn(self, text, **kwargs):
        self.delegate.warn(text, **kwargs)
        self.events.put({'kind': 'log', 'message': str(text)})

    def error(self, text, **kwargs):
        self.delegate.error(text, **kwargs)
        self.events.put({'kind': 'log', 'message': str(text)})


class TaskController:
    def __init__(self):
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.running = False
        self.process = None

    def start(self, name, factory):
        if self.running:
            raise RuntimeError('已有任务运行中。')
        self.running = True
        self.cancel.clear()
        def worker():
            async def run():
                job = asyncio.create_task(factory(self.events.put))
                async def watch():
                    while not self.cancel.is_set() and not job.done():
                        await asyncio.sleep(0.1)
                    if self.cancel.is_set() and not job.done():
                        job.cancel()
                watcher = asyncio.create_task(watch())
                try:
                    return await job
                finally:
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
            try:
                result = asyncio.run(run())
                self.events.put({'kind': 'done', 'name': name, 'result': result})
            except asyncio.CancelledError:
                self.events.put({'kind': 'cancelled', 'name': name})
            except Exception as exc:
                self.events.put({'kind': 'error', 'name': name, 'message': str(exc)})
            finally:
                self.running = False
        threading.Thread(target=worker, daemon=True).start()

    def start_video(self, root):
        if self.running:
            raise RuntimeError('已有任务运行中。')
        self.running = True
        self.cancel.clear()
        def worker():
            try:
                env = dict(os.environ, PYTHONUTF8='1', PYTHONUNBUFFERED='1')
                executable = project_python(root)
                self.events.put({'kind': 'log', 'message': f'视频任务 Python：{executable}'})
                self.process = subprocess.Popen([executable, '-u', str(Path(root) / 'AutoCourse.py'),
                    '--config', str(Path(root) / 'config')], cwd=root, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', env=env)
                for line in self.process.stdout:
                    clean = re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
                    if clean:
                        self.events.put({'kind': 'log', 'message': clean})
                code = self.process.wait()
                self.events.put({'kind': 'cancelled' if self.cancel.is_set() else ('done' if code == 0 else 'error'),
                    'name': '刷课', 'result': code, 'message': f'视频任务退出码 {code}'})
            except Exception as exc:
                self.events.put({'kind': 'error', 'name': '刷课', 'message': str(exc)})
            finally:
                self.process = None
                self.running = False
        threading.Thread(target=worker, daemon=True).start()

    def stop(self):
        self.cancel.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
