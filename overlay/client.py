"""Reads the tuner service's event stream on a background thread, reconnecting as needed."""
from __future__ import annotations

import json
import queue
import threading
import urllib.request
from pathlib import Path
from typing import Iterable, Iterator

DEFAULT_TOKEN_PATH = Path.home() / ".handheld-tuner" / "token"


def parse_sse(lines: Iterable[bytes]) -> Iterator[dict]:
    """Turns Server-Sent Events lines into the JSON dicts the service sends. Ignores comments."""
    data = []
    for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data:
                try:
                    event = json.loads("\n".join(data))
                except ValueError:
                    event = None
                if isinstance(event, dict) and "type" in event:
                    yield event
            data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())


class EventReader(threading.Thread):
    """Puts service events on `out`, plus {"type": "_online"} and {"type": "_offline", "reason": ...}
    as the connection comes and goes. The token is re-read on every attempt because a restarted
    service writes a new one."""

    def __init__(self, out: "queue.Queue[dict]", port: int = 8765, token_file=DEFAULT_TOKEN_PATH,
                 host: str = "127.0.0.1", retry_start: float = 1.0, retry_max: float = 5.0) -> None:
        super().__init__(daemon=True)
        self.retry_start, self.retry_max = retry_start, retry_max
        self.out = out
        self.url = f"http://{host}:{port}/v1/events"
        self.token_file = Path(token_file)
        self._stop_flag = threading.Event()

    def stop(self) -> None:
        self._stop_flag.set()

    def run(self) -> None:
        backoff = self.retry_start
        while not self._stop_flag.is_set():
            try:
                token = self.token_file.read_text().strip()
                req = urllib.request.Request(
                    self.url, headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"})
                # the service sends a keepalive every 15 s, so 20 s of silence means it is gone
                with urllib.request.urlopen(req, timeout=20) as resp:
                    self.out.put({"type": "_online"})
                    backoff = self.retry_start
                    for event in parse_sse(resp):
                        self.out.put(event)
                        if self._stop_flag.is_set():
                            return
                reason = "stream ended"
            except Exception as e:  # any failure just means "offline, try again"
                reason = str(getattr(e, "reason", e))
            self.out.put({"type": "_offline", "reason": reason})
            self._stop_flag.wait(backoff)
            backoff = min(backoff * 2, self.retry_max)
