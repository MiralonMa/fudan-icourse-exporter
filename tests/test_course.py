from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import extract_course as course
from test_ppt import JPEG, sample as ppt_sample


URL = 'https://icourse.fudan.edu.cn/livingroom?course_id=0&sub_id=0&tenant_code=0'
TARGET = {'url': URL, 'webSocketDebuggerUrl': 'ws://127.0.0.1/devtools/page/synthetic'}


def subtitle_sample():
    return {'title': '合成双选测试', 'url': URL, 'domCount': 2,
            'rows': [{'time': '00:00:01', 'text': '合成示例一。'},
                     {'time': '00:00:02', 'text': '合成示例二。'}]}


def options(folder):
    return course.build_parser().parse_args(['--output-dir', str(folder), '--retries', '0'])


class CourseTests(unittest.TestCase):
    def test_all_menu_choices_and_explicit_arguments(self):
        for answer, expected in (('1', ('subtitles',)), ('2', ('ppt',)), ('3', ('subtitles', 'ppt')), ('q', ())):
            with self.subTest(answer=answer), patch('builtins.input', return_value=answer), patch('sys.stdout', new=io.StringIO()):
                self.assertEqual(course.choose_mode(), expected)
        with patch('builtins.input') as prompt:
            self.assertEqual(course.choose_mode('both'), ('subtitles', 'ppt'))
            prompt.assert_not_called()

    def test_invalid_menu_does_not_silently_choose(self):
        with patch('builtins.input', side_effect=['', 'x', '3']) as prompt, patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(course.choose_mode(), ('subtitles', 'ppt'))
            self.assertEqual(prompt.call_count, 3)

    def test_subtitles_only_does_not_request_ppt(self):
        with tempfile.TemporaryDirectory() as directory, patch('sys.stdout', new=io.StringIO()):
            with patch.object(course.subtitles, 'extract_page', return_value=subtitle_sample()), patch.object(course.ppt, 'export_ppt') as ppt:
                succeeded, errors = course.run_pending(TARGET, ('subtitles',), options(Path(directory)))
            ppt.assert_not_called()
            self.assertFalse(errors)
            self.assertEqual(set(succeeded), {'subtitles'})
            self.assertTrue(succeeded['subtitles'].parent.name.startswith('合成双选测试_'))
            self.assertEqual(succeeded['subtitles'].parent.parent, Path(directory))
            self.assertIn('合成示例二。', succeeded['subtitles'].read_text(encoding='utf-8-sig'))

    def test_ppt_only_does_not_read_subtitles(self):
        with tempfile.TemporaryDirectory() as directory, patch('sys.stdout', new=io.StringIO()):
            with patch.object(course.subtitles, 'CDP'), patch.object(course.ppt, 'evaluate', return_value={'url': URL}), patch.object(course.ppt, 'export_ppt', return_value=(Path(directory)/'synthetic.pdf', {'missing_orders': [], 'source_count': 2})), patch.object(course.subtitles, 'extract_page') as read_subtitles:
                succeeded, errors = course.run_pending(TARGET, ('ppt',), options(Path(directory)))
            read_subtitles.assert_not_called()
            self.assertFalse(errors)
            self.assertEqual(set(succeeded), {'ppt'})

    def test_course_folder_is_shared_across_separate_choices_and_title_change(self):
        with tempfile.TemporaryDirectory() as directory:
            first = course.course_folder(options(Path(directory)), subtitle_sample())
            first.mkdir()
            other_title = dict(subtitle_sample(), title='临时页面标题')
            second = course.course_folder(options(Path(directory)), other_title)
            self.assertEqual(first, second)
            another = dict(subtitle_sample(), url=URL.replace('sub_id=0', 'sub_id=1'))
            self.assertNotEqual(first, course.course_folder(options(Path(directory)), another))

    def test_both_exports_are_passed_the_same_directory(self):
        with tempfile.TemporaryDirectory() as directory, patch('sys.stdout', new=io.StringIO()):
            config = options(Path(directory))
            with patch.object(course.subtitles, 'extract_page', return_value=subtitle_sample()), patch.object(course.subtitles, 'CDP'), patch.object(course.ppt, 'evaluate', return_value=subtitle_sample()), patch.object(course.ppt, 'export_ppt', return_value=(Path(directory)/'synthetic.pdf', {'missing_orders': [], 'source_count': 2})) as export:
                succeeded, errors = course.run_pending(TARGET, ('subtitles', 'ppt'), config)
            self.assertFalse(errors)
            self.assertEqual(succeeded['subtitles'].parent, export.call_args.kwargs['course_folder'])

    def test_retry_only_failed_export_with_one_browser_session(self):
        sessions = []

        @contextmanager
        def fake_session(*_):
            sessions.append(True)
            yield 'synthetic-local-endpoint'

        with tempfile.TemporaryDirectory() as directory, patch('sys.stdout', new=io.StringIO()):
            with patch.object(course, 'browser_session', fake_session), patch.object(course.subtitles, 'list_pages', return_value=[TARGET]), patch.object(course.subtitles, 'extract_page', return_value=subtitle_sample()) as read, patch.object(course.subtitles, 'CDP'), patch.object(course.ppt, 'evaluate', return_value={'url': URL}), patch.object(course.ppt, 'export_ppt', side_effect=[course.subtitles.ExportError('synthetic offline'), (Path(directory)/'synthetic.pdf', {'missing_orders': [], 'source_count': 2})]) as export, patch('builtins.input', side_effect=['', '']):
                status = course.main(['--extract', 'both', '--url', URL, '--output-dir', directory])
            self.assertEqual(status, 0)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(export.call_count, 2)
            self.assertEqual(len(list(Path(directory).rglob('*.txt'))), 1)

    def test_course_change_during_retry_is_rejected(self):
        other = dict(TARGET, url=URL.replace('course_id=0', 'course_id=1'))

        @contextmanager
        def fake_session(*_):
            yield 'synthetic-local-endpoint'

        with tempfile.TemporaryDirectory() as directory, patch('sys.stdout', new=io.StringIO()):
            with patch.object(course, 'browser_session', fake_session), patch.object(course.subtitles, 'list_pages', side_effect=[[TARGET], [other], [TARGET]]), patch.object(course.subtitles, 'extract_page', return_value=subtitle_sample()) as read, patch.object(course.subtitles, 'CDP'), patch.object(course.ppt, 'evaluate', return_value={'url': URL}), patch.object(course.ppt, 'export_ppt', side_effect=[course.subtitles.ExportError('synthetic offline'), (Path(directory)/'synthetic.pdf', {'missing_orders': [], 'source_count': 2})]) as export, patch('builtins.input', side_effect=['', '', '']):
                status = course.main(['--extract', 'both', '--url', URL, '--output-dir', directory])
            self.assertEqual(status, 0)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(export.call_count, 2)

    def test_cancel_before_launch_does_not_open_browser(self):
        with patch('builtins.input', return_value='q'), patch.object(course, 'browser_session') as browser, patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(course.main([]), 0)
            browser.assert_not_called()

    @unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS') == '1', 'opt-in local browser integration')
    def test_both_exports_end_to_end_in_one_browser(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.endswith('.jpg'):
                    payload, content_type = JPEG, 'image/jpeg'
                else:
                    data = ppt_sample('http://' + self.headers['Host'])
                    markup = ('<!doctype html><meta charset="utf-8"><title>合成双选测试</title>'
                              '<img src="' + data['originalResources'][0] + '">'
                              '<div class="pptSlider" aria-valuemax="2"></div>')
                    for row in data['rows']:
                        markup += ('<div class="note-ppt_item"><div class="ppt_wrap"><div class="ppt_order">' +
                            str(row['order']) + '</div><div class="ppt_time">' + row['time'] +
                            '</div><img data-src="' + row['thumbnail'] + '"></div></div>')
                    markup += '<div class="trans-list_wrap">'
                    for row in subtitle_sample()['rows']:
                        markup += ('<p class="item-time">' + row['time'] + '</p><div class="trans-item">'
                            '<div class="item-origin"><span>' + row['text'] + '</span></div></div>')
                    payload, content_type = (markup + '</div>').encode('utf-8'), 'text/html; charset=utf-8'
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        launches = []
        original_launch = course.subtitles.launch_browser

        def headless_launch(browser, profile, url):
            launches.append(url)
            process, endpoint, browser_ws = original_launch(browser, profile, url, headless=True)
            deadline = time.monotonic() + 20
            while True:
                pages = course.subtitles.list_pages(endpoint)
                target = next((page for page in pages if page.get('url') == url), None)
                if target:
                    with course.subtitles.CDP(target['webSocketDebuggerUrl']) as cdp:
                        slides = course.ppt.evaluate(cdp, course.ppt.SLIDES_JS)
                    data = course.subtitles.extract_page(target)
                    if len(data['rows']) == 2 and slides.get('originalResources'):
                        return process, endpoint, browser_ws
                if time.monotonic() > deadline:
                    with course.subtitles.CDP(browser_ws) as cdp:
                        try: cdp.call('Browser.close')
                        except (OSError, course.subtitles.ExportError): pass
                    process.wait(timeout=15)
                    self.fail('Combined synthetic page did not load')
                time.sleep(.1)

        try:
            with tempfile.TemporaryDirectory(prefix='course-both-test-') as directory:
                url = f'http://127.0.0.1:{server.server_port}/livingroom?course_id=0'
                with patch.object(course.subtitles, 'launch_browser', headless_launch), patch('builtins.input', return_value=''), patch('sys.stdout', new=io.StringIO()):
                    status = course.main(['--extract', 'both', '--url', url, '--output-dir', directory,
                        '--min-width', '1', '--min-height', '1', '--retries', '0', '--json'])
                self.assertEqual(status, 0)
                self.assertEqual(len(launches), 1)
                text_files = list(Path(directory).rglob('*.txt'))
                self.assertEqual(len(text_files), 1)
                self.assertIn('[00:00:02] 合成示例二。', text_files[0].read_text(encoding='utf-8-sig'))
                self.assertTrue(text_files[0].with_suffix('.json').is_file())
                pdf_files = list(Path(directory).rglob('*.pdf'))
                self.assertEqual(len(pdf_files), 1)
                self.assertEqual(pdf_files[0].parent, text_files[0].parent)
                self.assertEqual(pdf_files[0].read_bytes().count(JPEG), 2)
                report = json.loads((pdf_files[0].parent/'PPT清单.json').read_text(encoding='utf-8'))
                self.assertEqual(report['missing_orders'], [])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
