"""Tiny static server for news/index.html.

For local preview (default :8766, or $PORT). Production is served by Vercel.
Routes:
  /            → index.html
  /api/health  → 'ok'
"""
import os
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).parent
os.chdir(ROOT)


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if self.path in ("/", ""):
            self.path = "/index.html"
        return super().do_GET()

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8766"))
    print(f"serving {ROOT} on 0.0.0.0:{port}", file=sys.stderr)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
