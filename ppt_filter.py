"""Local filtering of Fudan recording-guide pages anywhere in the timeline.

No network service or Python image package is required. A browser decodes
local JPEGs into small visual descriptors; Windows' installed OCR confirms
recording-guide templates before matching them throughout the course.
Subtitle timestamps are recorded
as corroborating evidence, never treated as a reliable lecture start/end alone.
"""
from __future__ import annotations

import base64
from bisect import bisect_left
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from extract_subtitles import CDP, ExportError, find_browser, launch_browser
from local_ocr import read_image_text


DEFAULT_DISTANCE = .035


def seconds(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,3}:\d{2}:\d{2}', value):
        raise ExportError('时间须为 HH:MM:SS。')
    h, m, s = map(int, value.split(':'))
    if m >= 60 or s >= 60:
        raise ExportError('时间中的分、秒须小于 60。')
    return h * 3600 + m * 60 + s


def validate_window(start=None, end=None):
    lower = seconds(start) if start else None
    upper = seconds(end) if end else None
    if lower is not None and upper is not None and lower >= upper:
        raise ExportError('开始时间必须早于结束时间。')
    return lower, upper


def normalize_subtitles(rows):
    normalized = []
    for row in rows or []:
        try:
            stamp = seconds(row.get('time', ''))
            count = row.get('chars', len(row.get('text', '').strip()))
            normalized.append((stamp, max(0, int(count))))
        except (ExportError, TypeError, ValueError):
            continue
    return sorted(normalized)


def load_subtitle_timeline(path):
    path = Path(path)
    text = path.read_text(encoding='utf-8-sig')
    if path.suffix.lower() == '.json':
        data = json.loads(text)
        rows = data if isinstance(data, list) else data.get('rows', data.get('subtitles', []))
    else:
        rows = [{'time': match.group(1), 'chars': len(match.group(2).strip())}
                for line in text.splitlines()
                if (match := re.match(r'^\[(\d{1,3}:\d{2}:\d{2})\]\s*(.*)$', line))]
    if not normalize_subtitles(rows):
        raise ExportError('字幕文件中没有有效时间戳；支持本工具导出的 TXT 或 JSON。')
    return rows


@contextmanager
def local_image_browser(browser_path=None):
    profile = Path(tempfile.mkdtemp(prefix='course-image-analysis-'))
    process = browser_ws = None
    stopped = True
    try:
        process, endpoint, browser_ws = launch_browser(find_browser(browser_path), profile,
            'about:blank', headless=True)
        with CDP(browser_ws) as connection:
            target = connection.call('Target.createTarget', {'url': 'about:blank'})
        # The target was created by this browser; use its loopback endpoint
        # directly, without racing the HTTP /json/list discovery endpoint.
        page_ws = endpoint.replace('http://', 'ws://', 1) + '/devtools/page/' + target['targetId']
        with CDP(page_ws) as connection:
            yield connection
    finally:
        if browser_ws:
            try:
                with CDP(browser_ws) as connection:
                    connection.call('Browser.close')
            except (ExportError, OSError, ValueError):
                pass
        if process:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                stopped = False
        resolved = profile.resolve()
        if stopped and resolved.parent == Path(tempfile.gettempdir()).resolve() and resolved.name.startswith('course-image-analysis-'):
            try:
                shutil.rmtree(resolved)
            except OSError:
                pass


def visual_descriptor(connection, image):
    encoded = base64.b64encode(image).decode('ascii')
    expression = r"""(async () => {
      const binary = atob(IMAGE_DATA);
      const bytes = Uint8Array.from(binary, c => c.charCodeAt(0));
      const bitmap = await createImageBitmap(new Blob([bytes], {type:'image/jpeg'}));
      const canvas = new OffscreenCanvas(48, 27);
      const context = canvas.getContext('2d');
      context.drawImage(bitmap, 0, 0, 48, 27);
      bitmap.close();
      const rgba = context.getImageData(0, 0, 48, 27).data;
      let result = '';
      for (let i=0; i<rgba.length; i+=4)
        result += String.fromCharCode(rgba[i], rgba[i+1], rgba[i+2]);
      return btoa(result);
    })()""".replace('IMAGE_DATA', json.dumps(encoded))
    response = connection.call('Runtime.evaluate', {'expression': expression,
        'returnByValue': True, 'awaitPromise': True})
    if 'exceptionDetails' in response:
        raise ExportError('本机图片分析失败，原图与完整 PDF 数据不受影响。')
    value = response.get('result', {}).get('value')
    if not isinstance(value, str):
        raise ExportError('图片分析未返回有效数据。')
    pixels = base64.b64decode(value, validate=True)
    if len(pixels) != 48 * 27 * 3:
        raise ExportError('图片特征尺寸异常。')
    return pixels


def distance(first, second):
    if len(first) != len(second) or not first:
        raise ExportError('图片特征无效。')
    return sum(abs(a-b) for a, b in zip(first, second)) / (255 * len(first))


def is_default_page_text(text):
    """Require several independent recording-guide markers, never one keyword."""
    compact = re.sub(r'\s+', '', text).casefold()
    recording = '录制' in compact and ('演示' in compact or '录屏' in compact)
    instructions = any(word in compact for word in ('视频获取', '录制效果', '教室分布'))
    identity = any(word in compact for word in (
        '本科课程评教', '智慧教学资源平台', '录播指南',
        'cfd.fudan.edu', 'icourse.fudan.edu', 'ce.fudan.edu'))
    return recording and instructions and identity


def default_page_references(rows, descriptor, read_text):
    """OCR representatives of visual groups, including singleton groups.

    Repetition/cadence alone does not establish that a page is uninformative.
    A normal slide, browser demonstration, or repeated cover must not become a
    removal template just because the camera captured it many times.
    """
    representatives = []
    for row in rows:
        pixels = descriptor(row)
        if not any(distance(pixels, descriptor(item)) <= DEFAULT_DISTANCE
                   for item in representatives):
            representatives.append(row)
    try:
        texts = read_text(representatives)
    except OSError:
        return [], {'status': 'unavailable', 'visual_groups': len(representatives),
                    'reference_orders': []}
    if len(texts) != len(representatives):
        raise OSError('本机文字识别结果数量异常。')
    references = [row['order'] for row, text in zip(representatives, texts)
                  if isinstance(text, str) and is_default_page_text(text)]
    failed = sum(text is None for text in texts)
    return references, {'status': 'partial' if failed else 'complete',
                        'visual_groups': len(representatives),
                        'ocr_failed_groups': failed,
                        'reference_orders': references,
                        'distance_threshold': DEFAULT_DISTANCE}


def filter_plan(rows, descriptor, *, subtitle_rows=None, mode='auto', start=None, end=None,
                default_reference_orders=(), detection=None):
    if mode not in ('auto', 'off'):
        raise ExportError('未知 PPT 筛选模式。')
    lower, upper = validate_window(start, end)
    stamps = [seconds(row['time']) for row in rows]
    if not rows or stamps != sorted(stamps):
        raise ExportError('PPT 时间轴为空或顺序异常。')
    timeline = normalize_subtitles(subtitle_rows)
    subtitle_times = [item[0] for item in timeline]
    excluded = {}
    notes = []
    automatic = []
    matched = {}
    if mode == 'auto':
        orders = set(default_reference_orders)
        references = [row for row in rows if row['order'] in orders]
        if not references:
            notes.append('未确认录播默认页面，保留所有未确认页面；不会仅凭重复或无字幕删页。')
        else:
            for index, row in enumerate(rows):
                candidates = [(distance(descriptor(row), descriptor(reference)), reference['order'])
                              for reference in references]
                score, reference = min(candidates)
                if score <= DEFAULT_DISTANCE:
                    automatic.append(index)
                    excluded[index] = '匹配本机文字识别确认的录播默认页面（含课间）'
                    matched[index] = {'reference_order': reference, 'distance': round(score, 6)}
        if detection and detection.get('status') in ('unavailable', 'partial'):
            notes.append('部分或全部本机文字识别不可用；无法确认的画面已保留。')
    if not timeline:
        notes.append('未取得字幕时间轴；筛选仅使用画面与截图时间。')
    evidence = []
    for index, row in enumerate(rows):
        stamp = stamps[index]
        finish = stamps[index+1] if index+1 < len(rows) else None
        # A slide begins at its timestamp and remains visible until the next.
        # Keep a slide overlapping the requested start, even if it started earlier.
        if lower is not None and finish is not None and finish <= lower:
            excluded[index] = '显示区间早于手动开始时间'
        if upper is not None and stamp >= upper:
            excluded[index] = '显示区间晚于手动结束时间'
        begin_sub = bisect_left(subtitle_times, stamp)
        end_sub = bisect_left(subtitle_times, finish) if finish is not None else len(timeline)
        info = {'order': row['order'], 'time': row['time'],
                'display_until': rows[index+1]['time'] if finish is not None else None,
                'subtitle_count': end_sub-begin_sub,
                'subtitle_chars': sum(item[1] for item in timeline[begin_sub:end_sub]),
                'keep': index not in excluded,
                'reason': excluded.get(index, '保留：未确认属于无信息重复页')}
        if index in matched:
            info['default_page_match'] = matched[index]
        evidence.append(info)
    selected = [row for index, row in enumerate(rows) if index not in excluded]
    if not selected:
        raise ExportError('筛选后没有课程页面；未生成空 PDF。请检查时间窗，或用 --ppt-filter off 保留全部原页。')
    report = {'version': 2, 'mode': mode, 'start': start, 'end': end,
              'source_count': len(rows), 'included_orders': [row['order'] for row in selected],
              'excluded_orders': [row['order'] for index, row in enumerate(rows) if index in excluded],
              'automatic_excluded_count': len(automatic), 'pages': evidence, 'notes': notes,
              'default_page_detection': detection or {'status': 'not_run',
                  'reference_orders': list(default_reference_orders)},
              'subtitle_timeline': {'count': len(timeline),
                  'first_seconds': timeline[0][0] if timeline else None,
                  'last_seconds': timeline[-1][0] if timeline else None}}
    return selected, report


def filter_local_rows(rows, image_dir, *, connection=None, browser_path=None,
                      subtitle_rows=None, mode='auto', start=None, end=None):
    memo = {}

    def run(cdp):
        def get(row):
            key = row['order']
            if key not in memo:
                memo[key] = visual_descriptor(cdp, (image_dir/f'{key:04d}.jpg').read_bytes())
            return memo[key]
        references, detection = [], {'status': 'disabled', 'reference_orders': []}
        if mode == 'auto':
            print('正在分析整段课程的画面，并用 Windows 本机文字识别确认录播默认页…', flush=True)

            def read_text(representatives):
                print(f'本机文字识别：核对 {len(representatives)} 组画面；不上传图片。', flush=True)
                return read_image_text([image_dir/f"{row['order']:04d}.jpg" for row in representatives])

            references, detection = default_page_references(rows, get, read_text)
            if detection['status'] == 'unavailable':
                print('本机文字识别不可用，未确认的页面将保留；可查看筛选报告。', flush=True)
        return filter_plan(rows, get, subtitle_rows=subtitle_rows, mode=mode, start=start, end=end,
                           default_reference_orders=references, detection=detection)

    image_dir = Path(image_dir)
    if connection is not None or mode == 'off':
        return run(connection)
    with local_image_browser(browser_path) as cdp:
        return run(cdp)


def rebuild_existing(folder, *, output=None, browser_path=None, subtitles_file=None,
                     mode='auto', start=None, end=None, allow_partial=False):
    from extract_ppt import jpeg_info, unique_path, write_pdf
    folder = Path(folder).expanduser().resolve()
    manifest = folder if folder.is_file() else folder/'PPT清单.json'
    data = json.loads(manifest.read_text(encoding='utf-8-sig'))
    folder = manifest.parent
    rows = data.get('slides', [])
    if (not rows or data.get('source_count') != len(rows)
            or [row.get('order') for row in rows] != list(range(1, len(rows)+1))):
        raise ExportError('PPT 清单中的原始序号不完整。')
    images = folder/'images'
    completed = {row['order']: row for row in data.get('completed', [])}
    valid, missing = [], []
    for row in rows:
        path = images/f"{row['order']:04d}.jpg"
        try:
            content = path.read_bytes()
            jpeg_info(content)
            if hashlib.sha256(content).hexdigest() != completed.get(row['order'], {}).get('sha256'):
                raise ExportError('图片哈希不一致。')
            valid.append(row)
        except (OSError, ExportError):
            missing.append(row['order'])
    if missing and not allow_partial:
        raise ExportError(f'已有下载中有 {len(missing)} 张缺失或损坏；请先续传，或明确使用 --allow-partial。')
    timeline = load_subtitle_timeline(subtitles_file) if subtitles_file else []
    selected, report = filter_local_rows(valid, images, browser_path=browser_path,
        subtitle_rows=timeline, mode=mode, start=start, end=end)
    report['available_count'] = len(valid)
    report['source_count'] = len(rows)
    report['missing_orders'] = missing
    if output:
        target = Path(output).expanduser().resolve()
        if target.suffix.lower() != '.pdf':
            raise ExportError('筛选输出文件须使用 .pdf 扩展名。')
        if target.exists():
            raise ExportError('指定输出文件已存在，请选择新的文件名，避免覆盖原 PDF。')
        target.parent.mkdir(parents=True, exist_ok=True)
    else:
        suffix = '_PPT_部分精简' if missing else '_PPT_精简'
        target = unique_path(folder, Path(data.get('pdf_file', '课程PPT.pdf')).stem + suffix, '.pdf')
    write_pdf(target, data.get('title', '课程 PPT') + '（筛选版）', selected, images, len(rows))
    report['pdf_file'] = target.name
    target.with_suffix('.筛选报告.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return target, report
