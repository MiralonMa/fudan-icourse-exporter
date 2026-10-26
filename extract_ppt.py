#!/usr/bin/env python3
"""Export icourse's original PPT JPEGs through the manually logged-in browser.

Python standard library only. No cookies are read, copied or exported. The
original-image naming rule was verified against resources loaded by the PPT
viewer, rather than guessed from a thumbnail alone.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit
import ppt_filter as filtering

from extract_subtitles import (CDP, ExportError, choose_course, find_browser,
                               launch_browser, list_pages, safe_name,
                               sanitized_source_url, validate_page_url)


SLIDES_JS = r"""(() => {
  const rows = Array.from(document.querySelectorAll('.note-ppt_item')).map(e => {
    const image = e.querySelector('.ppt_wrap > img');
    return {order: Number(e.querySelector('.ppt_order')?.textContent.trim()),
      time: e.querySelector('.ppt_time')?.textContent.trim() || '',
      thumbnail: image ? new URL(image.getAttribute('data-src') || image.getAttribute('src'), location.href).href : ''};
  });
  const originalResources = performance.getEntriesByType('resource')
    .map(entry => entry.name).filter(url => /\/ppt\/.*\/\d+\.jpe?g(?:\?|$)/i.test(url));
  const total = Number(document.querySelector('.pptSlider')?.getAttribute('aria-valuemax') ||
    document.querySelector('.ppt_page_con')?.textContent.split('/')[1] || 0);
  const subtitleTimeline = Array.from(document.querySelectorAll('.trans-item')).map(e => ({
    time: e.previousElementSibling?.classList.contains('item-time') ? e.previousElementSibling.textContent.trim() : '',
    chars: (e.querySelector('.item-origin')?.textContent || '').trim().length
  }));
  return {title: document.title, url: location.href, total, rows, originalResources, subtitleTimeline};
})()"""


def evaluate(cdp, expression):
    response = cdp.call('Runtime.evaluate', {'expression': expression,
                        'awaitPromise': True, 'returnByValue': True})
    if 'exceptionDetails' in response:
        raise ExportError('浏览器读取失败，请保持已登录的录播窗口打开。')
    result = response.get('result', {})
    if 'value' not in result:
        raise ExportError('浏览器没有返回有效数据。')
    return result['value']


def asset_identity(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def prepare_slides(data):
    rows = data.get('rows', [])
    total = data.get('total', 0)
    if not rows or total != len(rows):
        raise ExportError('PPT 列表未完整加载或无法核对总页数，请打开 PPT 查看器后重试。')
    if [row.get('order') for row in rows] != list(range(1, total + 1)):
        raise ExportError('PPT 序号存在缺失或重复，停止以免错页。')
    origin = urlsplit(data['url'])
    observed = {asset_identity(url) for url in data.get('originalResources', [])}
    prepared = []
    for row in rows:
        if not re.fullmatch(r'\d{2,3}:\d{2}:\d{2}', row.get('time', '')):
            raise ExportError('PPT 时间戳缺失，请等待列表完整加载。')
        thumbnail = row.get('thumbnail', '')
        parts = urlsplit(thumbnail)
        # This exact s_<timestamp>.jpg -> <timestamp>.jpg mapping was observed
        # in the live viewer. Never fall back to the small thumbnail on failure.
        if not re.search(r'/ppt/.*/s_\d+\.jpe?g$', parts.path, re.I):
            raise ExportError('PPT 图片路径结构变化，需重新核实原图规则。')
        if (parts.scheme, parts.netloc) != (origin.scheme, origin.netloc):
            raise ExportError('图片与录播页不同源，当前登录浏览器下载流程尚未适配。')
        original_path = re.sub(r'/s_(\d+\.jpe?g)$', r'/\1', parts.path, flags=re.I)
        original = urlunsplit((parts.scheme, parts.netloc, original_path, parts.query, ''))
        prepared.append(dict(row, original=original))
    if not any(asset_identity(row['original']) in observed for row in prepared):
        raise ExportError('尚未观察到查看器加载的原图。请点击右侧小窗口的切换图标，打开清晰 PPT，再按回车。')
    return prepared


def fetch_jpeg(cdp, url, min_width=800, min_height=450):
    # The fetch runs in the same page and normal browser session. Credentials
    # stay in the browser, and authentication is never automated here.
    expression = r"""(async () => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 20000);
      try {
        const url = URL_VALUE;
        if (new URL(url, location.href).origin !== location.origin)
          return {ok:false, error:'图片与录播页不同源'};
        const response = await fetch(url, {credentials:'same-origin', signal:controller.signal});
        if (!response.ok) return {ok:false, error:'HTTP ' + response.status};
        if (new URL(response.url).origin !== location.origin)
          return {ok:false, error:'图片请求已跳转，请检查登录状态'};
        const type = (response.headers.get('content-type') || '').split(';')[0].toLowerCase();
        if (type && type !== 'image/jpeg' && type !== 'image/jpg' && type !== 'application/octet-stream')
          return {ok:false, error:'响应不是 JPEG 图片，可能需要重新登录'};
        const declaredSize = Number(response.headers.get('content-length') || 0);
        if (declaredSize > 8388608) return {ok:false, error:'单张图片超过 8 MB'};
        const blob = await response.blob();
        if (blob.size > 8388608) return {ok:false, error:'单张图片超过 8 MB'};
        const bitmap = await createImageBitmap(blob);
        const width = bitmap.width, height = bitmap.height;
        bitmap.close();
        if (width < MIN_WIDTH || height < MIN_HEIGHT)
          return {ok:false, error:'图片尺寸过低：' + width + '×' + height};
        const bytes = new Uint8Array(await blob.arrayBuffer());
        let binary = '';
        for (let i=0; i<bytes.length; i+=32768)
          binary += String.fromCharCode(...bytes.subarray(i, i+32768));
        return {ok:true, width, height, data:btoa(binary)};
      } catch (_) { return {ok:false, error:'网络请求或图片解码失败，请检查校园网与登录状态'}; }
      finally { clearTimeout(timer); }
    })()""".replace('URL_VALUE', json.dumps(url)).replace('MIN_WIDTH', str(int(min_width))).replace('MIN_HEIGHT', str(int(min_height)))
    result = evaluate(cdp, expression)
    if not result.get('ok'):
        raise ExportError(result.get('error', '图片下载失败'))
    content = base64.b64decode(result['data'], validate=True)
    width, height, components = jpeg_info(content)
    if (width, height) != (result['width'], result['height']):
        raise ExportError('图片尺寸校验不一致。')
    if width < min_width or height < min_height:
        raise ExportError('原图分辨率未达到要求。')
    return content


def jpeg_info(content):
    if not content.startswith(b'\xff\xd8') or b'\xff\xd9' not in content[-128:]:
        raise ExportError('不是完整的 JPEG 文件。')
    position = 2
    frame = None
    while position < len(content):
        if content[position] != 0xff:
            raise ExportError('JPEG 标记异常。')
        while position < len(content) and content[position] == 0xff:
            position += 1
        if position >= len(content):
            break
        marker = content[position]
        position += 1
        if marker in (0xd8, 0xd9, 0x01) or 0xd0 <= marker <= 0xd7:
            continue
        if position + 2 > len(content):
            break
        length = struct.unpack('!H', content[position:position + 2])[0]
        if length < 2 or position + length > len(content):
            raise ExportError('JPEG 数据截断。')
        if marker in (0xc0, 0xc1, 0xc2):
            if length < 8:
                raise ExportError('JPEG 图像头不完整。')
            bits, height, width, components = struct.unpack('!BHHB', content[position + 2:position + 8])
            if bits != 8 or components not in (1, 3) or not width or not height:
                raise ExportError('目前仅支持 8 位灰度或 RGB JPEG 原图。')
            frame = (width, height, components)
        if marker == 0xda:
            break
        position += length
    if frame is None:
        raise ExportError('无法读取 JPEG 尺寸。')
    return frame


def unique_path(folder, stem, suffix):
    index = 1
    while True:
        path = folder / (stem + ('' if index == 1 else f'_{index}') + suffix)
        if not path.exists():
            return path
        index += 1


def write_pdf(path, title, rows, image_dir, total):
    """Embed original JPEG bytes directly; no conversion or recompression.

    Small PDF writer for this site's RGB JPEG slide exports, not a general PDF
    engine. Every page has source order/time and a navigation bookmark.
    """
    if not rows:
        raise ExportError('没有可写入 PDF 的图片。')
    path = Path(path)
    count = len(rows)
    outline_root = 5 + 3 * count
    last_object = outline_root + count
    offsets = [0] * (last_object + 1)
    temporary = path.with_suffix(path.suffix + '.part')
    with temporary.open('wb') as stream:
        stream.write(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')

        def obj(number, payload):
            offsets[number] = stream.tell()
            stream.write(f'{number} 0 obj\n'.encode('ascii') + payload + b'\nendobj\n')

        def pdf_text(text):
            return b'<' + ('FEFF' + text.encode('utf-16-be').hex()).encode('ascii') + b'>'

        obj(1, f'<< /Type /Catalog /Pages 2 0 R /Outlines {outline_root} 0 R >>'.encode())
        kids = ' '.join(f'{5 + 3 * i} 0 R' for i in range(count))
        obj(2, f'<< /Type /Pages /Count {count} /Kids [{kids}] >>'.encode())
        obj(3, b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
        obj(4, b'<< /Title ' + pdf_text(title) + b' /Producer (Local icourse PPT exporter) >>')
        for index, row in enumerate(rows):
            image_path = image_dir / f"{row['order']:04d}.jpg"
            image = image_path.read_bytes()
            width, height, components = jpeg_info(image)
            draw_width = 720
            draw_height = draw_width * height / width
            page_height = draw_height + 28
            page_id = 5 + 3 * index
            obj(page_id, (f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 720 {page_height:.6f}] '
                f'/Resources << /Font << /F1 3 0 R >> /XObject << /Im1 {page_id+1} 0 R >> >> '
                f'/Contents {page_id+2} 0 R >>').encode())
            color_space = '/DeviceRGB' if components == 3 else '/DeviceGray'
            header = (f'<< /Type /XObject /Subtype /Image /Width {width} /Height {height} '
                      f'/ColorSpace {color_space} /BitsPerComponent 8 /Filter /DCTDecode '
                      f'/Length {len(image)} >>\nstream\n').encode()
            obj(page_id + 1, header + image + b'\nendstream')
            label = f"Source PPT {row['order']:03d}/{total} | {row['time']} | {width} x {height} px"
            content = (f'q 720 0 0 {draw_height:.6f} 0 28 cm /Im1 Do Q\n'
                       f'BT /F1 9 Tf 0.25 g 12 10 Td ({label}) Tj ET\n').encode('ascii')
            obj(page_id + 2, f'<< /Length {len(content)} >>\nstream\n'.encode() + content + b'endstream')
        obj(outline_root, (f'<< /Type /Outlines /First {outline_root+1} 0 R '
                           f'/Last {outline_root+count} 0 R /Count {count} >>').encode())
        for index, row in enumerate(rows):
            number = outline_root + index + 1
            neighbors = ''
            if index: neighbors += f'/Prev {number-1} 0 R '
            if index + 1 < count: neighbors += f'/Next {number+1} 0 R '
            label = f"PPT {row['order']:03d} - {row['time']}"
            obj(number, b'<< /Title ' + pdf_text(label) +
                f' /Parent {outline_root} 0 R {neighbors}/Dest [{5+3*index} 0 R /Fit] >>'.encode())
        xref = stream.tell()
        stream.write(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
        for offset in offsets[1:]:
            stream.write(f'{offset:010d} 00000 n \n'.encode())
        stream.write((f'trailer\n<< /Size {len(offsets)} /Root 1 0 R /Info 4 0 R >>\n'
                      f'startxref\n{xref}\n%%EOF\n').encode())
    temporary.replace(path)
    return path


def export_ppt(cdp, data, output_dir, *, min_width=800, min_height=450, retries=2, allow_partial=False,
               filter_mode='auto', start_time=None, end_time=None, course_folder=None):
    filtering.validate_window(start_time, end_time)
    rows = prepare_slides(data)
    clean_rows = [{'order': row['order'], 'time': row['time'],
                   'original': asset_identity(row['original'])} for row in rows]
    identity = hashlib.sha256(json.dumps(clean_rows, sort_keys=True).encode()).hexdigest()[:10]
    folder = (Path(course_folder).expanduser().resolve() if course_folder is not None else
              Path(output_dir).expanduser().resolve() / (safe_name(data['title']) + '_' + identity))
    images = folder / 'images'
    images.mkdir(parents=True, exist_ok=True)
    report_path = folder / 'PPT清单.json'
    previous = {}
    if report_path.is_file():
        try:
            prior = json.loads(report_path.read_text(encoding='utf-8'))
            if (prior.get('source_url') != sanitized_source_url(data['url'])
                    or prior.get('slides') != clean_rows):
                raise ExportError('目录中已有不同课程或不同图片清单；请指定新的 --output-dir，避免混用原图缓存。')
            previous = {row['order']: row for row in prior.get('completed', [])}
        except (OSError, ValueError, KeyError):
            previous = {}
    report = {'title': data['title'], 'source_url': sanitized_source_url(data['url']),
              'source_count': len(rows), 'slides': clean_rows, 'completed': [], 'failed': []}
    # Verify the entire existing checkpoint before starting new requests. Even
    # if this run is interrupted early, later cached pages stay in the manifest.
    verified_cache = {}
    for row in rows:
        image_path = images / f"{row['order']:04d}.jpg"
        try:
            if image_path.is_file() and row['order'] in previous:
                candidate = image_path.read_bytes()
                w, h, _ = jpeg_info(candidate)
                digest = hashlib.sha256(candidate).hexdigest()
                if w >= min_width and h >= min_height and digest == previous[row['order']].get('sha256'):
                    verified_cache[row['order']] = candidate
                    report['completed'].append({'order': row['order'], 'time': row['time'],
                        'width': w, 'height': h, 'sha256': digest})
        except (ExportError, OSError):
            pass

    def checkpoint():
        part = report_path.with_suffix('.json.part')
        part.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        part.replace(report_path)

    for row in rows:
        order = row['order']
        image_path = images / f'{order:04d}.jpg'
        image = verified_cache.get(order)
        cached = image is not None
        if image is None:
            error = '未知错误'
            for attempt in range(retries + 1):
                try:
                    print(f'[{order}/{len(rows)}] 读取原图，尝试 {attempt+1}/{retries+1}…', flush=True)
                    image = fetch_jpeg(cdp, row['original'], min_width, min_height)
                    part = image_path.with_suffix('.jpg.part')
                    part.write_bytes(image)
                    part.replace(image_path)
                    break
                except (ExportError, OSError, ValueError) as exc:
                    error = str(exc)
                    if attempt < retries:
                        time.sleep(min(1 + attempt, 3))
            if image is None:
                report['failed'].append({'order': order, 'time': row['time'], 'error': error})
                print(f'[{order}/{len(rows)}] 失败：{error}', flush=True)
                checkpoint()
                # Several consecutive failures are usually campus-network loss.
                # Stop quickly; keep checkpoints for the next manually logged-in run.
                if len(report['failed']) >= 3 and not allow_partial:
                    print('连续或累计 3 张读取失败，停止请求并保留进度。', flush=True)
                    break
                continue
        w, h, _ = jpeg_info(image)
        if not cached:
            report['completed'].append({'order': order, 'time': row['time'], 'width': w, 'height': h,
                                        'sha256': hashlib.sha256(image).hexdigest()})
        report['completed'].sort(key=lambda entry: entry['order'])
        print(f'[{order}/{len(rows)}] {"已验证缓存" if cached else "原图已保存"} {w}×{h}', flush=True)
        checkpoint()
    completed = {entry['order'] for entry in report['completed']}
    report['missing_orders'] = [row['order'] for row in rows if row['order'] not in completed]
    checkpoint()
    if report['missing_orders'] and not allow_partial:
        raise ExportError(f"已保存 {len(completed)}/{len(rows)} 张，尚缺 {len(report['missing_orders'])} 张。未生成完整 PDF。恢复网络后以相同输出目录运行即可续传。清单：{report_path}")
    if not completed:
        raise ExportError('没有成功保存任何原图，未生成 PDF。')
    selected = [row for row in rows if row['order'] in completed]
    print('正在筛选整段课程的录播默认页面并核对时间轴…', flush=True)
    selected, filter_report = filtering.filter_local_rows(selected, images, connection=cdp,
        subtitle_rows=data.get('subtitleTimeline', []), mode=filter_mode, start=start_time, end=end_time)
    report['filter'] = filter_report
    report['pdf_included_orders'] = [row['order'] for row in selected]
    report['filtered_out_orders'] = filter_report['excluded_orders']
    removed = len(filter_report['excluded_orders'])
    suffix = f'_PPT_部分{len(selected)}张_共{len(rows)}张' if report['missing_orders'] else f'_PPT_完整{len(rows)}张'
    if removed:
        suffix = f'_PPT_{"部分精简" if report["missing_orders"] else "精简"}{len(selected)}张_原{len(rows)}张'
    print(f'PDF 保留 {len(selected)} 张，筛除 {removed} 张；原图缓存保持完整。', flush=True)
    path = unique_path(folder, safe_name(data['title']) + suffix, '.pdf')
    write_pdf(path, data['title'] + suffix, selected, images, len(rows))
    report['pdf_file'] = path.name
    checkpoint()
    return path, report


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='复旦 icourse 清晰 PPT 原图及 PDF 本地导出，无第三方 Python 依赖。')
    parser.add_argument('--url', help='录播网址，省略则交互输入；代码不限制域名')
    parser.add_argument('--output-dir', default=str(Path(__file__).resolve().parent / 'PPT输出'))
    parser.add_argument('--browser', help='Edge/Chrome 程序路径')
    parser.add_argument('--retries', type=int, default=2, help='每张图片额外重试次数，默认 2')
    parser.add_argument('--min-width', type=int, default=800, help='原图最小宽度，默认 800 像素')
    parser.add_argument('--min-height', type=int, default=450, help='原图最小高度，默认 450 像素')
    parser.add_argument('--allow-partial', action='store_true', help='显式允许输出标为“部分”的缺页 PDF')
    parser.add_argument('--ppt-filter', choices=['auto', 'off'], default='auto', help='默认筛除整段课程中的录播默认页面（含课间）；off 保留全部画面')
    parser.add_argument('--start-time', help='PPT 手动开始时间 HH:MM:SS，保留跨越开始点的页面')
    parser.add_argument('--end-time', help='PPT 手动结束时间 HH:MM:SS')
    args = parser.parse_args(argv)
    filtering.validate_window(args.start_time, args.end_time)
    if args.retries < 0 or args.retries > 5 or min(args.min_width, args.min_height) < 1:
        raise ExportError('重试次数须在 0 到 5 之间，最小宽高须为正数。')
    requested = (args.url or input('请输入课程录播网址：')).strip()
    validate_page_url(requested)
    profile = Path(tempfile.mkdtemp(prefix='course-ppt-browser-'))
    process = browser_ws = None
    print('\n将在独立浏览器中打开网址。请手动登录并返回录播页。')
    print('点击右侧小窗口左上角的切换图标，将 PPT 显示出来，可再切换为大窗口。')
    print('请确认能切到后面的 PPT 并看到新内容，再回到此窗口按回车。')
    print('所有图片、续传清单和 PDF 仅保存在本机。')
    try:
        process, endpoint, browser_ws = launch_browser(find_browser(args.browser), profile, requested)
        while True:
            if input('\n准备好后按回车开始（q 退出）：').strip().lower() == 'q':
                break
            try:
                pages = list_pages(endpoint)
                if not pages:
                    raise ExportError('没有可读取的录播标签页。')
                target = choose_course(pages, requested)
                validate_page_url(target.get('url', ''))
                with CDP(target['webSocketDebuggerUrl']) as cdp:
                    data = evaluate(cdp, SLIDES_JS)
                    result, report = export_ppt(cdp, data, args.output_dir,
                        min_width=args.min_width, min_height=args.min_height,
                        retries=args.retries, allow_partial=args.allow_partial,
                        filter_mode=args.ppt_filter, start_time=args.start_time, end_time=args.end_time)
                print(f'\n导出完成：{result}\n原图：{len(report["completed"])}/{report["source_count"]} 张；PDF：{len(report["pdf_included_orders"])} 张。PDF 内保留原图像素及原序号、时间戳。')
                break
            except (ExportError, OSError, ValueError) as error:
                print(f'\n提取未完成：{error}')
                print('可在浏览器恢复网络／登录后再次按回车；已校验的图片可续传。')
    finally:
        if browser_ws:
            try:
                with CDP(browser_ws) as cdp:
                    cdp.call('Browser.close')
            except (OSError, ExportError, ValueError):
                pass
        stopped = True
        if process:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                stopped = False
                print('请手动关闭本工具的独立浏览器窗口，以便清理临时登录资料。')
        if stopped:
            try:
                shutil.rmtree(profile)
            except OSError:
                print(f'临时浏览器目录暂未清理：{profile}')


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('\n已取消，已下载的图片与续传清单仍保留在输出目录。')
    except (ExportError, OSError, ValueError) as error:
        print(f'\n无法完成：{error}', file=sys.stderr)
        sys.exit(1)
