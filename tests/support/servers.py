"""Real uvicorn servers running in threads: tests talk to them over real HTTP.

Several instances run at once, each with its own engine and connection pool, which is what
proves the guarantees do not depend on any one process (NFR-2).
"""

import threading
import time

import uvicorn

from courtlock.config import Settings
from courtlock.main import create_app


class LiveServer:
    def __init__(self, settings: Settings) -> None:
        self._server = uvicorn.Server(
            uvicorn.Config(
                create_app(settings),
                host="127.0.0.1",
                port=0,
                log_level="warning",
                lifespan="on",
            )
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 20
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("test server did not start")
            time.sleep(0.02)

    @property
    def base_url(self) -> str:
        port = self._server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=15)
