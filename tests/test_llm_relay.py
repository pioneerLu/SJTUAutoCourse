import asyncio
import configparser
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from modules.configs import Config
from modules.llm import Answer, LLMError, LLMRelay, LLMSettings, Question, VisionLLM, answer_from_record
from modules.llm_adapters import prepare_question, resolve_adapter


def write_test_image(path):
    Image.new("RGB", (2, 2), "white").save(path, format="PNG")


class RelayFormatsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        image = Path(self.directory.name) / 'stem.png'
        write_test_image(image)
        self.q = Question(1, 'q-1', '多选题', '', str(image), [
            {'id': '4911', 'text': 'D. fourth'}, {'id': '4912', 'text': 'A. first'},
            {'id': '4913', 'text': 'C. third'}, {'id': '4914', 'text': 'B. second'}])
        self.prepared = prepare_question(self.q)

    def parse(self, content, model='MiniMax-M3', finish='stop'):
        return resolve_adapter(model).parse_response(content, finish, self.prepared)

    def test_request_uses_virtual_ids_and_keeps_actual_visual_labels(self):
        adapter = resolve_adapter('MiniMax-M3')
        payload = adapter.build_request(self.prepared, LLMSettings(model='MiniMax-M3'))
        info = json.loads(payload['messages'][1]['content'][0]['text'])
        self.assertEqual(info['options'][0]['id'], 'option_1')
        self.assertEqual(info['options'][0]['label'], 'D')
        self.assertNotIn('4911', json.dumps(info))
        self.assertEqual(self.prepared.code_to_id['option_1'], '4911')
        self.assertTrue(payload['reasoning_split'])

    def test_aliases_wrappers_scalar_and_percentage_normalize_for_all_families(self):
        replies = [
            '{"choices":["option_1","option_4"],"confidence":0.9}',
            '```json\n{"answer":"DB","confidence":"90%","reason":"fixture"}\n```',
            '{"result":{"answer_ids":[4911,4914],"confidence":"0.9"}}',
            '说明：{"answer":{"selected_options":["fourth","second"],"confidence":0.9}} 完毕',
            '<think>{"choices":["option_2"],"confidence":1}</think>{"choices":"D, B","confidence":0.9}',
            '{"答案":{"选择":[{"code":"option_1","label":"D"},{"id":"option_4"}],"置信度":"90%","解析":"示例"}}',
        ]
        for model in ('MiniMax-M3', 'qwen3-vl-plus', 'glm-4.6v', 'kimi-for-coding', 'deepseek-v4-flash-vision-exp', 'custom-vision'):
            for reply in replies:
                with self.subTest(model=model, reply=reply):
                    self.assertEqual(self.parse(reply, model).option_ids, ['4911', '4914'])
                    self.assertEqual(self.parse(reply, model).confidence, 0.9)

    def test_ambiguous_conflicting_missing_and_duplicate_answers_fail(self):
        invalid = [
            '{"choices":["option_1"],"option_ids":["4912"],"confidence":0.9}',
            '{"choices":["option_1"],"confidence":true}',
            '{"choices":["option_1"],"confidence":90}',
            '{"choices":["option_1"]}',
            '{"choices":[],"confidence":0.9}',
            '{"choices":["option_1","D"],"confidence":0.9}',
            '{"choices":["option_1"],"option_ids":["4911","4911"],"confidence":0.9}',
            '{"choices":[1],"confidence":0.9}',
            '{"choices":[true],"confidence":0.9}',
            '{"choices":["missing"],"confidence":0.9}',
            '{"choices":["option_1"],"confidence":0.9} {"choices":["option_2"],"confidence":0.9}',
            '<think>{"choices":["option_1"],"confidence":0.9}</think>',
            '说明：<thinking mode="private">{"choices":["option_1"],"confidence":0.9}</thinking>',
            '{"choices":[{"id":"option_1","label":"A"}],"confidence":0.9}',
            '{"choices":["option_1"],"confidence":0.9,"置信度":"20%"}',
            '{"result":{"choices":["option_1"],"confidence":0.9},"confidence":0.2}',
        ]
        for content in invalid:
            with self.subTest(content=content), self.assertRaises(LLMError):
                self.parse(content)
        self.q.options[1]['text'] = 'D. duplicate'
        self.prepared = prepare_question(self.q)
        with self.assertRaises(LLMError):
            self.parse('{"choices":["D"],"confidence":0.9}')

    def test_single_choice_and_record_validator_remain_strict(self):
        self.q.type_and_score = '单选题'
        self.prepared = prepare_question(self.q)
        with self.assertRaises(LLMError):
            self.parse('{"choices":["option_1","option_2"],"confidence":0.9}')
        answer = self.parse('{"answer":"D","confidence":0.9}')
        self.assertEqual(answer.option_ids, ['4911'])
        record = {'status': 'answered', 'answer': {'option_ids': ['D'], 'confidence': 0.9}}
        with self.assertRaises(LLMError):
            answer_from_record(record, self.q, 0.8)

    def test_legacy_id_numbers_do_not_fall_back_to_answer_text_or_positions(self):
        self.q.options[0]['id'] = '04911'
        self.q.options[1]['text'] = 'A. 4911'
        self.prepared = prepare_question(self.q)
        with self.assertRaises(LLMError):
            self.parse('{"option_ids":["4911"],"confidence":0.9}')
        self.assertEqual(self.parse('{"answer":"4911","confidence":0.9}').option_ids, ['4912'])
        self.assertEqual(self.parse('{"answer":{"id":"option_1"},"confidence":0.9}').option_ids, ['04911'])

    def test_text_blocks_and_separate_reasoning_only_use_final_content(self):
        adapter = resolve_adapter('MiniMax-M3')
        content, finish = adapter.read_response({'choices': [{'finish_reason': 'stop', 'message': {
            'reasoning_content': '{"choices":["option_2"],"confidence":1}',
            'content': [{'type': 'thinking', 'thinking': 'private reasoning'},
                        {'type': 'text', 'text': '{"choices":["option_1"],'},
                        {'type': 'output_text', 'text': '"confidence":0.9}'}]}}]})
        self.assertEqual(adapter.parse_response(content, finish, self.prepared).option_ids, ['4911'])
        with self.assertRaises(LLMError):
            adapter.read_response({'choices': [{'message': {'content': None, 'reasoning_content': '{"choices":["option_1"]}'}}]})

    def test_schema_contains_only_codes_and_input_validation_precedes_network(self):
        payload = resolve_adapter('qwen3.8-max').build_request(self.prepared,
            LLMSettings(model='qwen3.8-max', response_mode='schema'))
        enum = payload['response_format']['json_schema']['schema']['properties']['choices']['items']['enum']
        self.assertEqual(enum, list(self.prepared.code_to_id))
        self.q.options[1]['id'] = self.q.options[0]['id']
        with self.assertRaisesRegex(LLMError, '重复'):
            prepare_question(self.q)
        self.q.options[1]['id'] = '4912'
        self.q.stem_screenshot = ''
        with self.assertRaises(LLMError):
            prepare_question(self.q)
        self.q.stem_dom_text = '完整文字题干'
        prepared = prepare_question(self.q)
        self.assertIsNone(prepared.image_url)
        payload = resolve_adapter('glm-5').build_request(prepared, LLMSettings(model='glm-5'))
        self.assertIsInstance(payload['messages'][1]['content'], str)

    def test_prepared_question_is_immutable_snapshot_and_dict_content_is_supported(self):
        adapter = resolve_adapter('kimi-for-coding')
        self.q.options[0]['id'] = 'changed-after-preparation'
        content, finish = adapter.read_response({'choices': [{'message': {'content': {
            'choices': ['option_1'], 'confidence': 0.9}}, 'finish_reason': 'stop'}]})
        self.assertEqual(adapter.parse_response(content, finish, self.prepared).option_ids, ['4911'])

    def test_non_images_and_wrong_input_types_fail_before_sending(self):
        with self.assertRaises(LLMError):
            prepare_question({'number': 1})
        self.q.stem_screenshot = True
        with self.assertRaises(LLMError):
            prepare_question(self.q)
        path = Path(self.directory.name) / 'fake-image.png'
        path.write_text('not-an-image', encoding='utf-8')
        self.q.stem_screenshot = str(path)
        with self.assertRaisesRegex(LLMError, '不是有效图片'):
            prepare_question(self.q)

    def test_case_aliases_and_three_wrapper_levels_are_supported_without_conflicts(self):
        content = '{"Result":{"Data":{"Answer":{"SelectedOptionIds":["D"],"Confidence":"0.9"}}}}'
        self.assertEqual(self.parse(content).option_ids, ['4911'])
        with self.assertRaises(LLMError):
            self.parse('{"choices":["option_1"],"CHOICES":["option_2"],"confidence":0.9}')
        with self.assertRaises(LLMError):
            self.parse('{"result":{"data":{"answer":{"result":{"choices":["option_1"],"confidence":0.9}}}}}')

    def test_compact_multi_choice_and_exact_text_ambiguity_is_not_guessed(self):
        self.q.options[0]['text'] = 'D. AB'
        self.prepared = prepare_question(self.q)
        with self.assertRaisesRegex(LLMError, '歧义'):
            self.parse('{"answer":"AB","confidence":0.9}')
        with self.assertRaises(LLMError):
            self.parse('<think><think>private</think>{"choices":["option_1"],"confidence":0.9}</think>')

    def test_profiles_override_and_inherit_adapter_configuration(self):
        config = Config.__new__(Config)
        config._config = configparser.ConfigParser(interpolation=None)
        config._config.read_dict({'llm-option': {'adapter': 'generic', 'responseMode': 'schema', 'model': 'qwen3.8-max'},
            'llm-model-2': {'model': 'MiniMax-M3', 'adapter': 'minimax', 'responseMode': 'prompt'}})
        with patch.dict('os.environ', {}, clear=True):
            first, second = LLMSettings.from_config(config, '1'), LLMSettings.from_config(config, '2')
        self.assertEqual((first.adapter, first.response_mode), ('generic', 'schema'))
        self.assertEqual((second.adapter, second.response_mode), ('minimax', 'prompt'))
        self.assertEqual(resolve_adapter('custom-name').name, 'generic')
        self.assertEqual(resolve_adapter('custom-name', 'qwen').name, 'qwen')
        with self.assertRaises(LLMError):
            resolve_adapter('custom-name', 'nonexistent')


class RelayTransportTests(unittest.IsolatedAsyncioTestCase):
    async def run_relay(self, handler, settings, *, twice=False):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'stem.png'
            write_test_image(image)
            q = Question(1, 'q-1', '单选题', '', str(image), [{'id': '100', 'text': 'A. first'}, {'id': '200', 'text': 'B. second'}])
            relay = VisionLLM(settings, httpx.MockTransport(handler))
            self.assertIsInstance(relay, LLMRelay)
            answer = await relay.answer(q)
            if twice:
                self.assertEqual(await relay.answer(q), answer)
            return answer, relay

    async def test_optional_format_rejection_falls_back_and_is_cached_without_losing_image(self):
        calls = []
        def respond(request):
            data = json.loads(request.content)
            calls.append(data)
            self.assertTrue(data['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/png'))
            if 'response_format' in data:
                return httpx.Response(400, json={'error': {'message': 'response_format not supported'}})
            return httpx.Response(200, json={'choices': [{'message': {'content': '{"choices":["option_2"],"confidence":"95%"}'}}]})
        answer, relay = await self.run_relay(respond, LLMSettings('https://llm.test/v1', 'fixture-key', 'qwen3.8-max',
            retry_delay=0, response_mode='schema'), twice=True)
        self.assertEqual(answer, Answer(['200'], 0.95, ''))
        self.assertEqual(len(calls), 3)
        self.assertEqual(relay.last_trace['response_mode'], 'prompt')

    async def test_minimax_optional_split_rejection_keeps_think_filter_and_final_answer(self):
        calls = []
        def respond(request):
            data = json.loads(request.content)
            calls.append(data)
            if 'reasoning_split' in data:
                return httpx.Response(400, json={'error': {'message': 'unknown reasoning_split'}})
            return httpx.Response(200, json={'choices': [{'message': {'content': '<think>private</think>{"answer":"B","confidence":0.9}'}}]})
        answer, relay = await self.run_relay(respond, LLMSettings('https://llm.test/v1', 'fixture-key', 'MiniMax-M3', retry_delay=0))
        self.assertEqual(answer.option_ids, ['200'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(relay.last_trace['adapter'], 'minimax')

    async def test_auth_refusal_and_image_refusal_do_not_trigger_parameter_fallback(self):
        for status, message in ((401, 'response_format unknown'), (400, 'image_url not supported')):
            calls = []
            def respond(request):
                calls.append(request)
                return httpx.Response(status, json={'error': {'message': message}})
            with self.subTest(status=status), self.assertRaisesRegex(LLMError, f'HTTP {status}'):
                await self.run_relay(respond, LLMSettings('https://llm.test/v1', 'fixture-key', 'fixture',
                    retry_delay=0, response_mode='json'))
            self.assertEqual(len(calls), 1)

    async def test_explicit_model_refusal_is_not_format_repaired(self):
        for choice in ({'finish_reason': 'content_filter', 'message': {'content': ''}},
                       {'message': {'content': None, 'refusal': 'not allowed'}}):
            calls = []
            def respond(request):
                calls.append(request)
                return httpx.Response(200, json={'choices': [choice]})
            with self.subTest(choice=choice), self.assertRaisesRegex(LLMError, '停止请求'):
                await self.run_relay(respond, LLMSettings('https://llm.test/v1', 'fixture-key', 'fixture', retry_delay=0))
            self.assertEqual(len(calls), 1)

    async def test_low_confidence_is_not_retried_or_increased(self):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(200, json={'choices': [{'message': {'content': '{"choices":["option_2"],"confidence":"20%"}'}}]})
        with self.assertRaisesRegex(LLMError, '置信度 0.20'):
            await self.run_relay(respond, LLMSettings('https://llm.test/v1', 'fixture-key', 'fixture', retry_delay=0))
        self.assertEqual(len(calls), 1)

    async def test_format_repair_is_fresh_and_does_not_replay_reasoning(self):
        calls = []
        def respond(request):
            data = json.loads(request.content)
            calls.append(data)
            if len(calls) == 1:
                return httpx.Response(200, json={'choices': [{'message': {'content': '{"choices":["bad"],"confidence":0.9}', 'reasoning_content': 'private secret reasoning'}}]})
            self.assertEqual(len(data['messages']), 2)
            self.assertNotIn('private secret reasoning', json.dumps(data))
            self.assertIn('format_correction', json.loads(data['messages'][1]['content'][0]['text']))
            return httpx.Response(200, json={'choices': [{'message': {'content': '{"answer":"B","confidence":0.9}'}}]})
        answer, _ = await self.run_relay(respond, LLMSettings('https://llm.test/v1', 'fixture-key', 'deepseek-v4-flash', retry_delay=0))
        self.assertEqual(answer.option_ids, ['200'])
        self.assertEqual(len(calls), 2)
