"""Exercise real loopback HTTP requests and the durable demo graph together."""
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from researchflow.engine import DEMO_URLS
from researchflow.server import Application, make_handler


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.directory.name))
        try:
            self.launch_server()
        except Exception:
            self.app.close()
            self.directory.cleanup()
            raise

    def launch_server(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.app, 0))
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.server.RequestHandlerClass = make_handler(self.app, self.port)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def restart_application(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.app.close()
        self.app = Application(Path(self.directory.name))
        self.launch_server()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.app.close()
        self.directory.cleanup()

    def request(self, path, method='GET', payload=None, headers=None, raw=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        request_headers = {}
        body = raw
        if method == 'POST':
            request_headers.update({'Content-Type': 'application/json', 'X-App-Token': self.app.token,
                                    'Origin': f'http://127.0.0.1:{self.port}'})
            if raw is None:
                body = json.dumps(payload).encode()
        request_headers.update(headers or {})
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            content = response.read()
            data = json.loads(content) if response.getheader('Content-Type', '').startswith('application/json') else content
            return response.status, data, dict(response.getheaders())
        finally:
            connection.close()

    def start(self):
        status, data, _ = self.request('/api/runs', 'POST', {'question': 'How do LangChain and LangGraph compare?', 'urls': DEMO_URLS, 'mode': 'demo'})
        self.assertEqual(status, 202, data)
        return data['id']

    def poll(self, identifier, expected):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            code, state, _ = self.request(f'/api/runs/{identifier}')
            self.assertEqual(code, 200, state)
            if state['status'] == expected:
                return state
            if state['status'] in ('complete', 'cancelled', 'failed'):
                self.fail(f"Workflow ended in unexpected state: {state}")
            time.sleep(0.01)
        self.fail(f'Run did not reach {expected}: {state}')

    def test_config_and_static_security_headers(self):
        status, config, headers = self.request('/api/config')
        self.assertEqual(status, 200)
        self.assertEqual(config['token'], self.app.token)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        status, page, _ = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn(b'ResearchFlow', page)

    def test_start_history_review_and_complete(self):
        identifier = self.start()
        review = self.poll(identifier, 'awaiting_review')
        self.assertGreaterEqual(len(review['claims']), 2)
        self.assertTrue(all('text' not in source for source in review['sources']))
        status, history, _ = self.request('/api/runs')
        self.assertEqual(status, 200)
        self.assertEqual(history[0]['id'], identifier)
        status, _, _ = self.request(f'/api/runs/{identifier}/review', 'POST', {'approved': True, 'outline': ['Framework roles', 'Workflow choices']})
        self.assertEqual(status, 202)
        complete = self.poll(identifier, 'complete')
        self.assertEqual(complete['outline'], ['Framework roles', 'Workflow choices'])
        self.assertIn('[S1]', complete['brief'])
        self.assertIn('[S2]', complete['brief'])
        status, _, _ = self.request(f'/api/runs/{identifier}/review', 'POST', {'approved': True})
        self.assertEqual(status, 400)

    def test_review_rejection_cancels_run(self):
        identifier = self.start()
        self.poll(identifier, 'awaiting_review')
        status, _, _ = self.request(f'/api/runs/{identifier}/review', 'POST', {'approved': False})
        self.assertEqual(status, 202)
        cancelled = self.poll(identifier, 'cancelled')
        self.assertFalse(cancelled['brief'])

    def test_invalid_review_remains_recoverable(self):
        identifier = self.start()
        self.poll(identifier, 'awaiting_review')
        for decision in ({'approved': 'yes'}, {'approved': True, 'outline': ['One heading']}, {'approved': True, 'outline': ['A', 'B\nInjected heading']}, {'approved': True, 'unexpected': 'field'}):
            with self.subTest(decision=decision):
                status, _, _ = self.request(f'/api/runs/{identifier}/review', 'POST', decision)
                self.assertEqual(status, 400)
                state = self.poll(identifier, 'awaiting_review')
                self.assertNotEqual(state['status'], 'failed')
        status, _, _ = self.request(f'/api/runs/{identifier}/review', 'POST', {'approved': True})
        self.assertEqual(status, 202)
        self.poll(identifier, 'complete')

    def test_unknown_and_malformed_ids_return_404(self):
        for identifier in (str(uuid4()), 'not-a-uuid'):
            with self.subTest(identifier=identifier):
                status, _, _ = self.request(f'/api/runs/{identifier}')
                self.assertEqual(status, 404)
        self.assertEqual(self.request('/missing')[0], 404)

    def test_hostile_host_is_denied_on_reads_and_writes(self):
        for method, path in (('GET', '/api/config'), ('GET', '/'), ('POST', '/api/runs')):
            with self.subTest(method=method, path=path):
                status, _, _ = self.request(path, method, {}, headers={'Host': f'evil.example:{self.port}'})
                self.assertEqual(status, 403)

    def test_origin_and_token_protect_mutations(self):
        for headers in ({'Origin': 'https://evil.example'}, {'X-App-Token': ''}, {'X-App-Token': 'incorrect'}, {'Origin': 'null'}):
            with self.subTest(headers=headers):
                status, _, _ = self.request('/api/runs', 'POST', {}, headers=headers)
                self.assertEqual(status, 403)
        self.assertEqual(self.app.jobs(), [])

    def test_body_limits_json_and_content_type(self):
        for raw, headers in ((b'{', {}), (b'[]', {}), (b'{}', {'Content-Type': 'text/plain'}), (b'x' * 16001, {}), (b'', {}), (b'{}', {'Content-Length': '-1'})):
            with self.subTest(raw_size=len(raw), headers=headers):
                status, _, _ = self.request('/api/runs', 'POST', raw=raw, headers=headers)
                self.assertEqual(status, 400)
        self.assertEqual(self.app.jobs(), [])

    def test_invalid_source_urls_rejected_before_job_creation(self):
        for urls in (['http://example.com/a', 'https://example.com/b'], ['https://localhost/a', 'https://example.com/b'], [DEMO_URLS[0], DEMO_URLS[0]], ['https://example.com:8443/a', 'https://example.com/b']):
            with self.subTest(urls=urls):
                status, _, _ = self.request('/api/runs', 'POST', {'question': 'Compare these selected sources.', 'urls': urls, 'mode': 'demo'})
                self.assertEqual(status, 400)
        self.assertEqual(self.app.jobs(), [])

    def test_starting_checkpoint_and_busy_worker(self):
        gate = threading.Event()
        original = self.app.engine.start

        def blocked_start(*args, **kwargs):
            if not gate.wait(timeout=3):
                raise RuntimeError('Test gate timeout')
            return original(*args, **kwargs)

        with patch.object(self.app.engine, 'start', side_effect=blocked_start):
            try:
                identifier = self.start()
                status, state, _ = self.request(f'/api/runs/{identifier}')
                self.assertEqual(status, 200)
                self.assertEqual(state['status'], 'starting')
                status, _, _ = self.request('/api/runs', 'POST', {'question': 'Compare another set of sources.', 'urls': DEMO_URLS, 'mode': 'demo'})
                self.assertEqual(status, 400)
                self.assertEqual(len(self.app.jobs()), 1)
            finally:
                gate.set()
            self.poll(identifier, 'awaiting_review')

    def test_review_survives_application_restart_and_resumes_same_id(self):
        identifier = self.start()
        before = self.poll(identifier, 'awaiting_review')
        old_token = self.app.token
        self.restart_application()
        after = self.poll(identifier, 'awaiting_review')
        self.assertEqual(after['claims'], before['claims'])
        self.assertEqual(after['outline'], before['outline'])
        self.assertNotEqual(self.app.token, old_token)
        self.assertEqual(self.request('/api/runs')[1][0]['id'], identifier)
        status, _, _ = self.request(f'/api/runs/{identifier}/review', 'POST', {'approved': True})
        self.assertEqual(status, 202)
        complete = self.poll(identifier, 'complete')
        self.assertEqual(complete['id'], identifier)
        self.assertEqual(sum(item['stage'] == 'plan' for item in complete['events']), 1)

    def test_saved_pending_node_recovers_automatically_on_startup(self):
        identifier = str(uuid4())
        question = 'How do LangChain and LangGraph compare?'
        initial = {'question': question, 'urls': DEMO_URLS, 'mode': 'demo', 'status': 'planning',
                   'sources': [], 'claims': [], 'candidates': [], 'rounds': 0,
                   'events': [], 'errors': [], 'brief': '', 'outline': []}
        self.app.engine.graph.invoke(initial, self.app.engine._config(identifier), interrupt_before=['gather'])
        with self.app.lock:
            self.app.db.execute('INSERT INTO jobs(id,question) VALUES (?,?)', (identifier, question))
            self.app.db.commit()
        self.assertEqual(self.app.snapshot(identifier)['status'], 'gathering')
        self.restart_application()
        recovered = self.poll(identifier, 'awaiting_review')
        self.assertEqual(recovered['rounds'], 1)
        self.assertEqual(sum(item['stage'] == 'plan' for item in recovered['events']), 1)
        self.assertEqual(sum(item['stage'] == 'gather' for item in recovered['events']), 1)

    def test_missing_first_checkpoint_becomes_actionable_failure(self):
        identifier = str(uuid4())
        with self.app.lock:
            self.app.db.execute('INSERT INTO jobs(id,question) VALUES (?,?)', (identifier, 'Compare these selected sources.'))
            self.app.db.commit()
        self.restart_application()
        status, state, _ = self.request(f'/api/runs/{identifier}')
        self.assertEqual(status, 200)
        self.assertEqual(state['status'], 'failed')
        self.assertIn('first checkpoint', state['errors'][0])

    def test_stale_concurrent_review_does_not_overwrite_completed_run(self):
        identifier = self.start()
        captured = self.poll(identifier, 'awaiting_review')
        lookup_started = threading.Event()
        release_lookup = threading.Event()
        original = self.app.snapshot
        seen = False
        errors = []

        def delayed_snapshot(run_id):
            nonlocal seen
            if threading.current_thread().name == 'stale-review' and not seen:
                seen = True
                lookup_started.set()
                if not release_lookup.wait(timeout=3):
                    raise RuntimeError('Test lookup gate timeout')
                return captured
            return original(run_id)

        def stale_request():
            try:
                self.app.resume(identifier, {'approved': True})
            except ValueError as exc:
                errors.append(str(exc))

        with patch.object(self.app, 'snapshot', side_effect=delayed_snapshot):
            thread = threading.Thread(target=stale_request, name='stale-review')
            thread.start()
            try:
                self.assertTrue(lookup_started.wait(timeout=2))
                status, _, _ = self.request(f'/api/runs/{identifier}/review', 'POST', {'approved': True})
                self.assertEqual(status, 202)
                self.poll(identifier, 'complete')
            finally:
                release_lookup.set()
                thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors, 'The stale review should be rejected before scheduling work')
        # Any accepted stale work must also finish before final-state assertions.
        self.app.pool.submit(lambda: None).result(timeout=2)
        self.assertEqual(self.app.snapshot(identifier)['status'], 'complete')
        self.assertEqual(self.app.jobs()[0]['error'], '')


if __name__ == '__main__':
    unittest.main()
