import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import extract_course as course
import extract_ppt as ppt
import ppt_filter as filtering
from extract_subtitles import ExportError
from test_ppt import JPEG


def rows_at(times):
    return [{'order': i+1, 'time': f'{stamp//3600:02d}:{stamp//60%60:02d}:{stamp%60:02d}'}
            for i, stamp in enumerate(times)]


def dense_sample():
    return rows_at([0, 5, 10, 15, 20, 300, 600, 900, 1200, 1500,
                    1800, 1805, 1810, 1815, 1820])


def features(row):
    return bytes([100] * 48) if row['order'] <= 5 or row['order'] >= 11 else bytes([220] * 48)


class FilterTests(unittest.TestCase):
    def test_dense_matching_edges_removed_and_lecture_retained(self):
        selected, report = filtering.filter_plan(dense_sample(), features, default_reference_orders=[1])
        self.assertEqual([row['order'] for row in selected], list(range(6, 11)))
        self.assertEqual(len(report['excluded_orders']), 10)

    def test_normal_export_writes_only_the_filtered_pdf(self):
        rows = dense_sample()
        prefix = 'https://icourse.fudan.edu.cn/play/1/ai3/ppt/synthetic/'
        data = {'title': 'Synthetic filtered export',
                'url': 'https://icourse.fudan.edu.cn/livingroom?course_id=0',
                'total': len(rows),
                'rows': [dict(row, thumbnail=prefix+f"s_{row['order']}000.jpg") for row in rows],
                'originalResources': [prefix+'1000.jpg']}
        def plan(rows, image_dir, **kwargs):
            self.assertEqual(kwargs['mode'], 'auto')
            return filtering.filter_plan(rows, features, default_reference_orders=[1])
        original_writer = ppt.write_pdf
        with tempfile.TemporaryDirectory() as directory, patch('sys.stdout', new=io.StringIO()):
            with patch.object(ppt, 'fetch_jpeg', return_value=JPEG), patch.object(filtering, 'filter_local_rows', side_effect=plan), patch.object(ppt, 'write_pdf', wraps=original_writer) as writer:
                path, report = ppt.export_ppt(None, data, directory, min_width=1, min_height=1, retries=0)
            self.assertEqual(writer.call_count, 1)
            self.assertEqual(report['pdf_included_orders'], list(range(6, 11)))
            self.assertEqual(len(report['completed']), 15)
            self.assertEqual(path.read_bytes().count(JPEG), 5)
            self.assertEqual(len(list(Path(directory).rglob('*.pdf'))), 1)
            self.assertEqual(len(list(Path(directory).rglob('*.jpg'))), 15)

    def test_confirmed_default_page_is_also_removed_in_middle(self):
        def mixed(row):
            return bytes([100]*48) if row['order'] == 8 else features(row)
        selected, _ = filtering.filter_plan(dense_sample(), mixed, default_reference_orders=[1])
        self.assertNotIn(8, [row['order'] for row in selected])
        self.assertEqual([row['order'] for row in selected], [6, 7, 9, 10])

    def test_speech_on_background_is_evidence_not_a_global_time_cut(self):
        timeline = [{'time': row['time'], 'chars': 20} for row in dense_sample()]
        selected, report = filtering.filter_plan(dense_sample(), features, subtitle_rows=timeline,
                                                  default_reference_orders=[1])
        self.assertEqual(len(selected), 5)
        self.assertEqual(report['subtitle_timeline']['count'], 15)
        self.assertEqual(report['pages'][-1]['subtitle_count'], 1)
        self.assertFalse(report['pages'][-1]['keep'])

    def test_no_visual_change_in_recording_is_not_enough(self):
        selected, _ = filtering.filter_plan(dense_sample(), lambda _: bytes([100]*48))
        self.assertEqual(len(selected), 15)

    def test_regular_long_held_teaching_slides_not_removed(self):
        rows = rows_at(list(range(0, 3600, 240)))
        selected, report = filtering.filter_plan(rows, features)
        self.assertEqual(len(selected), len(rows))
        self.assertEqual(report['excluded_orders'], [])

    def test_different_start_and_end_keep_all(self):
        selected, _ = filtering.filter_plan(dense_sample(), lambda row: bytes([row['order']*10]*48))
        self.assertEqual(len(selected), 15)

    def test_repetition_without_semantic_confirmation_keeps_all(self):
        selected, report = filtering.filter_plan(dense_sample(), features)
        self.assertEqual(len(selected), 15)
        self.assertFalse(report['excluded_orders'])

    def test_default_majority_does_not_disable_filtering(self):
        rows = rows_at(list(range(0, 300, 5)))
        def majority(row):
            return bytes([100]*48) if row['order'] != 30 else bytes([220]*48)
        selected, report = filtering.filter_plan(rows, majority, default_reference_orders=[1])
        self.assertEqual([row['order'] for row in selected], [30])
        self.assertEqual(report['automatic_excluded_count'], 59)

    def test_only_default_pages_does_not_create_empty_pdf(self):
        with self.assertRaisesRegex(ExportError, '没有课程页面'):
            filtering.filter_plan(rows_at([0, 5]), lambda _: bytes([100]*48),
                                  default_reference_orders=[1])

    def test_text_confirmation_requires_multiple_recording_guide_markers(self):
        positive = '本 科 课 程 评 教 提 醒\n录 制 操 作 演 示\n教 室 分 布\n视频获取及注意事项'
        self.assertTrue(filtering.is_default_page_text(positive))
        self.assertTrue(filtering.is_default_page_text('cfd.fudan.edu 录制操作演示 视频获取'))
        for text in ('', '录制操作演示', '课程讲义 教室分布 录制操作演示',
                     '智慧教学资源平台', 'icourse.fudan.edu 课程知识点'):
            with self.subTest(text=text):
                self.assertFalse(filtering.is_default_page_text(text))

    def test_group_ocr_is_required_and_includes_singletons(self):
        def read(representatives):
            self.assertEqual([row['order'] for row in representatives], [1, 6])
            return ['录制操作演示 视频获取 本科课程评教提醒', '普通课程封面']
        references, detection = filtering.default_page_references(dense_sample(), features, read)
        self.assertEqual(references, [1])
        self.assertEqual(detection['visual_groups'], 2)
        self.assertEqual(detection['status'], 'complete')
        rows = rows_at([0, 5])
        refs, result = filtering.default_page_references(rows,
            lambda row: bytes([row['order']*100]*48), lambda _: [None, '普通课件'])
        self.assertFalse(refs)
        self.assertEqual(result['ocr_failed_groups'], 1)

    def test_unavailable_local_ocr_keeps_unconfirmed_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            for row in dense_sample():
                (Path(directory)/f"{row['order']:04d}.jpg").write_bytes(JPEG)
            with patch.object(filtering, 'visual_descriptor', return_value=bytes([100]*48)), patch.object(filtering, 'read_image_text', side_effect=OSError('Unavailable')), patch('sys.stdout', new=io.StringIO()):
                selected, report = filtering.filter_local_rows(dense_sample(), Path(directory), connection=object())
            self.assertEqual(len(selected), 15)
            self.assertEqual(report['default_page_detection']['status'], 'unavailable')

    def test_image_read_failure_is_not_disguised_as_missing_ocr(self):
        with patch.object(filtering, 'visual_descriptor', side_effect=OSError('Image failure')):
            with self.assertRaises(OSError):
                filtering.filter_local_rows(rows_at([0]), Path('missing-images'), connection=object())

    def test_off_mode_never_reads_pixels(self):
        def forbidden(_):
            raise AssertionError('pixel analysis should not run')
        selected, _ = filtering.filter_plan(dense_sample(), forbidden, mode='off')
        self.assertEqual(len(selected), 15)

    def test_manual_window_keeps_page_displayed_across_start(self):
        rows = rows_at([0, 200, 500, 1000])
        selected, report = filtering.filter_plan(rows, None, mode='off', start='00:04:10', end='00:12:30')
        self.assertEqual([row['order'] for row in selected], [2, 3])
        self.assertEqual(report['pages'][1]['display_until'], '00:08:20')

    def test_invalid_or_empty_window_rejected(self):
        for start, end in [('00:60:00', None), ('00:05:00', '00:04:00'), (None, '00:00:00')]:
            with self.subTest(start=start, end=end), self.assertRaises(ExportError):
                filtering.filter_plan(rows_at([0, 200, 500]), None, mode='off', start=start, end=end)

    def test_subtitle_txt_and_json_time_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            txt = Path(directory)/'synthetic.txt'
            txt.write_text('示例标题\n[00:00:01] 合成字幕\n[00:00:03] Second example\n', encoding='utf-8-sig')
            self.assertEqual([stamp for stamp, _ in filtering.normalize_subtitles(filtering.load_subtitle_timeline(txt))], [1, 3])
            structured = txt.with_suffix('.json')
            structured.write_text(json.dumps({'rows': [{'time': '00:00:04', 'text': 'Synthetic'}]}), encoding='utf-8')
            self.assertEqual(filtering.normalize_subtitles(filtering.load_subtitle_timeline(structured))[0][0], 4)

    def test_existing_download_is_rebuilt_without_touching_original(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory); images = folder/'images'; images.mkdir()
            rows = rows_at([0, 200, 500])
            completed = []
            for row in rows:
                path = images/f"{row['order']:04d}.jpg"; path.write_bytes(JPEG)
                completed.append(dict(row, sha256=hashlib.sha256(JPEG).hexdigest()))
            original = folder/'original.pdf'; original.write_bytes(b'Original must stay')
            manifest = folder/'PPT清单.json'
            payload = {'title': 'Synthetic', 'source_count': 3, 'slides': rows,
                       'completed': completed, 'pdf_file': original.name}
            manifest.write_text(json.dumps(payload), encoding='utf-8')
            before = manifest.read_bytes()
            target, report = filtering.rebuild_existing(folder, mode='off', start='00:04:10')
            self.assertEqual(report['included_orders'], [2, 3])
            self.assertEqual(target.read_bytes().count(JPEG), 2)
            self.assertEqual(manifest.read_bytes(), before)
            self.assertEqual(original.read_bytes(), b'Original must stay')
            (images/'0003.jpg').write_bytes(b'Broken')
            with self.assertRaises(ExportError): filtering.rebuild_existing(folder, mode='off')

    def test_filter_existing_cli_bypasses_login_and_menu(self):
        report = {'included_orders': [2], 'excluded_orders': [1]}
        with patch.object(course.filtering, 'rebuild_existing', return_value=(Path('synthetic.pdf'), report)) as rebuild, patch.object(course, 'browser_session') as browser, patch('builtins.input') as prompt, patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(course.main(['--filter-existing', '.', '--ppt-filter', 'off']), 0)
            rebuild.assert_called_once()
            browser.assert_not_called()
            prompt.assert_not_called()

    @unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS') == '1', 'opt-in local browser integration')
    def test_local_browser_decodes_jpeg_into_reproducible_descriptor(self):
        with filtering.local_image_browser() as connection:
            first = filtering.visual_descriptor(connection, JPEG)
            second = filtering.visual_descriptor(connection, JPEG)
        self.assertEqual(len(first), 48*27*3)
        self.assertEqual(filtering.distance(first, second), 0)

    @unittest.skipUnless(os.name == 'nt' and os.environ.get('RUN_BROWSER_TESTS') == '1',
                         'opt-in Windows local OCR integration')
    def test_windows_ocr_reads_browser_generated_local_image(self):
        # Large synthetic text image, created offline without image packages.
        with tempfile.TemporaryDirectory() as directory:
            with filtering.local_image_browser() as connection:
                expression = r'''(async () => {
                  const canvas = new OffscreenCanvas(1280, 720);
                  const ctx = canvas.getContext('2d');
                  ctx.fillStyle = 'white'; ctx.fillRect(0, 0, 1280, 720);
                  ctx.fillStyle = 'black'; ctx.font = '40px Arial';
                  ctx.fillText('Synthetic local OCR integration test', 40, 100);
                  const blob = await canvas.convertToBlob({type:'image/jpeg'});
                  const bytes = new Uint8Array(await blob.arrayBuffer());
                  let binary = ''; for (const byte of bytes) binary += String.fromCharCode(byte);
                  return btoa(binary);
                })()'''
                result = connection.call('Runtime.evaluate', {'expression': expression,
                    'awaitPromise': True, 'returnByValue': True})
            import base64
            image = Path(directory)/'synthetic.jpg'
            image.write_bytes(base64.b64decode(result['result']['value']))
            texts = filtering.read_image_text([image])
            self.assertEqual(len(texts), 1)
            self.assertIsInstance(texts[0], str)
            self.assertIn('synthetic', texts[0].replace(' ', '').casefold())


if __name__ == '__main__':
    unittest.main()
