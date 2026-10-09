#!/usr/bin/env python3
"""Loopback-only desktop web interface; no third-party Python dependencies."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import webbrowser

import manuscript
import workflow
from i18n import LANGUAGES, LocalizedError, error_text, language, text

HERE = Path(__file__).resolve().parent
ACTIVE = {'preparing', 'running', 'planning', 'cancelling'}


class Desktop:
    def __init__(self):
        self.lock = threading.RLock()
        self.job = None
        self.worker = None
        self.cancel = threading.Event()

    def snapshot(self):
        with self.lock:
            model, reasoning = manuscript.local_defaults()
            return {'defaults': {'model': model, 'reasoning': reasoning},
                    'job': copy.deepcopy(self.job)}

    def update(self, status):
        with self.lock:
            self.job = copy.deepcopy(status)

    def start(self, config):
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise LocalizedError('A workflow is already running. Wait for it to finish or cancel it.',
                                     '已有任务正在运行，请等待完成或取消。')
            config = workflow.validate_config(config)
            output = Path(config.get('output') or HERE / 'runs' / (
                time.strftime('workflow-%Y%m%d-%H%M%S-') + secrets.token_hex(4)))
            if output.exists():
                raise LocalizedError('Output directory already exists; choose a new directory.',
                                     '输出目录已存在，请选择一个新的目录名称。')
            self.cancel = threading.Event()
            self.job = {'status': 'preparing', 'config': config, 'output': str(output)}
            def work():
                try:
                    result = workflow.run_workflow(config, output, self.update, self.cancel)
                    self.update(result)
                except BaseException as exc:
                    with self.lock:
                        self.job.update(status='failed', error=error_text(exc, config.get('language')))
            self.worker = threading.Thread(target=work, name='workflow', daemon=False)
            self.worker.start()
            return copy.deepcopy(self.job)

    def stop(self):
        with self.lock:
            if self.worker and self.worker.is_alive():
                self.cancel.set()
                self.job['status'] = 'cancelling'
            return copy.deepcopy(self.job)


def native_pick(kind, locale='en'):
    if sys.platform != 'darwin':
        raise LocalizedError('The native picker requires macOS. You can paste an absolute path instead.',
                             '原生选择器目前支持 macOS；也可直接粘贴绝对路径。')
    prompt = (text(locale, 'Choose a project or output parent folder', '选择项目或输出父目录')
              if kind == 'folder' else text(locale, 'Choose a manuscript TeX file', '选择论文 TeX 文件'))
    script = f'POSIX path of (choose {"folder" if kind == "folder" else "file"} with prompt "{prompt}")'
    result = subprocess.run(['osascript', '-e', 'activate', '-e', script], capture_output=True, text=True)
    if result.returncode:
        if '(-128)' in result.stderr:
            return ''
        raise ValueError(result.stderr.strip() or text(locale, 'Could not open the file picker.', '无法打开文件选择器。'))
    return result.stdout.strip()


def handler_for(desktop, token):
    class Handler(BaseHTTPRequestHandler):
        def locale(self):
            return language(self.headers.get('X-Manuscript-Language'))

        def log_message(self, *_args):
            pass

        def reply(self, code, data, content_type='application/json; charset=utf-8', cookie=False):
            body = json.dumps(data, ensure_ascii=False).encode() if isinstance(data, (dict, list)) else data
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'")
            if cookie:
                self.send_header('Set-Cookie', f'manuscript_session={token}; HttpOnly; SameSite=Strict; Path=/')
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            host = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != host:
                return False
            cookies = self.headers.get('Cookie', '').split(';')
            return any(secrets.compare_digest(c.strip(), f'manuscript_session={token}') for c in cookies)

        def do_GET(self):
            parsed = urlparse(self.path)
            supplied = parse_qs(parsed.query).get('token', [''])[0]
            if parsed.path == '/' and secrets.compare_digest(supplied, token):
                return self.reply(200, (HERE / 'ui/index.html').read_bytes(), 'text/html; charset=utf-8', True)
            if not self.authorized():
                return self.reply(403, {'error': text(self.locale(), 'Open the local interface with the launcher.', '请使用启动器打开本地界面。')})
            if parsed.path == '/':
                return self.reply(200, (HERE / 'ui/index.html').read_bytes(), 'text/html; charset=utf-8')
            if parsed.path in ('/ui/app.js', '/ui/i18n.js'):
                return self.reply(200, (HERE / parsed.path.lstrip('/')).read_bytes(), 'text/javascript; charset=utf-8')
            if parsed.path == '/api/state':
                try:
                    return self.reply(200, desktop.snapshot())
                except (ValueError, OSError, manuscript.AuditError) as exc:
                    return self.reply(400, {'error': text(self.locale(),
                        'Could not read the local Codex configuration: {detail}',
                        '无法读取本机 Codex 配置：{detail}', detail=str(exc))})
            return self.reply(404, {'error': text(self.locale(), 'Not found.', '未找到。')})

        def do_POST(self):
            origin = f'http://127.0.0.1:{self.server.server_port}'
            if not self.authorized() or self.headers.get('Origin') != origin:
                return self.reply(403, {'error': text(self.locale(), 'Invalid request origin.', '请求来源无效。')})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1024 * 1024:
                    raise LocalizedError('Invalid request size.', '请求大小无效。')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise LocalizedError('A JSON object is required.', '需要 JSON 对象。')
                if self.path == '/api/start':
                    data['language'] = self.locale()
                    return self.reply(200, desktop.start(data))
                if self.path == '/api/cancel':
                    return self.reply(200, desktop.stop() or {})
                if self.path == '/api/pick':
                    return self.reply(200, {'path': native_pick(data.get('kind'), self.locale())})
                if self.path == '/api/open':
                    job = desktop.snapshot()['job']
                    if not job:
                        raise LocalizedError('No workflow results yet.', '尚无运行结果。')
                    target = Path(job['output'])
                    if data.get('kind') == 'report':
                        target = Path(job.get('report') or target / 'report.md')
                    elif data.get('kind') != 'folder':
                        raise LocalizedError('Unknown result type.', '未知结果类型。')
                    if not target.exists():
                        raise LocalizedError('The result is not available yet.', '结果尚未生成。')
                    subprocess.run(['open', str(target)], check=True)
                    return self.reply(200, {'ok': True})
                return self.reply(404, {'error': text(self.locale(), 'Not found.', '未找到。')})
            except (ValueError, OSError, subprocess.SubprocessError, manuscript.AuditError) as exc:
                return self.reply(400, {'error': error_text(exc, self.locale())})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--language', choices=LANGUAGES,
                        help='Initial interface language; otherwise use browser preferences')
    args = parser.parse_args()
    desktop, token = Desktop(), secrets.token_urlsafe(32)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(desktop, token))
    server.daemon_threads = True
    url = f'http://127.0.0.1:{server.server_port}/?token={token}'
    if args.language:
        url += '&language=' + args.language
    print(text(args.language, 'ManuscriptAgent local interface: ', 'ManuscriptAgent 本地界面：') + url, flush=True)
    print(text(args.language, 'Closing the browser does not stop the review. Quitting this launcher cancels the current workflow and retains completed revisions.',
               '关闭浏览器不会停止审阅。退出此启动器会取消当前任务并保留已完成稿件。'), flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    def interrupt(_sig, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGHUP, interrupt)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        desktop.stop()
    finally:
        server.server_close()
        if desktop.worker:
            desktop.worker.join()


if __name__ == '__main__':
    main()
