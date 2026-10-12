"""模型配置、题目/答案契约和本地记录的严格校验。"""
from dataclasses import dataclass, asdict
import json
import math
import os
import re
from urllib.parse import urlparse



class LLMError(RuntimeError):
    pass


class LLMRefusalError(LLMError):
    """接口明确拒绝作答，不能作为 JSON 格式错误反复纠正。"""


@dataclass
class LLMSettings:
    url: str = ''
    key: str = ''
    model: str = ''
    timeout: float = 90
    min_confidence: float = 0.8
    auto_submit: bool = False
    json_mode: bool = False
    retry_delay: float = 15
    request_interval: float = 30
    adapter: str = 'auto'
    response_mode: str = 'auto'

    @classmethod
    def from_config(cls, config, model_profile=None):
        parser = config._config
        def get(name, default=''):
            return parser.get('llm-option', name, raw=True, fallback=str(default)).strip()
        profile = str(model_profile or get('modelProfile')).strip()
        if profile and profile not in ('1', '2'):
            raise LLMError('模型配置槽必须为 1 或 2。')
        section = f'llm-model-{profile}' if profile else 'llm-option'
        def connection(name):
            value = parser.get(section, name, raw=True, fallback='').strip()
            if value:
                return value
            # 第二个槽没有模型名时明确报缺配置，避免悄悄调用模型 1。
            return get('model_ds') if name == 'model' and profile == '2' else get(name)
        def environment(name):
            # 新名称优先，兼容用户已经设置的旧环境变量。
            suffix = name.upper()
            return os.environ.get(f'AUTOCOURSE_LLM_{suffix}',
                                  os.environ.get(f'AUTOVISOR_LLM_{suffix}', connection(name))).strip()
        try:
            return cls(
                url=environment('url'),
                key=environment('key'),
                model=environment('model'),
                timeout=float(get('timeout', 90)),
                min_confidence=float(get('minConfidence', 0.8)),
                auto_submit=config.get_bool_field('llm-option', 'autoSubmit'),
                json_mode=config.get_bool_field('llm-option', 'jsonMode'),
                retry_delay=float(get('retryDelay', 15)),
                request_interval=float(get('requestInterval', 30)),
                adapter=(connection('adapter') or 'auto').lower(),
                response_mode=(connection('responseMode') or 'auto').lower(),
            )
        except ValueError as exc:
            raise LLMError('LLM timeout/minConfidence 必须是数值。') from exc

    def validate(self):
        missing = [name for name in ('url', 'key', 'model') if not getattr(self, name).strip()]
        if missing:
            raise LLMError(f"请先填写 config 的 [llm-option]: {', '.join(missing)}；模型需要支持图片输入。")
        parsed = urlparse(self.url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise LLMError('LLM url 必须是有效 HTTP(S) 接口地址。')
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise LLMError('LLM timeout 必须大于 0。')
        if not math.isfinite(self.min_confidence) or not 0 <= self.min_confidence <= 1:
            raise LLMError('LLM minConfidence 必须在 0 到 1 之间。')
        if any(not math.isfinite(v) or v < 0 for v in (self.retry_delay, self.request_interval)):
            raise LLMError('重试等待和请求间隔必须为不小于 0 的秒数。')
        if self.response_mode not in ('auto', 'prompt', 'json', 'schema'):
            raise LLMError('responseMode 必须为 auto、prompt、json 或 schema。')
        from modules.llm_adapters import resolve_adapter
        resolve_adapter(self.model, self.adapter, url=self.url)

    @property
    def endpoint(self):
        parsed = urlparse(self.url.strip())
        path = parsed.path.rstrip('/')
        # 只补已核对的官方服务商版本路径，不猜测第三方网关路径。
        if parsed.hostname == 'api.siliconflow.cn' and not path:
            path = '/v1'
        if not path.endswith('/chat/completions'):
            path += '/chat/completions'
        return parsed._replace(path=path).geturl()


@dataclass
class Question:
    number: int
    question_id: str
    type_and_score: str
    stem_dom_text: str
    stem_screenshot: str
    options: list[dict]

    def to_dict(self):
        return asdict(self)


@dataclass
class Answer:
    option_ids: list[str]
    confidence: float
    explanation: str


def parse_answer(content: str, question: Question, *, normalize_ids=False) -> Answer:
    text = content.strip()
    if text.startswith('```') and text.endswith('```'):
        text = text.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise LLMError('LLM 未返回有效 JSON，已停止回填。') from exc
    if not isinstance(data, dict):
        raise LLMError('LLM 回答必须是 JSON 对象。')
    if 'option_ids' not in data:
        raise LLMError('LLM 回答缺少 option_ids 字段。')
    ids = data['option_ids']
    valid = {str(option['id']) for option in question.options}
    if isinstance(ids, list) and not ids:
        raise LLMError('LLM 没有给出任何选项（option_ids 为空），无法回填。')
    if normalize_ids and isinstance(ids, list):
        # 只规范化明确的格式差异；字母按实际标签唯一匹配，不按位置猜测。
        labels = {}
        for option in question.options:
            match = re.match(r'^\s*([A-Za-z])[.、．]\s*', option.get('text', ''))
            if match:
                labels.setdefault(match[1].upper(), []).append(str(option['id']))
        normalized = []
        for value in ids:
            if type(value) is int and str(value) in valid:
                value = str(value)
            elif isinstance(value, str) and value not in valid and re.fullmatch('[A-Za-z]', value):
                matches = labels.get(value.upper(), [])
                if len(matches) == 1:
                    value = matches[0]
            normalized.append(value)
        ids = normalized
    if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
        raise LLMError('LLM option_ids 必须是非空字符串数组。')
    if len(ids) != len(set(ids)) or not set(ids) <= valid:
        raise LLMError('LLM 返回了重复或不存在的选项 ID，已停止回填。')
    if '多选' not in question.type_and_score and len(ids) != 1:
        raise LLMError('单选题或判断题必须只选择一个选项。')
    confidence = data.get('confidence')
    if isinstance(confidence, bool) or not isinstance(confidence, (float, int)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise LLMError('LLM confidence 必须是 0 到 1 的数值。')
    explanation = data.get('explanation', '')
    if not isinstance(explanation, str):
        raise LLMError('LLM explanation 必须是字符串。')
    return Answer(ids, float(confidence), explanation[:1000])


def answer_from_record(record, question, min_confidence, *, enforce_confidence=True):
    """本地记录按当前阈值及选项重新校验，不信任历史通过标记。"""
    if not math.isfinite(min_confidence) or not 0 <= min_confidence <= 1:
        raise LLMError('置信度阈值必须在 0 到 1 之间。')
    if record.get('status') not in ('answered', 'manual'):
        raise LLMError(f'第 {question.number} 题答案未完成，请重试或手动修改。')
    answer = parse_answer(json.dumps(record.get('answer'), ensure_ascii=False), question)
    if record['status'] == 'manual':
        record.pop('error', None)
    record['meets_confidence'] = record['status'] == 'manual' or answer.confidence >= min_confidence
    if enforce_confidence and not record['meets_confidence']:
        raise LLMError(f'第 {question.number} 题置信度 {answer.confidence:.2f} 低于当前阈值 {min_confidence:.2f}，已停止回填。')
    return answer
