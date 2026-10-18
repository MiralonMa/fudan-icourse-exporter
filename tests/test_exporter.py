import html
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

import extract_subtitles as app


class ExportTests(unittest.TestCase):
    def sample(self):
        return {'title': '合成示例课程',
                'url': 'https://icourse.fudan.edu.cn/livingroom?course_id=0&sub_id=0&tenant_code=0',
                'domCount': 2,
                'rows': [{'time': '00:00:01', 'text': '中文与 English 示例。'},
                         {'time': '00:00:02', 'text': '重复重复示例。'}]}

    def test_roundtrip_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            first = app.save_subtitles(self.sample(), directory, True)
            second = app.save_subtitles(self.sample(), directory)
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes()[:3], b'\xef\xbb\xbf')
            self.assertIn('[00:00:01] 中文与 English 示例。', first.read_text(encoding='utf-8-sig'))
            self.assertEqual(json.loads(first.with_suffix('.json').read_text(encoding='utf-8')), self.sample())

    def test_invalid_rows_do_not_create_files(self):
        for case in ('empty', 'count', 'blank', 'time', 'order'):
            data = self.sample()
            if case == 'empty': data['rows'] = []
            if case == 'count': data['domCount'] = 3
            if case == 'blank': data['rows'][0]['text'] = ' '
            if case == 'time': data['rows'][0]['time'] = ''
            if case == 'order': data['rows'].reverse()
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(app.ExportError):
                    app.save_subtitles(data, directory)
                self.assertFalse(list(Path(directory).glob('*.txt')))

    def test_safe_names(self):
        self.assertEqual(app.safe_name('CON'), '_CON')
        self.assertEqual(app.safe_name('a/b:c?'), 'a_b_c_')
        self.assertEqual(app.safe_name('...'), '课程录播')

    def test_url_format_without_host_restriction(self):
        for url in (self.sample()['url'], 'https://example.invalid/',
                    'http://localhost:8080/course', 'https://icourse.fudan.edu.cn:9222/'):
            app.validate_page_url(url)
        for url in ('https://' + 'synthetic-user:synthetic-password' + '@icourse.fudan.edu.cn/',
                    'file:///course.html', 'javascript:void(0)', 'https:///no-host'):
            with self.subTest(url=url), self.assertRaises(app.ExportError):
                app.validate_page_url(url)

    def test_loopback_debug_only(self):
        with self.assertRaises(app.ExportError):
            app.CDP('ws://example.invalid/devtools/page/test')

    def test_url_secrets_not_saved(self):
        data = self.sample()
        data['url'] += '&token=SYNTHETIC_SECRET&code=SYNTHETIC_CODE#SYNTHETIC_FRAGMENT'
        with tempfile.TemporaryDirectory() as directory:
            target = app.save_subtitles(data, directory, True)
            for path in (target, target.with_suffix('.json')):
                saved = path.read_text(encoding='utf-8-sig')
                self.assertNotIn('SYNTHETIC_SECRET', saved)
                self.assertNotIn('SYNTHETIC_CODE', saved)
                self.assertNotIn('SYNTHETIC_FRAGMENT', saved)
                self.assertIn('course_id=0', saved)

    @unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS') == '1', 'opt-in local browser integration')
    def test_synthetic_2903_rows_in_browser(self):
        rows = []
        for i in range(2903):
            h, remainder = divmod(i, 3600)
            m, s = divmod(remainder, 60)
            rows.append({'time': f'{h:02d}:{m:02d}:{s:02d}',
                         'text': f'合成测试第 {i + 1} 条：中文、English、符号 < > &。'})
        with tempfile.TemporaryDirectory(prefix='subtitle-test-') as directory:
            folder = Path(directory)
            fixture = folder / 'synthetic.html'
            markup = '<!doctype html><meta charset="utf-8"><title>合成示例课程</title>'
            for row in rows:
                markup += ('<p class="item-time">' + row['time'] + '</p>'
                           '<div class="trans-item"><div class="item-origin"><span>' +
                           html.escape(row['text']) + '</span></div>'
                           '<div class="translation">DO NOT EXPORT</div></div>')
            fixture.write_text(markup, encoding='utf-8')
            profile = folder / 'profile'
            profile.mkdir()
            process, endpoint, browser_ws = app.launch_browser(app.find_browser(), profile, fixture.as_uri(), headless=True)
            try:
                deadline = time.monotonic() + 20
                while True:
                    pages = app.list_pages(endpoint)
                    target = next((p for p in pages if p.get('url') == fixture.as_uri()), None)
                    if target:
                        result = app.extract_page(target)
                        if len(result['rows']) == len(rows): break
                    if time.monotonic() > deadline:
                        self.fail('Synthetic page did not load')
                    time.sleep(.1)
                self.assertEqual(result['domCount'], 2903)
                self.assertEqual([{'time': r['time'], 'text': r['text']} for r in result['rows']], rows)
                # All fixture URLs and saved test files remain in this temporary folder.
                output = app.save_subtitles(result, folder / 'exports')
                saved = output.read_text(encoding='utf-8-sig')
                self.assertNotIn('DO NOT EXPORT', saved)
                self.assertEqual(saved.splitlines()[5:], [f"[{r['time']}] {r['text']}" for r in rows])
            finally:
                try:
                    with app.CDP(browser_ws) as cdp:
                        cdp.call('Browser.close')
                except (app.ExportError, OSError):
                    pass
                process.wait(timeout=15)


if __name__ == '__main__':
    unittest.main()
