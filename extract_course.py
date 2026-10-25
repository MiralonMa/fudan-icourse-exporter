#!/usr/bin/env python3
"""One interactive entry point for local subtitle and original PPT exports."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import extract_ppt as ppt
import extract_subtitles as subtitles
import ppt_filter as filtering


LABELS = {'subtitles': '字幕', 'ppt': 'PPT'}
MODES = {'subtitles': ('subtitles',), 'ppt': ('ppt',), 'both': ('subtitles', 'ppt')}


def choose_mode(value=None):
    if value is not None:
        return MODES[value]
    print('\n请选择需要提取的内容：\n  1. 仅字幕（TXT）\n  2. 仅 PPT（原图与 PDF）\n  3. 字幕和 PPT 都提取\n  q. 退出')
    while True:
        answer = input('输入 1、2 或 3：').strip().lower()
        if answer == 'q':
            return ()
        mode = {'1': 'subtitles', '2': 'ppt', '3': 'both'}.get(answer)
        if mode:
            return MODES[mode]
        print('无效选择，请输入 1、2、3；输入 q 退出。')


@contextmanager
def browser_session(url, browser_path=None):
    profile = Path(tempfile.mkdtemp(prefix='course-exporter-browser-'))
    process = browser_ws = None
    try:
        process, endpoint, browser_ws = subtitles.launch_browser(
            subtitles.find_browser(browser_path), profile, url)
        yield endpoint
    finally:
        if browser_ws:
            try:
                with subtitles.CDP(browser_ws) as cdp:
                    cdp.call('Browser.close')
            except (OSError, subtitles.ExportError, ValueError):
                pass
        stopped = True
        if process:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                stopped = False
                print('请手动关闭工具打开的独立浏览器窗口，以便清理临时登录资料。')
        if stopped:
            # Delete only this invocation's own temporary browser directory.
            temporary_root = Path(tempfile.gettempdir()).resolve()
            resolved = profile.resolve()
            if resolved.parent == temporary_root and resolved.name.startswith('course-exporter-browser-'):
                try:
                    shutil.rmtree(resolved)
                except OSError:
                    print(f'临时浏览器目录暂未清理：{profile}')


def run_pending(target, pending, options):
    """Each requested export is independent; a failure never discards a success."""
    succeeded, errors = {}, {}
    expected = subtitles.sanitized_source_url(target.get('url', ''))
    for kind in pending:
        print(f'\n开始提取{LABELS[kind]}…', flush=True)
        try:
            if kind == 'subtitles':
                data = subtitles.extract_page(target)
                if subtitles.sanitized_source_url(data.get('url', '')) != expected:
                    raise subtitles.ExportError('录播页已切换，请回到本次选择的课程后重试。')
                path = subtitles.save_subtitles(data, options.output_dir / '字幕', options.json)
                print(f"字幕完成：{len(data['rows'])} 条，{data['rows'][0]['time']} - {data['rows'][-1]['time']}")
                print('请核对末条时间是否覆盖所需课程，原始识别错误将保留。')
            else:
                with subtitles.CDP(target['webSocketDebuggerUrl']) as cdp:
                    data = ppt.evaluate(cdp, ppt.SLIDES_JS)
                    if subtitles.sanitized_source_url(data.get('url', '')) != expected:
                        raise subtitles.ExportError('录播页已切换，请回到本次选择的课程后重试。')
                    path, report = ppt.export_ppt(cdp, data, options.output_dir / 'PPT',
                        min_width=options.min_width, min_height=options.min_height,
                        retries=options.retries, allow_partial=options.allow_partial,
                        filter_mode=options.ppt_filter, start_time=options.start_time, end_time=options.end_time)
                if report['missing_orders']:
                    print(f"PPT 部分完成：{len(report['completed'])}/{report['source_count']} 张；缺页见清单。")
                else:
                    included = len(report.get('pdf_included_orders', range(report['source_count'])))
                    print(f"PPT 完成：{report['source_count']} 张原图，PDF 保留 {included} 张。")
            succeeded[kind] = path
            print(f'保存位置：{path}', flush=True)
        except (subtitles.ExportError, OSError, ValueError) as error:
            errors[kind] = str(error)
            print(f'{LABELS[kind]}未完成：{error}', flush=True)
    return succeeded, errors


def build_parser():
    parser = argparse.ArgumentParser(
        description='复旦 icourse 字幕与清晰 PPT 本地提取：选择字幕、PPT 或两者，共用一次手动登录。')
    parser.add_argument('--url', help='录播网址，省略则交互输入；代码不限制域名')
    parser.add_argument('--extract', choices=tuple(MODES), help='subtitles=仅字幕，ppt=仅PPT，both=两者；省略则显示选择菜单')
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).resolve().parent / '提取结果')
    parser.add_argument('--browser', help='Edge/Chrome 程序路径')
    parser.add_argument('--json', action='store_true', help='额外保存字幕 JSON')
    parser.add_argument('--retries', type=int, default=2, help='每张 PPT 额外重试次数，默认 2')
    parser.add_argument('--min-width', type=int, default=800, help='PPT 原图最小宽度，默认 800 像素')
    parser.add_argument('--min-height', type=int, default=450, help='PPT 原图最小高度，默认 450 像素')
    parser.add_argument('--allow-partial', action='store_true', help='明确允许输出标为“部分”的缺页 PPT PDF')
    parser.add_argument('--ppt-filter', choices=['auto', 'off'], default='auto', help='自动筛除头尾密集重复画面；off 关闭自动筛选')
    parser.add_argument('--start-time', help='PPT 手动开始时间 HH:MM:SS')
    parser.add_argument('--end-time', help='PPT 手动结束时间 HH:MM:SS')
    parser.add_argument('--filter-existing', type=Path, help='离线重建已下载 PPT；指定含 PPT清单.json 和 images 的文件夹，不需登录')
    parser.add_argument('--subtitles-file', type=Path, help='离线筛选时可选的已导出字幕 TXT／JSON')
    parser.add_argument('--filtered-output', type=Path, help='离线筛选 PDF 的另存位置，默认保存到原下载目录')
    return parser


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    options = build_parser().parse_args(argv)
    if options.filter_existing:
        result, report = filtering.rebuild_existing(options.filter_existing,
            output=options.filtered_output, browser_path=options.browser,
            subtitles_file=options.subtitles_file, mode=options.ppt_filter,
            start=options.start_time, end=options.end_time, allow_partial=options.allow_partial)
        print(f'精简 PDF：{result}\n保留 {len(report["included_orders"])} 张，筛除 {len(report["excluded_orders"])} 张。原 PDF 和原图未覆盖。')
        return 0
    selected = choose_mode(options.extract)
    if not selected:
        print('已取消。')
        return 0
    if 'ppt' in selected and (not 0 <= options.retries <= 5 or min(options.min_width, options.min_height) < 1):
        raise subtitles.ExportError('PPT 重试次数须在 0 到 5 之间，最小宽高须为正数。')
    if 'ppt' in selected:
        filtering.validate_window(options.start_time, options.end_time)
    options.output_dir = options.output_dir.expanduser().resolve()
    requested = (options.url or input('请输入课程录播网址：')).strip()
    subtitles.validate_page_url(requested)
    print('\n将打开一个独立浏览器窗口。请在其中手动登录，并回到目标录播页。')
    if 'ppt' in selected:
        print('PPT：点击右侧小窗口的切换图标，显示清晰 PPT，并确认后面的图片能正常加载。')
    if 'subtitles' in selected:
        print('字幕：打开“语音识别”，清空搜索框，等待完整字幕加载。')
    if len(selected) == 2:
        print('两者都选时，先确认清晰 PPT，再打开“语音识别”；只需登录一次。')
    print('导出结果仅保存在本机；不提取音频或视频。')
    completed = {}
    pending = selected
    locked_course = None
    with browser_session(requested, options.browser) as endpoint:
        while pending:
            action = input('\n准备好后按回车开始（q 退出）：').strip().lower()
            if action == 'q':
                print('已停止。已保存的字幕、图片和续传清单仍保留在输出目录。')
                return 1
            try:
                pages = subtitles.list_pages(endpoint)
                if not pages:
                    raise subtitles.ExportError('没有可读取的录播标签页，请保持独立浏览器打开。')
                target = subtitles.choose_course(pages, requested)
                subtitles.validate_page_url(target.get('url', ''))
                identity = subtitles.sanitized_source_url(target['url'])
                if completed and locked_course != identity:
                    raise subtitles.ExportError('已有内容导出成功，请回到同一课程后重试剩余内容，避免混合不同课程。')
                succeeded, errors = run_pending(target, pending, options)
                if succeeded:
                    locked_course = identity
                    completed.update(succeeded)
                pending = tuple(kind for kind in selected if kind not in completed)
                if pending:
                    remaining = '、'.join(LABELS[kind] for kind in pending)
                    print(f'\n仍待提取：{remaining}。请在浏览器修正网络、登录或加载状态，再按回车。')
                    if completed:
                        print('已成功的内容不会重复导出；下次仅重试未完成部分。')
            except (subtitles.ExportError, OSError, ValueError) as error:
                print(f'\n暂不能提取：{error}')
    print('\n所选内容已导出：')
    for kind in selected:
        print(f'{LABELS[kind]}：{completed[kind]}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print('\n已取消，已经保存的结果仍保留在本机。')
        sys.exit(130)
    except (subtitles.ExportError, OSError, ValueError) as error:
        print(f'\n无法完成：{error}', file=sys.stderr)
        sys.exit(1)
