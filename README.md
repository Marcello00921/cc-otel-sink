# cc-otel-sink

A tiny **local** OpenTelemetry collector for Claude Code. It takes the OTLP/HTTP-JSON that Claude
Code exports and appends it to one `.jsonl` file per day and signal. Nothing leaves the machine.
No Docker, no Grafana, standard library only.

```sh
python otel_sink.py --ensure                  # start the collector detached, if it is not up yet
python otel_sink.py --summary                 # cost and tokens per session, today
python otel_sink.py --summary --day=2026-10-05
```

## The problem

Claude Code reports cost, tokens and API events through OpenTelemetry, but you need somewhere to
send them. The exporters it offers are `otlp`, `prometheus`, `console` and `none`:

- `console` writes into the interactive session's terminal and garbles the UI;
- `prometheus` opens one port **per process**: with several `claude` processes alive (subagents,
  background sessions, scheduled runs) they fight over it;
- `otlp` needs a collector. With none listening, the SDK drops the data, and the usual collector
  stacks (Docker, the OpenTelemetry Collector, Grafana) are a lot for one machine.

## How it works

- `--serve` listens on `127.0.0.1:14318` and accepts `POST /v1/metrics`, `/v1/logs` and
  `/v1/traces`. Each JSON body is appended, as is, to `<dir>/<date>_<signal>.jsonl`, with a
  timestamp. Protobuf or broken bodies are dropped and answered with 200, so the collector never
  crashes and never makes the exporter retry.
- `--ensure` is meant for a `SessionStart` hook. If the port already answers, it returns in
  milliseconds. If not, it starts `--serve` fully detached (no console window on Windows, a new
  session elsewhere) and returns. It always exits 0, so it cannot break a session.
- On Windows the port is bound with `SO_EXCLUSIVEADDRUSE`. Python's HTTP server sets
  `SO_REUSEADDR`, and on Windows that lets a **second** server bind a port that is already
  listening, so two collectors would split the data between them.
- `--summary` reads the day's `logs` file and adds up the `api_request` events per `session.id`:
  `cost_usd`, input, output and cache-read tokens, and the number of requests.

The default port is 14318, not the usual 4318: Windows often reserves ranges around 4318 for
Hyper-V and WSL (`netsh interface ipv4 show excludedportrange protocol=tcp`).

## Install

Python 3.8+ and the standard library only. Copy `otel_sink.py` anywhere, for example into your
project's `tools/` folder.

| environment variable | default | meaning |
|---|---|---|
| `OTEL_SINK_DIR` | `~/.claude/otel-sink` | where the `.jsonl` files go |
| `OTEL_SINK_PORT` | `14318` | port on `127.0.0.1` |

## Using it with Claude Code

Turn telemetry on and point it at the collector, in `~/.claude/settings.json` (or the project's
`.claude/settings.json`), and start the collector from a `SessionStart` hook:

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:14318"
  },
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "python tools/otel_sink.py --ensure" } ] }
    ]
  }
}
```

The protocol must be `http/json`: the collector does not decode protobuf. Prompt text and tool
content are **not** exported unless you turn on `OTEL_LOG_USER_PROMPTS` or the `OTEL_LOG_TOOL_*`
options yourself, and this collector does not need them.

Then ask for the day's numbers:

```sh
python tools/otel_sink.py --summary
```

```json
{
 "5f0c2a7e-1b3d-4c8e-9a10-7d2b6e4f8c31": {"cost_usd": 1.42, "input": 18233, "output": 9120, "cache_read": 2210554, "requests": 61}
}
```

## Platform notes

- Tested on Windows 11 (Python 3.13). The POSIX path (`start_new_session`) is standard library
  but has not been run on macOS or Linux yet. Reports are welcome.
- The `.jsonl` files grow by a few MB per busy day. Nothing rotates or deletes them; remove old
  days yourself.
- `--summary` reads the event names `api_request` and `claude_code.api_request`. If a Claude Code
  release renames the event or its attributes, the summary goes to zero while the raw files keep
  everything.

## Tests

```sh
python -m unittest -v
```

The suite starts a collector on a free port, posts real OTLP-JSON bodies and checks the file and
the summary. It also checks that unknown paths are not written, that `--serve` yields a busy port,
and has a negative control: with the `event.name` filter switched off, the summary must count a
`tool_result` event it should ignore. That proves the filter test can fail.

## License

MIT, see [LICENSE](LICENSE).
