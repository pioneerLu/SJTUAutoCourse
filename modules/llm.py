"""兼容入口：模型调用统一交给 LLMRelay；本地答案继续严格校验。"""
import asyncio  # 保留已有调用方对重试等待的测试注入入口。

from modules.llm_types import LLMError, LLMSettings, Question, Answer, parse_answer, answer_from_record
from modules.llm_relay import LLMRelay


class VisionLLM(LLMRelay):
    """兼容旧名称，GUI、命令行和章节任务均复用同一个中继。"""


__all__ = ['LLMError', 'LLMSettings', 'Question', 'Answer', 'parse_answer',
           'answer_from_record', 'LLMRelay', 'VisionLLM']
