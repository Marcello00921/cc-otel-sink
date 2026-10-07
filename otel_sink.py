# -*- coding: utf-8 -*-
"""otel_sink: a tiny LOCAL OTLP/HTTP-JSON collector for Claude Code telemetry. Nothing leaves the machine.

Claude Code exports telemetry through OpenTelemetry with four exporters: `otlp`, `prometheus`,
`console`, `none`.
  * `console` writes into the interactive session's terminal and garbles the TUI;
  * `prometheus` opens ONE port per process: with many `claude` processes alive they fight for it;
  * `otlp` with `OTEL_EXPORTER_OTLP_PROTOCOL=http/json` to 127.0.0.1:14318 -> this collector, which
    only appends each body to `<dir>/<date>_<signal>.jsonl`. With no collector up, the SDK drops data.
No Docker, no Grafana, standard library only. Protobuf or garbage bodies are dropped, never crash it.
Prompt and tool content are not captured unless you turn on OTEL_LOG_USER_PROMPTS / OTEL_LOG_TOOL_*
yourself; this collector does not need them.

    python otel_sink.py --ensure     (SessionStart hook: start detached if the port is free; ~ms otherwise)
    python otel_sink.py --serve      (the server itself, in the foreground)
    python otel_sink.py --summary [--day=YYYY-MM-DD]   (cost and tokens per session from api_request events)

Settings (environment):
  OTEL_SINK_DIR    where the .jsonl files go (default: ~/.claude/otel-sink)
  OTEL_SINK_PORT   port on 127.0.0.1 (default 14318; 4318 falls in a range Windows often reserves,
                   see `netsh interface ipv4 show excludedportrange protocol=tcp`)
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

for _f in (sys.stdout, sys.stderr):
    try:
        _f.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DIR = pathlib.Path(os.environ.get("OTEL_SINK_DIR") or (pathlib.Path.home() / ".claude" / "otel-sink"))
HOST, PORT = "127.0.0.1", int(os.environ.get("OTEL_SINK_PORT") or 14318)
SIGNALS = {"/v1/metrics": "metrics", "/v1/logs": "logs", "/v1/traces": "traces"}
MAX_BODY = 8_000_000


class _H(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        signal = SIGNALS.get(self.path.split("?")[0])
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(n, MAX_BODY)) if n else b""
        if signal and body:
            try:
                d = json.loads(body)
                DIR.mkdir(parents=True, exist_ok=True)
                with open(DIR / ("%s_%s.jsonl" % (time.strftime("%Y-%m-%d"), signal)), "a", encoding="utf-8") as f:
                    f.write(json.dumps({"t": time.time(), "d": d}, ensure_ascii=False) + "\n")
            except Exception:
                pass                                     # protobuf or garbage: drop it, never crash
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def is_up(port: int = PORT) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=0.2):
            return True
    except OSError:
        return False


def _spawn_detached(cmd):
    """Start a process that outlives the hook that launched it, with no console window."""
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(cmd, **kw)


def ensure() -> int:
    if is_up():
        return 0
    exe = sys.executable
    w = pathlib.Path(exe).with_name("pythonw.exe")
    _spawn_detached([str(w) if w.exists() else exe, str(pathlib.Path(__file__).resolve()), "--serve"])
    return 0


class _ExclusiveServer(ThreadingHTTPServer):
    """ThreadingHTTPServer sets SO_REUSEADDR, and on Windows that lets a SECOND server bind a port that is
    already listening: two collectors would split the data. Bind exclusively instead."""
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(port: int = PORT) -> int:
    if is_up(port):
        return 0
    try:
        srv = _ExclusiveServer((HOST, port), _H)
    except OSError:
        return 0                                         # another collector won the port
    srv.serve_forever()
    return 0


def _attrs(items) -> dict:
    out = {}
    for a in items or []:
        v = a.get("value") or {}
        out[a.get("key")] = next(iter(v.values()), None) if v else None
    return out


def summary(day: str | None = None) -> dict:
    """{session.id: {cost_usd, input, output, cache_read, requests}} from the day's api_request events."""
    day = day or time.strftime("%Y-%m-%d")
    out: dict = {}
    p = DIR / ("%s_logs.jsonl" % day)
    if not p.exists():
        return out
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(ln)["d"]
        except Exception:
            continue
        for rl in d.get("resourceLogs") or []:
            for sl in rl.get("scopeLogs") or []:
                for lr in sl.get("logRecords") or []:
                    a = _attrs(lr.get("attributes"))
                    if a.get("event.name") not in ("api_request", "claude_code.api_request"):
                        continue
                    s = out.setdefault(str(a.get("session.id")),
                                       {"cost_usd": 0.0, "input": 0, "output": 0, "cache_read": 0, "requests": 0})
                    s["requests"] += 1
                    for k, c in (("cost_usd", "cost_usd"), ("input", "input_tokens"), ("output", "output_tokens"),
                                 ("cache_read", "cache_read_tokens")):
                        try:
                            s[k] += float(a.get(c) or 0) if k == "cost_usd" else int(float(a.get(c) or 0))
                        except Exception:
                            pass
    return out


def main(argv=None) -> int:
    a = sys.argv[1:] if argv is None else argv
    if "--serve" in a:
        return serve()
    if "--summary" in a:
        day = next((x.split("=", 1)[1] for x in a if x.startswith("--day=")), None)
        print(json.dumps(summary(day), ensure_ascii=False, indent=1))
        return 0
    if "--ensure" in a:
        try:
            return ensure()
        except Exception:
            return 0                                     # a SessionStart hook must never break the session
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
