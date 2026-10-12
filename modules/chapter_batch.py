"""章节批量回填：逐章隔离失败、传递取消信号并汇总结果。"""
import asyncio
from pathlib import Path

from modules.chapter_records import answer_source, read_result_metadata


async def apply_review_batch(config, logger, cookie_path, targets, *, auto_submit=False,
                             callback=None, sources=None):
    """targets 为有序的目录到题目 ID 集合映射；None 表示整个章节。

    延迟加载网页操作，GUI 只负责选取目标和展示事件。sources 可提供界面中
    已加载的课程/测试编号，磁盘 context.json 优先；不访问任何 Tk 控件。
    """
    from modules.chapter_tests import apply_review, chapter_error_text, ChapterReviewBrowser

    def notify(event):
        if callback:
            callback(event)

    statuses = []
    async with ChapterReviewBrowser(config, logger, cookie_path) as runtime:
        for directory, ids in targets.items():
            await asyncio.sleep(0)  # 在章节之间响应取消，不吞掉 CancelledError。
            directory = Path(directory)
            context = read_result_metadata(directory / 'context.json')
            source = (sources or {}).get(directory) or answer_source(directory)
            course = context.get('course', source['course'] or 1)
            test = context['index'] + 1 if isinstance(context.get('index'), int) else (source['test'] or 1)
            notify({'kind': 'test_started', 'directory': str(directory), 'course': course, 'test': test})
            finished = False

            def forward(event):
                nonlocal finished
                if event['kind'] == 'test_finished':
                    finished = True
                notify(event)

            try:
                status = await apply_review(config, logger, cookie_path, directory, auto_submit=auto_submit,
                                            runtime=runtime, callback=forward, question_ids=sorted(ids) if ids is not None else None)
            except Exception as exc:
                status = 'failed'
                reason = chapter_error_text(exc, config)
                if not finished:
                    notify({'kind': 'test_finished', 'directory': str(directory), 'course': course,
                            'test': test, 'status': status, 'error': reason})
                notify({'kind': 'log', 'message': f'当前章节失败，继续处理后续章节：{reason}'})
            statuses.append(status)
    if len(statuses) == 1 and statuses[0] != 'failed':
        return statuses[0]
    return {'batch': True, 'total': len(statuses),
            'completed': sum(s in ('saved', 'submitted') for s in statuses),
            'failed': statuses.count('failed'),
            'skipped': sum(s in ('already_submitted', 'skipped_no_answers') for s in statuses)}
