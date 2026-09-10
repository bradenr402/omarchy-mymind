#!/usr/bin/env python3
"""Security regression tests for the omarchy-mymind CLI and setup scripts.

    python3 test/test_security.py

Runs entirely offline against throwaway HOME/XDG dirs and a local HTTP server.
Covers the marketplace review blockers:

  1. Unified setup dispatch, terminal requirements, and secret handling (including
     /proc cmdline/environ); note bodies are accepted on stdin.
  2. Setup refuses to replace foreign / relative / dangling symlinks in
     ~/.local/bin, is idempotent, and deletes credentials only with terminal consent.
  3. bin/omarchy-mymind bounds every network body (JSON, error, image), rejects
     oversized / chunked / endless responses, and refuses thumbnail redirects
     to off-domain, loopback, non-https or userinfo URLs.
"""
import base64, errno, hashlib, hmac, json, os, pty, select, shutil, signal, subprocess, sys, tempfile, termios, threading, time
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


def terminal_run(argv, env, replies=(), timeout=15):
    """Send replies only after prompts; never put a secret in argv/env or echo it.

    A separate session lets timeout cleanup kill the shell and all its helpers.
    Both stdin and stdout are terminals, including with gum command substitution.
    """
    master, slave = pty.openpty()
    attrs = termios.tcgetattr(slave)
    attrs[3] &= ~(termios.ECHO | termios.ECHONL)
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    output = bytearray()
    pending = list(replies)
    consumed = 0
    proc = None
    timed_out = False
    try:
        proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                env=env, start_new_session=True)
        os.close(slave)
        slave = None
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            if not select.select([master], [], [], min(0.1, max(0, deadline - time.monotonic())))[0]:
                if proc.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > 1024 * 1024:
                raise AssertionError("PTY output exceeded 1 MiB")
            if pending:
                prompt, reply = pending[0]
                pos = output.find(prompt.encode(), consumed)
                if pos >= 0:
                    os.write(master, (reply + "\n").encode())
                    consumed = pos + len(prompt.encode())
                    pending.pop(0)
        if timed_out:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=5 if timed_out else max(0.1, deadline - time.monotonic()))
    finally:
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        os.close(master)
        if slave is not None:
            os.close(slave)
    text = output.decode(errors="replace")
    # Do not print the transcript in assertion details: that could disclose a leak.
    check("PTY command completes before deadline", not timed_out)
    check("PTY command consumes expected prompts", not pending)
    check("secret not echoed in terminal transcript", SECRET_B64 not in text)
    return subprocess.CompletedProcess(argv, proc.returncode, text, "")


def snapshot(root):
    """Include empty dirs, file contents/modes and literal symlink targets."""
    result = {}
    for parent, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(parent, name)
            rel = os.path.relpath(path, root)
            if os.path.islink(path):
                result[rel] = ("link", os.readlink(path))
            elif os.path.isdir(path):
                result[rel] = ("dir", os.stat(path).st_mode & 0o777)
            else:
                with open(path, "rb") as fh:
                    result[rel] = ("file", os.stat(path).st_mode & 0o777, fh.read())
    return result


def main():
    tmp = tempfile.mkdtemp(prefix="mymind-test-")
    home = os.path.join(tmp, "home"); os.makedirs(os.path.join(home, ".local", "bin"))
    cfg = os.path.join(home, ".config"); os.makedirs(os.path.join(cfg, "omarchy-mymind"), mode=0o700)
    creds = os.path.join(cfg, "omarchy-mymind", "credentials.json")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Hostile)
    port = srv.server_address[1]
    Hostile.origin = f"http://127.0.0.1:{port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # Do not inherit real credentials, API overrides, proxies or desktop sockets.
    env = dict(HOME=home, XDG_CONFIG_HOME=cfg, PATH=os.defpath,
               LANG="C.UTF-8", TERM="xterm", NO_PROXY="*",
               XDG_DATA_HOME=os.path.join(home, ".local/share"),
               XDG_RUNTIME_DIR=os.path.join(tmp, "runtime"),
               XDG_STATE_HOME=os.path.join(home, ".local/state"),
               XDG_CACHE_HOME=os.path.join(home, ".cache"),
               MYMIND_CREDENTIALS=creds, MYMIND_API_URL=Hostile.origin)

    # ---------------- 1. public setup and secret handling ----------------
    print("1. omarchy-mymind setup: dispatch, terminals and secrets")
    shim = os.path.join(tmp, "shim"); os.makedirs(shim)
    argv_log = os.path.join(tmp, "process.jsonl")
    gum_log = os.path.join(tmp, "gum.log")
    real_py = shutil.which("python3")
    with open(os.path.join(shim, "python3"), "w") as fh:
        fh.write(f'''#!{real_py}
import json, os, stat, sys
record = {{"argv": sys.argv[1:], "env": dict(os.environ)}}
for name in ("cmdline", "environ"):
    with open("/proc/self/" + name, "rb") as source:
        record[name] = source.read().decode(errors="replace")
if "/dev/fd/3" in sys.argv:
    record["fd3_open"] = os.fstat(3).st_size >= 0
    record["stdin_pipe"] = stat.S_ISFIFO(os.fstat(0).st_mode)
with open({argv_log!r}, "a") as log:
    log.write(json.dumps(record) + "\\n")
os.execv({real_py!r}, [{real_py!r}, *sys.argv[1:]])
''')
    os.chmod(os.path.join(shim, "python3"), 0o755)
    # Prompt labels go to stderr, just like real gum's terminal UI. Replies come
    # from the PTY, not stub arguments/environment (especially the secret).
    with open(os.path.join(shim, "gum"), "w") as fh:
        fh.write(f'''#!/bin/bash
printf '%s\\n' "$*" >> {gum_log!r}
case "$1" in
  input)
    shift
    while (( $# )); do
      if [[ $1 == --prompt ]]; then printf '%s' "$2" >&2; break; fi
      shift
    done
    IFS= read -r line || exit 1
    printf '%s\\n' "$line"
    ;;
  confirm)
    printf '%s ' "$2" >&2
    IFS= read -r answer || exit 1
    [[ $answer == y || $answer == Y ]]
    ;;
  *) shift; printf '%s\\n' "$*" ;;
esac
''')
    os.chmod(os.path.join(shim, "gum"), 0o755)
    stubs = os.path.join(tmp, "stubs"); os.makedirs(stubs)
    for name in ("hyprctl", "omarchy", "omarchy-shell", "omarchy-launch-webapp",
                 "omarchy-launch-floating-terminal-with-presentation", "xdg-open", "notify-send"):
        with open(os.path.join(stubs, name), "w") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        os.chmod(os.path.join(stubs, name), 0o755)
    env["PATH"] = stubs + ":" + shim + ":" + os.defpath
    public = os.path.join(BIN, "omarchy-mymind")
    lbin = os.path.join(home, ".local", "bin")
    command_path = os.path.join(lbin, "omarchy-mymind")
    bindings = os.path.join(cfg, "hypr", "bindings.lua")
    menu = os.path.join(cfg, "omarchy", "extensions", "omarchy-menu.jsonc")
    check("setup helpers are internal", all(not os.path.lexists(os.path.join(BIN, name))
          for name in ("setup", "omarchy-mymind-setup")))

    def cli(*args, stdin="", extra_env=None):
        return subprocess.run([public, *args], input=stdin, capture_output=True,
                              text=True, env=dict(env, **(extra_env or {})), timeout=120)

    def problem(p):
        try:
            return json.loads(p.stdout.strip().splitlines()[-1])
        except Exception:
            return {}

    for args in (("--help",), ("setup", "--help")):
        p = cli(*args)
        check("CLI help: " + " ".join(args), p.returncode == 0 and p.stdout.startswith("usage: omarchy-mymind "))
        check("help advertises setup options", "setup" in p.stdout and
              (len(args) == 1 or all(flag in p.stdout for flag in ("--credentials", "--uninstall", "--yes", "-y"))))

    # Probe execv without executing any helper, through a differently named link
    # and from a foreign cwd. No implementation-specific handler name is assumed.
    invocation_link = os.path.join(tmp, "public-link")
    os.symlink(os.path.relpath(public, tmp), invocation_link)
    dispatch_probe = '''
import json, os, runpy, sys
from unittest.mock import patch
class Executed(BaseException):
    pass
def execv(path, argv):
    print(json.dumps({"path": path, "argv": argv}))
    raise Executed()
sys.argv = sys.argv[1:]
with patch("os.execv", side_effect=execv):
    try:
        runpy.run_path(sys.argv[0], run_name="__main__")
    except Executed:
        pass
'''
    for entry in (public, invocation_link):
        for flags, helper, delegated in (
                ((), "setup", []), (("--yes",), "setup", ["--yes"]),
                (("-y",), "setup", ["--yes"]),
                (("--uninstall",), "setup", ["--uninstall"]),
                (("--uninstall", "--yes"), "setup", ["--uninstall", "--yes"]),
                (("--credentials",), "credentials", []),
                (("--credentials", "--yes"), "credentials", []),
                (("--credentials", "-y"), "credentials", [])):
            p = subprocess.run([real_py, "-c", dispatch_probe, entry, "setup", *flags],
                               capture_output=True, text=True, env=env, cwd=tmp, timeout=15)
            result = problem(p)
            argv = result.get("argv", [])
            check("execv dispatch " + os.path.basename(entry) + " setup " + " ".join(flags),
                  p.returncode == 0 and result.get("path") == "/bin/bash" and
                  argv[:2] == ["/bin/bash", os.path.join(ROOT, "libexec", helper)] and
                  sorted(argv[2:]) == sorted(delegated), p.stderr[-200:])

    # Real exec, not just a mock: non-executable helpers must run under bash,
    # inherit both output streams and stdin, and retain arbitrary exit statuses.
    fixture = os.path.join(tmp, "repo with spaces")
    os.makedirs(os.path.join(fixture, "bin"))
    os.makedirs(os.path.join(fixture, "libexec"))
    fixture_cli = os.path.join(fixture, "bin", "omarchy-mymind")
    shutil.copy2(public, fixture_cli)
    fixture_link = os.path.join(tmp, "fixture-link")
    os.symlink(os.path.relpath(fixture_cli, tmp), fixture_link)
    for helper in ("setup", "credentials"):
        with open(os.path.join(fixture, "libexec", helper), "w") as fh:
            fh.write('[[ -n $BASH_VERSION ]] || exit 99\n'
                     'IFS= read -r value\nprintf "stdout:%s\\n" "$value"\n'
                     'printf "stderr:inherited\\n" >&2\nexit 37\n')
    for entry in (fixture_cli, fixture_link):
        for flags in ((), ("--credentials",), ("--uninstall",)):
            p = subprocess.run([entry, "setup", *flags], input="stdio sentinel\n", capture_output=True,
                               text=True, env=env, cwd=tmp, timeout=15)
            check("bash delegation preserves stdio/exit: " + os.path.basename(entry) + " " + " ".join(flags),
                  p.returncode == 37 and p.stdout == "stdout:stdio sentinel\n" and p.stderr == "stderr:inherited\n")

    before = snapshot(home)
    for flags in (("--credentials", "--uninstall"), ("--uninstall", "--credentials", "--yes"),
                  ("--unknown",)):
        p = cli("setup", *flags)
        check("argparse rejects " + " ".join(flags), p.returncode == 2 and "usage:" in p.stderr and "error:" in p.stderr)
        check("invalid flags make no edits", snapshot(home) == before)

    # Exercise neither, stdin-only and stdout-only terminals. The public entry
    # point and helper must reject redirection before creating even a symlink.
    for flags in ((), ("--credentials",), ("--credentials", "--yes"), ("--yes",), ("-y",)):
        for terminal in ("neither", "stdin", "stdout"):
            master, slave = pty.openpty()
            try:
                p = subprocess.run([public, "setup", *flags],
                                   stdin=slave if terminal == "stdin" else subprocess.DEVNULL,
                                   stdout=slave if terminal == "stdout" else subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env=env, timeout=15)
                check("missing terminal rejected: " + " ".join(flags) + " tty=" + terminal,
                      p.returncode == 1 and any(word in p.stderr.lower() for word in ("terminal", "tty", "interactive"))
                      and "Traceback" not in p.stderr, p.stderr[-200:])
                check("terminal rejection occurs before edits", snapshot(home) == before)
            finally:
                os.close(master); os.close(slave)

    # `check` inside credentials setup only reaches our loopback mock API.
    p = terminal_run([public, "setup", "--credentials"], env,
                     (("kid > ", "kid123"), ("secret > ", SECRET_B64)))
    check("direct credentials setup exits 0", p.returncode == 0)
    saved = json.load(open(creds)) if os.path.isfile(creds) else {}
    check("credentials file has correct secret", saved.get("secret") == SECRET_B64 and saved.get("kid") == "kid123")
    check("credentials file mode 0600", os.path.isfile(creds) and (os.stat(creds).st_mode & 0o777) == 0o600)
    check("credentials mode creates no integration files", not any(os.path.lexists(p) for p in (command_path, bindings, menu))
          and os.listdir(lbin) == [])

    logged = open(argv_log).read() if os.path.exists(argv_log) else ""
    records = [json.loads(line) for line in logged.splitlines()]
    check("secret absent from Python argv, environment and /proc", SECRET_B64 not in logged)
    check("process shim observed /proc cmdline and environ", bool(records) and
          all("cmdline" in r and "environ" in r for r in records))
    check("credential writer uses fd3 script and secret pipe", any(
        r["argv"] == ["/dev/fd/3", creds, "kid123"] and r.get("fd3_open") and r.get("stdin_pipe") for r in records))
    check("kid present in process log (shim sanity)", "kid123" in logged)

    # A symlink invocation still locates libexec under the real repository root.
    before = snapshot(home)
    for flag in ("--yes", "-y"):
        p = terminal_run([invocation_link, "setup", "--credentials", flag], env, (("Replace them?", "n"),))
        check("credentials " + flag + " still confirms replacement; decline exits 0", p.returncode == 0)
        check("declining replacement leaves files and permissions unchanged", snapshot(home) == before)

    p = cli("setup")
    check("full setup with credentials still requires terminal without --yes", p.returncode == 1 and
          any(word in p.stderr.lower() for word in ("terminal", "tty", "interactive")))
    check("full setup terminal rejection makes no edits", snapshot(home) == before)
    p = cli("setup", "--credentials", "--yes")
    check("replacement with --yes still requires terminal", p.returncode == 1)
    check("non-terminal replacement leaves credentials unchanged", snapshot(home) == before)

    # A curated PATH makes gum genuinely unavailable even when installed on the
    # developer's machine. Bash read must support both input and replacement.
    plain = os.path.join(tmp, "plain"); os.makedirs(plain)
    for name in ("bash", "dirname", "mkdir", "chmod", "readlink", "realpath"):
        os.symlink(shutil.which(name), os.path.join(plain, name))
    os.symlink(os.path.join(shim, "python3"), os.path.join(plain, "python3"))
    plain_creds = os.path.join(cfg, "omarchy-mymind", "plain.json")
    plain_env = dict(env, PATH=plain, MYMIND_CREDENTIALS=plain_creds)
    p = terminal_run([invocation_link, "setup", "--credentials", "--yes"], plain_env,
                     (("kid > ", "plain-kid"), ("secret > ", SECRET_B64)))
    check("plain bash read credentials setup works through symlink", p.returncode == 0)
    plain_saved = json.load(open(plain_creds)) if os.path.isfile(plain_creds) else {}
    check("plain read saves secret with 0600 permissions", plain_saved == {"kid": "plain-kid", "secret": SECRET_B64}
          and (os.stat(plain_creds).st_mode & 0o777) == 0o600)
    before = snapshot(home)
    p = terminal_run([public, "setup", "--credentials", "--yes"], plain_env, (("Replace them?", "n"),))
    check("plain read replacement decline succeeds without edits", p.returncode == 0 and snapshot(home) == before)
    check("plain credentials creates no integration files", not any(os.path.lexists(p) for p in (command_path, bindings, menu)))
    logged = open(argv_log).read() if os.path.exists(argv_log) else ""
    check("plain read secret absent from process argv/environ", SECRET_B64 not in logged)

    # No override: all entry points must agree, without consulting the old path.
    print("1a. credential defaults: HOME fallback and XDG_CONFIG_HOME")
    mock_probe = '''
import runpy, sys
from unittest.mock import patch
with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")), \
     patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")):
    module = runpy.run_path(sys.argv[1])
assert module["CREDS"] == sys.argv[2]
'''
    for label in ("HOME fallback", "XDG alternative"):
        default_home = os.path.join(tmp, label)
        default_cfg = os.path.join(default_home, ".config" if label == "HOME fallback" else "xdg config")
        default_creds = os.path.join(default_cfg, "omarchy-mymind", "credentials.json")
        legacy_creds = os.path.join(default_cfg, "mymind", "credentials.json")
        os.makedirs(os.path.dirname(legacy_creds))
        shutil.copy2(creds, legacy_creds)
        legacy_before = snapshot(os.path.dirname(legacy_creds))
        default_env = dict(env, HOME=default_home,
                           XDG_CACHE_HOME=os.path.join(default_home, ".cache"),
                           XDG_STATE_HOME=os.path.join(default_home, ".local/state"),
                           XDG_DATA_HOME=os.path.join(default_home, ".local/share"))
        default_env.pop("MYMIND_CREDENTIALS")
        default_env.pop("XDG_CONFIG_HOME")
        if label == "XDG alternative":
            default_env["XDG_CONFIG_HOME"] = default_cfg
        before = snapshot(default_home)
        p = subprocess.run([public, "check"], capture_output=True, text=True, env=default_env, timeout=15)
        check(label + " CLI refuses legacy credentials", p.returncode != 0 and
              problem(p).get("type") == "NotConfigured" and default_creds in problem(p).get("detail", ""))
        p = subprocess.run([public, "setup", "--yes"], input="", capture_output=True,
                           text=True, env=default_env, timeout=15)
        check(label + " installer refuses legacy credentials before edits", p.returncode == 1 and
              snapshot(default_home) == before)
        p = terminal_run([public, "setup", "--credentials"], default_env,
                         (("kid > ", "default-kid"), ("secret > ", SECRET_B64)))
        saved = json.load(open(default_creds)) if os.path.isfile(default_creds) else {}
        check(label + " setup writes new default with mode 0600", p.returncode == 0 and
              saved == {"kid": "default-kid", "secret": SECRET_B64} and
              os.stat(default_creds).st_mode & 0o777 == 0o600)
        p = subprocess.run([public, "check"], capture_output=True, text=True, env=default_env, timeout=15)
        check(label + " check reads new default", p.returncode == 0 and problem(p).get("ok") is True and
              problem(p).get("credentials") == default_creds)
        p = subprocess.run([real_py, "-c", mock_probe, os.path.join(ROOT, "test", "mock_api.py"), default_creds],
                           capture_output=True, text=True, env=default_env, timeout=15)
        check(label + " mock API reads same default offline", p.returncode == 0)
        before_creds = snapshot(os.path.dirname(default_creds))
        p = subprocess.run([public, "setup", "--yes"], input="", capture_output=True,
                           text=True, env=default_env, timeout=15)
        check(label + " installer recognizes new default without terminal", p.returncode == 0 and
              os.path.islink(os.path.join(default_home, ".local", "bin", "omarchy-mymind")) and
              snapshot(os.path.dirname(default_creds)) == before_creds)
        p = subprocess.run([public, "setup", "--uninstall", "--yes"], input="yes\n", capture_output=True,
                           text=True, env=default_env, timeout=15)
        check(label + " noninteractive uninstall preserves new default", p.returncode == 0 and
              snapshot(os.path.dirname(default_creds)) == before_creds and "Also delete" not in p.stdout + p.stderr)
        before = snapshot(default_home)
        p = terminal_run([public, "setup", "--uninstall"], default_env,
                         ((f"Also delete the saved access key at {default_creds}? [y/N] ", "yes"),))
        before.pop(os.path.relpath(default_creds, default_home), None)
        check(label + " terminal uninstall deletes only new default", p.returncode == 0 and
              snapshot(default_home) == before)
        check(label + " legacy credentials never changed", snapshot(os.path.dirname(legacy_creds)) == legacy_before)

    # ---------------- 2. symlink safety ----------------
    print("2. omarchy-mymind setup: symlink handling and managed integration")
    foreign_abs = os.path.join(tmp, "other-tool"); open(foreign_abs, "w").close()
    for label, target in (("foreign absolute", foreign_abs),
                          ("relative dangling", "../share/other/omarchy-mymind"),
                          ("relative to our executable", os.path.relpath(public, lbin)),
                          ("dangling absolute", os.path.join(tmp, "does-not-exist"))):
        os.symlink(target, command_path)
        for flags in (("--yes",), ("--uninstall",)):
            p = cli("setup", *flags)
            check(label + " setup " + " ".join(flags) + " exits 0", p.returncode == 0, p.stderr[-300:])
            check(label + " untouched by " + " ".join(flags), os.path.islink(command_path) and os.readlink(command_path) == target)
            check("only the public command name installed", os.listdir(lbin) == ["omarchy-mymind"])
        os.remove(command_path)

    # regular file (not a link) with our name
    with open(command_path, "w") as fh: fh.write("#!/bin/sh\necho other\n")
    for flags in (("--yes",), ("--uninstall",)):
        p = cli("setup", *flags)
        check("regular file untouched by " + " ".join(flags), p.returncode == 0 and not os.path.islink(command_path)
              and open(command_path).read() == "#!/bin/sh\necho other\n")
    os.remove(command_path)

    # Preserve unrelated content before and after the marked integration blocks.
    os.makedirs(os.path.dirname(bindings), exist_ok=True)
    os.makedirs(os.path.dirname(menu), exist_ok=True)
    with open(bindings, "w") as fh:
        fh.write('-- user binding before\no.bind("SUPER + Z", "User", "user-command")\n')
    with open(menu, "w") as fh:
        fh.write('{\n  // user menu before\n  "user.entry": {"label":"Keep me"},\n}\n')
    creds_before = snapshot(os.path.dirname(creds))
    gum_before = open(gum_log).read()
    p = cli("setup", "--yes")
    check("full setup --yes with existing credentials works without terminal", p.returncode == 0, p.stderr[-300:])
    check("--yes skips integration confirmations", not any(line.startswith("confirm ")
          for line in open(gum_log).read()[len(gum_before):].splitlines()))
    check("only public symlink created when free", os.listdir(lbin) == ["omarchy-mymind"] and
          os.path.islink(command_path) and os.readlink(command_path) == public)
    for file, suffix in ((bindings, "-- user binding after\n"), (menu, "// user menu after\n")):
        with open(file, "a") as fh:
            fh.write(suffix)

    p = subprocess.run([command_path, "--help"], capture_output=True, text=True, env=env, timeout=15)
    check("installed CLI help uses command name", p.returncode == 0 and p.stdout.startswith("usage: omarchy-mymind "))
    for flag in ("-y", "--yes", "--yes"):
        p = subprocess.run([command_path, "setup", flag], input="", capture_output=True, text=True,
                           env=env, cwd=tmp, timeout=15)
        check("repeated full setup through installed symlink " + flag, p.returncode == 0, p.stderr[-300:])
        binding_text = open(bindings).read()
        menu_text = open(menu).read()
        for label, text in (("bindings", binding_text), ("menu", menu_text)):
            check(label + " has exactly one managed block", text.count("BEGIN omarchy-mymind") == 1 and
                  text.count("END omarchy-mymind") == 1)
        check("exactly three managed keybindings", all(binding_text.count(key) == 1 for key in
              ('SUPER + ALT + PERIOD', 'SUPER + ALT + M', 'SUPER + ALT + N')))
        check("menu entries not duplicated", all(menu_text.count('"' + key + '":') == 1 for key in
              ("trigger.mymind", "trigger.mymind.search", "trigger.mymind.save-clipboard", "trigger.mymind.note",
               "trigger.mymind.open", "setup.mymind")))
        check("menu uses unified credential command", "omarchy-mymind setup --credentials" in menu_text and
              "omarchy-mymind-setup" not in menu_text)
        check("full --yes never replaces credentials", snapshot(os.path.dirname(creds)) == creds_before)

    p = subprocess.run([command_path, "setup", "--uninstall"], input="", capture_output=True,
                       text=True, env=env, cwd=tmp, timeout=15)
    check("uninstall through installed symlink works without terminal", p.returncode == 0, p.stderr[-300:])
    check("uninstall removes exactly our public link", os.listdir(lbin) == [])
    for file, expected in ((bindings, ('-- user binding before', 'o.bind("SUPER + Z", "User", "user-command")', '-- user binding after')),
                           (menu, ('// user menu before', '"user.entry": {"label":"Keep me"},', '// user menu after'))):
        text = open(file).read()
        check("uninstall preserves outside markers: " + os.path.basename(file), all(line in text for line in expected) and
              "omarchy-mymind" not in text and "trigger.mymind" not in text and "setup.mymind" not in text)
    check("uninstall preserves credentials and permissions", snapshot(os.path.dirname(creds)) == creds_before)
    before = snapshot(home)
    p = cli("setup", "--uninstall", "--yes")
    check("repeated uninstall with --yes is harmless", p.returncode == 0 and snapshot(home) == before)

    print("2a. uninstall: explicit terminal consent and exact-path cleanup")
    cleanup_home = os.path.join(tmp, "cleanup home")
    cleanup_cfg = os.path.join(cleanup_home, ".config")
    selected = os.path.join(cleanup_home, "override dir", "saved key.json")
    cleanup_env = dict(env, HOME=cleanup_home, XDG_CONFIG_HOME=cleanup_cfg,
                       XDG_CACHE_HOME=os.path.join(cleanup_home, ".cache"),
                       XDG_STATE_HOME=os.path.join(cleanup_home, ".local/state"),
                       XDG_DATA_HOME=os.path.join(cleanup_home, ".local/share"), MYMIND_CREDENTIALS=selected)
    for path in (selected, os.path.join(os.path.dirname(selected), "sibling.json"),
                 os.path.join(cleanup_cfg, "omarchy-mymind", "credentials.json"),
                 os.path.join(cleanup_cfg, "mymind", "credentials.json"),
                 os.path.join(cleanup_env["XDG_CACHE_HOME"], "omarchy-mymind", "thumbnail.png"),
                 os.path.join(cleanup_env["XDG_STATE_HOME"], "omarchy-mymind", "state.json")):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copy2(creds, path)
    p = subprocess.run([real_py, "-c", mock_probe, os.path.join(ROOT, "test", "mock_api.py"), selected],
                       capture_output=True, text=True, env=cleanup_env, timeout=15)
    check("mock API honors credential override with spaces offline", p.returncode == 0)
    p = subprocess.run([public, "check"], capture_output=True, text=True, env=cleanup_env, timeout=15)
    check("CLI honors credential override with spaces", p.returncode == 0 and
          problem(p).get("ok") is True and problem(p).get("credentials") == selected)
    prompt = f"Also delete the saved access key at {selected}? [y/N] "
    answers = ("y", "Y", "yes", "YES", "Yes", "", "n", "no", "yEs", "yep")
    for flags, answer in ([((), answer) for answer in answers] +
                          [(("--yes",), answer) for answer in ("", "no", "yes")]):
        shutil.copy2(creds, selected)
        before = snapshot(cleanup_home)
        gum_before = open(gum_log).read()
        p = terminal_run([public, "setup", "--uninstall", *flags], cleanup_env, ((prompt, answer),))
        remove = answer in ("y", "Y", "yes", "YES", "Yes")
        if remove:
            del before[os.path.relpath(selected, cleanup_home)]
        label = "uninstall " + " ".join(flags) + " answer=" + repr(answer)
        check(label + " changes only consented credential file", p.returncode == 0 and snapshot(cleanup_home) == before)
        check(label + " reports outcome and deletion revocation reminder", selected in p.stdout and
              ("removed" if remove else "kept") in p.stdout.lower() and
              (not remove or "does not revoke" in p.stdout.lower()))
        check(label + " uses Bash read even with gum available", "Also delete" not in open(gum_log).read()[len(gum_before):])

    shutil.copy2(creds, selected)
    before = snapshot(cleanup_home)
    for terminal in ("neither", "stdin", "stdout"):
        master, slave = pty.openpty()
        try:
            if terminal == "stdin":
                os.write(master, b"yes\n")
            p = subprocess.run([public, "setup", "--uninstall", "--yes"],
                               stdin=slave if terminal == "stdin" else None,
                               input=None if terminal == "stdin" else "yes\n",
                               stdout=slave if terminal == "stdout" else subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, env=cleanup_env, timeout=15)
            check("uninstall --yes tty=" + terminal + " preserves despite yes input", p.returncode == 0 and
                  snapshot(cleanup_home) == before and "Also delete" not in (p.stdout or "") + p.stderr)
        finally:
            os.close(master); os.close(slave)

    os.remove(selected)
    target = os.path.join(os.path.dirname(selected), "sibling.json")
    for label, link_target in (("file symlink", target), ("dangling symlink", target + ".missing"),
                               ("directory symlink", os.path.dirname(creds))):
        os.symlink(link_target, selected)
        before = snapshot(cleanup_home)
        target_before = snapshot(os.path.dirname(creds))
        p = terminal_run([public, "setup", "--uninstall", "--yes"], cleanup_env, ((prompt, "y"),))
        del before[os.path.relpath(selected, cleanup_home)]
        check(label + " unlinks only selected path, never target", p.returncode == 0 and
              not os.path.lexists(selected) and snapshot(cleanup_home) == before and
              snapshot(os.path.dirname(creds)) == target_before)

    for label in ("absent file", "accidental directory"):
        if label == "accidental directory":
            os.makedirs(selected)
            shutil.copy2(creds, os.path.join(selected, "keep.json"))
        before = snapshot(cleanup_home)
        p = terminal_run([public, "setup", "--uninstall", "--yes"], cleanup_env)
        check(label + " does not prompt or recursively delete", p.returncode == 0 and
              "Also delete" not in p.stdout and snapshot(cleanup_home) == before)

    # The menu parses the action once, then the terminal wrapper joins $* and
    # parses it again with bash -c. Do not launch a terminal or any desktop UI.
    menu_home = os.path.join(tmp, "menu-home")
    os.makedirs(menu_home)
    menu_env = dict(env, HOME=menu_home, XDG_CONFIG_HOME=os.path.join(menu_home, ".config"))
    launcher = '''
omarchy-launch-floating-terminal-with-presentation() {
    cmd="$*"
    /bin/bash -c "$cmd"
}
'''
    for dirname in ("menu repo with spaces",
                    "menu repo $(touch dollar-evaluated) `touch backtick-evaluated` ' \" \\ ; &"):
        menu_fixture = os.path.join(tmp, dirname)
        os.makedirs(os.path.join(menu_fixture, "bin"))
        os.makedirs(os.path.join(menu_fixture, "libexec"))
        menu_setup = os.path.join(menu_fixture, "libexec", "setup")
        shutil.copy2(os.path.join(ROOT, "libexec", "setup"), menu_setup)
        shutil.copy2(os.path.join(ROOT, "manifest.json"), menu_fixture)
        menu_cli = os.path.join(menu_fixture, "bin", "omarchy-mymind")
        with open(menu_cli, "w") as fh:
            fh.write(f"#!{real_py}\nimport json, sys\nprint(json.dumps(sys.argv))\n")
        os.chmod(menu_cli, 0o755)
        p = subprocess.run(["/bin/bash", menu_setup, "--yes"], input="", capture_output=True,
                           text=True, env=menu_env, cwd=menu_home, timeout=15)
        check("menu fixture setup succeeds: " + dirname, p.returncode == 0, p.stderr[-300:])
        generated_menu = os.path.join(menu_env["XDG_CONFIG_HOME"], "omarchy", "extensions", "omarchy-menu.jsonc")
        with open(generated_menu) as fh:
            entry = next(line.strip().removesuffix(",") for line in fh
                         if line.lstrip().startswith('"setup.mymind":'))
        try:
            action = json.loads("{" + entry + "}")["setup.mymind"]["action"]
        except (ValueError, KeyError, TypeError):
            action = None
        check("menu action is correctly JSON encoded: " + dirname, isinstance(action, str))
        if isinstance(action, str):
            # Avoid login shells: they could restore the real desktop PATH.
            p = subprocess.run(["/bin/bash", "-c", launcher + action], input="", capture_output=True,
                               text=True, env=menu_env, cwd=menu_home, timeout=15)
            try:
                received = json.loads(p.stdout)
            except ValueError:
                received = None
            check("both menu shell layers preserve exact argv: " + dirname,
                  p.returncode == 0 and received == [menu_cli, "setup", "--credentials"], p.stderr[-300:])
        check("menu path substitutions never execute: " + dirname,
              not any(os.path.lexists(os.path.join(menu_home, name))
                      for name in ("dollar-evaluated", "backtick-evaluated")))

    # ---------------- 1b. note body via stdin ----------------
    print("1b. omarchy-mymind: note bodies over stdin")
    # save-note POST isn't handled by the hostile server, so we just check it reads stdin and gets past validation.
    p = cli("save-note", stdin="   \n")
    check("empty stdin note refused", p.returncode != 0 and problem(p).get("type") == "BadRequest")
    p = cli("add-note", "ok1", stdin="")
    check("empty stdin add-note refused", p.returncode != 0 and problem(p).get("type") == "BadRequest")

    # ---------------- 3. bounded bodies / redirects ----------------
    print("3. omarchy-mymind: bounded reads")
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

    print("3. omarchy-mymind: thumbnails")
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

    # Test CDN allow-list overrides without DNS or an external connection. The
    # socket guards also make an accidental future network call fail offline.
    probe = '''
import os, runpy, sys
from unittest.mock import patch
with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")), \
     patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")):
    module = runpy.run_path(sys.argv[1])
    validate = module["validate_download_url"]
    error = module["MymindError"]
    url = "https://evil.example.com/x.png"
    try:
        validate(url)
    except error as exc:
        assert exc.problem["type"] == "BadResponse"
    else:
        raise AssertionError("off-domain URL accepted without override")
    os.environ["MYMIND_THUMBNAIL_HOSTS"] = "evil.example.com"
    assert validate(url) == url
    for url in ("http://evil.example.com/x.png", "https://evil.example.com:8443/x.png",
                "https://evil.example.com@other.example/x.png"):
        try:
            validate(url)
        except error as exc:
            assert exc.problem["type"] == "BadResponse"
        else:
            raise AssertionError("override bypasses scheme/port/userinfo validation")
print("offline allowlist probe passed")
'''
    p = subprocess.run([real_py, "-c", probe, public], capture_output=True, text=True, env=env, timeout=15)
    check("MYMIND_THUMBNAIL_HOSTS override validated entirely offline",
          p.returncode == 0 and "offline allowlist probe passed" in p.stdout, p.stderr[-300:])

    srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS)); return 1
    print("all passed"); return 0


if __name__ == "__main__":
    sys.exit(main())
