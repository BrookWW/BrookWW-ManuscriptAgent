"""Loopback API authentication, validation and single-job lifecycle."""
import json
from pathlib import Path
import sys
import threading
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gui


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.desktop = gui.Desktop()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), gui.handler_for(self.desktop, 'test-token'))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, data=None, auth=True, origin=True, language=None):
        headers = {}
        if language:
            headers['X-Manuscript-Language'] = language
        if auth:
            headers['Cookie'] = 'manuscript_session=test-token'
        if origin:
            headers['Origin'] = self.url
        body = json.dumps(data).encode() if data is not None else None
        return urlopen(Request(self.url + path, data=body, headers=headers))

    def test_unauthenticated_and_cross_origin_mutation_rejected(self):
        for auth, origin in [(False, True), (True, False)]:
            with self.assertRaises(HTTPError) as caught:
                self.request('/api/cancel', {}, auth=auth, origin=origin)
            self.assertEqual(caught.exception.code, 403)

    def test_authenticated_state_and_bad_configuration(self):
        with self.request('/api/state') as response:
            self.assertIsNone(json.load(response)['job'])
        with self.assertRaises(HTTPError) as caught:
            self.request('/api/start', {'stages': []})
        self.assertEqual(caught.exception.code, 400)

    def test_http_start_runs_real_scheduler_with_synthetic_cli(self):
        from test_workflow import FAKE
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / 'source'
            source.mkdir()
            (source / 'main.tex').write_text('Synthetic manuscript')
            cli = root / 'fake.py'
            cli.write_text(FAKE)
            output = root / 'output'
            config = dict(input=str(source / 'main.tex'), model='fixture',
                          codex=sys.executable, output=str(output),
                          stages=[dict(mode='segmented', count=2), dict(mode='full', count=1)])
            with patch.object(gui.workflow, '_CLI', cli):
                with self.request('/api/start', config) as response:
                    self.assertIn(json.load(response)['status'], ('preparing', 'running'))
                self.desktop.worker.join(10)
                self.assertFalse(self.desktop.worker.is_alive())
                with self.request('/api/state') as response:
                    result = json.load(response)['job']
            self.assertEqual(result['status'], 'completed', result.get('error'))
            self.assertEqual(len(result['stages'][0]['runs']), 2)
            self.assertEqual(len(result['stages'][1]['runs']), 1)
            self.assertTrue(Path(result['report']).is_file())
            self.assertEqual((source / 'main.tex').read_text(), 'Synthetic manuscript')
            final = Path(result['last_revision']) / result['last_entry']
            self.assertEqual(final.read_text().count('% reviewed'), 3)

    def test_job_lock_and_cancel(self):
        gate = threading.Event()
        def run(config, output, update, cancel):
            gate.wait(3)
            return {'status': 'cancelled', 'output': str(output)}
        with patch.object(gui.workflow, 'validate_config', return_value={}), \
             patch.object(gui.workflow, 'run_workflow', side_effect=run):
            self.desktop.start({})
            try:
                with self.assertRaises(ValueError):
                    self.desktop.start({})
                self.desktop.stop()
                self.assertTrue(self.desktop.cancel.is_set())
            finally:
                gate.set()
                self.desktop.worker.join()
        self.assertEqual(self.desktop.snapshot()['job']['status'], 'cancelled')

    def test_validation_and_operation_errors_follow_request_language(self):
        for locale, expected in [('en', 'Choose a manuscript .tex file.'),
                                 ('zh-CN', '请选择论文入口 .tex 文件。')]:
            with self.subTest(locale=locale), self.assertRaises(HTTPError) as caught:
                self.request('/api/start', {'stages': []}, language=locale)
            self.assertEqual(json.load(caught.exception)['error'], expected)
        with self.assertRaises(HTTPError) as caught:
            self.request('/api/open', {'kind': 'report'}, language='zh-CN')
        self.assertEqual(json.load(caught.exception)['error'], '尚无运行结果。')

    def test_static_scripts_require_authentication_and_use_exact_paths(self):
        for path in ['/ui/app.js', '/ui/i18n.js']:
            with self.request(path) as response:
                self.assertEqual(response.headers.get_content_type(), 'text/javascript')
                self.assertTrue(response.read())
            with self.assertRaises(HTTPError) as caught:
                self.request(path, auth=False)
            self.assertEqual(caught.exception.code, 403)
        for path in ['/ui/../gui.py', '/ui/missing.js', '/gui.py']:
            with self.assertRaises(HTTPError) as caught:
                self.request(path)
            self.assertEqual(caught.exception.code, 404)

    def test_native_picker_receives_ui_language(self):
        with patch.object(gui, 'native_pick', return_value='/tmp/manuscript.tex') as picker:
            with self.request('/api/pick', {'kind': 'file'}, language='zh-CN') as response:
                self.assertEqual(json.load(response)['path'], '/tmp/manuscript.tex')
            picker.assert_called_once_with('file', 'zh-CN')

    def test_configuration_failure_returns_localized_json(self):
        with patch.object(gui.manuscript, 'local_defaults', side_effect=gui.manuscript.AuditError('Unsupported provider')):
            with self.assertRaises(HTTPError) as caught:
                self.request('/api/state', language='zh-CN')
            self.assertEqual(caught.exception.code, 400)
            self.assertIn('无法读取本机 Codex 配置', json.load(caught.exception)['error'])

    def test_start_uses_request_language_and_does_not_pass_it_to_paper(self):
        with patch.object(self.desktop, 'start', return_value={'status': 'preparing'}) as start:
            with self.request('/api/start', {'language': 'en'}, language='zh-CN'):
                pass
            self.assertEqual(start.call_args.args[0]['language'], 'zh-CN')


if __name__ == '__main__':
    unittest.main()
