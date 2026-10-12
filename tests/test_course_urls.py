import unittest

from modules.course_urls import CourseUrlError, normalize_course_urls


BASE = 'https://wisdom-mooc.zhihuishu.com/study/index?recruitAndCourseId=Course_42'


class CourseUrlTests(unittest.TestCase):
    def test_common_paste_formats_resolve_to_the_same_course(self):
        for text in (BASE, f'[课程页面]({BASE})', f'[Chapter1.pdf]({BASE})', f'课程地址：<{BASE}>。',
                     BASE.removeprefix('https://'), BASE.replace('https:', 'http:'),
                     BASE.replace('https:', ''), '  ' + BASE + '  ',
                     BASE.replace('https://', 'https:/'),
                     BASE.replace('https://', 'https//'),
                     BASE.replace('https://', 'https：//'),
                     BASE.replace('wisdom', 'wis\u200bdom'),
                     BASE.replace('/study/index', '/study'),
                     BASE.replace('recruitAndCourseId', 'recruitandcourseid')):
            with self.subTest(text=text):
                self.assertEqual(normalize_course_urls(text), [BASE])

    def test_query_values_are_not_decoded_or_reencoded(self):
        url = BASE + '&title=%E4%B8%AD&token=a%2Bb%2Fz%3D&lesson=42&name=课程'
        self.assertEqual(normalize_course_urls('[视频](' + url.replace('&', '&amp;') + ')'), [url])
        self.assertEqual(normalize_course_urls(BASE.replace('Course_42', '%43ourse_42')), [BASE.replace('Course_42', '%43ourse_42')])

    def test_hash_route_identifier_is_recovered(self):
        url = 'wisdom-mooc.zhihuishu.com/#/study/index?recruitAndCourseId=Course_42&x=%2B'
        self.assertEqual(normalize_course_urls(url), [BASE + '&x=%2B'])

    def test_multiple_links_deduplicate_by_course_and_preserve_order(self):
        second = BASE.replace('Course_42', 'Other_99')
        self.assertEqual(normalize_course_urls(f'{BASE}，{second}\n[{BASE}]({BASE}&utm_source=copy)'), [BASE, second])
        self.assertEqual(normalize_course_urls([BASE, '', second, BASE]), [BASE, second])
        self.assertEqual(normalize_course_urls(BASE + ',' + second), [BASE, second])

    def test_other_supported_routes_keep_their_path_query_and_fragment(self):
        for host in ('onlineweb', 'studyvideoh5', 'studywisdomh5', 'fusioncourseh5', 'hike'):
            url = f'https://{host}.zhihuishu.com/stuStudy?courseId=42&x=%E4%B8%AD#/video/3'
            with self.subTest(host=host):
                self.assertEqual(normalize_course_urls(url.removeprefix('https://')), [url])

    def test_unusable_or_ambiguous_links_are_rejected(self):
        for text in ('', '没有链接', 'https://wisdom-mooc.zhihuishu.com',
                     'https://wisdom-mooc.zhihuishu.com/study?courseId=42',
                     BASE.replace('Course_42', ''), BASE + '&recruitAndCourseId=Other_99',
                     BASE + '...', BASE + '…', BASE.replace('Course_42', '%ZZ'),
                     BASE.replace('Course_42', 'a%2Fb'),
                     BASE.replace('zhihuishu.com', 'zhihuishu.com.evil.test'),
                     BASE.replace('wisdom-mooc.', 'evil.'),
                     BASE.replace('https://', 'ftp://'),
                     BASE.replace('.com/', '.com:8080/'),
                     BASE.replace('/study/index', '/login'),
                     'https://passport.zhihuishu.com/login?ticket=private-token',
                     'https://studentexam-api.zhihuishu.com/studentExam/gateway/f/v1/gologin/private-token'):
            with self.subTest(text=text), self.assertRaises(CourseUrlError) as caught:
                normalize_course_urls(text)
            self.assertNotIn('private-token', str(caught.exception))

    def test_invalid_line_is_not_silently_skipped_and_empty_config_is_allowed(self):
        with self.assertRaisesRegex(CourseUrlError, '第 2 行'):
            normalize_course_urls(BASE + '\n不是网址')
        self.assertEqual(normalize_course_urls(['', '   '], required=False), [])

    def test_parser_is_idempotent_and_repeated_id_is_unambiguous(self):
        self.assertEqual(normalize_course_urls(BASE + '&recruitAndCourseId=Course_42'), [BASE])
        result = normalize_course_urls('[课程](' + BASE + ')')
        self.assertEqual(normalize_course_urls(result), result)


if __name__ == '__main__':
    unittest.main()
