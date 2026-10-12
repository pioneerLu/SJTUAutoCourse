"""答案采用策略：覆盖、保留现有选择和逐题置信度判断。"""
import math

from modules.llm_types import LLMError, answer_from_record


def overwrite_enabled(config):
    return config.get_bool_field('llm-option', 'overwriteAnswers', fallback=False)


def selected_ids(question):
    return [option['id'] for option in question.options if option.get('selected')]


def keep_existing_choice(question, record, threshold, overwrite):
    """网页上来源不明的选择按手动答案保留，已知低置信度答案允许替换。"""
    if overwrite or not selected_ids(question):
        return False
    if record is None or record.get('status') not in ('answered', 'manual'):
        return True
    try:
        answer_from_record(record, question, threshold, enforce_confidence=False)
    except LLMError:
        return True
    return record['meets_confidence']


def answer_for_submission(record, question, threshold):
    """无效、缺失或低置信度答案逐题留空，不阻断其他题目。"""
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise LLMError('置信度阈值必须在 0 到 1 之间。')
    try:
        if not isinstance(record, dict):
            raise LLMError('尚未生成答案')
        answer = answer_from_record(record, question, threshold, enforce_confidence=False)
    except (LLMError, ValueError, TypeError):
        return None, {'number': question.number, 'question_id': question.question_id,
                      'kind': 'invalid_answer', 'reason': '答案未完成或格式无效，跳过并留空'}
    if not record['meets_confidence']:
        return None, {'number': question.number, 'question_id': question.question_id,
                      'kind': 'low_confidence', 'reason': '低置信度，已留空'}
    return answer, None
