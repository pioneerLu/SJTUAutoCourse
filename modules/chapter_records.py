"""章节记录、题目指纹与历史查询；不依赖浏览器或 GUI。"""
from datetime import datetime
import hashlib
import json
import re
from pathlib import Path

from modules.llm_types import Question, answer_from_record
from modules.storage import read_json, read_json_safely


def question_signature(question):
    data = {'id': question.question_id, 'type': question.type_and_score,
            'options': sorted((o['id'], o['text']) for o in question.options),
            'image': hashlib.sha256(Path(question.stem_screenshot).read_bytes()).hexdigest()}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


def content_signature(question):
    data = read_json_safely(Path(question.stem_screenshot).with_suffix('.content.json'), {})
    if data:
        return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return question_signature(question)

def reusable_history(directory):
    """只复用相同课程与章节的记录，按最新到最旧排列。"""
    directory = Path(directory).resolve()
    context = read_json_safely(directory / 'context.json', {})
    root = directory.parent.parent.parent
    if root.name != 'chapter-tests' or not context.get('url') or 'index' not in context:
        return []
    records = []
    for previous in sorted(root.glob('*/course-*/test-*'), reverse=True):
        if previous.resolve() == directory:
            continue
        old_context = read_json_safely(previous / 'context.json', {})
        if old_context.get('url') != context['url'] or old_context.get('index') != context['index']:
            continue
        if old_context.get('chapter_name') and context.get('chapter_name') and old_context['chapter_name'] != context['chapter_name']:
            continue
        for record in read_json_safely(previous / 'review.json', []):
            if not isinstance(record, dict):
                continue
            try:
                old_question = Question(**record['question'])
                signature = record.get('signature') or question_signature(old_question)
                content = record.get('content_signature') or content_signature(old_question)
            except (KeyError, TypeError, ValueError, OSError):
                continue
            records.append(dict(record, signature=signature, content_signature=content, source_directory=str(previous)))
    return records


def review_record_state(directory):
    directory = Path(directory)
    if read_json_safely(directory / 'result.json', {}).get('status') in ('submitted', 'already_submitted'):
        return 'already_submitted'
    for name in ('context', 'questions', 'review'):
        if not (directory / f'{name}.json').is_file():
            return f'missing_{name}'
    return 'ready'


def read_result_metadata(path):
    """损坏的显示元数据不妨碍查看其他运行记录。"""
    return read_json_safely(path, {})


def answer_source(directory):
    directory = Path(directory).resolve()
    context = read_result_metadata(directory / 'context.json')
    course = context.get('course')
    test = context.get('index')
    chapter_layout = re.fullmatch(r'test-(\d+)', directory.name)
    course_layout = re.fullmatch(r'course-(\d+)', directory.parent.name)
    if chapter_layout and course_layout:
        run = directory.parent.parent
        course = course if isinstance(course, int) else int(course_layout[1])
        test = test + 1 if isinstance(test, int) else int(chapter_layout[1])
    else:
        run = directory
        test = test + 1 if isinstance(test, int) else None
    metadata = read_result_metadata(run / 'run.json')
    started = metadata.get('started_at')
    try:
        timestamp = datetime.fromisoformat(started) if isinstance(started, str) else datetime.strptime(run.name, '%Y%m%d-%H%M%S-%f')
        label = timestamp.strftime('%Y-%m-%d %H:%M:%S.%f')
    except ValueError:
        label = '历史导入 · ' + run.name
    name = context.get('chapter_name', '')
    if course is not None and test is not None:
        test_label = f'课程 {course} · 测试 {test}' + (f' · {name}' if name else '')
    elif test is not None:
        test_label = f'测试 {test}' + (f' · {name}' if name else '')
    else:
        test_label = '历史结果（无章节关联）'
    return {'directory': directory, 'run': run, 'run_label': label, 'test_label': test_label,
            'course': course, 'test': test, 'context': context, 'metadata': metadata}


def discover_result_runs(project_root):
    root = Path(project_root) / '.autovisor' / 'chapter-tests'
    if not root.is_dir():
        return []
    return [(run, sorted(run.glob('course-*/test-*'))) for run in sorted(root.iterdir()) if run.is_dir()]


def load_answer_history(path, min_confidence):
    """兼容 review.json 和早期 results.json；不改写历史文件。"""
    path = Path(path)
    records = read_json(path)
    if not isinstance(records, list):
        raise ValueError('历史结果必须是答案记录列表。')
    loaded = []
    question_files = {}
    for original in records:
        if not isinstance(original, dict):
            raise ValueError('历史答案记录格式不正确。')
        if 'question' in original:
            record = dict(original, question=dict(original['question']))
            directory = path.parent
        elif original.get('stem_screenshot'):
            screenshot = Path(original['stem_screenshot'])
            if not screenshot.is_absolute():
                screenshot = path.parent / screenshot
            directory = screenshot.parent
            if directory not in question_files:
                question_files[directory] = read_json(directory / 'questions.json')
            questions = question_files[directory]
            if not isinstance(questions, list) or any(not isinstance(q, dict) for q in questions):
                raise ValueError('历史题目文件格式不正确。')
            question_id = original.get('question_id')
            if not question_id:
                raise ValueError('历史答案缺少题目 ID。')
            matches = [q for q in questions if q.get('question_id') == question_id]
            if not matches:
                # 早期导出没有 question_id，只可用题号关联这些旧记录。
                matches = [q for q in questions if not q.get('question_id')
                           and q.get('number') == original.get('question_number')]
            if len(matches) != 1:
                raise ValueError('历史答案无法唯一关联到题目。')
            question = dict(matches[0], question_id=question_id)
            record = {'question': question,
                'status': 'answered' if original.get('status') == 'valid_response' else 'failed',
                'model': original.get('model', ''), 'answer': original.get('answer', {}),
                'error': original.get('error', '')}
        else:
            raise ValueError('无法识别历史答案记录。')
        question = Question(**record['question'])
        if record.get('status') in ('answered', 'manual'):
            answer_from_record(record, question, min_confidence, enforce_confidence=False)
        loaded.append((record, directory))
    return loaded
