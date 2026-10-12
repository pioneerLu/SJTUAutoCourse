"""官方 Chat Completions 协议夹具；不连接真实服务或读取用户凭据。"""
import configparser
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from modules.configs import Config
from modules.llm import LLMError, LLMSettings, Question, VisionLLM
from modules.llm_adapters import prepare_question, resolve_adapter


FINAL = '{"choices":["option_2"],"confidence":0.9,"explanation":"fixture"}'


def response_body(content=FINAL, finish='stop'):
    return {'id': 'fixture', 'object': 'chat.completion', 'choices': [
        {'index': 0, 'finish_reason': finish, 'message': {
            'role': 'assistant', 'content': content,
            # 故意与最终答案矛盾，确保从未采用思考字段中的答案。
            'reasoning_content': '{"choices":["option_1"],"confidence":1}'}}],
        'usage': {'prompt_tokens': 20, 'completion_tokens': 40, 'total_tokens': 60}}


class QuestionFixture:
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        image = Path(directory.name) / 'stem.png'
        Image.new('RGB', (2, 2), 'white').save(image)
        self.question = Question(1, 'q-fixture', '单选题', '', str(image), [
            {'id': '100', 'text': 'A. first'}, {'id': '200', 'text': 'B. second'}])
        self.prepared = prepare_question(self.question)


class ProviderContractTests(QuestionFixture, unittest.TestCase):
    def test_official_base_urls_and_full_urls_resolve_without_guessing_gateway_paths(self):
        cases = {
            'https://api.deepseek.com': 'https://api.deepseek.com/chat/completions',
            'https://api.deepseek.com/v1/': 'https://api.deepseek.com/v1/chat/completions',
            'https://api.deepseek.com/beta': 'https://api.deepseek.com/beta/chat/completions',
            'https://api.siliconflow.cn/': 'https://api.siliconflow.cn/v1/chat/completions',
            'https://api.siliconflow.cn/v1': 'https://api.siliconflow.cn/v1/chat/completions',
            'https://api.siliconflow.cn/v1/chat/completions/': 'https://api.siliconflow.cn/v1/chat/completions',
            'https://api.siliconflow.cn/custom': 'https://api.siliconflow.cn/custom/chat/completions',
            'https://gateway.test': 'https://gateway.test/chat/completions',
            'https://api.siliconflow.cn.gateway.test': 'https://api.siliconflow.cn.gateway.test/chat/completions',
        }
        for url, endpoint in cases.items():
            with self.subTest(url=url):
                self.assertEqual(LLMSettings(url=url).endpoint, endpoint)

    def test_auto_selection_uses_official_host_and_explicit_adapter_takes_priority(self):
        self.assertEqual(resolve_adapter('custom', url='https://api.deepseek.com').name, 'deepseek')
        self.assertEqual(resolve_adapter('MiniMax-M3', url='https://api.siliconflow.cn/v1').name, 'siliconflow')
        self.assertEqual(resolve_adapter('Pro/deepseek-ai/DeepSeek-V4-Flash').name, 'deepseek')
        self.assertEqual(resolve_adapter('Qwen/Qwen-VL-fixture').name, 'qwen')
        self.assertEqual(resolve_adapter('custom', 'generic', url='https://api.siliconflow.cn').name, 'generic')
        self.assertEqual(resolve_adapter('custom', url='https://api.siliconflow.cn.gateway.test').name, 'generic')

    def test_siliconflow_is_valid_in_model_slot_and_inherits_public_url(self):
        config = Config.__new__(Config)
        config._config = configparser.ConfigParser(interpolation=None)
        config._config.read_dict({'llm-option': {'url': 'https://api.siliconflow.cn', 'key': 'fixture-key'},
            'llm-model-2': {'model': 'Pro/OpenGVLab/InternVL-fixture', 'adapter': 'siliconflow'}})
        with patch.dict('os.environ', {}, clear=True):
            settings = LLMSettings.from_config(config, '2')
        settings.validate()
        self.assertEqual(settings.adapter, 'siliconflow')
        self.assertEqual(settings.endpoint, 'https://api.siliconflow.cn/v1/chat/completions')

    def test_deepseek_schema_downgrade_is_limited_to_official_host(self):
        for url, expected in (('https://api.deepseek.com', 'json_object'),
                              ('https://gateway.test/v1', 'json_schema')):
            with self.subTest(url=url):
                settings = LLMSettings(url=url, model='deepseek-flash', response_mode='schema')
                payload = resolve_adapter(settings.model, url=url).build_request(self.prepared, settings)
                self.assertEqual(payload['response_format']['type'], expected)
                self.assertFalse(payload['stream'])
                self.assertNotIn('reasoning_split', payload)
                self.assertNotIn('n', payload)

    def test_siliconflow_reorders_only_internvl_and_preserves_image_detail_and_prompt(self):
        for model, types in (
                ('Pro/OpenGVLab/InternVL-fixture', ['image_url', 'text']),
                ('Qwen/Qwen-VL-fixture', ['text', 'image_url'])):
            with self.subTest(model=model):
                settings = LLMSettings(model=model)
                payload = resolve_adapter(model, 'siliconflow').build_request(self.prepared, settings)
                parts = payload['messages'][1]['content']
                self.assertEqual([part['type'] for part in parts], types)
                self.assertEqual(payload['n'], 1)
                self.assertFalse(payload['stream'])
                image = next(part['image_url'] for part in parts if part['type'] == 'image_url')
                self.assertEqual(image['url'], self.prepared.image_url)
                self.assertEqual(image['detail'], 'high')
                data = json.loads(next(part['text'] for part in parts if part['type'] == 'text'))
                self.assertEqual(data['allowed_choices'], ['option_1', 'option_2'])
                self.assertNotIn('response_format', payload)

    def test_text_only_questions_do_not_require_multimodal_content(self):
        self.question.stem_screenshot = ''
        self.question.stem_dom_text = 'Complete text question'
        prepared = prepare_question(self.question)
        for name in ('deepseek', 'siliconflow'):
            with self.subTest(adapter=name):
                payload = resolve_adapter('fixture', name).build_request(prepared, LLMSettings(model='fixture'))
                self.assertIsInstance(payload['messages'][1]['content'], str)

    def test_provider_reasoning_is_never_used_as_final_answer(self):
        for name in ('deepseek', 'siliconflow'):
            with self.subTest(adapter=name):
                adapter = resolve_adapter('fixture', name)
                content, finish = adapter.read_response(response_body())
                self.assertEqual(adapter.parse_response(content, finish, self.prepared).option_ids, ['200'])
                with self.assertRaises(LLMError):
                    adapter.read_response(response_body(content=None))

    def test_incomplete_finish_reasons_reject_even_well_formed_answer(self):
        for finish in ('length', 'aborted', 'insufficient_system_resource'):
            for content in (FINAL, None):
                with self.subTest(finish=finish, content=content):
                    adapter = resolve_adapter('deepseek-flash')
                    body, reason = adapter.read_response(response_body(content, finish))
                    with self.assertRaises(LLMError):
                        adapter.parse_response(body, reason, self.prepared)

    def test_unexpected_tool_calls_are_not_interpreted_as_answer_json(self):
        body = response_body()
        body['choices'][0]['message']['tool_calls'] = [{'id': 'fixture-tool'}]
        with self.assertRaisesRegex(LLMError, '工具调用'):
            resolve_adapter('deepseek-flash').read_response(body)


class ProviderTransportTests(QuestionFixture, unittest.IsolatedAsyncioTestCase):
    async def test_official_requests_authentication_images_and_final_content(self):
        for url, model, path, adapter in (
                ('https://api.deepseek.com', 'deepseek-flash', '/chat/completions', 'deepseek'),
                ('https://api.siliconflow.cn', 'Pro/OpenGVLab/InternVL-fixture', '/v1/chat/completions', 'siliconflow')):
            calls = []
            def respond(request):
                calls.append(request)
                self.assertEqual(request.url.path, path)
                self.assertEqual(request.headers['authorization'], 'Bearer fixture-key')
                payload = json.loads(request.content)
                self.assertEqual(payload['model'], model)
                self.assertFalse(payload['stream'])
                self.assertNotIn('reasoning_split', payload)
                parts = payload['messages'][1]['content']
                image = next(part['image_url'] for part in parts if part['type'] == 'image_url')
                self.assertTrue(image['url'].startswith('data:image/png;base64,'))
                return httpx.Response(200, json=response_body())
            with self.subTest(url=url):
                relay = VisionLLM(LLMSettings(url, 'fixture-key', model, retry_delay=0), httpx.MockTransport(respond))
                answer = await relay.answer(self.question)
                self.assertEqual(answer.option_ids, ['200'])
                self.assertEqual(relay.last_trace['adapter'], adapter)
                self.assertEqual(len(calls), 1)

    async def test_deepseek_downgrade_is_visible_and_requires_no_failed_request(self):
        calls = []
        def respond(request):
            payload = json.loads(request.content)
            calls.append(payload)
            self.assertEqual(payload['response_format'], {'type': 'json_object'})
            return httpx.Response(200, json=response_body())
        relay = VisionLLM(LLMSettings('https://api.deepseek.com', 'fixture-key', 'deepseek-flash',
            response_mode='schema', retry_delay=0), httpx.MockTransport(respond))
        await relay.answer(self.question)
        self.assertEqual(len(calls), 1)
        self.assertEqual(relay.last_trace['requested_mode'], 'schema')
        self.assertEqual(relay.last_trace['response_mode'], 'json_object')
        self.assertIn({'stage': 'parameters', 'converted': 'schema_to_json'}, relay.last_trace['events'])

    async def test_siliconflow_flat_parameter_error_can_recover_without_dropping_image(self):
        calls = []
        def respond(request):
            payload = json.loads(request.content)
            calls.append(payload)
            self.assertEqual(payload['messages'][1]['content'][0]['type'], 'image_url')
            if len(calls) == 1:
                return httpx.Response(400, json={'code': 20012, 'message': 'response_format not supported', 'data': None})
            self.assertNotIn('response_format', payload)
            return httpx.Response(200, json=response_body())
        relay = VisionLLM(LLMSettings('https://api.siliconflow.cn/v1', 'fixture-key', 'Pro/OpenGVLab/InternVL-fixture',
            response_mode='schema', retry_delay=0), httpx.MockTransport(respond))
        self.assertEqual((await relay.answer(self.question)).option_ids, ['200'])
        self.assertEqual(len(calls), 2)

    async def test_provider_billing_and_auth_errors_do_not_repeat_requests(self):
        for status in (401, 402):
            calls = []
            def respond(request):
                calls.append(request)
                return httpx.Response(status, json={'code': 20012, 'message': 'Insufficient balance', 'data': None})
            with self.subTest(status=status):
                relay = VisionLLM(LLMSettings('https://api.siliconflow.cn/v1', 'fixture-key', 'fixture',
                    retry_delay=0), httpx.MockTransport(respond))
                with self.assertRaisesRegex(LLMError, f'HTTP {status}'):
                    await relay.answer(self.question)
                self.assertEqual(len(calls), 1)

    async def test_incomplete_generation_retries_without_format_correction_or_reasoning_replay(self):
        for finish in ('length', 'aborted', 'insufficient_system_resource'):
            calls = []
            def respond(request):
                payload = json.loads(request.content)
                calls.append(payload)
                self.assertNotIn('format_correction', json.loads(payload['messages'][1]['content'][0]['text']))
                self.assertNotIn('reasoning_content', json.dumps(payload))
                # 截断正文即使看似完整，也必须等待第二次正常结束的生成。
                return httpx.Response(200, json=response_body(finish=finish if len(calls) == 1 else 'stop'))
            with self.subTest(finish=finish):
                relay = VisionLLM(LLMSettings('https://api.deepseek.com', 'fixture-key', 'deepseek-flash',
                    retry_delay=0), httpx.MockTransport(respond))
                self.assertEqual((await relay.answer(self.question)).option_ids, ['200'])
                self.assertEqual(len(calls), 2)
                self.assertEqual(relay.last_trace['events'][0]['stage'], 'generation')

    async def test_interruption_uses_same_three_request_budget(self):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(200, json=response_body(content=None, finish='insufficient_system_resource'))
        relay = VisionLLM(LLMSettings('https://api.deepseek.com', 'fixture-key', 'deepseek-flash',
            retry_delay=0), httpx.MockTransport(respond))
        with self.assertRaisesRegex(LLMError, '多次生成未完成') as failure:
            await relay.answer(self.question)
        self.assertEqual(len(calls), 3)
        self.assertEqual(failure.exception.relay_trace['attempts'], 3)


if __name__ == '__main__':
    unittest.main()
