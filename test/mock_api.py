#!/usr/bin/env python3
"""Tiny fake mymind API for local UI testing. NOT part of the plugin runtime.

    python3 test/mock_api.py 8765
    # then in ~/.config/mymind/credentials.json add "apiUrl": "http://127.0.0.1:8765"

Verifies the HS256 JWT against the secret in the credentials file, echoes
back plausible objects, and simulates rate-limit headers.
"""
import base64, hashlib, hmac, json, os, re, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit, parse_qs

CREDS = os.path.expanduser("~/.config/mymind/credentials.json")
with open(CREDS) as fh:
    C = json.load(fh)
SECRET = C["secret"].strip().replace("-", "+").replace("_", "/")
SECRET = base64.b64decode(SECRET + "=" * (-len(SECRET) % 4))

OBJECTS = {
    "a1B2c3D4e5F6g7H8i9J0k1": {"id": "a1B2c3D4e5F6g7H8i9J0k1", "title": "The Art of Plain Text", "source": {"url": "https://example.com/plain-text"}, "summary": "Plain text is the most portable format.", "tags": [{"name": "writing"}, {"name": "tools"}], "created": "2024-03-01T12:00:00Z", "bumped": "2024-03-01T12:00:00Z"},
    "z9Y8x7W6v5U4t3S2r1Q0p9": {"id": "z9Y8x7W6v5U4t3S2r1Q0p9", "title": "Meeting notes", "content": {"type": "text/markdown", "body": "# Meeting\n- kickoff"}, "tags": [], "created": "2024-03-02T12:00:00Z", "bumped": "2024-03-02T12:00:00Z"},
    "m5N6o7P8q9R0s1T2u3V4w5": {"id": "m5N6o7P8q9R0s1T2u3V4w5", "title": "Sunset", "blob": {"type": "image/png", "path": "x"}, "tags": [{"name": "travel"}], "created": "2024-03-03T12:00:00Z", "bumped": "2024-03-03T12:00:00Z"},
}
SPACES = [{"id": "sP7k4M2n8Q3r5V9x1L6y0T", "name": "Design research", "color": "#e0f2fe"}, {"id": "p2Q3r4S5t6U7v8W9x0Y1z2", "name": "Travel", "color": "#fef3c7"}]
LOG = []


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class H(BaseHTTPRequestHandler):
    def _auth(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return "missing bearer"
        if not self.headers.get("User-Agent"):
            return "missing user-agent"
        h, c, s = auth[7:].split(".")
        expect = hmac.new(SECRET, f"{h}.{c}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expect, b64d(s)):
            return "bad signature"
        claims = json.loads(b64d(c))
        path = urlsplit(self.path).path
        if claims.get("path") != path or claims.get("method") != self.command:
            return f"claims mismatch {claims} vs {self.command} {path}"
        if claims.get("exp", 0) < time.time():
            return "expired"
        return None

    def _send(self, status, body=None, ctype="application/json", extra=None):
        data = b"" if body is None else (json.dumps(body).encode() if not isinstance(body, bytes) else body)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("RateLimit-Policy", '"burst";q=500;w=300, "sustained";q=5000;w=2592000')
        self.send_header("RateLimit", '"burst";r=490;t=120, "sustained";r=4900;t=2500000')
        self.send_header("RateLimit-Cost", "10")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def handle_any(self):
        err = self._auth()
        LOG.append((self.command, self.path))
        print(self.command, self.path, self.headers.get("Content-Type", ""), file=sys.stderr)
        if err:
            return self._send(401, {"type": "Unauthorized", "status": 401, "detail": err}, "application/problem+json")
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        p = u.path
        body = self._body()
        if p == "/objects" and self.command == "GET":
            ids = q.get("id")
            objs = [OBJECTS[i] for i in ids if i in OBJECTS] if ids else list(OBJECTS.values())[: int(q.get("limit", ["10"])[0])]
            return self._send(200, objs)
        if p == "/search":
            return self._send(200, {"matches": [{"id": i, "score": 0.9 - n * 0.1} for n, i in enumerate(OBJECTS)]})
        if p == "/objects" and self.command == "POST":
            ct = self.headers.get("Content-Type", "")
            if ct.startswith("multipart/form-data"):
                m = re.search(rb'name="metadata"\r\nContent-Type: application/json\r\n\r\n(.*?)\r\n--', body, re.S)
                meta = json.loads(m.group(1)) if m else {}
                fn = re.search(rb'filename="([^"]*)"', body)
                title = meta.get("title") or (fn.group(1).decode() if fn else "upload")
                obj = {"id": "NEWimg000000000000000", "title": title, "blob": {"type": "image/png"}}
            else:
                meta = json.loads(body or b"{}")
                if "url" in meta:
                    if meta["url"] == "https://example.com/plain-text":
                        return self._send(200, OBJECTS["a1B2c3D4e5F6g7H8i9J0k1"])
                    obj = {"id": "NEWurl0000000000000000", "title": "Saved: " + meta["url"], "source": {"url": meta["url"]}}
                else:
                    obj = {"id": "NEWnote000000000000000", "title": (meta.get("content", {}).get("body", "note")[:30])}
            obj.update({"tags": meta.get("tags", []), "created": "now", "modified": "now", "bumped": "now"})
            OBJECTS[obj["id"]] = obj
            return self._send(201, obj)
        if p == "/spaces" and self.command == "GET":
            return self._send(200, SPACES)
        if re.match(r"^/objects/[^/]+/thumbnail$", p):
            # 1x1 PNG
            png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==")
            return self._send(200, png, "image/png")
        if re.match(r"^/objects/[^/]+/tags$", p) and self.command == "POST":
            return self._send(200, {})
        if re.match(r"^/objects/[^/]+/notes$", p) and self.command == "POST":
            assert self.headers.get("Content-Type", "").startswith("text/markdown")
            return self._send(201, {"id": "n4K8m2N6p9Q3r5T7v1X0z2"})
        if re.match(r"^/spaces/[^/]+/objects/[^/]+$", p) and self.command == "PUT":
            return self._send(200, {})
        if re.match(r"^/objects/[^/]+$", p) and self.command == "GET":
            oid = p.rsplit("/", 1)[1]
            if oid in OBJECTS:
                return self._send(200, OBJECTS[oid])
            return self._send(404, {"type": "NotFound", "status": 404, "detail": "no"}, "application/problem+json")
        return self._send(404, {"type": "NotFound", "status": 404, "detail": f"unhandled {self.command} {p}"}, "application/problem+json")

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_any

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"mock mymind on http://127.0.0.1:{port}", file=sys.stderr)
    HTTPServer(("127.0.0.1", port), H).serve_forever()
