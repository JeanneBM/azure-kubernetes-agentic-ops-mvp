from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI


def create_app(*, watcher=None) -> FastAPI:
    """Expose health checks and manage the PodWatcher lifecycle."""
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if watcher is not None:
            threading.Thread(
                target=watcher.run, args=(stop,), daemon=True, name="pod-watcher"
            ).start()
        yield
        stop.set()

    app = FastAPI(title="AKS Agentic Ops", lifespan=lifespan)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    return app
