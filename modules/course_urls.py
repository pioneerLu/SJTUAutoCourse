"""Parse pasted course links without guessing course identifiers or following redirects."""
import re
from collections.abc import Iterable
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit


COURSE_HOSTS = frozenset({
    'wisdom-mooc.zhihuishu.com', 'onlineweb.zhihuishu.com',
    'studyvideoh5.zhihuishu.com', 'studywisdomh5.zhihuishu.com',
    'fusioncourseh5.zhihuishu.com', 'hike.zhihuishu.com',
})
_URL = re.compile(
    r'(?<![\w@./-])(?:(?:[a-z][a-z0-9+.-]*://|//)'
    r'[a-z0-9-]+(?:\.[a-z0-9-]+)+|(?:'
    + '|'.join(re.escape(host) for host in sorted(COURSE_HOSTS))
    + r')(?![\w.-]))'
    + r'''(?:[/:?#][^\s<>"'`()\[\]，。；！？：、【】（）“”‘’]*)?''', re.IGNORECASE,
)
_INVISIBLE = str.maketrans('', '', '\u200b\u200c\u200d\ufeff')
_ID_KEY = 'recruitAndCourseId'


class CourseUrlError(ValueError):
    """An input cannot uniquely identify a supported course page."""


def _normalize(candidate: str) -> tuple[str, tuple]:
    if '…' in candidate or candidate.endswith('...'):
        raise CourseUrlError('链接被省略号截断，请复制地址栏中的完整链接。')
    candidate = candidate.rstrip('.,;!')
    if candidate.startswith('//'):
        candidate = 'https:' + candidate
    elif '://' not in candidate:
        candidate = 'https://' + candidate
    try:
        parts = urlsplit(candidate)
        host = (parts.hostname or '').lower()
        port = parts.port
    except ValueError:
        raise CourseUrlError('网址结构不完整，请重新复制课程播放页地址。') from None
    if parts.scheme.lower() not in ('http', 'https'):
        raise CourseUrlError('课程链接只支持 HTTP / HTTPS。')
    if host not in COURSE_HOSTS:
        raise CourseUrlError('不是支持的智慧树课程播放页域名，请从视频页面复制链接。')
    if parts.username is not None or parts.password is not None or port not in (None, 80, 443):
        raise CourseUrlError('课程链接不应包含账号、密码或自定义端口。')
    if re.search(r'%(?![0-9a-f]{2})', candidate, re.IGNORECASE):
        raise CourseUrlError('网址含有不完整的百分号编码，请重新复制完整链接。')
    path = parts.path or '/'
    if re.search(r'(?:login|gologin|passport|stuexam|studentexam)', unquote(path), re.IGNORECASE):
        raise CourseUrlError('这是登录或考试中转链接，请复制课程视频页面的地址。')
    query, fragment = parts.query, parts.fragment
    identity = None
    if host == 'wisdom-mooc.zhihuishu.com':
        # Some copied SPA links place the study route and parameters after #.
        fragment_query = fragment.partition('?')[2] if '?' in fragment else fragment
        pairs = parse_qsl(query, keep_blank_values=True)
        fragment_pairs = parse_qsl(fragment_query, keep_blank_values=True)
        ids = [value for key, value in pairs + fragment_pairs if key.lower() == _ID_KEY.lower()]
        if not ids or not all(ids):
            raise CourseUrlError('链接缺少 recruitAndCourseId，无法确定课程；请进入视频页后复制地址。')
        if len(set(ids)) != 1:
            raise CourseUrlError('链接包含不同的课程标识，无法确定应打开哪门课。')
        identity = ids[0]
        if not re.fullmatch(r'[A-Za-z0-9_-]+', identity):
            raise CourseUrlError('课程标识格式不完整，请重新复制课程播放页地址。')
        if any(key.lower() == _ID_KEY.lower() for key, _ in fragment_pairs):
            query = '&'.join(filter(None, (query, fragment_query)))
            fragment = ''
        # Preserve encoded values and extra parameters byte for byte; only repair the key.
        segments = []
        found_id = False
        for segment in query.split('&'):
            key, separator, value = segment.partition('=')
            if unquote(key).lower() == _ID_KEY.lower():
                if found_id:
                    continue
                segment = _ID_KEY + separator + value
                found_id = True
            segments.append(segment)
        query = '&'.join(segments)
        path = '/study/index'
    elif path == '/' and not query and not fragment:
        raise CourseUrlError('这是网站首页，无法确定课程；请复制课程播放页的完整地址。')
    url = urlunsplit(('https', host, path, query, fragment))
    return url, (host, identity) if identity is not None else (url,)


def normalize_course_urls(inputs: str | Iterable[str], *, required: bool = True) -> list[str]:
    """Accept Markdown/prose, bare domains and multiple links; keep first-seen order.

    Errors deliberately contain positions and reasons, never raw URLs or login tickets.
    A malformed nonempty line aborts the whole batch instead of changing course numbers.
    """
    entries = [inputs] if isinstance(inputs, str) else list(inputs)
    urls, seen = [], set()
    for entry_number, entry in enumerate(entries, 1):
        for line_number, line in enumerate(entry.splitlines(), 1):
            line = line.translate(_INVISIBLE).strip()
            if not line:
                continue
            line = re.sub(r'&(?:amp|#0*38|#x0*26);', '&', line, flags=re.IGNORECASE)
            # Repair common protocol punctuation mistakes; host validation still follows.
            line = re.sub(r'\b(https?)(?:[:：]/{0,2}|/{1,2})(?=[a-z0-9-]+\.)',
                          r'\1://', line, flags=re.IGNORECASE)
            line = re.sub(r'[,;](?=(?:https?://|//|[a-z0-9-]+(?:\.[a-z0-9-]+)+[/:?#]))',
                          ' ', line, flags=re.IGNORECASE)
            candidates = _URL.findall(line)
            try:
                if not candidates:
                    raise CourseUrlError('未识别到课程链接，请粘贴课程视频页面地址。')
                for candidate in candidates:
                    url, identity = _normalize(candidate)
                    if identity not in seen:
                        urls.append(url)
                        seen.add(identity)
            except CourseUrlError as error:
                position = f'第 {line_number} 行' if len(entries) == 1 else f'第 {entry_number} 项第 {line_number} 行'
                raise CourseUrlError(f'{position}：{error}') from None
    if required and not urls:
        raise CourseUrlError('请填写至少一个课程播放页链接。')
    return urls
