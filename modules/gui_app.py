"""Tkinter 主界面：视频、章节测试、双模型配置与答案预览。"""
import asyncio
from dataclasses import asdict
from datetime import datetime
import math
import os
from pathlib import Path
import queue
import re
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from PIL import Image, ImageTk, ImageDraw, ImageFont
import sv_ttk

from modules.configs import Config
from modules.course_urls import CourseUrlError, normalize_course_urls
from modules.gui_support import SettingsStore, TaskController, GuiLogger
from modules.chapter_records import (load_answer_history, answer_source, discover_result_runs,
                                     read_result_metadata, review_record_state, question_signature, content_signature)
from modules.storage import read_json, write_json
from modules.chapter_batch import apply_review_batch
from modules.llm import LLMSettings, VisionLLM, Question, Answer, LLMError, answer_from_record


MODES = {'生成答案预览': 'preview', '回填并暂存': 'save', '自动交卷': 'submit', '仅导出题目': 'export'}
MODEL_LIST = ('qwen3-vl-plus', 'kimi-for-coding', 'qwen3.8-max', 'MiniMax-M3')
BROWSER_CHOICES = ('Chrome', 'Edge')
RESULT_STATES = {'preview': '预览完成', 'preview_partial': '部分失败', 'saved': '已暂存',
                 'submitted': '已交卷', 'already_submitted': '已交卷（跳过）',
                 'exported': '已导出', 'failed': '失败'}
RESULT_STATES['skipped_no_answers'] = '无可用答案（跳过）'


def configure_dpi():
    if os.name == 'nt':
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


class AutoCourseApp:
    def __init__(self, root, project_root):
        self.root = root
        self.project_root = Path(project_root)
        self.store = SettingsStore(project_root)
        self.controller = TaskController()
        self.vars = {}
        self.model_selectors = {}
        self.action_buttons = []
        self.chapters = {}
        self.result_rows = {}
        self.result_keys = {}
        self.result_runs = {}
        self.result_tests = {}
        self.view_run = None
        self.view_test = None
        self.run_filter = tk.StringVar(value='全部运行')
        self.test_filter = tk.StringVar(value='全部测试')
        self.result_detail = tk.StringVar(value='选择运行或测试查看本地结果。')
        self.results_all_selected = tk.BooleanVar(value=False)
        self.active_run = None
        self._log_context = ''
        self.current_directory = None
        self.status = tk.StringVar(value='就绪')
        self.active_profile = tk.StringVar(value=self.store.get('llm-option', 'modelProfile', '1') or '1')
        self.mode = tk.StringVar(value='生成答案预览')
        self.follow_tests = tk.BooleanVar(value=False)
        self.show_chapters = tk.BooleanVar(value=True)
        self.show_courses = tk.BooleanVar(value=True)
        root.title('AutoCourse · 课程与章节测试')
        width = min(1320, int(root.winfo_screenwidth() * 0.94))
        height = min(960, int(root.winfo_screenheight() * 0.90))
        root.geometry(f'{width}x{height}+40+20')
        root.minsize(min(980, width), min(700, height))
        sv_ttk.set_theme('light')
        self._build()
        root.protocol('WM_DELETE_WINDOW', self.close)
        self._poll_id = root.after(150, self.poll)
        self._history_id = root.after_idle(self.refresh_results)

    def variable(self, section, option, default=''):
        key = (section, option)
        if key not in self.vars:
            self.vars[key] = tk.StringVar(value=self.store.get(section, option, default))
        return self.vars[key]

    def field(self, parent, row, label, section, option, default='', secret=False, width=48, choices=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', padx=(0, 15), pady=4)
        if choices is not None:
            entry = ttk.Combobox(parent, textvariable=self.variable(section, option, default),
                                 values=choices, width=width, state='normal')
        else:
            entry = ttk.Entry(parent, textvariable=self.variable(section, option, default), width=width, show='*' if secret else '')
        entry.grid(row=row, column=1, sticky='ew', pady=4)
        if secret:
            shown = tk.BooleanVar(value=False)
            ttk.Checkbutton(parent, text='显示', variable=shown,
                command=lambda: entry.configure(show='' if shown.get() else '*')).grid(row=row, column=2, padx=8)
        parent.columnconfigure(1, weight=1)
        return entry

    def button(self, parent, label, command, **pack):
        button = ttk.Button(parent, text=label, command=command)
        button.pack(side='left', padx=(0, 8), **pack)
        self.action_buttons.append(button)
        return button

    def _build(self):
        header = ttk.Frame(self.root, padding=(20, 14))
        header.pack(fill='x')
        ttk.Label(header, text='AutoCourse', font=('Microsoft YaHei', 20, 'bold')).pack(side='left')
        ttk.Label(header, text='Chrome / Edge · 视频学习与章节测试', foreground='#64748b').pack(side='left', padx=20)
        ttk.Button(header, text='保存配置', command=self.save_config).pack(side='right')
        ttk.Checkbutton(header, text='显示课程链接', variable=self.show_courses,
                        command=self.toggle_course_links).pack(side='right', padx=15)
        course = ttk.LabelFrame(self.root, text='课程链接（支持 Markdown、附带说明和省略 https://；保存时自动整理）', padding=10)
        self.course_box = course
        course.pack(fill='x', padx=20)
        self.course_text = tk.Text(course, height=2, font=('Microsoft YaHei', 10), wrap='word')
        self.course_text.pack(fill='x')
        self.course_text.insert('1.0', '\n'.join(self.store.courses()))
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill='both', expand=True, padx=20, pady=10)
        self.video_tab = ttk.Frame(self.tabs, padding=14)
        self.tests_tab = ttk.Frame(self.tabs, padding=14)
        self.models_tab = ttk.Frame(self.tabs, padding=14)
        for tab, title in [(self.video_tab, '视频学习'), (self.tests_tab, '章节测试'), (self.models_tab, '模型设置')]:
            self.tabs.add(tab, text=title)
        self.tabs.bind('<<NotebookTabChanged>>', self.tab_changed)
        self._build_video()
        self._build_tests()
        self._build_models()
        self.profile_label = tk.StringVar()
        self.profile_select.configure(textvariable=self.profile_label, width=25)
        self.profile_select.bind('<<ComboboxSelected>>', lambda _: self.active_profile.set(self.profile_label.get().split(' · ', 1)[0]))
        for slot in ('1', '2'):
            self.variable(f'llm-model-{slot}', 'model').trace_add('write', lambda *_: self.refresh_model_picker())
        self.active_profile.trace_add('write', lambda *_: self.refresh_model_picker())
        self.refresh_model_picker()
        bottom = ttk.Frame(self.root, padding=(20, 0, 20, 12))
        self.tabs.pack_forget()
        bottom.pack(side='bottom', fill='x')
        self.tabs.pack(fill='both', expand=True, padx=20, pady=10)
        line = ttk.Frame(bottom)
        line.pack(fill='x')
        ttk.Label(line, textvariable=self.status).pack(side='left', fill='x', expand=True)
        self.stop_button = ttk.Button(line, text='停止任务', command=self.stop, state='disabled')
        self.stop_button.pack(side='right')
        self.progress = ttk.Progressbar(bottom, maximum=100)
        self.progress.pack(fill='x', pady=6)
        self.log_text = tk.Text(bottom, height=3, state='disabled', wrap='word', font=('Consolas', 9))
        self.log_text.pack(fill='x')

    def refresh_model_picker(self):
        labels = {slot: slot + ' · ' + (self.variable(f'llm-model-{slot}', 'model').get().strip() or '待配置') for slot in ('1', '2')}
        self.profile_select.configure(values=tuple(labels.values()))
        self.profile_label.set(labels.get(self.active_profile.get(), labels['1']))

    def _build_video(self):
        form = ttk.Frame(self.video_tab)
        form.pack(fill='x')
        self.field(form, 0, '手机号（可留空手动登录）', 'user-account', 'username')
        self.field(form, 1, '密码', 'user-account', 'password', secret=True)
        self.field(form, 2, '每门课时限 / 分钟（0 不限）', 'course-option', 'limitMaxTime', '0')
        self.field(form, 3, '播放倍速（0.5～1.8）', 'course-option', 'limitSpeed', '1.0')
        driver = self.variable('browser-option', 'driver', 'Chrome')
        driver.set({name.lower(): name for name in BROWSER_CHOICES}.get(driver.get().strip().lower(), 'Chrome'))
        ttk.Label(form, text='浏览器 / 可执行文件（可留空）').grid(
            row=4, column=0, sticky='w', padx=(0, 15), pady=4)
        browser_controls = ttk.Frame(form)
        browser_controls.grid(row=4, column=1, sticky='ew', pady=4)
        self.browser_select = ttk.Combobox(browser_controls, textvariable=driver, values=BROWSER_CHOICES,
                                          state='readonly', width=10)
        self.browser_select.pack(side='left', padx=(0, 8))
        ttk.Entry(browser_controls, textvariable=self.variable('browser-option', 'EXE_PATH')).pack(
            side='left', fill='x', expand=True)
        self._selected_browser = driver.get().lower()
        driver.trace_add('write', self.browser_changed)
        choices = ttk.Frame(self.video_tab)
        choices.pack(fill='x', pady=2)
        for label, section, option, default in [('静音', 'course-option', 'soundOff', 'True'),
            ('隐藏浏览器窗口', 'script-option', 'enableHideWindow', 'False'),
            ('自动滑块', 'script-option', 'enableAutoCaptcha', 'False')]:
            ttk.Checkbutton(choices, text=label, variable=self.variable(section, option, default),
                onvalue='True', offvalue='False').pack(side='left', padx=(0, 20))
        ttk.Checkbutton(self.video_tab, text='视频任务正常结束后，运行已勾选的章节测试（使用章节测试页的模式）',
            variable=self.follow_tests).pack(anchor='w', pady=2)
        controls = ttk.Frame(self.video_tab)
        controls.pack(fill='x', pady=2)
        self.button(controls, '保存并开始视频学习', self.start_video)
        self.button(controls, '打开日志目录', lambda: os.startfile(self.project_root / 'logs'))

    def browser_changed(self, *_):
        driver = self.variable('browser-option', 'driver').get().strip().lower()
        if driver != self._selected_browser:
            self.variable('browser-option', 'EXE_PATH').set('')
            self.variable('browser-option', 'attachExistingChrome', 'False').set('False')
            self._selected_browser = driver

    def _build_tests(self):
        self.tests_tab.columnconfigure(0, weight=1)
        # 章节列表和答案表共享剩余高度，答案区优先占 3/4。
        self.tests_tab.rowconfigure(2, weight=1, uniform='test_tables')
        self.tests_tab.rowconfigure(6, weight=3, uniform='test_tables')
        controls = ttk.Frame(self.tests_tab)
        controls.grid(row=0, column=0, sticky='ew')
        self.button(controls, '读取章节', self.scan_chapters)
        self.button(controls, '全选', lambda: self.select_chapters(True))
        self.button(controls, '取消全选', lambda: self.select_chapters(False))
        ttk.Checkbutton(controls, text='显示章节列表', variable=self.show_chapters,
                        command=self.toggle_chapter_list).pack(side='left', padx=8)
        controls = ttk.Frame(self.tests_tab)
        controls.grid(row=1, column=0, sticky='ew', pady=(8, 0))
        ttk.Label(controls, text='使用模型').pack(side='left', padx=(12, 6))
        self.profile_select = ttk.Combobox(controls, textvariable=self.active_profile, values=('1', '2'), width=4, state='readonly')
        self.profile_select.pack(side='left')
        ttk.Label(controls, text='运行模式').pack(side='left', padx=(12, 6))
        ttk.Combobox(controls, textvariable=self.mode, values=tuple(MODES), width=15, state='readonly').pack(side='left')
        self.button(controls, '开始所选章节', self.start_tests)
        chapter_box = ttk.Frame(self.tests_tab)
        self.chapter_box = chapter_box
        chapter_box.grid(row=2, column=0, sticky='nsew', pady=6)
        self.chapter_tree = ttk.Treeview(chapter_box, columns=('selected', 'course', 'chapter', 'state'), show='headings', height=1)
        self.chapter_min_height = self.chapter_tree.winfo_reqheight() + 12
        self.tests_tab.rowconfigure(2, minsize=self.chapter_min_height)
        for name, title, width in [('selected', '勾选', 55), ('course', '课程', 60), ('chapter', '章节', 570), ('state', '状态', 180)]:
            self.chapter_tree.heading(name, text=title)
            self.chapter_tree.column(name, width=width, stretch=name == 'chapter')
        self.chapter_tree.pack(side='left', fill='both', expand=True)
        scrollbar = ttk.Scrollbar(chapter_box, orient='vertical', command=self.chapter_tree.yview)
        scrollbar.pack(side='right', fill='y')
        self.chapter_tree.configure(yscrollcommand=scrollbar.set)
        self.chapter_tree.bind('<Button-1>', self.toggle_chapter)
        self.results_toolbar = ttk.Frame(self.tests_tab)
        self.results_toolbar.grid(row=3, column=0, sticky='ew')
        self.results_heading = ttk.Label(self.results_toolbar, text='运行记录与答案', font=('Microsoft YaHei', 11, 'bold'))
        self.results_heading.pack(side='left')
        self.results_select_all = ttk.Checkbutton(self.results_toolbar, text='全选当前范围',
            variable=self.results_all_selected, command=self.toggle_all_results)
        self.results_select_all.pack(side='left', padx=(16, 0))
        self.overwrite_checkbox = ttk.Checkbutton(self.results_toolbar, text='覆盖已有答案',
            variable=self.variable('llm-option', 'overwriteAnswers', 'False'), onvalue='True', offvalue='False')
        self.overwrite_checkbox.pack(side='left', padx=(16, 0))
        filters = ttk.Frame(self.tests_tab)
        filters.grid(row=4, column=0, sticky='ew', pady=(6, 0))
        ttk.Label(filters, text='运行').grid(row=0, column=0, padx=(0, 6))
        self.run_select = ttk.Combobox(filters, textvariable=self.run_filter, state='readonly', width=35)
        self.run_select.grid(row=0, column=1, sticky='ew')
        self.run_select.bind('<<ComboboxSelected>>', self.change_result_run)
        ttk.Label(filters, text='测试').grid(row=0, column=2, padx=(12, 6))
        self.test_select = ttk.Combobox(filters, textvariable=self.test_filter, state='readonly', width=32)
        self.test_select.grid(row=0, column=3, sticky='ew')
        self.test_select.bind('<<ComboboxSelected>>', self.change_result_test)
        ttk.Button(filters, text='刷新运行记录', command=self.refresh_results).grid(row=0, column=4, padx=(10, 0))
        filters.columnconfigure(1, weight=1)
        filters.columnconfigure(3, weight=1)
        self.result_detail_label = ttk.Label(self.tests_tab, textvariable=self.result_detail, foreground='#64748b')
        self.result_detail_label.grid(row=5, column=0, sticky='ew', pady=(4, 0))
        self.result_detail_label.bind('<Configure>', lambda e: self.result_detail_label.configure(wraplength=max(100, e.width)))
        answer_box = ttk.Frame(self.tests_tab)
        self.answer_box = answer_box
        answer_box.grid(row=6, column=0, sticky='nsew', pady=4)
        self.answer_tree = ttk.Treeview(answer_box, columns=('number', 'type', 'model', 'answer', 'confidence', 'state'), show='tree headings', height=6, selectmode='extended')
        self.answer_tree.heading('#0', text='运行 / 课程 / 测试')
        self.answer_tree.column('#0', width=285, minwidth=160, stretch=False)
        for name, title, width in [('number', '题号', 45), ('type', '题型', 75),
            ('model', '模型', 180), ('answer', '答案', 300), ('confidence', '置信度', 65), ('state', '状态', 110)]:
            self.answer_tree.heading(name, text=title)
            self.answer_tree.column(name, width=width, stretch=name == 'answer')
        self.answer_tree.tag_configure('failed', foreground='#b91c1c')
        self.answer_tree.tag_configure('low', foreground='#b45309')
        self.answer_tree.grid(row=0, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(answer_box, orient='vertical', command=self.answer_tree.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(answer_box, orient='horizontal', command=self.answer_tree.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        answer_box.rowconfigure(0, weight=1)
        answer_box.columnconfigure(0, weight=1)
        self.answer_tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        self.answer_tree.bind('<<TreeviewSelect>>', self.result_selected)
        self.answer_tree.bind('<Double-1>', lambda _: self.show_answer() if self.answer_tree.selection() and self.answer_tree.selection()[0] in self.result_rows else None)
        actions = ttk.Frame(self.tests_tab)
        self.results_actions = actions
        actions.grid(row=7, column=0, sticky='ew')
        self.button(actions, '题目详情 / 修改', self.show_answer)
        self.button(actions, '重试选中题', self.retry_answer)
        self.button(actions, '回填并暂存所选', lambda: self.apply_answers(False))
        self.button(actions, '交卷所选章节', lambda: self.apply_answers(True))
        self.button(actions, '打开历史结果', self.load_history)
        ttk.Label(self.tests_tab, text='全选按当前运行 / 测试筛选范围选中测试分组；覆盖关闭时只补空题和低置信度题。Ctrl / Shift 可多选题目。',
                  foreground='#64748b').grid(row=8, column=0, sticky='ew', pady=(4, 0))
        self.refresh_result_filters()

    def _build_models(self):
        canvas = tk.Canvas(self.models_tab, highlightthickness=0, background='#fafafa')
        scrollbar = ttk.Scrollbar(self.models_tab, orient='vertical', command=canvas.yview)
        scrollbar.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        form = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=form, anchor='nw')
        form.bind('<Configure>', lambda _: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        common = ttk.LabelFrame(form, text='公共接口（模型槽内留空的 URL / Key 使用此处）', padding=10)
        common.pack(fill='x')
        self.field(common, 0, '接口 URL（基础地址或完整接口）', 'llm-option', 'url')
        self.field(common, 1, 'API Key', 'llm-option', 'key', secret=True)
        profiles = ttk.Frame(form)
        profiles.pack(fill='x', pady=10)
        for index in ('1', '2'):
            frame = ttk.LabelFrame(profiles, text=f'模型 {index}', padding=10)
            frame.pack(side='left', fill='both', expand=True, padx=(0, 10) if index == '1' else 0)
            section = f'llm-model-{index}'
            fallback = self.store.get('llm-option', 'model' if index == '1' else 'model_ds')
            self.model_selectors[index] = self.field(frame, 0, '模型名称（可选择或输入）', section,
                'model', fallback, width=25, choices=MODEL_LIST)
            if not self.variable(section, 'model').get().strip():
                self.variable(section, 'model').set(fallback)
            self.field(frame, 1, '独立 URL（可留空）', section, 'url', width=25)
            self.field(frame, 2, '独立 Key（可留空）', section, 'key', secret=True, width=25)
            self.field(frame, 3, '中继适配器（留空继承）', section, 'adapter', width=25,
                       choices=('', 'auto', 'generic', 'minimax', 'qwen', 'glm', 'kimi', 'deepseek', 'siliconflow'))
            self.field(frame, 4, '输出约束（留空继承）', section, 'responseMode', width=25,
                       choices=('', 'auto', 'prompt', 'json', 'schema'))
        options = ttk.Frame(form)
        options.pack(fill='x')
        self.field(options, 0, '置信度阈值（0～1）', 'llm-option', 'minConfidence', '0.8')
        self.field(options, 1, '请求超时 / 秒', 'llm-option', 'timeout', '90')
        self.field(options, 2, '失败重试等待 / 秒', 'llm-option', 'retryDelay', '15')
        self.field(options, 3, '题目请求间隔 / 秒', 'llm-option', 'requestInterval', '30')
        actions = ttk.Frame(form)
        actions.pack(fill='x', pady=10)
        self.button(actions, '测试所选模型接口（自制图片）', self.test_model)
        ttk.Label(actions, text='当前选择由“章节测试”页的模型 1 / 2 决定。').pack(side='left')

    def save_config(self, quiet=False):
        if self.controller.running:
            if not quiet:
                messagebox.showinfo('任务运行中', '请先停止任务后保存配置。')
            return False
        try:
            courses = normalize_course_urls(self.course_text.get('1.0', 'end'))
            driver = self.variable('browser-option', 'driver').get().strip().lower()
            browser_names = {name.lower(): name for name in BROWSER_CHOICES}
            if driver not in browser_names:
                raise ValueError('请选择 Chrome 或 Edge 浏览器。')
            for section, option, low, high in [('course-option', 'limitMaxTime', 0, None),
                ('course-option', 'limitSpeed', 0.5, 1.8), ('llm-option', 'minConfidence', 0, 1),
                ('llm-option', 'timeout', 0.01, None), ('llm-option', 'retryDelay', 0, None),
                ('llm-option', 'requestInterval', 0, None)]:
                value = float(self.variable(section, option).get())
                if not math.isfinite(value) or value < low or (high is not None and value > high):
                    raise ValueError(f'{option} 不在允许范围。')
            values = {}
            for (section, option), variable in self.vars.items():
                values.setdefault(section, {})[option] = variable.get().strip() if option != 'password' else variable.get()
            values.setdefault('browser-option', {})['driver'] = browser_names[driver]
            values.setdefault('llm-option', {})['modelProfile'] = self.active_profile.get()
            previous_courses = self.store.courses()
            try:
                previous_courses = normalize_course_urls(previous_courses, required=False)
            except CourseUrlError:
                # An invalid old setting must not prevent the user from correcting it.
                pass
            self.store.save(values, courses)
            self.course_text.delete('1.0', 'end')
            self.course_text.insert('1.0', '\n'.join(courses))
            if previous_courses != courses:
                self.chapters.clear()
                self.chapter_tree.delete(*self.chapter_tree.get_children())
            for record, directory in list(self.result_rows.values()):
                self.update_answer(record, directory)
            if not quiet:
                self.status.set(f'配置已保存，已识别 {len(courses)} 门课程')
            return True
        except (ValueError, OSError) as exc:
            messagebox.showerror('配置错误', str(exc))
            return False

    def config(self):
        return Config(self.store.path, self.project_root / 'data' / 'mirrors.json')

    def begin(self, name, factory, directory=None):
        try:
            if self.controller.running:
                raise RuntimeError('已有任务运行中。')
            self.active_run = None
            self.current_directory = Path(directory).resolve() if directory else None
            self._log_context = ''
            if directory:
                source = self.register_result_test(directory)
                self._log_context = self.result_runs[source['run']]['label'] + ' · ' + source['label']
            self.controller.start(name, factory)
            self.log(f'—— {name}开始 ——')
            self.status.set(name + '…')
            self.progress.configure(value=0)
        except RuntimeError as exc:
            messagebox.showinfo('任务状态', str(exc))

    def scan_chapters(self):
        if not self.save_config(quiet=True):
            return
        self.chapters.clear()
        self.chapter_tree.delete(*self.chapter_tree.get_children())
        self.show_chapters.set(True)
        self.toggle_chapter_list()
        config = self.config()
        async def work(callback):
            from modules.chapter_tests import list_chapter_tests
            return await list_chapter_tests(config, GuiLogger(self.controller.events),
                self.project_root / 'data' / 'cookies.json', callback)
        self.begin('读取章节', work)

    def toggle_chapter_list(self):
        if self.show_chapters.get():
            self.tests_tab.rowconfigure(2, weight=1, uniform='test_tables', minsize=self.chapter_min_height)
            self.chapter_box.grid()
        else:
            self.chapter_box.grid_remove()
            self.tests_tab.rowconfigure(2, weight=0, uniform='', minsize=0)

    def toggle_course_links(self):
        if self.show_courses.get():
            self.course_box.pack(fill='x', padx=20, before=self.tabs)
        else:
            self.course_box.pack_forget()

    def tab_changed(self, _=None):
        if self.tabs.select() == str(self.video_tab):
            self.show_courses.set(True)
            self.toggle_course_links()
        elif self.tabs.select() == str(self.tests_tab):
            # 课程链接仍可从顶部展开；进入章节页时给答案和章节表留出空间。
            self.show_courses.set(False)
            self.toggle_course_links()

    def focus_results(self):
        self.tabs.select(self.tests_tab)
        self.show_courses.set(False)
        self.toggle_course_links()
        self.show_chapters.set(False)
        self.toggle_chapter_list()

    def select_chapters(self, selected):
        for iid, chapter in self.chapters.items():
            chapter['selected'] = selected
            self.chapter_tree.set(iid, 'selected', '☑' if selected else '☐')

    def toggle_chapter(self, event):
        if self.controller.running or self.chapter_tree.identify_column(event.x) != '#1':
            return
        iid = self.chapter_tree.identify_row(event.y)
        if iid in self.chapters:
            entry = self.chapters[iid]
            entry['selected'] = not entry['selected']
            self.chapter_tree.set(iid, 'selected', '☑' if entry['selected'] else '☐')

    def start_tests(self):
        if not self.save_config(quiet=True):
            return
        selected = {}
        for chapter in self.chapters.values():
            if chapter.get('selected'):
                selected.setdefault(chapter['course'], []).append(chapter['index'])
        if not selected:
            messagebox.showinfo('选择章节', '请先读取章节，并勾选本次运行的章节。')
            return
        config = self.config()
        profile, mode = self.active_profile.get(), MODES[self.mode.get()]
        if mode != 'export':
            try:
                LLMSettings.from_config(config, profile).validate()
            except LLMError as exc:
                messagebox.showerror('模型配置', str(exc))
                self.tabs.select(self.models_tab)
                return
        async def work(callback):
            from modules.chapter_tests import run_chapter_tests
            return await run_chapter_tests(config, GuiLogger(self.controller.events), self.project_root / 'data' / 'cookies.json',
                mode=mode, selected_tests=selected, callback=callback, model_profile=profile)
        self.begin(self.mode.get(), work)

    def start_video(self):
        if not self.save_config(quiet=True):
            return
        if self.follow_tests.get() and not any(c.get('selected') for c in self.chapters.values()):
            messagebox.showinfo('选择章节', '请先在章节测试页读取并勾选视频任务结束后要运行的测试。')
            return
        self._log_context = '视频学习'
        self.log('—— 视频学习开始 ——')
        self.controller.start_video(self.project_root)
        self.status.set('视频任务运行中…')

    def register_result_run(self, directory):
        directory = Path(directory).resolve()
        if directory in self.result_runs:
            info = self.result_runs[directory]
            metadata = read_result_metadata(directory / 'run.json')
            if metadata:
                info['metadata'] = metadata
                state = '进行中' if directory == self.active_run else {'completed': '已结束', 'cancelled': '已停止', 'failed': '失败', 'running': '未完成'}.get(metadata.get('status'), '历史记录')
                self.answer_tree.set(info['iid'], 'state', state)
                self.answer_tree.set(info['iid'], 'model', metadata.get('model', ''))
            return info
        source = answer_source(directory)
        metadata = source['metadata']
        mode = {'preview': '预览', 'export': '导出', 'save': '暂存', 'submit': '交卷'}.get(metadata.get('mode'), '')
        label = source['run_label'] + (f' · {mode}' if mode else '')
        if label in {r['label'] for r in self.result_runs.values()}:
            label += f' · #{len(self.result_runs) + 1}'
        iid = f'run-{len(self.result_runs)}'
        state = {'completed': '已结束', 'cancelled': '已停止', 'failed': '失败', 'running': '未完成'}.get(metadata.get('status'), '历史记录')
        info = {'directory': directory, 'iid': iid, 'label': label, 'metadata': metadata}
        self.result_runs[directory] = info
        text = label[5:] if re.fullmatch(r'\d{4}-.*', label) else label
        self.answer_tree.insert('', 0, iid=iid, text=text, open=True,
                                values=('', '', metadata.get('model', ''), '', '', state))
        return info

    def register_result_test(self, directory, *, course=None, test=None, name=None):
        directory = Path(directory).resolve()
        if directory in self.result_tests:
            info = self.result_tests[directory]
            course = course if course is not None else info['course']
            test = test if test is not None else info['test']
            name = name or info['name']
            if course is not None and test is not None:
                label = f'课程 {course} · 测试 {test}' + (f' · {name}' if name else '')
                if label != info['label']:
                    info.update(course=course, test=test, name=name, label=label)
                    self.answer_tree.item(info['iid'], text=label)
                    self.refresh_result_filters()
            if not info['context']:
                info['context'] = read_result_metadata(directory / 'context.json')
            return info
        source = answer_source(directory)
        course = course if course is not None else source['course']
        test = test if test is not None else source['test']
        name = name or source['context'].get('chapter_name', '')
        chapter = self.chapters.get(f'c{course}-t{test - 1}') if isinstance(test, int) else None
        if not name and chapter and (not source['context'].get('url') or source['context']['url'] == chapter['url']):
            name = chapter['name']
        label = source['test_label']
        if course is not None and test is not None:
            label = f'课程 {course} · 测试 {test}' + (f' · {name}' if name else '')
        run = self.register_result_run(source['run'])
        iid = f'test-{len(self.result_tests)}'
        status = read_result_metadata(directory / 'result.json').get('status', '')
        info = {'directory': directory, 'run': source['run'], 'iid': iid, 'label': label,
                'context': source['context'], 'loaded': False, 'course': course, 'test': test, 'name': name}
        self.result_tests[directory] = info
        self.answer_tree.insert(run['iid'], 'end', iid=iid, text=label, open=True,
                                values=('', '', '', '', '', RESULT_STATES.get(status, status)))
        self.refresh_result_filters()
        return info

    def refresh_result_filters(self):
        self.run_choices = {r['label']: path for path, r in self.result_runs.items()}
        self.run_select.configure(values=('全部运行', *reversed(tuple(self.run_choices))))
        self.run_filter.set(self.result_runs[self.view_run]['label'] if self.view_run in self.result_runs else '全部运行')
        self.test_choices = {}
        for path, item in self.result_tests.items():
            if self.view_run is None or item['run'] == self.view_run:
                label = item['label'] if self.view_run else self.result_runs[item['run']]['label'] + ' · ' + item['label']
                if label in self.test_choices:
                    label += ' · ' + path.name
                self.test_choices[label] = path
        self.test_select.configure(values=('全部测试', *self.test_choices))
        self.test_filter.set(next((label for label, path in self.test_choices.items() if path == self.view_test), '全部测试'))
        self.apply_result_filter()

    def directory_in_view(self, directory):
        info = self.result_tests.get(Path(directory).resolve())
        return bool(info and (self.view_run is None or info['run'] == self.view_run)
                    and (self.view_test is None or info['directory'] == self.view_test))

    def result_group_ids_in_view(self):
        return [item['iid'] for directory, item in self.result_tests.items()
                if self.directory_in_view(directory) and review_record_state(directory) == 'ready']

    def sync_result_select_all(self):
        groups = self.result_group_ids_in_view()
        selected = set(self.answer_tree.selection())
        self.results_all_selected.set(bool(groups) and set(groups) <= selected)

    def toggle_all_results(self):
        groups = self.result_group_ids_in_view()
        if self.results_all_selected.get() and groups:
            # Select groups, so unloaded history also includes all of its questions.
            for directory, item in tuple(self.result_tests.items()):
                if item['iid'] in groups:
                    self.answer_tree.item(self.result_runs[item['run']]['iid'], open=True)
            self.answer_tree.selection_set(groups)
        else:
            self.answer_tree.selection_remove(self.answer_tree.selection())
        self.result_selected()
        if not groups:
            self.result_detail.set('当前范围没有可回填的答案：已交卷或尚未生成答案的记录已略过。')

    def apply_result_filter(self):
        for path, run in reversed(tuple(self.result_runs.items())):
            visible = (self.view_run is None or path == self.view_run) and (
                self.view_test is None or self.result_tests[self.view_test]['run'] == path)
            if visible:
                self.answer_tree.move(run['iid'], '', 'end')
            else:
                self.answer_tree.detach(run['iid'])
        for path, item in self.result_tests.items():
            if self.directory_in_view(path):
                self.answer_tree.move(item['iid'], self.result_runs[item['run']]['iid'], 'end')
            else:
                self.answer_tree.detach(item['iid'])
        for iid in self.answer_tree.selection():
            if iid in self.result_rows and not self.directory_in_view(self.result_rows[iid][1]):
                self.answer_tree.selection_remove(iid)
            elif iid.startswith('test-') and not any(t['iid'] == iid and self.directory_in_view(p) for p, t in self.result_tests.items()):
                self.answer_tree.selection_remove(iid)
            elif iid.startswith('run-') and iid not in self.answer_tree.get_children():
                self.answer_tree.selection_remove(iid)
        visible_count = sum(self.directory_in_view(directory) for _, directory in self.result_rows.values())
        self.result_detail.set(f'{self.run_filter.get()} · {self.test_filter.get()} · 已加载 {visible_count} 题')
        self.sync_result_select_all()

    def load_result_test(self, directory):
        item = self.result_tests[Path(directory).resolve()]
        if item['loaded']:
            return
        path = item.get('source_file', item['directory'] / 'review.json')
        try:
            if path.is_file():
                records = [(record, source) for record, source in load_answer_history(path,
                    float(self.store.get('llm-option', 'minConfidence', '0.8'))) if Path(source).resolve() == item['directory']]
            elif (item['directory'] / 'questions.json').is_file():
                questions = read_json(item['directory'] / 'questions.json')
                records = [({'question': Question(**question).to_dict(), 'status': 'read', 'model': ''}, item['directory']) for question in questions]
            else:
                return
            for record, source in records:
                self.update_answer(record, source)
            item['loaded'] = True
        except (OSError, ValueError, KeyError, TypeError, LLMError) as exc:
            item['loaded'] = True
            self.answer_tree.set(item['iid'], 'state', '记录读取失败')
            self.log(f'{self.result_runs[item["run"]]["label"]} · {item["label"]}：{exc}')

    def select_result_scope(self, run=None, test=None):
        self.view_run = Path(run).resolve() if run else None
        self.view_test = Path(test).resolve() if test else None
        if self.view_test:
            self.load_result_test(self.view_test)
        elif self.view_run:
            for path, item in tuple(self.result_tests.items()):
                if item['run'] == self.view_run:
                    self.load_result_test(path)
        self.refresh_result_filters()
        if self.view_run:
            self.answer_tree.item(self.result_runs[self.view_run]['iid'], open=True)
        if self.view_test:
            run = self.result_tests[self.view_test]['run']
            self.answer_tree.item(self.result_runs[run]['iid'], open=True)
            self.answer_tree.item(self.result_tests[self.view_test]['iid'], open=True)

    def change_result_run(self, _=None):
        self.select_result_scope(self.run_choices.get(self.run_filter.get()))

    def change_result_test(self, _=None):
        self.select_result_scope(self.view_run, self.test_choices.get(self.test_filter.get()))

    def result_selected(self, _=None):
        self.sync_result_select_all()
        selection = self.answer_tree.selection()
        if not selection:
            return
        if len(selection) > 1:
            for path, item in tuple(self.result_tests.items()):
                if item['iid'] in selection:
                    self.load_result_test(path)
            self.result_detail.set(f'已选择 {len(selection)} 项；测试分组选整章，题目行只填所选题目。')
            return
        iid = selection[0]
        if iid in self.result_rows:
            record, directory = self.result_rows[iid]
            info = self.result_tests[directory]
            self.result_detail.set(f'{self.result_runs[info["run"]]["label"]} · {info["label"]} · 第 {record["question"]["number"]} 题')
        else:
            for path, item in tuple(self.result_tests.items()):
                if item['iid'] == iid:
                    self.load_result_test(path)
                    self.result_detail.set(f'{self.result_runs[item["run"]]["label"]} · {item["label"]}')
                    return
            for path, item in tuple(self.result_runs.items()):
                if item['iid'] == iid:
                    for directory, test in tuple(self.result_tests.items()):
                        if test['run'] == path:
                            self.load_result_test(directory)
                    self.result_detail.set(item['label'])
                    return

    def refresh_results(self):
        if getattr(self, '_history_id', None):
            self.root.after_cancel(self._history_id)
        self._history_id = None
        newest = None
        for run, tests in discover_result_runs(self.project_root):
            self.register_result_run(run)
            for directory in tests:
                if directory.is_dir():
                    item = self.register_result_test(directory)
                    if not self.controller.running:
                        status = read_result_metadata(directory / 'result.json').get('status', '')
                        self.answer_tree.set(item['iid'], 'state', RESULT_STATES.get(status, status))
                        if item['loaded']:
                            item['loaded'] = False
                            self.load_result_test(directory)
            newest = run
        if newest and self.view_run is None and not self.result_rows:
            self.select_result_scope(newest)
            self.focus_results()
        else:
            self.refresh_result_filters()

    def selection_is_expanded(self, iid):
        parent = self.answer_tree.parent(iid)
        while parent:
            if not self.answer_tree.item(parent, 'open'):
                return False
            parent = self.answer_tree.parent(parent)
        return True

    def selected_answer(self):
        selection = self.answer_tree.selection()
        if len(selection) != 1 or selection[0] not in self.result_rows or not self.directory_in_view(self.result_rows[selection[0]][1]) or not self.selection_is_expanded(selection[0]):
            messagebox.showinfo('选择题目', '请展开运行和测试，选择一条题目记录。')
            return None
        return self.result_rows[selection[0]]

    def update_answer(self, record, directory):
        directory = Path(directory).resolve()
        source = self.register_result_test(directory)
        key = (directory, record['question']['question_id'])
        iid = self.result_keys.get(key)
        if iid is None:
            iid = f'r{len(self.result_keys)}'
            self.result_keys[key] = iid
            self.answer_tree.insert(source['iid'], 'end', iid=iid, text=f'第 {record["question"]["number"]} 题')
        self.result_rows[iid] = (record, Path(directory))
        q = record['question']
        if record.get('status') in ('answered', 'manual'):
            try:
                answer_from_record(record, Question(**q), float(self.store.get('llm-option', 'minConfidence', '0.8')),
                                   enforce_confidence=False)
            except LLMError as exc:
                record.update(status='failed', error=str(exc), meets_confidence=False)
        answer = record.get('answer', {})
        options = {o['id']: o['text'] for o in q['options']}
        text = '；'.join(options.get(i, i).replace('\n', ' ') for i in answer.get('option_ids', []))
        state = '已读取' if record.get('status') == 'read' else ('失败' if record.get('status') == 'failed' else ('手动修改' if record.get('status') == 'manual' else ('低置信度' if not record.get('meets_confidence', True) else '已生成')))
        tags = ('failed',) if state == '失败' else (('low',) if state == '低置信度' else ())
        self.answer_tree.item(iid, values=(q['number'], q['type_and_score'].split('(', 1)[0].strip(),
            record.get('model', ''), text, answer.get('confidence', ''), state), tags=tags)
        self.apply_result_filter()

    def show_answer(self):
        chosen = self.selected_answer()
        if not chosen:
            return
        record, directory = chosen
        q = record['question']
        dialog = tk.Toplevel(self.root)
        source = self.result_tests[directory]
        dialog.title(f'{source["label"]} · 第 {q["number"]} 题')
        dialog.geometry('900x700')
        ttk.Label(dialog, text=self.result_runs[source['run']]['label'] + ' · ' + source['label'],
                  wraplength=850).pack(fill='x', padx=20, pady=(12, 0))
        try:
            image = Image.open(q['stem_screenshot'])
            image.thumbnail((850, 350))
            photo = ImageTk.PhotoImage(image)
            label = ttk.Label(dialog, image=photo)
            label.image = photo
            label.pack(padx=15, pady=15)
        except OSError:
            ttk.Label(dialog, text='题干截图不可用，请重新读取题目。').pack(pady=10)
        selections = {}
        selected = set(record.get('answer', {}).get('option_ids', []))
        single = tk.StringVar(value=next(iter(selected), ''))
        for option in q['options']:
            if '多选' in q['type_and_score']:
                variable = tk.BooleanVar(value=option['id'] in selected)
                selections[option['id']] = variable
                ttk.Checkbutton(dialog, text=option['text'].replace('\n', ' '), variable=variable).pack(anchor='w', padx=20, pady=4)
            else:
                ttk.Radiobutton(dialog, text=option['text'].replace('\n', ' '), variable=single, value=option['id']).pack(anchor='w', padx=20, pady=4)
        detail = record.get('error') or record.get('answer', {}).get('explanation', '')
        ttk.Label(dialog, text=detail, wraplength=850).pack(fill='x', padx=20, pady=15)
        def save():
            if self.controller.running:
                messagebox.showinfo('任务运行中', '请先停止任务后修改答案。', parent=dialog)
                return
            ids = [i for i, v in selections.items() if v.get()] if selections else ([single.get()] if single.get() else [])
            if not ids:
                messagebox.showinfo('选择答案', '请至少选择一个选项。', parent=dialog)
                return
            record.update(status='manual', meets_confidence=True,
                answer=asdict(Answer(ids, record.get('answer', {}).get('confidence', 0), '用户手动修改')))
            record.pop('error', None)
            path = directory / 'review.json'
            records = read_json(path) if path.is_file() else []
            records = [r for r in records if r['question']['question_id'] != q['question_id']] + [record]
            write_json(path, records)
            self.update_answer(record, directory)
            dialog.destroy()
        ttk.Button(dialog, text='保存本地修改', command=save).pack(pady=10)

    def retry_answer(self):
        chosen = self.selected_answer()
        if not chosen or not self.save_config(quiet=True):
            return
        record, directory = chosen
        settings = LLMSettings.from_config(self.config(), self.active_profile.get())
        try:
            settings.validate()
        except LLMError as exc:
            messagebox.showerror('模型配置', str(exc))
            return
        q = Question(**record['question'])
        async def work(callback):
            relay = VisionLLM(settings)
            try:
                answer = await relay.answer(q, enforce_confidence=False,
                    on_retry=lambda info: callback({'kind': 'retry', 'number': q.number, **info}))
            except LLMError as error:
                if getattr(error, 'response_diagnostics', None):
                    errors = directory / 'response-errors'
                    errors.mkdir(parents=True, exist_ok=True)
                    path = errors / (datetime.now().strftime('%Y%m%d-%H%M%S-%f') + f'-question-{q.number}.json')
                    write_json(path, {'model': settings.model, 'question_number': q.number,
                                     'responses': error.response_diagnostics,
                                     'relay': getattr(error, 'relay_trace', {})})
                    callback({'kind': 'log', 'message': f'模型返回格式诊断：{path}'})
                raise
            updated = {'question': q.to_dict(), 'signature': question_signature(q), 'status': 'answered',
                'content_signature': content_signature(q),
                'model': settings.model, 'answer': asdict(answer), 'meets_confidence': answer.confidence >= settings.min_confidence}
            if isinstance(getattr(relay, 'last_trace', None), dict):
                updated['relay'] = relay.last_trace
            path = directory / 'review.json'
            records = read_json(path) if path.is_file() else []
            records = [r for r in records if r['question']['question_id'] != q.question_id] + [updated]
            write_json(path, records)
            callback({'kind': 'answer_ready', 'result': updated, 'directory': str(directory)})
        self.begin('重试选中题', work, directory)

    def apply_answers(self, submit):
        selection = self.answer_tree.selection()
        targets = {}
        for iid in selection:
            if iid in self.result_rows:
                record, directory = self.result_rows[iid]
                ids = {record['question']['question_id']}
            else:
                directory = next((path for path, item in self.result_tests.items() if item['iid'] == iid), None)
                ids = None
            if directory is None or not self.directory_in_view(directory) or not self.selection_is_expanded(iid):
                messagebox.showinfo('选择测试', '请展开运行和测试，选择可见的测试分组或题目行；可按 Ctrl / Shift 多选。')
                return
            if directory not in targets:
                targets[directory] = ids
            elif ids is None:
                targets[directory] = None
            elif targets[directory] is not None:
                targets[directory].update(ids)
        if not targets:
            messagebox.showinfo('选择测试', '请选择测试分组或题目行；可按 Ctrl / Shift 多选。')
            return
        missing = [directory for directory in targets
                   if review_record_state(directory) not in ('ready', 'already_submitted')]
        if missing and len(missing) == len(targets):
            labels = '\n'.join(self.result_tests[directory]['label'] for directory in missing)
            messagebox.showinfo('尚无完整答案', f'以下测试没有可回填的完整答案：\n{labels}\n请先重新运行“生成答案预览”。')
            return
        if not self.save_config(quiet=True):
            return
        config = self.config()
        # Freeze task metadata before the worker starts; it need not read GUI state.
        sources = {directory: {'course': self.result_tests[directory]['course'],
                               'test': self.result_tests[directory]['test']} for directory in targets}
        async def work(callback):
            return await apply_review_batch(config, GuiLogger(self.controller.events),
                self.project_root / 'data' / 'cookies.json', targets, auto_submit=submit,
                callback=callback, sources=sources)
        self.begin('交卷所选章节' if submit else '回填并暂存所选', work,
                   next(iter(targets)) if len(targets) == 1 else None)

    def load_history(self):
        path = filedialog.askopenfilename(title='选择 review.json 或 results.json', initialdir=self.project_root / '.autovisor', filetypes=[('JSON', '*.json')])
        if not path:
            return
        try:
            records = load_answer_history(path, float(self.store.get('llm-option', 'minConfidence', '0.8')))
            for record, directory in records:
                self.update_answer(record, directory)
                self.result_tests[Path(directory).resolve()]['loaded'] = True
                self.result_tests[Path(directory).resolve()]['source_file'] = Path(path)
            directories = {Path(directory).resolve() for _, directory in records}
            if len(directories) == 1:
                directory = next(iter(directories))
                self.select_result_scope(self.result_tests[directory]['run'], directory)
            elif directories:
                runs = {self.result_tests[directory]['run'] for directory in directories}
                self.select_result_scope(next(iter(runs)) if len(runs) == 1 else None)
            self.focus_results()
            self.status.set('历史结果已载入')
        except (OSError, ValueError, KeyError, StopIteration, TypeError, LLMError) as exc:
            messagebox.showerror('读取结果失败', str(exc))

    def test_model(self):
        if not self.save_config(quiet=True):
            return
        settings = LLMSettings.from_config(self.config(), self.active_profile.get())
        try:
            settings.validate()
        except LLMError as exc:
            messagebox.showerror('模型配置', str(exc))
            return
        async def work(callback):
            path = self.project_root / '.autovisor' / 'gui-interface-test.png'
            path.parent.mkdir(parents=True, exist_ok=True)
            image = Image.new('RGB', (600, 140), 'white')
            ImageDraw.Draw(image).text((25, 45), 'TEST IMAGE: 2 + 3 = ?', fill='black', font=ImageFont.load_default(size=28))
            image.save(path)
            q = Question(1, 'interface-test', '单选题', '', str(path), [{'id': 'four', 'text': 'A. 4'}, {'id': 'five', 'text': 'B. 5'}])
            answer = await VisionLLM(settings).answer(q, enforce_confidence=False,
                on_retry=lambda info: callback({'kind': 'retry', 'number': 1, **info}))
            if answer.option_ids != ['five']:
                raise LLMError('接口返回成功，但自制图片答案与预期不符，请检查模型的图片识别能力。')
            callback({'kind': 'log', 'message': f'{settings.model} 图片接口测试成功，置信度 {answer.confidence:.2f}。'})
        self.begin('测试模型接口', work)

    def stop(self):
        self.controller.stop()
        self.status.set('正在停止任务，保留已生成结果…')

    def log(self, text):
        self.log_text.configure(state='normal')
        prefix = datetime.now().strftime('[%H:%M:%S]')
        if self._log_context:
            prefix += ' [' + self._log_context + ']'
        self.log_text.insert('end', prefix + ' ' + text + '\n')
        self.log_text.see('end')
        self.log_text.configure(state='disabled')

    def poll(self):
        if getattr(self, '_poll_id', None):
            self.root.after_cancel(self._poll_id)
        try:
            while True:
                event = self.controller.events.get_nowait()
                kind = event['kind']
                if kind == 'log':
                    self.log(event['message'])
                    if '登录' in event['message'] or '验证' in event['message']:
                        self.status.set(event['message'])
                elif kind == 'artifact':
                    self.active_run = Path(event['path']).resolve()
                    run = self.register_result_run(self.active_run)
                    for path, previous in self.result_runs.items():
                        self.answer_tree.item(previous['iid'], open=path == self.active_run)
                    self.answer_tree.set(run['iid'], 'state', '进行中')
                    self.select_result_scope(self.active_run)
                    self.focus_results()
                    self._log_context = run['label']
                    self.log('开始新运行')
                elif kind == 'chapter':
                    chapter = event['chapter']
                    iid = f'c{chapter["course"]}-t{chapter["index"]}'
                    chapter['selected'] = not self.chapters
                    self.chapters[iid] = chapter
                    self.chapter_tree.insert('', 'end', iid=iid, values=('☑' if chapter['selected'] else '☐', chapter['course'], chapter['name'], '待运行'))
                elif kind == 'test_started':
                    self.current_directory = Path(event['directory']).resolve()
                    source = self.register_result_test(self.current_directory, course=event['course'],
                        test=event['test'], name=event.get('name'))
                    self._log_context = self.result_runs[source['run']]['label'] + ' · ' + source['label']
                    self.answer_tree.set(source['iid'], 'state', '读取题目')
                    self.status.set(source['label'] + '：读取题目')
                    self.log('开始读取题目')
                elif kind == 'test_navigation':
                    self.status.set(event['message'])
                    self.log(event['message'])
                    if event.get('state') and self.current_directory:
                        source = self.result_tests.get(Path(self.current_directory).resolve())
                        if source:
                            self.answer_tree.set(source['iid'], 'state', event['state'])
                elif kind == 'question_read':
                    self.progress.configure(maximum=event['total'], value=event['question']['number'])
                    self.update_answer({'question': event['question'], 'status': 'read', 'model': ''}, self.current_directory)
                elif kind == 'answer_started':
                    self.status.set(f'{event["model"]} · 正在解答第 {event["number"]}/{event["total"]} 题')
                    self.progress.configure(maximum=event['total'], value=event['number'] - 1)
                elif kind == 'answer_ready':
                    self.update_answer(event['result'], event.get('directory', self.current_directory))
                    self.progress.configure(value=event['result']['question']['number'])
                elif kind in ('retry', 'waiting'):
                    text = f'接口重试：{event["reason"]}，等待 {event["delay"]:g} 秒' if kind == 'retry' else f'{event["message"]}：{event["seconds"]:g} 秒'
                    self.status.set(text)
                    self.log(text)
                elif kind == 'answer_filled':
                    self.status.set(f'已回填第 {event["number"]} 题')
                elif kind == 'answer_skipped':
                    text = f'第 {event["number"]} 题：{event["reason"]}'
                    self.status.set(text)
                    self.log(text)
                elif kind == 'answer_preserved':
                    text = f'第 {event["number"]} 题：保留已有答案（覆盖关闭）'
                    self.status.set(text)
                    self.log(text)
                elif kind == 'test_finished':
                    iid = f'c{event.get("course", 1)}-t{event.get("test", 1)-1}'
                    source = self.register_result_test(event.get('source_directory', event['directory']),
                        course=event.get('course', 1), test=event.get('test', 1))
                    state = RESULT_STATES.get(event['status'], event['status'])
                    self.answer_tree.set(source['iid'], 'state', state)
                    self.sync_result_select_all()
                    if iid in self.chapters and (not source['context'].get('url') or source['context']['url'] == self.chapters[iid]['url']):
                        self.chapter_tree.set(iid, 'state', state)
                    if event['status'] == 'failed':
                        reason = event.get('error', '请查看诊断记录')
                        self.status.set('章节测试失败：' + reason)
                        self.log('章节测试失败：' + reason)
                        self.log('诊断记录：' + event['directory'])
                    else:
                        self.log(source['label'] + ' · ' + state + '：' + event['directory'])
                elif kind in ('done', 'cancelled', 'error'):
                    if self.active_run in self.result_runs:
                        run = self.result_runs[self.active_run]
                        self.answer_tree.set(run['iid'], 'state', {'done': '已结束', 'cancelled': '已停止', 'error': '失败'}[kind])
                    text = '任务已停止' if kind == 'cancelled' else ('失败：' + event['message'] if kind == 'error' else event['name'] + '已完成')
                    if kind == 'done' and event.get('result') == 'already_submitted':
                        text = '该章节已提交，当前为结果页，已跳过本次回填。'
                    elif kind == 'done' and event.get('result') == 'skipped_no_answers':
                        text = '本章节没有可用答案，已跳过保存或交卷。'
                    elif kind == 'done' and isinstance(event.get('result'), dict) and event['result'].get('batch'):
                        summary = event['result']
                        text = f'处理结束：完成 {summary["completed"]} 个章节，跳过 {summary["skipped"]} 个，失败 {summary["failed"]} 个。'
                    self.status.set(text)
                    self.log(text)
                    self.active_run = None
                    self._log_context = ''
                    if kind == 'error' and event['name'] in ('回填并暂存', '回填并暂存该章节', '自动交卷', '交卷该章节', '回填并暂存所选', '交卷所选章节'):
                        messagebox.showerror(event['name'] + '失败', event['message'])
                    if kind == 'done' and event['name'] == '刷课' and self.follow_tests.get():
                        self.root.after(300, self.start_tests)
        except queue.Empty:
            pass
        busy = self.controller.running
        for button in self.action_buttons:
            button.configure(state='disabled' if busy else 'normal')
        self.stop_button.configure(state='normal' if busy and not self.controller.cancel.is_set() else 'disabled')
        self._poll_id = self.root.after(150, self.poll)

    def close(self):
        self.controller.stop()
        if getattr(self, '_history_id', None):
            self.root.after_cancel(self._history_id)
        if getattr(self, '_poll_id', None):
            self.root.after_cancel(self._poll_id)
        self.root.destroy()


def main(project_root=None):
    configure_dpi()
    root = tk.Tk()
    app = AutoCourseApp(root, project_root or Path(__file__).resolve().parents[1])
    root.mainloop()
    return app
