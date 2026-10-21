"""Single-process web server for the whole site — for local preview.
Production is served by Vercel (see vercel.json).

Routes:
  /            → blog/index.html (book landing page)
  /news/       → blog/news/index.html (commentary site)
  /原初种族.pdf → PDF download
  /api/health  → 'ok'

Local:  python3 serve.py            (defaults to :8765, or $PORT)
"""
import os
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
os.chdir(ROOT)


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        return super().do_GET()

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    print(f"serving {ROOT} on 0.0.0.0:{port}", file=sys.stderr)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
