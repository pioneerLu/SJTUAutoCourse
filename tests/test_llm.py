import configparser
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from modules.configs import Config
from modules.llm import Answer, LLMError, LLMSettings, Question, VisionLLM, parse_answer, answer_from_record


def write_test_image(path):
    Image.new("RGB", (2, 2), "white").save(path, format="PNG")


def question(kind='【单选题】', screenshot='unused.png'):
    return Question(1, 'anchor-1', kind, '', screenshot,
                    [{'id': '11', 'text': 'A. first'}, {'id': '22', 'text': 'B. second'}])


class AnswerValidationTests(unittest.TestCase):
    def test_saved_record_uses_current_threshold_in_both_directions(self):
        record = {'status': 'answered', 'meets_confidence': True,
                  'answer': {'option_ids': ['11'], 'confidence': 0.9, 'explanation': 'fixture'}}
        with self.assertRaisesRegex(LLMError, '当前阈值'):
            answer_from_record(record, question(), 0.95)
        self.assertFalse(record['meets_confidence'])
        self.assertEqual(answer_from_record(record, question(), 0.8).confidence, 0.9)
        self.assertTrue(record['meets_confidence'])

    def test_manual_choice_bypasses_threshold_but_still_validates_options(self):
        record = {'status': 'manual', 'error': 'previous model failure',
                  'answer': {'option_ids': ['11'], 'confidence': 0, 'explanation': 'manual'}}
        self.assertEqual(answer_from_record(record, question(), 1).option_ids, ['11'])
        self.assertNotIn('error', record)
        record['answer']['option_ids'] = ['invalid']
        with self.assertRaises(LLMError):
            answer_from_record(record, question(), 1)

    def test_fenced_json_and_multiple_answers(self):
        result = parse_answer('```json\n{"option_ids":["22","11"],"confidence":0.9}\n```', question('多选题'))
        self.assertEqual(result.option_ids, ['22', '11'])

    def test_rejects_unknown_duplicate_and_multiple_single_choice_ids(self):
        for ids in [['99'], ['11', '11'], ['11', '22'], [11], []]:
            with self.subTest(ids=ids), self.assertRaises(LLMError):
                parse_answer(json.dumps({'option_ids': ids, 'confidence': 0.9}), question())

    def test_network_numeric_ids_only_normalize_exact_provided_ids(self):
        answer = parse_answer('{"option_ids":[11,"22"],"confidence":0.9}', question('多选题'), normalize_ids=True)
        self.assertEqual(answer.option_ids, ['11', '22'])
        for ids in ([True], [11.0], [99], [11, '11'], ['A', 11], '11', ['C'], []):
            with self.subTest(ids=ids), self.assertRaises(LLMError):
                parse_answer(json.dumps({'option_ids': ids, 'confidence': 0.9}), question(), normalize_ids=True)
        leading_zero = question()
        leading_zero.options[0]['id'] = '011'
        with self.assertRaises(LLMError):
            parse_answer('{"option_ids":[11],"confidence":0.9}', leading_zero, normalize_ids=True)

    def test_letter_aliases_match_actual_unique_labels_not_option_positions(self):
        q = question()
        q.options = [{'id': '44', 'text': 'D. fourth'}, {'id': '11', 'text': 'A. first'}]
        answer = parse_answer('{"option_ids":["d"],"confidence":0.9}', q, normalize_ids=True)
        self.assertEqual(answer.option_ids, ['44'])
        with self.assertRaises(LLMError):
            parse_answer('{"option_ids":["D"],"confidence":0.9}', q)
        q.options[1]['text'] = 'D. duplicate label'
        with self.assertRaises(LLMError):
            parse_answer('{"option_ids":["D"],"confidence":0.9}', q, normalize_ids=True)

    def test_missing_option_field_and_empty_choices_have_specific_errors(self):
        for reply, reason in [('{"confidence":0.9}', '缺少 option_ids'),
                              ('{"option_ids":[],"confidence":0.1}', 'option_ids 为空')]:
            with self.subTest(reply=reply), self.assertRaisesRegex(LLMError, reason):
                parse_answer(reply, question())

    def test_rejects_malformed_confidence_and_json(self):
        for confidence in [True, '0.9', float('nan'), 1.1, None]:
            with self.subTest(confidence=confidence), self.assertRaises(LLMError):
                parse_answer(json.dumps({'option_ids': ['11'], 'confidence': confidence}), question())
        with self.assertRaises(LLMError):
            parse_answer('answer: A', question())

    def test_empty_settings_fail_without_network(self):
        with self.assertRaisesRegex(LLMError, 'url, key, model'):
            VisionLLM(LLMSettings())

    def test_endpoint_and_environment_configuration(self):
        config = Config.__new__(Config)
        config._config = configparser.ConfigParser(interpolation=None)
        config._config.read_string('[llm-option]\nautoSubmit=True\nminConfidence=0.7\n')
        with patch.dict('os.environ', {'AUTOCOURSE_LLM_URL': 'https://llm.example/v1/',
            'AUTOCOURSE_LLM_KEY': 'test', 'AUTOCOURSE_LLM_MODEL': 'vision-test'}):
            settings = LLMSettings.from_config(config)
            settings.validate()
        self.assertEqual(settings.endpoint, 'https://llm.example/v1/chat/completions')
        self.assertTrue(settings.auto_submit)
        settings.url = 'https://llm.example/v1/chat/completions'
        self.assertEqual(settings.endpoint, settings.url)

    def test_new_environment_names_override_legacy_names(self):
        config = Config.__new__(Config)
        config._config = configparser.ConfigParser(interpolation=None)
        config._config.read_string('[llm-option]\n')
        legacy = {'AUTOVISOR_LLM_URL': 'https://legacy.example/v1',
                  'AUTOVISOR_LLM_KEY': 'legacy-test-key', 'AUTOVISOR_LLM_MODEL': 'legacy-model'}
        with patch.dict('os.environ', legacy, clear=True):
            settings = LLMSettings.from_config(config)
            self.assertEqual((settings.url, settings.key, settings.model),
                             ('https://legacy.example/v1', 'legacy-test-key', 'legacy-model'))
            with patch.dict('os.environ', {'AUTOCOURSE_LLM_MODEL': 'new-model'}):
                self.assertEqual(LLMSettings.from_config(config).model, 'new-model')


class LLMRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_answer_is_repaired_then_numeric_ids_are_normalized(self):
        calls, retries = [], []
        def respond(request):
            payload = json.loads(request.content)
            calls.append(payload)
            if len(calls) == 1:
                content = '{"answer":"UNKNOWN","confidence":0.9}'
            else:
                self.assertEqual(len(payload['messages']), 2)
                info = json.loads(payload['messages'][1]['content'][0]['text'])
                self.assertIn('字符串数组', info['format_correction'])
                self.assertEqual(info['allowed_choices'], ['option_1', 'option_2'])
                content = '{"option_ids":[22],"confidence":0.9}'
            return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'stem.png'
            write_test_image(image)
            solver = VisionLLM(LLMSettings('https://llm.test/v1', 'fixture-key', 'fixture', retry_delay=0), httpx.MockTransport(respond))
            answer = await solver.answer(question(screenshot=str(image)), on_retry=retries.append)
        self.assertEqual(answer.option_ids, ['22'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(retries), 1)

    async def test_unanswered_output_stops_after_three_requests_with_redacted_diagnostics(self):
        calls, retries = [], []
        content = json.dumps({'option_ids': [], 'confidence': 0.1,
            'explanation': 'echoed-secret Bearer sk-other-token https://example.test/?ticket=private data:image/png;base64,AAAA'})
        def respond(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]})
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'stem.png'
            write_test_image(image)
            solver = VisionLLM(LLMSettings('https://llm.test/v1', 'echoed-secret', 'fixture', retry_delay=0), httpx.MockTransport(respond))
            with self.assertRaisesRegex(LLMError, 'option_ids 为空') as failure:
                await solver.answer(question(screenshot=str(image)), on_retry=retries.append)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(retries), 2)
        diagnostics = json.dumps(failure.exception.response_diagnostics)
        for private in ('echoed-secret', 'sk-other-token', 'ticket=private', 'base64,AAAA'):
            self.assertNotIn(private, diagnostics)
            self.assertNotIn(private, json.dumps(calls[1]['messages']))

    async def test_empty_missing_and_truncated_replies_retry_with_original_image(self):
        for first in ({'content': ''}, {}, {'content': '{"option_ids":["11"', 'finish': 'length'}):
            with self.subTest(first=first), tempfile.TemporaryDirectory() as directory:
                calls = []
                def respond(request):
                    payload = json.loads(request.content)
                    calls.append(payload)
                    message = {k: v for k, v in first.items() if k != 'finish'} if len(calls) == 1 else {'content': '{"option_ids":["11"],"confidence":1}'}
                    return httpx.Response(200, json={'choices': [{'message': message, 'finish_reason': first.get('finish') if len(calls) == 1 else 'stop'}]})
                image = Path(directory) / 'stem.png'
                write_test_image(image)
                solver = VisionLLM(LLMSettings('https://llm.test/v1', 'fixture-key', 'fixture', retry_delay=0), httpx.MockTransport(respond))
                self.assertEqual((await solver.answer(question(screenshot=str(image)))).option_ids, ['11'])
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[1]['messages'][1]['content'][1]['type'], 'image_url')

    async def test_retryable_errors_explain_limits_and_upstream_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'stem.png'
            write_test_image(image)
            for status, explanation in [(429, '频率或配额限制'), (503, '模型上游')]:
                calls = []
                def respond(request):
                    calls.append(request)
                    return httpx.Response(status, text='echoed-secret')
                solver = VisionLLM(LLMSettings('https://llm.test/v1', 'echoed-secret', 'fixture-model', retry_delay=0),
                                   httpx.MockTransport(respond))
                with self.assertRaisesRegex(LLMError, explanation) as error:
                    await solver.answer(question(screenshot=str(image)))
                self.assertEqual(len(calls), 3)
                self.assertNotIn('echoed-secret', str(error.exception))

    async def test_402_explains_billing_failure_without_retrying_or_leaking_key(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'stem.png'
            write_test_image(image)
            for body, expected in [
                ({'error': {'message': 'Insufficient Balance echoed-secret'}}, '余额不足'),
                ({'error': {'message': 'request rejected echoed-secret'}}, '计费、额度或模型权限'),
            ]:
                calls = []
                def respond(request):
                    calls.append(request)
                    return httpx.Response(402, json=body)
                solver = VisionLLM(LLMSettings('https://llm.test/v1', 'echoed-secret', 'fixture-model'),
                                   httpx.MockTransport(respond))
                with self.assertRaisesRegex(LLMError, expected) as error:
                    await solver.answer(question(screenshot=str(image)))
                self.assertEqual(len(calls), 1)
                self.assertNotIn('echoed-secret', str(error.exception))

    async def test_multimodal_request_and_answer_contract(self):
        def respond(request):
            payload = json.loads(request.content)
            self.assertEqual(request.url.path, '/v1/chat/completions')
            self.assertEqual(request.headers['authorization'], 'Bearer test-key')
            self.assertEqual(payload['model'], 'vision-test')
            content = payload['messages'][1]['content']
            self.assertTrue(content[1]['image_url']['url'].startswith('data:image/png;base64,'))
            self.assertEqual(json.loads(content[0]['text'])['options'][1]['id'], 'option_2')
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(
                {'option_ids': ['22'], 'confidence': 0.95, 'explanation': 'short reason'})}}]})
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'stem.png'
            write_test_image(image)
            solver = VisionLLM(LLMSettings('https://llm.example/v1', 'test-key', 'vision-test'), httpx.MockTransport(respond))
            answer = await solver.answer(question(screenshot=str(image)))
        self.assertEqual(answer, Answer(['22'], 0.95, 'short reason'))

    async def test_low_confidence_stops_and_http_error_does_not_leak_key(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'stem.png'
            write_test_image(image)
            for response, message in [
                (httpx.Response(200, json={'choices': [{'message': {'content': '{"option_ids":["11"],"confidence":0.2}'}}]}), '置信度'),
                (httpx.Response(401, text='echoed-secret'), 'HTTP 401'),
            ]:
                solver = VisionLLM(LLMSettings('https://llm.example/v1', 'echoed-secret', 'vision-test'), httpx.MockTransport(lambda r: response))
                with self.assertRaisesRegex(LLMError, message) as error:
                    await solver.answer(question(screenshot=str(image)))
                self.assertNotIn('echoed-secret', str(error.exception))

    async def test_retries_transient_service_failure(self):
        calls = []
        def respond(request):
            calls.append(request)
            if len(calls) < 3:
                return httpx.Response(503)
            return httpx.Response(200, json={'choices': [{'message': {'content': '{"option_ids":["11"],"confidence":1}'}}]})
        with tempfile.TemporaryDirectory() as directory, patch('modules.llm.asyncio.sleep'):
            image = Path(directory) / 'stem.png'
            write_test_image(image)
            solver = VisionLLM(LLMSettings('https://llm.example/v1', 'test-key', 'vision-test'), httpx.MockTransport(respond))
            self.assertEqual((await solver.answer(question(screenshot=str(image)))).option_ids, ['11'])
        self.assertEqual(len(calls), 3)
