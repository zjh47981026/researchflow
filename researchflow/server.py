"""Local-only HTTP application. One bounded worker; durable jobs and graph state."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
import threading
from urllib.parse import urlsplit
import uuid

from .engine import Engine
from .models import ReviewDecision
from .sources import validate_url

STATIC = Path(__file__).parent / 'static'

class Application:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.engine = Engine(directory / 'checkpoints.sqlite')
        self.db = sqlite3.connect(directory / 'jobs.sqlite', check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, question TEXT, error TEXT DEFAULT "")')
        self.db.commit()
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.busy = threading.BoundedSemaphore(1)
        self.token = secrets.token_urlsafe(32)
        self._recover_saved()

    def _recover_saved(self):
        pending = []
        for job in self.jobs():
            if job['error']:
                continue
            try:
                state = self.engine.snapshot(job['id'])
                if state.get('status') not in ('complete', 'cancelled', 'failed', 'awaiting_review'):
                    pending.append(job['id'])
            except KeyError:
                with self.lock:
                    self.db.execute('UPDATE jobs SET error=? WHERE id=?', ('The process stopped before its first checkpoint. Start a new research run.',job['id']))
                    self.db.commit()
        if pending:
            self.busy.acquire()
            def recover_all():
                try:
                    for identifier in pending:
                        try:
                            self.engine.recover(identifier)
                        except Exception:
                            with self.lock:
                                self.db.execute('UPDATE jobs SET error=? WHERE id=?', ('Saved workflow could not recover. Start a new research run.',identifier))
                                self.db.commit()
                finally:
                    self.busy.release()
            self.pool.submit(recover_all)

    def jobs(self):
        with self.lock:
            rows = self.db.execute('SELECT id,question,error FROM jobs ORDER BY rowid DESC LIMIT 100').fetchall()
        return [{'id': row[0], 'question': row[1], 'error': row[2]} for row in rows]

    def snapshot(self, identifier):
        identifier = str(uuid.UUID(identifier))
        with self.lock:
            row = self.db.execute('SELECT question,error FROM jobs WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise KeyError('Research run not found')
        try:
            result = self.engine.snapshot(identifier)
        except KeyError:
            result = {}
        if not result or not result.get('question'):
            result = {'id': identifier, 'question': row[0], 'status': 'starting', 'events': []}
        result['id'] = identifier
        if row[1]:
            result['status'] = 'failed'
            result['errors'] = [row[1]]
        # Full downloaded text is retained locally in checkpoints, not sent to the browser.
        result['sources'] = [{k:v for k,v in source.items() if k != 'text'} for source in result.get('sources', [])]
        return result

    def submit(self, payload):
        question = payload.get('question')
        urls = payload.get('urls')
        mode = payload.get('mode', 'demo')
        if not isinstance(question, str) or not 10 <= len(question.strip()) <= 500:
            raise ValueError('Enter a research question between 10 and 500 characters.')
        if mode not in ('demo', 'live') or not isinstance(urls, list) or not 2 <= len(urls) <= 6 or any(not isinstance(u,str) or len(u)>2000 for u in urls):
            raise ValueError('Choose a mode and provide two to six source URLs.')
        urls = [validate_url(u) for u in urls]
        if len({u.rstrip('/') for u in urls}) != len(urls):
            raise ValueError('Choose distinct source URLs.')
        if not self.busy.acquire(blocking=False):
            raise ValueError('Another run is working. Wait for its review step before starting another.')
        identifier = str(uuid.uuid4())
        with self.lock:
            if self.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] >= 100:
                self.busy.release()
                raise ValueError('Local storage has reached 100 runs. Start with a new data directory.')
            self.db.execute('INSERT INTO jobs(id,question) VALUES (?,?)', (identifier,question.strip()))
            self.db.commit()
        self.pool.submit(self._run, identifier, lambda: self.engine.start(question.strip(), urls, mode=mode, thread_id=identifier))
        return identifier

    def resume(self, identifier, decision):
        state = self.snapshot(identifier)
        if state['status'] != 'awaiting_review':
            raise ValueError('This run is not waiting for review.')
        decision = ReviewDecision.model_validate(decision).model_dump()
        if not self.busy.acquire(blocking=False):
            raise ValueError('Another run is working. Try again when it pauses.')
        try:
            if self.snapshot(identifier)['status'] != 'awaiting_review':
                raise ValueError('This run is not waiting for review.')
            self.pool.submit(self._run, identifier, lambda: self.engine.resume(identifier,decision))
        except Exception:
            self.busy.release()
            raise

    def _run(self, identifier, action):
        try:
            action()
        except Exception as exc:
            with self.lock:
                self.db.execute('UPDATE jobs SET error=? WHERE id=?', ('The workflow could not finish: '+str(exc)[:350],identifier))
                self.db.commit()
        finally:
            self.busy.release()

    def close(self):
        self.pool.shutdown(wait=True)
        self.engine.close()
        self.db.close()


def make_handler(app, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, content, kind='application/json'):
            data = json.dumps(content).encode() if kind == 'application/json' else content
            self.send_response(code)
            self.send_header('Content-Type',kind + '; charset=utf-8')
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def allowed_host(self):
            return self.headers.get('Host') in (f'127.0.0.1:{port}', f'localhost:{port}')

        def do_GET(self):
            if not self.allowed_host():
                return self.reply(403, {'error':'Invalid host'})
            path = urlsplit(self.path).path
            try:
                if path == '/api/config':
                    return self.reply(200, {'token':app.token})
                if path == '/api/runs':
                    return self.reply(200,app.jobs())
                if path.startswith('/api/runs/'):
                    return self.reply(200,app.snapshot(path.rsplit('/',1)[1]))
                files = {'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
                if path not in files:
                    return self.reply(404,{'error':'Not found'})
                kinds = {'index.html':'text/html','app.js':'text/javascript','style.css':'text/css'}
                name = files[path]
                return self.reply(200,(STATIC/name).read_bytes(),kinds[name])
            except (KeyError,ValueError):
                self.reply(404,{'error':'Run not found'})

        def do_POST(self):
            origin = self.headers.get('Origin')
            if not self.allowed_host() or origin not in (None,f'http://127.0.0.1:{port}',f'http://localhost:{port}') or not secrets.compare_digest(self.headers.get('X-App-Token',''),app.token):
                return self.reply(403,{'error':'Refresh this local page before making changes.'})
            try:
                self.connection.settimeout(5)
                length = int(self.headers.get('Content-Length','0'))
                if not 1 <= length <= 16000 or self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                    raise ValueError('Expected a small JSON request.')
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload,dict):
                    raise ValueError('Expected a JSON object.')
                path = urlsplit(self.path).path
                if path == '/api/runs':
                    return self.reply(202,{'id':app.submit(payload)})
                if path.startswith('/api/runs/') and path.endswith('/review'):
                    identifier = path.split('/')[3]
                    app.resume(identifier,payload)
                    return self.reply(202,{'id':identifier})
                self.reply(404,{'error':'Not found'})
            except (ValueError,KeyError,TimeoutError) as exc:
                self.reply(400,{'error':str(exc)[:350]})
    return Handler


def main():
    parser = argparse.ArgumentParser(description='Run ResearchFlow locally')
    parser.add_argument('--port',type=int,default=8767)
    parser.add_argument('--data-dir',default='.runtime')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Use a port between 1024 and 65535.')
    app = Application(args.data_dir)
    server = ThreadingHTTPServer(('127.0.0.1',args.port),make_handler(app,args.port))
    server.daemon_threads = True
    print(f'ResearchFlow: http://127.0.0.1:{args.port}',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()

if __name__ == '__main__':
    main()
