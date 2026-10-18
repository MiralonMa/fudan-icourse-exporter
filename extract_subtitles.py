#!/usr/bin/env python3
"""Local, dependency-free exporter for the Fudan recording site's rendered subtitles.

Python 3.10+, installed Microsoft Edge or Google Chrome. Authentication is manual.
Only the loopback Chrome DevTools endpoint is contacted by Python; the browser
loads the course website normally. No account cookies are read or exported.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.request import build_opener, ProxyHandler


EXTRACT_JS = r"""(() => {
  const documents = [document];
  for (let i = 0; i < documents.length; i++) {
    for (const frame of documents[i].querySelectorAll('iframe')) {
      try { if (frame.contentDocument) documents.push(frame.contentDocument); } catch (_) {}
    }
  }
  const candidates = documents.map(doc => {
    const elements = Array.from(doc.querySelectorAll('.trans-item'));
    const rows = elements.map((element, index) => {
      const spans = Array.from(element.querySelectorAll('.item-origin > span'));
      const text = spans.map(span => span.textContent || '').join('');
      let previous = element.previousElementSibling;
      let timestamp = '';
      while (previous && !previous.classList.contains('trans-item')) {
        if (previous.classList.contains('item-time')) {
          timestamp = (previous.textContent || '').trim();
          break;
        }
        previous = previous.previousElementSibling;
      }
      return {index: index + 1, time: timestamp, text};
    });
    return {title: document.title, url: location.href, rows, domCount: elements.length};
  });
  candidates.sort((a, b) => b.rows.length - a.rows.length);
  return candidates[0];
})()"""


class ExportError(Exception):
    pass


def validate_page_url(url):
    """Validate URL syntax without restricting the host; login remains manual."""
    parts = urlsplit(url)
    if (parts.scheme not in ('https', 'http')
            or not parts.hostname
            or parts.username or parts.password):
        raise ExportError('请输入有效的 http/https 录播网址，不要在网址中包含账号密码。')
    return url


def sanitized_source_url(url):
    """Do not copy login tokens, unrelated URL parameters or fragments to files."""
    parts = urlsplit(url)
    allowed = {'course_id', 'sub_id', 'tenant_code'}
    query = [(key, value) for key, value in parse_qsl(parts.query)
             if key in allowed and value.isdigit()]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


class CDP:
    """Small loopback-only WebSocket client; handles masking, fragments and ping.

    No external WebSocket package is needed. It is deliberately restricted to
    the browser process started by this program's temporary profile.
    """
    MAX_MESSAGE = 32 * 1024 * 1024

    def __init__(self, endpoint: str):
        u = urlsplit(endpoint)
        if u.scheme != 'ws' or u.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ExportError('拒绝连接非本机的浏览器调试地址。')
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=30)
        self.buffer = b''
        self.next_id = 0
        try:
            key = base64.b64encode(os.urandom(16)).decode('ascii')
            route = u.path + (('?' + u.query) if u.query else '')
            request = (
                f'GET {route} HTTP/1.1\r\nHost: {u.netloc}\r\n'
                'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n'
            )
            self.sock.sendall(request.encode('ascii'))
            while b'\r\n\r\n' not in self.buffer:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise ExportError('浏览器关闭了调试连接。')
                self.buffer += chunk
                if len(self.buffer) > 65536:
                    raise ExportError('浏览器握手响应异常。')
            header, self.buffer = self.buffer.split(b'\r\n\r\n', 1)
            lines = header.decode('latin1').split('\r\n')
            fields = dict(line.split(':', 1) for line in lines[1:] if ':' in line)
            fields = {k.lower(): v.strip() for k, v in fields.items()}
            expected = base64.b64encode(hashlib.sha1(
                (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode('ascii')
            ).digest()).decode('ascii')
            if lines[0].split()[1] != '101' or fields.get('sec-websocket-accept') != expected:
                raise ExportError('无法连接本机浏览器：WebSocket 握手失败。')
        except Exception:
            self.close()
            raise

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _read(self, size):
        while len(self.buffer) < size:
            chunk = self.sock.recv(max(4096, min(size - len(self.buffer), 65536)))
            if not chunk:
                raise ExportError('浏览器连接已断开，请保持提取窗口打开。')
            self.buffer += chunk
        result, self.buffer = self.buffer[:size], self.buffer[size:]
        return result

    def _send(self, opcode, payload):
        size = len(payload)
        prefix = bytes([0x80 | opcode])
        if size < 126:
            prefix += bytes([0x80 | size])
        elif size <= 65535:
            prefix += bytes([0x80 | 126]) + struct.pack('!H', size)
        else:
            prefix += bytes([0x80 | 127]) + struct.pack('!Q', size)
        mask = os.urandom(4)
        masked = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
        self.sock.sendall(prefix + mask + masked)

    def _receive(self):
        fragments = []
        total = 0
        started = False
        while True:
            a, b = self._read(2)
            final, opcode, masked, size = bool(a & 0x80), a & 15, bool(b & 0x80), b & 127
            if a & 0x70:
                raise ExportError('不支持压缩的 WebSocket 帧。')
            if size == 126:
                size = struct.unpack('!H', self._read(2))[0]
            elif size == 127:
                size = struct.unpack('!Q', self._read(8))[0]
            if size > self.MAX_MESSAGE or total + size > self.MAX_MESSAGE:
                raise ExportError('字幕响应超过 32 MB，停止读取。')
            mask = self._read(4) if masked else None
            payload = self._read(size)
            if mask:
                payload = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
            if opcode == 8:
                raise ExportError('浏览器调试连接已关闭。')
            if opcode == 9:
                self._send(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode == 1 and not started:
                started = True
            elif opcode != 0 or not started:
                raise ExportError('收到异常的 WebSocket 数据帧。')
            fragments.append(payload)
            total += size
            if final:
                return b''.join(fragments).decode('utf-8')

    def call(self, method, params=None):
        self.next_id += 1
        request_id = self.next_id
        message = json.dumps({'id': request_id, 'method': method, 'params': params or {}})
        self._send(1, message.encode('utf-8'))
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            self.sock.settimeout(max(.1, deadline - time.monotonic()))
            response = json.loads(self._receive())
            if response.get('id') == request_id:
                if 'error' in response:
                    raise ExportError(f"浏览器执行失败：{response['error'].get('message', '未知错误')}")
                return response.get('result', {})
        raise ExportError('浏览器响应超时。')


def find_browser(explicit=None):
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise ExportError('指定的浏览器程序不存在。')
        return str(path)
    candidates = []
    for variable in ('PROGRAMFILES(X86)', 'PROGRAMFILES', 'LOCALAPPDATA'):
        parent = os.environ.get(variable)
        if parent:
            for relative in ('Microsoft/Edge/Application/msedge.exe', 'Google/Chrome/Application/chrome.exe'):
                candidates.append(Path(parent) / relative)
    for name in ('msedge', 'google-chrome', 'chromium', 'chromium-browser'):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for path in candidates:
        if path.is_file():
            return str(path)
    raise ExportError('未找到 Edge 或 Chrome。请安装浏览器，或通过 --browser 指定程序路径。')


def launch_browser(browser, profile, url, headless=False):
    command = [browser, '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
               '--user-data-dir=' + str(profile), '--no-first-run', '--no-default-browser-check']
    if headless:
        command += ['--headless=new', '--disable-gpu']
    command.append(url)
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    active = Path(profile) / 'DevToolsActivePort'
    deadline = time.monotonic() + 40
    opener = build_opener(ProxyHandler({}))
    while time.monotonic() < deadline:
        if active.is_file():
            lines = active.read_text(encoding='utf-8').splitlines()
            if lines and lines[0].isdigit():
                port = int(lines[0])
                if not 1 <= port <= 65535:
                    raise ExportError('浏览器调试端口无效。')
                endpoint = f'http://127.0.0.1:{port}'
                try:
                    with opener.open(endpoint + '/json/version', timeout=2) as response:
                        version = json.load(response)
                    return process, endpoint, version['webSocketDebuggerUrl']
                except (OSError, ValueError, KeyError):
                    pass
        time.sleep(.2)
    try:
        process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise ExportError('浏览器启动超时。请关闭本工具打开的窗口后重试；也可能是学校策略禁止调试端口。')


def list_pages(endpoint):
    with build_opener(ProxyHandler({})).open(endpoint + '/json/list', timeout=5) as response:
        targets = json.load(response)
    return [p for p in targets if p.get('type') == 'page' and p.get('webSocketDebuggerUrl')]


def extract_page(target):
    with CDP(target['webSocketDebuggerUrl']) as cdp:
        result = cdp.call('Runtime.evaluate', {
            'expression': EXTRACT_JS, 'returnByValue': True, 'awaitPromise': True,
        })
    if 'exceptionDetails' in result:
        raise ExportError('读取页面失败，请等待页面加载完成后重试。')
    value = result.get('result', {}).get('value')
    if not isinstance(value, dict) or not isinstance(value.get('rows'), list):
        raise ExportError('页面未返回有效字幕数据。')
    return value


def safe_name(title):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(title)).strip(' .')[:100].rstrip(' .')
    if not value:
        value = '课程录播'
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', value, re.I):
        value = '_' + value
    return value


def save_subtitles(data, output_dir, raw_json=False):
    rows = data['rows']
    if not rows:
        raise ExportError('没有读取到字幕，不创建空文件。')
    if data.get('domCount') != len(rows):
        raise ExportError('DOM 条数与返回条数不一致，停止保存以免漏字。')
    if any(not isinstance(row.get('text'), str) or not row['text'].strip() for row in rows):
        raise ExportError('存在未加载的空字幕，请等待加载完成后重试。')
    if any(not re.fullmatch(r'\d{1,3}:\d{2}:\d{2}(?:[.,]\d+)?', row.get('time', '')) for row in rows):
        raise ExportError('部分字幕缺少时间戳，页面结构可能变化；未输出不完整文件。')
    times = [sum(float(part.replace(',', '.')) * weight for part, weight in zip(row['time'].split(':'), (3600, 60, 1))) for row in rows]
    if times != sorted(times):
        raise ExportError('字幕时间顺序异常，请清除搜索筛选后重试。')
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_name(data.get('title', '课程录播')) + '_中文字幕'
    index = 1
    while True:
        suffix = '' if index == 1 else f'_{index}'
        path = output_dir / (stem + suffix + '.txt')
        try:
            stream = path.open('x', encoding='utf-8-sig', newline='\n')
            break
        except FileExistsError:
            index += 1
    exported_data = dict(data, url=sanitized_source_url(data.get('url', '')))
    header = [data.get('title', '课程录播'), '来源：' + exported_data['url'],
              '说明：网站原始语音识别字幕；保留识别错误、重复语句及中英混合内容。',
              f'条数：{len(rows)}', '']
    content = '\n'.join(header + [f"[{row['time']}] {row['text']}" for row in rows]) + '\n'
    with stream:
        stream.write(content)
    if path.read_text(encoding='utf-8-sig') != content:
        raise ExportError('文件回读校验失败。')
    if raw_json:
        path.with_suffix('.json').write_text(json.dumps(exported_data, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def choose_course(pages, requested_url):
    if len(pages) == 1:
        return pages[0]
    exact = [p for p in pages if p.get('url') == requested_url]
    if len(exact) == 1:
        return exact[0]
    courses = [p for p in pages if urlsplit(p.get('url', '')).hostname == urlsplit(requested_url).hostname
               and 'livingroom' in urlsplit(p.get('url', '')).path]
    if len(courses) == 1:
        return courses[0]
    print('\n独立浏览器中有多个标签页，请选择录播页：')
    for number, page in enumerate(pages, 1):
        print(f"  {number}. {page.get('title', '未命名页面')}")
    answer = input('输入序号（q 退出）：').strip()
    if answer.lower() == 'q':
        raise KeyboardInterrupt
    try:
        number = int(answer)
        if not 1 <= number <= len(pages):
            raise ValueError
        return pages[number - 1]
    except ValueError:
        raise ExportError('无效的标签页序号，请重试。')


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='仅适用于复旦大学 icourse 网站（icourse.fudan.edu.cn）的本地字幕提取工具，无第三方 Python 依赖。')
    parser.add_argument('--url', help='录播页面网址；省略则交互输入')
    parser.add_argument('--output-dir', default=str(Path(__file__).resolve().parent / '字幕输出'), help='TXT 输出目录')
    parser.add_argument('--browser', help='Edge/Chrome 程序路径')
    parser.add_argument('--json', action='store_true', help='额外保存字幕 JSON 清单')
    args = parser.parse_args(argv)
    requested_url = (args.url or input('请输入课程录播网址：')).strip()
    validate_page_url(requested_url)
    browser = find_browser(args.browser)
    profile = Path(tempfile.mkdtemp(prefix='course-subtitle-browser-'))
    browser_ws = None
    process = None
    print('\n将打开独立浏览器窗口。请在该窗口手动登录，并打开“语音识别”标签。')
    print('请清空字幕搜索框，等待字幕加载完成。校园网或校园 VPN 必须可用。')
    try:
        process, endpoint, browser_ws = launch_browser(browser, profile, requested_url)
        while True:
            answer = input('\n准备好后按回车提取（q 退出）：').strip().lower()
            if answer == 'q':
                break
            try:
                pages = list_pages(endpoint)
                if not pages:
                    raise ExportError('没有可读取的标签页，请保持独立浏览器打开。')
                target = choose_course(pages, requested_url)
                validate_page_url(target.get('url', ''))
                data = extract_page(target)
                if not data['rows']:
                    raise ExportError('未发现字幕。请完成登录，打开录播页的“语音识别”标签，清空搜索并等待加载。')
                print(f"已读取 {len(data['rows'])} 条：{data['rows'][0]['time']} - {data['rows'][-1]['time']}")
                print('请核对末条时间是否覆盖所需课程；如果不完整，请先在网页加载完整字幕。')
                path = save_subtitles(data, args.output_dir, args.json)
                print(f'\n导出成功：{path}\n已回读校验，编码为 UTF-8（含 BOM）。')
                print('提示：本工具不校正网站语音识别错误；仅导出当前页面已加载的字幕。')
                break
            except (ExportError, OSError, ValueError) as error:
                print(f'\n提取未完成：{error}')
                print('可在浏览器修正后再次按回车；没有字幕时不会生成空 TXT。')
    finally:
        if browser_ws:
            try:
                with CDP(browser_ws) as cdp:
                    cdp.call('Browser.close')
            except (OSError, ExportError, ValueError):
                pass
        if process:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                print('独立浏览器仍在运行，请手动关闭该窗口。')
        # Only this invocation's newly created temporary profile is cleaned up.
        try:
            shutil.rmtree(profile)
        except OSError:
            print(f'临时浏览器资料暂未清理（浏览器可能仍在关闭）：{profile}')


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('\n已取消。')
    except (ExportError, OSError, ValueError) as error:
        print(f'\n无法完成：{error}', file=sys.stderr)
        sys.exit(1)
