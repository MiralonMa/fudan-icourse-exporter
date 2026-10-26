import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import extract_ppt as ppt
import extract_subtitles as browser


# Generated locally from a 16x8 two-color synthetic image, not course material.
JPEG = base64.b64decode('/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/2wBDAQMEBAUEBQkFBQkUDQsNFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBT/wAARCAAIABADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD50r93qKK/XeP/APmF/wC3/wD2w+K4b/5ff9u/qf/Z')


def sample(origin='https://icourse.fudan.edu.cn'):
    prefix = origin + '/play/1/ai3/ppt/synthetic/'
    return {'title': '合成 PPT 测试', 'url': origin + '/livingroom?course_id=0', 'total': 2,
            'rows': [{'order': i, 'time': f'00:00:0{i-1}', 'thumbnail': prefix + f's_{i}000.jpg'} for i in (1, 2)],
            'originalResources': [prefix + '1000.jpg']}


class PPTTests(unittest.TestCase):
    def test_original_mapping_requires_viewer_evidence(self):
        data = sample()
        rows = ppt.prepare_slides(data)
        self.assertTrue(rows[0]['original'].endswith('/1000.jpg'))
        self.assertNotIn('/s_', rows[0]['original'])
        data['originalResources'] = []
        with self.assertRaises(browser.ExportError): ppt.prepare_slides(data)

    def test_missing_order_and_total_rejected(self):
        for case in ('total', 'order', 'timestamp', 'different-origin'):
            data = sample()
            if case == 'total': data['total'] = 3
            if case == 'order': data['rows'][1]['order'] = 3
            if case == 'timestamp': data['rows'][1]['time'] = ''
            if case == 'different-origin': data['rows'][1]['thumbnail'] = 'https://example.invalid/ppt/test/s_2000.jpg'
            with self.subTest(case=case), self.assertRaises(browser.ExportError):
                ppt.prepare_slides(data)

    def test_jpeg_validation(self):
        self.assertEqual(ppt.jpeg_info(JPEG), (16, 8, 3))
        for invalid in (b'<html>Login required</html>', JPEG[:-2], b'\xff\xd8\xff\xc0\x00\x40\xff\xd9'):
            with self.subTest(data=invalid[:8]), self.assertRaises(browser.ExportError):
                ppt.jpeg_info(invalid)

    def test_pdf_keeps_original_jpeg_bytes_and_page_count(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for i in (1, 2): (folder / f'{i:04d}.jpg').write_bytes(JPEG)
            target = ppt.write_pdf(folder/'test.pdf', '合成测试', sample()['rows'], folder, 2)
            content = target.read_bytes()
            self.assertTrue(content.startswith(b'%PDF-1.4'))
            self.assertEqual(content.count(JPEG), 2)
            self.assertIn(b'/Type /Pages /Count 2', content)
            self.assertIn(b'Source PPT 002/2 | 00:00:01', content)
            self.assertTrue(content.endswith(b'%%EOF\n'))

    def test_incomplete_download_does_not_create_full_pdf_and_can_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(ppt, 'fetch_jpeg', side_effect=[JPEG, browser.ExportError('synthetic network failure')]):
                with self.assertRaises(browser.ExportError):
                    ppt.export_ppt(None, sample(), directory, min_width=1, min_height=1, retries=0, filter_mode='off')
            self.assertEqual(len(list(Path(directory).rglob('*.pdf'))), 0)
            manifest = next(Path(directory).rglob('PPT清单.json'))
            self.assertEqual(json.loads(manifest.read_text(encoding='utf-8'))['missing_orders'], [2])
            with patch.object(ppt, 'fetch_jpeg', return_value=JPEG) as fetch:
                target, result = ppt.export_ppt(None, sample(), directory, min_width=1, min_height=1, retries=0, filter_mode='off')
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(result['missing_orders'], [])
                self.assertTrue(target.is_file())
            # A broken early cache must not discard later verified checkpoints.
            (manifest.parent/'images/0001.jpg').write_bytes(b'broken')
            with patch.object(ppt, 'fetch_jpeg', side_effect=browser.ExportError('synthetic offline')):
                with self.assertRaises(browser.ExportError):
                    ppt.export_ppt(None, sample(), directory, min_width=1, min_height=1, retries=0, filter_mode='off')
            checkpoint = json.loads(manifest.read_text(encoding='utf-8'))
            self.assertEqual([r['order'] for r in checkpoint['completed']], [2])
            self.assertEqual(checkpoint['missing_orders'], [1])

    def test_partial_is_explicit_and_labelled(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(ppt, 'fetch_jpeg', side_effect=[JPEG, browser.ExportError('synthetic failure')]):
                target, result = ppt.export_ppt(None, sample(), directory, min_width=1, min_height=1, retries=0, allow_partial=True, filter_mode='off')
            self.assertIn('部分1张_共2张', target.name)
            self.assertEqual(result['missing_orders'], [2])
            self.assertEqual(target.read_bytes().count(JPEG), 1)

    def test_shared_folder_rejects_changed_course_or_picture_list(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)/'synthetic-course'
            with patch.object(ppt, 'fetch_jpeg', return_value=JPEG):
                target, _ = ppt.export_ppt(None, sample(), directory, course_folder=folder,
                    min_width=1, min_height=1, retries=0, filter_mode='off')
            original_pdf = target.read_bytes()
            original_manifest = (folder/'PPT清单.json').read_bytes()
            for case in ('url', 'slides'):
                changed = sample()
                if case == 'url': changed['url'] += '&sub_id=1'
                else: changed['rows'][1]['time'] = '00:00:02'
                with self.subTest(case=case), patch.object(ppt, 'fetch_jpeg') as fetch:
                    with self.assertRaisesRegex(browser.ExportError, '不同课程或不同图片清单'):
                        ppt.export_ppt(None, changed, directory, course_folder=folder,
                            min_width=1, min_height=1, filter_mode='off')
                    fetch.assert_not_called()
                self.assertEqual(target.read_bytes(), original_pdf)
                self.assertEqual((folder/'PPT清单.json').read_bytes(), original_manifest)

    @unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS') == '1', 'opt-in local browser integration')
    def test_real_browser_fetch_and_original_size_gate(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.endswith('.jpg'):
                    payload, content_type = JPEG, 'image/jpeg'
                else:
                    data = sample('http://' + self.headers['Host'])
                    payload = ('<!doctype html><meta charset="utf-8"><title>合成 PPT 测试</title>'
                               '<img src="' + data['originalResources'][0] + '">'
                               '<div class="pptSlider" aria-valuemax="2"></div>' + ''.join(
                        '<div class="note-ppt_item"><div class="ppt_wrap"><div class="ppt_order">' +
                        str(row['order']) + '</div><div class="ppt_time">' + row['time'] +
                        '</div><img data-src="' + row['thumbnail'] + '"></div></div>'
                        for row in data['rows'])).encode('utf-8')
                    content_type = 'text/html; charset=utf-8'
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
        try:
            with tempfile.TemporaryDirectory(prefix='ppt-integration-') as directory:
                profile = Path(directory)/'profile'
                profile.mkdir()
                url = f'http://127.0.0.1:{server.server_port}/livingroom'
                process, endpoint, browser_ws = browser.launch_browser(browser.find_browser(), profile, url, headless=True)
                try:
                    deadline = time.monotonic() + 20
                    with browser.CDP(browser.list_pages(endpoint)[0]['webSocketDebuggerUrl']) as cdp:
                        while True:
                            data = ppt.evaluate(cdp, ppt.SLIDES_JS)
                            if len(data.get('rows', [])) == 2 and data.get('originalResources'): break
                            if time.monotonic() > deadline: self.fail('Synthetic page did not load')
                            time.sleep(.1)
                        rows = ppt.prepare_slides(data)
                        with self.assertRaises(browser.ExportError):
                            ppt.fetch_jpeg(cdp, rows[0]['original'])  # default gate rejects thumbnails
                        target, report = ppt.export_ppt(cdp, data, Path(directory)/'exports', min_width=1, min_height=1, retries=0, filter_mode='off')
                    self.assertEqual(len(report['completed']), 2)
                    self.assertEqual(target.read_bytes().count(JPEG), 2)
                    for image in target.parent.glob('images/*.jpg'):
                        self.assertEqual(image.read_bytes(), JPEG)
                finally:
                    try:
                        with browser.CDP(browser_ws) as cdp: cdp.call('Browser.close')
                    except (OSError, browser.ExportError): pass
                    process.wait(timeout=15)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
