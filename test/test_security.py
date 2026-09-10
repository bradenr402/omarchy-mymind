#!/usr/bin/env python3
"""Security regression tests for the mymind CLI and setup scripts.

    python3 test/test_security.py

Runs entirely offline against throwaway HOME/XDG dirs and a local HTTP server.
Covers the marketplace review blockers:

  1. mymind-setup never puts the secret in argv (checked via a python3 shim
     that records its argv); note bodies are accepted on stdin.
  2. bin/setup refuses to replace foreign / relative / dangling symlinks in
     ~/.local/bin, and --uninstall leaves them untouched.
  3. bin/mymind bounds every network body (JSON, error, image), rejects
     oversized / chunked / endless responses, and refuses thumbnail redirects
     to off-domain, loopback, non-https or userinfo URLs.
"""
import base64, hashlib, hmac, json, os, shutil, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
SECRET_B64 = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# --------------------------------------------------------------------------- #
# Fake API with hostile behaviours
# --------------------------------------------------------------------------- #

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==")
BIG = 9 * 1024 * 1024  # > MAX_JSON_BODY / MAX_IMAGE_BODY (8 MiB)


class Hostile(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    origin = ""  # set at startup

    def log_message(self, *a):
        pass

    def _hdr(self, status, ctype="application/json", length=None, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _json(self, status, obj, ctype="application/json"):
        data = json.dumps(obj).encode()
        self._hdr(status, ctype, len(data))
        self.wfile.write(data)

    def _chunked(self, status, ctype, total, stop=None):
        """Chunked body of `total` bytes (or endless when total is None)."""
        self._hdr(status, ctype, None, {"Transfer-Encoding": "chunked"})
        sent = 0
        chunk = b"A" * 65536
        try:
            while total is None or sent < total:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
                sent += len(chunk)
                if stop is not None and sent >= stop:
                    break
            if total is not None:
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def do_GET(self):
        p = self.path.split("?")[0]
        # --- normal API ---
        if p == "/objects":
            return self._json(200, [{"id": "ok1", "title": "fine"}])
        if p == "/search":
            return self._json(200, {"matches": [{"id": "ok1", "score": 1}]})
        # --- oversized JSON with honest Content-Length ---
        if p == "/big-cl/objects" or p == "/objects/big-cl":
            self._hdr(200, "application/json", BIG)
            try:
                self.wfile.write(b"[" + b"1," * (BIG // 2 - 1) + b"1]")
            except (BrokenPipeError, ConnectionResetError):
                pass  # expected: client refuses on Content-Length and hangs up
            return
        # --- oversized JSON, chunked (no Content-Length) ---
        if p == "/objects/big-chunked":
            return self._chunked(200, "application/json", BIG)
        # --- endless chunked ---
        if p == "/objects/endless":
            return self._chunked(200, "application/json", None, stop=64 * 1024 * 1024)
        # --- oversized error body ---
        if p == "/objects/big-error":
            return self._chunked(500, "application/problem+json", 1024 * 1024)
        # --- thumbnails ---
        if p == "/objects/direct/thumbnail":
            self._hdr(200, "image/png", len(PNG)); self.wfile.write(PNG); return
        if p == "/objects/bigimg/thumbnail":
            return self._chunked(200, "image/png", BIG)
        if p == "/objects/redir-same/thumbnail":
            return self._hdr(302, extra={"Location": self.origin + "/cdn/ok.png"}, length=0)
        if p == "/cdn/ok.png":
            self._hdr(200, "image/png", len(PNG)); self.wfile.write(PNG); return
        if p == "/objects/redir-bigimg/thumbnail":
            return self._hdr(302, extra={"Location": self.origin + "/cdn/big.png"}, length=0)
        if p == "/cdn/big.png":
            return self._chunked(200, "image/png", BIG)
        if p == "/objects/redir-offdomain/thumbnail":
            return self._hdr(302, extra={"Location": "https://evil.example.com/x.png"}, length=0)
        if p == "/objects/redir-lookalike/thumbnail":
            return self._hdr(302, extra={"Location": "https://mymind.com.evil.example/x.png"}, length=0)
        if p == "/objects/redir-loopback/thumbnail":
            # different port on loopback = not the API origin
            return self._hdr(302, extra={"Location": "http://127.0.0.1:1/x.png"}, length=0)
        if p == "/objects/redir-http/thumbnail":
            return self._hdr(302, extra={"Location": "http://cdn.mymind.com/x.png"}, length=0)
        if p == "/objects/redir-file/thumbnail":
            return self._hdr(302, extra={"Location": "file:///etc/passwd"}, length=0)
        if p == "/objects/redir-userinfo/thumbnail":
            return self._hdr(302, extra={"Location": "https://cdn.mymind.com@evil.example/x.png"}, length=0)
        if p == "/objects/redir-hop/thumbnail":
            # first hop fine (same origin), second hop escapes
            return self._hdr(302, extra={"Location": self.origin + "/cdn/hop2"}, length=0)
        if p == "/cdn/hop2":
            return self._hdr(302, extra={"Location": "https://evil.example.com/x.png"}, length=0)
        if p == "/objects/redir-loop/thumbnail":
            return self._hdr(302, extra={"Location": self.origin + "/objects/redir-loop/thumbnail"}, length=0)
        if p == "/objects/redir-port/thumbnail":
            return self._hdr(302, extra={"Location": "https://cdn.mymind.com:8443/x.png"}, length=0)
        if p == "/objects/redir-badport/thumbnail":
            return self._hdr(302, extra={"Location": "https://cdn.mymind.com:abc/x.png"}, length=0)
        if p == "/objects/redir-nonascii/thumbnail":
            self.send_response(302); self.send_header("Content-Length", "0")
            # raw non-ASCII bytes in the header value
            self.wfile.write(b"Location: https://cdn.mymind.com/\xff\xfe.png\r\n\r\n")
            self.close_connection = True
            return
        if p == "/objects/array-error":
            return self._json(500, [1, 2, 3], "application/problem+json")
        if p == "/objects/scalar-body":
            return self._json(200, 42)
        if p == "/objects/weird-shapes":
            return self._json(200, {"id": "w", "source": "str", "tags": "x", "blob": [], "content": 7, "mainEntity": None})
        if p == "/objects/truncimg/thumbnail":
            self._hdr(200, "image/png", len(PNG) + 100)
            self.wfile.write(PNG); self.close_connection = True
            return
        if p == "/objects/truncated":
            # Content-Length lies: promises 1000 bytes, sends a JSON prefix, closes.
            self._hdr(200, "application/json", 1000)
            self.wfile.write(b'{"id":"x", "title": "cut of')
            self.close_connection = True
            return
        if p == "/objects/slowdrip":
            # 1 byte every 0.5s, forever: must hit the wall-clock deadline
            self._hdr(200, "application/json", None, {"Transfer-Encoding": "chunked"})
            try:
                while True:
                    self.wfile.write(b"1\r\n[\r\n"); self.wfile.flush(); time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
            return
        return self._json(404, {"type": "NotFound", "status": 404, "detail": p}, "application/problem+json")


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def main():
    tmp = tempfile.mkdtemp(prefix="mymind-test-")
    home = os.path.join(tmp, "home"); os.makedirs(os.path.join(home, ".local", "bin"))
    cfg = os.path.join(home, ".config"); os.makedirs(os.path.join(cfg, "mymind"), mode=0o700)
    creds = os.path.join(cfg, "mymind", "credentials.json")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Hostile)
    port = srv.server_address[1]
    Hostile.origin = f"http://127.0.0.1:{port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    env = dict(os.environ, HOME=home, XDG_CONFIG_HOME=cfg,
               XDG_STATE_HOME=os.path.join(home, ".local/state"),
               XDG_CACHE_HOME=os.path.join(home, ".cache"),
               MYMIND_CREDENTIALS=creds, MYMIND_API_URL=Hostile.origin)

    # ---------------- 1. secret never in argv ----------------
    print("1. mymind-setup: secret handling")
    shim = os.path.join(tmp, "shim"); os.makedirs(shim)
    argv_log = os.path.join(tmp, "argv.log")
    real_py = shutil.which("python3")
    with open(os.path.join(shim, "python3"), "w") as fh:
        fh.write(f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{argv_log}"\nexec {real_py} "$@"\n')
    os.chmod(os.path.join(shim, "python3"), 0o755)
    # Fake gum first on PATH: `input` echoes a line from stdin, `confirm`
    # says no, `style` prints. Keeps the scripts non-interactive.
    with open(os.path.join(shim, "gum"), "w") as fh:
        fh.write('#!/bin/sh\ncase "$1" in input) IFS= read -r l; printf "%s\\n" "$l";; confirm) exit 1;; *) shift; printf "%s\\n" "$*";; esac\n')
    os.chmod(os.path.join(shim, "gum"), 0o755)
    path = shim + ":" + os.environ["PATH"]
    # `mymind check` inside setup hits our fake server, which answers /objects.
    p = subprocess.run([os.path.join(BIN, "mymind-setup")], input=f"kid123\n{SECRET_B64}\n",
                       capture_output=True, text=True, env=dict(env, PATH=path))
    check("setup exits 0", p.returncode == 0, p.stderr.strip()[-300:])
    logged = open(argv_log).read() if os.path.exists(argv_log) else ""
    check("secret absent from every python3 argv", SECRET_B64 not in logged, logged[:200])
    check("kid present in argv (sanity: shim works)", "kid123" in logged)
    saved = json.load(open(creds))
    check("credentials file has correct secret", saved.get("secret") == SECRET_B64 and saved.get("kid") == "kid123")
    check("credentials file mode 0600", (os.stat(creds).st_mode & 0o777) == 0o600)

    def cli(*args, stdin=None, extra_env=None):
        return subprocess.run([os.path.join(BIN, "mymind"), *args], input=stdin, capture_output=True,
                              text=True, env=dict(env, **(extra_env or {})), timeout=120)

    def problem(p):
        try:
            return json.loads(p.stdout.strip().splitlines()[-1])
        except Exception:
            return {}

    # ---------------- 2. symlink safety ----------------
    print("2. bin/setup: symlink handling")
    stubs = os.path.join(tmp, "stubs"); os.makedirs(stubs)
    for s in ("hyprctl", "omarchy"):
        with open(os.path.join(stubs, s), "w") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        os.chmod(os.path.join(stubs, s), 0o755)
    lbin = os.path.join(home, ".local", "bin")
    foreign_abs = os.path.join(tmp, "other-tool"); open(foreign_abs, "w").close()
    os.symlink(foreign_abs, os.path.join(lbin, "mymind"))                # foreign absolute
    os.symlink("../share/other/mymind-setup", os.path.join(lbin, "mymind-setup"))  # relative (also dangling)
    setup_env = dict(env, PATH=stubs + ":" + path)
    p = subprocess.run([os.path.join(BIN, "setup"), "--yes"], capture_output=True, text=True, env=setup_env)
    check("setup --yes exits 0 with foreign links present", p.returncode == 0, p.stderr[-300:])
    check("foreign absolute link untouched", os.readlink(os.path.join(lbin, "mymind")) == foreign_abs)
    check("relative/dangling link untouched", os.readlink(os.path.join(lbin, "mymind-setup")) == "../share/other/mymind-setup")
    p = subprocess.run([os.path.join(BIN, "setup"), "--uninstall"], capture_output=True, text=True, env=setup_env)
    check("uninstall exits 0", p.returncode == 0, p.stderr[-300:])
    check("uninstall leaves foreign absolute link", os.path.islink(os.path.join(lbin, "mymind")))
    check("uninstall leaves relative/dangling link", os.path.islink(os.path.join(lbin, "mymind-setup")))
    # dangling absolute link
    os.remove(os.path.join(lbin, "mymind")); os.remove(os.path.join(lbin, "mymind-setup"))
    os.symlink(os.path.join(tmp, "does-not-exist"), os.path.join(lbin, "mymind"))
    subprocess.run([os.path.join(BIN, "setup"), "--yes"], capture_output=True, text=True, env=setup_env)
    check("dangling absolute link untouched", os.readlink(os.path.join(lbin, "mymind")) == os.path.join(tmp, "does-not-exist"))
    check("free name gets linked", os.readlink(os.path.join(lbin, "mymind-setup")) == os.path.join(BIN, "mymind-setup"))
    os.remove(os.path.join(lbin, "mymind")); os.remove(os.path.join(lbin, "mymind-setup"))
    # regular file (not a link) with our name
    with open(os.path.join(lbin, "mymind"), "w") as fh: fh.write("#!/bin/sh\necho other\n")
    subprocess.run([os.path.join(BIN, "setup"), "--yes"], capture_output=True, text=True, env=setup_env)
    check("regular file untouched on install", not os.path.islink(os.path.join(lbin, "mymind")) and open(os.path.join(lbin, "mymind")).read().endswith("echo other\n"))
    subprocess.run([os.path.join(BIN, "setup"), "--uninstall"], capture_output=True, text=True, env=setup_env)
    check("regular file untouched on uninstall", os.path.isfile(os.path.join(lbin, "mymind")) and not os.path.islink(os.path.join(lbin, "mymind")))
    os.remove(os.path.join(lbin, "mymind"))
    subprocess.run([os.path.join(BIN, "setup"), "--yes"], capture_output=True, text=True, env=setup_env)
    check("our links created when free", os.readlink(os.path.join(lbin, "mymind")) == os.path.join(BIN, "mymind"))
    p = subprocess.run([os.path.join(BIN, "setup"), "--uninstall"], capture_output=True, text=True, env=setup_env)
    check("uninstall removes exactly our links", not os.path.lexists(os.path.join(lbin, "mymind")) and not os.path.lexists(os.path.join(lbin, "mymind-setup")))

    # ---------------- 1b. note body via stdin ----------------
    print("1b. mymind: note bodies over stdin")
    # save-note POST isn't handled by the hostile server, so we just check it reads stdin and gets past validation.
    p = cli("save-note", stdin="   \n")
    check("empty stdin note refused", p.returncode != 0 and problem(p).get("type") == "BadRequest")
    p = cli("add-note", "ok1", stdin="")
    check("empty stdin add-note refused", p.returncode != 0 and problem(p).get("type") == "BadRequest")

    # ---------------- 3. bounded bodies / redirects ----------------
    print("3. mymind: bounded reads")
    p = cli("check")
    check("normal API call works", p.returncode == 0 and problem(p).get("ok") is True, p.stderr[-200:])

    p = cli("get", "big-cl")
    check("oversized JSON w/ Content-Length rejected before read", problem(p).get("type") == "ResponseTooLarge", p.stdout[-200:])
    p = cli("get", "big-chunked")
    check("oversized chunked JSON rejected", problem(p).get("type") == "ResponseTooLarge", p.stdout[-200:])
    t = time.time(); p = cli("get", "endless")
    check("endless chunked JSON rejected (bounded time)", problem(p).get("type") == "ResponseTooLarge" and time.time() - t < 60, p.stdout[-200:])
    p = cli("get", "big-error")
    check("oversized error body rejected", problem(p).get("type") == "ResponseTooLarge", p.stdout[-200:])

    print("3. mymind: thumbnails")
    cache = os.path.join(home, ".cache", "omarchy-mymind", "thumbnails")
    os.makedirs(cache, exist_ok=True)
    p = cli("thumbnail", "direct")
    r = problem(p)
    check("direct thumbnail ok + cached", p.returncode == 0 and os.path.isfile(r.get("path", "")) and open(r["path"], "rb").read() == PNG)
    p = cli("thumbnail", "bigimg")
    check("oversized direct image rejected", problem(p).get("type") == "ResponseTooLarge", p.stdout[-200:])
    p = cli("thumbnail", "redir-same")
    check("same-origin redirect followed", p.returncode == 0 and open(problem(p)["path"], "rb").read() == PNG, p.stdout[-200:])
    p = cli("thumbnail", "redir-bigimg")
    check("oversized redirected image rejected", problem(p).get("type") == "ResponseTooLarge", p.stdout[-200:])
    for name in ("offdomain", "lookalike", "loopback", "http", "file", "userinfo", "hop"):
        p = cli("thumbnail", f"redir-{name}")
        check(f"redirect to {name} refused", p.returncode != 0 and problem(p).get("type") == "BadResponse", p.stdout[-200:])
    p = cli("thumbnail", "redir-loop")
    check("redirect loop bounded", p.returncode != 0 and problem(p).get("type") == "BadResponse", p.stdout[-200:])
    p = cli("thumbnail", "redir-port")
    check("redirect to allow-listed host on odd port refused", problem(p).get("type") == "BadResponse", p.stdout[-200:])
    p = cli("thumbnail", "redir-badport")
    check("redirect with non-numeric port refused cleanly (no traceback)",
          problem(p).get("type") == "BadResponse" and "Traceback" not in p.stderr, p.stderr[-200:])
    p = cli("get", "truncated")
    check("lying Content-Length / truncated body -> clean error, no traceback",
          problem(p).get("type") in ("Network", "BadResponse") and "Traceback" not in p.stderr, p.stderr[-200:] + p.stdout[-200:])
    p = cli("thumbnail", "redir-nonascii")
    check("non-ASCII Location refused cleanly", problem(p).get("type") in ("BadResponse", "Network") and "Traceback" not in p.stderr, p.stderr[-200:])
    p = cli("get", "array-error")
    check("JSON-array error body -> clean Http500", problem(p).get("type") == "Http500" and "Traceback" not in p.stderr, p.stderr[-200:])
    p = cli("get", "scalar-body")
    check("scalar JSON body -> BadResponse", problem(p).get("type") == "BadResponse" and "Traceback" not in p.stderr, p.stderr[-200:])
    p = cli("get", "weird-shapes")
    check("wrong-shaped object fields tolerated", p.returncode == 0 and problem(p).get("ok") is True, p.stderr[-200:] + p.stdout[-200:])
    n_before = len(os.listdir(cache))
    p = cli("thumbnail", "truncimg")
    check("truncated image not cached", problem(p).get("type") == "Network" and len(os.listdir(cache)) == n_before, p.stdout[-200:])
    t = time.time(); p = cli("get", "slowdrip", extra_env={"MYMIND_BODY_DEADLINE": "2"})
    check("slow-drip body hits wall-clock deadline",
          problem(p).get("type") == "Network" and time.time() - t < 10, f"{problem(p)} in {time.time()-t:.1f}s")
    leftovers = [f for f in os.listdir(cache) if f.endswith(".tmp")]
    check("no partial cache files left", not leftovers, str(leftovers))
    check("only the 2 good thumbnails cached", len(os.listdir(cache)) == 2, str(os.listdir(cache)))

    # allow-list override for mock CDNs
    p = cli("thumbnail", "redir-offdomain", extra_env={"MYMIND_THUMBNAIL_HOSTS": "evil.example.com"})
    check("MYMIND_THUMBNAIL_HOSTS override reaches validation (network error, not BadResponse)",
          problem(p).get("type") == "Network", p.stdout[-200:])

    srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS)); return 1
    print("all passed"); return 0


if __name__ == "__main__":
    sys.exit(main())
