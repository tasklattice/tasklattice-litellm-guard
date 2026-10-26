"""Controlled business model only; policy decisions come from real Guard."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

BLOCKED = 'RELEASE_FORBIDDEN_MARKER'
SAFE = 'A safe release verification answer.'


class Handler(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'calls': Handler.calls}).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        Handler.calls += 1
        text = BLOCKED if body['model'] == 'unsafe-output' else SAFE
        identity = {'id': 'chatcmpl-' + uuid4().hex, 'created': 1, 'model': body['model']}
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json')
        self.end_headers()
        if body.get('stream'):
            for content, finish in [(text[:8], None), (text[8:], None), ('', 'stop')]:
                chunk = {**identity, 'object': 'chat.completion.chunk', 'choices': [{
                    'index': 0, 'delta': {'content': content}, 'finish_reason': finish}]}
                self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                self.wfile.flush()
            self.wfile.write(b'data: [DONE]\n\n')
        else:
            self.wfile.write(json.dumps({**identity, 'object': 'chat.completion', 'choices': [{
                'index': 0, 'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 5, 'completion_tokens': 5, 'total_tokens': 10}}).encode())


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 5001), Handler).serve_forever()
