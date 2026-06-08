import asyncio
import json
import os
from http.server import BaseHTTPRequestHandler

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from bot import process_update


class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
            asyncio.run(process_update(data))
        except Exception as e:
            print(f"[ERROR] {e}")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"KOL Bot is running!")
