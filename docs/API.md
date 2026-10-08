# Tuner service API (v1)

`python -m tuner serve` runs the tuner as a background service. Any frontend (the overlay, a tray app,
a web page on the device) talks to it over HTTP on `127.0.0.1:8765`. The service owns the tuning loop,
and the original TDP and resolution are restored whenever a session ends, however it ends.

```
python -m tuner serve                     # Windows hardware
python -m tuner serve --simulate          # fake game, runs anywhere: develop frontends against this
python -m tuner ctl start --game eldenring.exe --fps 60 --hours 2.5
python -m tuner ctl watch                 # live stream in the terminal
```

## Authentication and safety

The API can change TDP and display resolution, so it is deliberately hard to reach by accident or from a web page:

- Loopback only. `serve` refuses any other `--host`.
- Every request except `GET /v1/health` needs `Authorization: Bearer <token>`. A new random token is created
  each time the service starts and written to `~/.handheld-tuner/token` (owner-only). It is deleted on shutdown.
- `GET /v1/events` also accepts `?token=<token>`, because browser `EventSource` cannot set headers.
  No other endpoint does.
- The `Host` header must be `127.0.0.1`, `localhost` or `[::1]` (blocks DNS rebinding).
- `POST` bodies must be `Content-Type: application/json`, at most 8 KB.
- No CORS headers are sent, so pages from other origins cannot read responses.
- Unknown fields and out-of-range values are rejected with `400`.

Errors are `{"error": "message"}` with status 400 (bad input), 401, 403, 404, 409 (wrong state), 413, 415 or 500.

## Endpoints

| Method and path | Body | Result |
| --- | --- | --- |
| `GET /v1/health` | | `{"ok": true, "api": "1"}`, no token needed |
| `GET /v1/status` | | the snapshot, below |
| `GET /v1/history?n=120` | | `{"samples": [...]}`, last `n` (max 600) sample events |
| `GET /v1/profiles` | | `{"profiles": {...}}`, saved per-game results |
| `GET /v1/events` | | Server-Sent Events stream, below |
| `POST /v1/session` | `game`, optional goal fields, `dry_run` | start tuning a game; replaces any running session |
| `POST /v1/goal` | any of `fps`, `hours`, `prefer`, `max_temp` | change the goal of the running session; omitted fields keep their value |
| `POST /v1/pause` | | keep reading telemetry, stop applying changes |
| `POST /v1/resume` | | apply changes again |
| `DELETE /v1/session` | | stop, restoring original settings |
| `POST /v1/shutdown` | | stop the service |

`pause`, `resume` and `goal` return `409` when no session is running, and `pause` or `resume` return `409`
for dry-run sessions (they never change anything).

### Goal fields

| Field | Type | Range | Default |
| --- | --- | --- | --- |
| `game` | string, an exe name like `eldenring.exe` | letters, digits, `_ . - space`, not starting with `-` | required |
| `fps` | whole number | 15 to 240 | 60 |
| `hours` | number or `null` | 0.1 to 24 | `null` (no battery target) |
| `prefer` | `"quality"` or `"battery"` | | `"quality"` |
| `max_temp` | number, Celsius | 50 to 100 | 85 |
| `dry_run` | boolean, `POST /v1/session` only | | `false` |

## Snapshot (`/v1/status`)

```json
{
  "api": "1",
  "state": "running",            // idle | running | paused | error
  "error": null,                 // message when state is "error"
  "game": "eldenring.exe",
  "dry_run": false,
  "paused": false,
  "goal": {"fps": 60, "hours": 2.5, "prefer": "quality", "max_temp": 85.0},
  "settings": {"tdp_w": 12, "res_index": 1, "resolution": "1600x1000", "fps_cap": 60},
  "converged": true,             // the tuner has stopped changing things
  "sample": { ...latest sample event... },
  "projected_hours": 3.29,       // battery left at the recent power draw
  "last_decision": { ...latest decision event... },
  "notes": ["saved profile for eldenring.exe"],
  "session_seconds": 412,
  "device": {"name": "Lenovo Legion Go", "tdp_min_w": 8, "tdp_max_w": 30, "tdp_step_w": 2,
             "resolutions": ["1280x800", "1600x1000", "1920x1200", "2560x1600"], "battery_wh": 49.2,
             "base_power_w": 4.0}   // screen and other draw on top of APU power
}
```

`settings` is `null` while idle. After an `error` the session has ended and original settings are restored;
start a new session to continue.

## Event stream (`/v1/events`)

Standard Server-Sent Events: `event: <type>` and `data: <json>`. The first event is always a full `state`.
A `: keepalive` comment arrives every 15 s of silence. A client that falls behind drops its oldest events.

| `event` | When | Fields |
| --- | --- | --- |
| `state` | session starts, stops, pauses, errors, or its goal changes | the full snapshot |
| `sample` | every second | `ts`, `t` (session seconds), `fps`, `fps_low`, `gpu_util` (0 to 1), `power_w`, `temp_c`, `battery_wh`, `settings` in effect |
| `decision` | every 12 s window (including `waiting for frames from the game` and `temperature unreadable, not raising power`) | `changed`, `applied`, `reason`, `settings` (proposed or in effect), `converged`, `stats` |
| `note` | something worth telling the user | `message` |

In a `decision`, `changed: true` with `applied: false` means the tuner wanted to change something but
the session is paused or a dry run. `changed: false` with a reason starting `could not apply` means the
hardware refused, and the tuner will not ask for that setting again this session.

## Example: a frontend in a few lines

```js
const token = /* read ~/.handheld-tuner/token */;
const es = new EventSource(`http://127.0.0.1:8765/v1/events?token=${token}`);
es.addEventListener("sample", e => draw(JSON.parse(e.data)));
es.addEventListener("decision", e => toast(JSON.parse(e.data).reason));

fetch("http://127.0.0.1:8765/v1/goal", {
  method: "POST",
  headers: {Authorization: `Bearer ${token}`, "Content-Type": "application/json"},
  body: JSON.stringify({fps: 40}),
});
```
