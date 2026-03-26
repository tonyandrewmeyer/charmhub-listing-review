#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Minimal OpenAI-compatible API server for spread integration tests.

Responds to the endpoints that SnapBackend expects:
  GET  /v3/models          — returns a single fake model
  POST /v3/chat/completions — echoes a canned response

Usage:
  python3 mock-ai-server.py [PORT]   # default port 18326

The server writes its PID to mock-ai-server.pid in the working directory.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18326
MODEL_ID = 'mock-model'


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/v3/models', '/models'):
            body = json.dumps({
                'object': 'list',
                'data': [{'id': MODEL_ID, 'object': 'model'}],
            })
            self._respond(200, body)
        else:
            self._respond(404, '{"error": "not found"}')

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(content_length)
        if self.path in ('/v3/chat/completions', '/chat/completions'):
            # Parse the request to make a semi-intelligent canned response.
            try:
                request = json.loads(raw)
                messages = request.get('messages', [])
                user_msg = ''
                for m in reversed(messages):
                    if m.get('role') == 'user':
                        user_msg = m.get('content', '')
                        break
            except (json.JSONDecodeError, KeyError):
                user_msg = ''

            reply = f'Mock AI response to: {user_msg[:80]}'
            body = json.dumps({
                'id': 'mock-completion',
                'object': 'chat.completion',
                'model': MODEL_ID,
                'choices': [
                    {
                        'index': 0,
                        'message': {'role': 'assistant', 'content': reply},
                        'finish_reason': 'stop',
                    }
                ],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30},
            })
            self._respond(200, body)
        else:
            self._respond(404, '{"error": "not found"}')

    def _respond(self, code, body):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, fmt, *args):
        # Suppress default stderr logging to keep spread output clean.
        pass


if __name__ == '__main__':
    # Write PID so tests can clean up.
    with open('mock-ai-server.pid', 'w') as f:
        f.write(str(os.getpid()))

    server = HTTPServer(('127.0.0.1', PORT), Handler)
    print(f'Mock AI server listening on http://127.0.0.1:{PORT}/v3', flush=True)
    server.serve_forever()
