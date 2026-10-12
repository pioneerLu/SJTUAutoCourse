"""统一的题目 → 模型 → Answer 中继，负责协议、格式和有界恢复。"""
import asyncio
import copy
from dataclasses import replace
import hashlib
import json
import re

import httpx

from modules.llm_adapters import prepare_question, resolve_adapter
from modules.llm_types import Answer, LLMError, LLMRefusalError, LLMSettings, Question


def sanitize_response(value, key):
    value = str(value).replace(key, '[REDACTED]')
    value = re.sub(r'https?://[^\s\"\x27<>]+', '[URL]', value)
    value = re.sub(r'data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+', '[IMAGE]', value)
    return re.sub(r'(?:sk-|Bearer\s+)[A-Za-z0-9_.-]+', '[REDACTED]', value, flags=re.I)[:12000]


def rejected_optional_parameter(response, payload):
    if response.status_code not in (400, 422):
        return None
    try:
        body = response.json()
        error = body.get('error', body) if isinstance(body, dict) else {}
        if isinstance(error, dict):
            message = str(error.get('message', error.get('detail', ''))).lower()
            parameter_name = str(error.get('param', error.get('parameter', ''))).lower()
            text = message + ' ' + parameter_name
        else:
            text = str(error).lower()
    except ValueError:
        return None
    # 仅移除明确被拒绝的可选格式参数；保留截图和所有题目信息。
    for parameter in ('response_format', 'reasoning_split'):
        if parameter in payload and parameter in text:
            return parameter
    if 'detail' in text and any(word in text for word in ('unsupported', 'not support', 'unknown', '不支持', '未知')):
        if any('detail' in part.get('image_url', {}) for message in payload['messages']
               if isinstance(message.get('content'), list) for part in message['content']):
            return 'image_detail'
    return None


def remove_optional_parameters(payload, disabled):
    payload = copy.deepcopy(payload)
    for parameter in disabled:
        if parameter == 'image_detail':
            for message in payload['messages']:
                if isinstance(message.get('content'), list):
                    for part in message['content']:
                        if isinstance(part.get('image_url'), dict):
                            part['image_url'].pop('detail', None)
        else:
            payload.pop(parameter, None)
    return payload


def http_failure(response):
    status = response.status_code
    if status == 429:
        return LLMError('LLM 接口返回 HTTP 429：请求受到服务端频率或配额限制，多次重试仍未成功。请延长请求间隔，或向 API 平台核查该模型的限流与配额。')
    if status >= 500:
        return LLMError(f'LLM 接口返回 HTTP {status}：服务端或模型上游暂时异常，多次重试仍未成功。请稍后重试失败题，或向 API 平台核查渠道状态。')
    if status == 402:
        try:
            body = response.json()
            error = body.get('error', body) if isinstance(body, dict) else {}
            message = str(error.get('message', '') if isinstance(error, dict) else error).lower()
        except ValueError:
            message = ''
        if 'insufficient balance' in message or '余额不足' in message:
            return LLMError('LLM 接口返回 HTTP 402：服务端报告余额不足。请核查 API 平台账户/令牌额度，或联系平台管理员检查该模型上游渠道余额。')
        return LLMError('LLM 接口返回 HTTP 402：服务端拒绝此请求。请向所用 API 平台核查计费、额度或模型权限。')
    return LLMError(f'LLM 接口返回 HTTP {status}，请检查 URL、Key、模型及图片支持。')


class LLMRelay:
    def __init__(self, settings: LLMSettings, transport=None):
        settings.validate()
        self.settings = settings
        self.transport = transport
        self.adapter = resolve_adapter(settings.model, settings.adapter, url=settings.url)
        self._disabled_parameters = {}
        self.last_trace = {}

    async def answer(self, question: Question, *, enforce_confidence=True, on_retry=None) -> Answer:
        settings = replace(self.settings)
        settings.validate()
        prepared = prepare_question(question)
        adapter = resolve_adapter(settings.model, settings.adapter, url=settings.url)
        self.adapter = adapter
        key = (settings.endpoint, settings.model, adapter.name, settings.response_mode, settings.json_mode,
               hashlib.sha256(settings.key.encode('utf-8')).hexdigest())
        disabled = self._disabled_parameters.setdefault(key, set())
        diagnostics, correction = [], None
        trace = {'adapter': adapter.name, 'events': [], 'attempts': 0, 'question_number': prepared.original.number,
                 'requested_mode': settings.response_mode, 'disabled_parameters': sorted(disabled),
                 'code_to_id': dict(prepared.code_to_id)}

        async def retry(attempt, reason, response=None):
            delay = settings.retry_delay * (attempt + 1)
            if response is not None:
                try:
                    delay = max(delay, min(float(response.headers.get('retry-after', 0)), 60))
                except ValueError:
                    pass
            if on_retry:
                on_retry({'attempt': attempt + 1, 'delay': delay, 'reason': reason})
            await asyncio.sleep(delay)

        def fail(error):
            if diagnostics:
                error.response_diagnostics = list(diagnostics)
            self.last_trace = copy.deepcopy(trace)
            error.relay_trace = copy.deepcopy(trace)
            return error

        async with httpx.AsyncClient(timeout=settings.timeout, transport=self.transport) as client:
            for attempt in range(3):
                trace['attempts'] = attempt + 1
                # 每次格式修复重新构造同一题，不把模型思考或原始返回回传为对话历史。
                payload = remove_optional_parameters(adapter.build_request(prepared, settings, correction), disabled)
                trace['response_mode'] = payload.get('response_format', {}).get('type', 'prompt')
                if attempt == 0 and settings.response_mode == 'schema' and trace['response_mode'] == 'json_object':
                    trace['events'].append({'stage': 'parameters', 'converted': 'schema_to_json'})
                try:
                    response = await client.post(settings.endpoint,
                        headers={'Authorization': 'Bearer ' + settings.key}, json=payload)
                except httpx.RequestError:
                    if attempt == 2:
                        raise fail(LLMError('LLM 请求超时或连接失败，请检查接口设置。')) from None
                    await retry(attempt, '连接失败')
                    continue
                parameter = rejected_optional_parameter(response, payload)
                if parameter and attempt < 2:
                    disabled.add(parameter)
                    trace['disabled_parameters'] = sorted(disabled)
                    trace['events'].append({'stage': 'parameters', 'removed': parameter})
                    await retry(attempt, f'接口拒绝可选参数 {parameter}，保留题目与图片重新请求')
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        await retry(attempt, f'HTTP {response.status_code}', response)
                        continue
                if not response.is_success:
                    raise fail(http_failure(response))
                content, finish = '', None
                try:
                    try:
                        body = response.json()
                    except (ValueError, RecursionError):
                        raise LLMError('接口没有返回有效的模型响应 JSON。') from None
                    content, finish = adapter.read_response(body)
                    answer = adapter.parse_response(content, finish, prepared)
                except LLMRefusalError as error:
                    diagnostics.append({'attempt': attempt + 1, 'model': settings.model,
                        'adapter': adapter.name, 'question_number': question.number, 'error': str(error),
                        'finish_reason': finish, 'content': sanitize_response(content, settings.key)})
                    raise fail(error)
                except LLMError as error:
                    diagnostics.append({'attempt': attempt + 1, 'model': settings.model,
                        'adapter': adapter.name, 'question_number': question.number,
                        'error': str(error), 'finish_reason': finish,
                        'content': sanitize_response(content, settings.key)})
                    interrupted = finish in ('length', 'aborted', 'insufficient_system_resource')
                    trace['events'].append({'stage': 'generation' if interrupted else 'output', 'error': str(error)})
                    if attempt < 2:
                        if not interrupted:
                            correction = str(error) + ' 请重新返回包含 choices 字符串数组、confidence 和 explanation 的完整 JSON，不要编造答案或置信度。'
                        await retry(attempt, f'生成未完成：{error}' if interrupted else f'返回格式无效：{error}')
                        continue
                    reason = '多次生成未完成' if interrupted else '返回多次未通过格式校验'
                    raise fail(LLMError(f'第 {question.number} 题模型{reason}：{error}')) from None
                if enforce_confidence and answer.confidence < settings.min_confidence:
                    raise fail(LLMError(f'第 {question.number} 题置信度 {answer.confidence:.2f} 低于阈值，已停止回填。'))
                answer.explanation = answer.explanation.replace(settings.key, '[REDACTED]')
                trace['status'] = 'validated'
                self.last_trace = copy.deepcopy(trace)
                return answer
        raise fail(LLMError('LLM 请求失败。'))
