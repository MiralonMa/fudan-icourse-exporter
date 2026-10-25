"""Conservative local filtering of repetitive opening/closing PPT captures.

No OCR, network service or Python image package is required. A browser decodes
the local JPEGs into small visual descriptors; decisions use image similarity,
capture cadence and displayed time intervals. Subtitle timestamps are recorded
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
        # The JSON listing is loopback-only, like the existing extraction tools.
        from extract_subtitles import list_pages
        pages = list_pages(endpoint)
        page = next((item for item in pages if item.get('id') == target['targetId']), None)
        if page is None:
            raise ExportError('无法建立本机图片分析页面。')
        with CDP(page['webSocketDebuggerUrl']) as connection:
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


def filter_plan(rows, descriptor, *, subtitle_rows=None, mode='auto', start=None, end=None):
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
    # Only trim a shared visual family at BOTH recording edges, captured in
    # dense repeated bursts. A long-held first/last teaching slide alone cannot
    # qualify. Ambiguous UI, middle-of-class pages and ordinary repeat slides stay.
    if mode == 'auto' and len(rows) >= 12 and stamps[-1] - stamps[0] >= 180:
        first, last = descriptor(rows[0]), descriptor(rows[-1])
        if distance(first, last) <= .035:
            limit = min(600, (stamps[-1] - stamps[0]) * .15)

            def matches(index):
                pixels = descriptor(rows[index])
                return min(distance(pixels, first), distance(pixels, last)) <= .035

            head = []
            for index in range(len(rows)):
                if stamps[index] - stamps[0] > limit or not matches(index): break
                head.append(index)
            tail = []
            for index in range(len(rows)-1, -1, -1):
                if stamps[-1] - stamps[index] > limit or not matches(index): break
                tail.append(index)
            tail.reverse()

            def dense(indices):
                if len(indices) < 4 or stamps[indices[-1]] - stamps[indices[0]] < 15:
                    return False
                gaps = [stamps[b] - stamps[a] for a, b in zip(indices, indices[1:])]
                return all(0 < gap <= 15 for gap in gaps)

            eligible = (dense(head) and dense(tail) and tail[0]-head[-1] > 3
                    and stamps[tail[0]] - stamps[head[-1]] >= 120
                    and len(head) + len(tail) <= len(rows) * .8)
            if eligible:
                left, right = head[-1]+1, tail[0]-1
                samples = {left, (left+right)//2, right}
                eligible = sum(not matches(index) for index in samples) >= 2
            if eligible:
                automatic = head + tail
                for index in automatic:
                    excluded[index] = '头尾相同画面，连续密集重复截图'
            else:
                notes.append('头尾重复证据不足，未按画面自动删除。')
        else:
            notes.append('头尾画面不属于同一视觉组，未自动裁剪。')
    elif mode == 'auto':
        notes.append('页数或录播跨度不足，未自动裁剪。')
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
        evidence.append(info)
    selected = [row for index, row in enumerate(rows) if index not in excluded]
    if not selected:
        raise ExportError('筛选结果没有保留任何页面，请检查手动时间窗。')
    report = {'version': 1, 'mode': mode, 'start': start, 'end': end,
              'source_count': len(rows), 'included_orders': [row['order'] for row in selected],
              'excluded_orders': [row['order'] for index, row in enumerate(rows) if index in excluded],
              'automatic_excluded_count': len(automatic), 'pages': evidence, 'notes': notes,
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
        return filter_plan(rows, get, subtitle_rows=subtitle_rows, mode=mode, start=start, end=end)

    if connection is not None or mode == 'off' or len(rows) < 12:
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
