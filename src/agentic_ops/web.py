from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .orchestrator import IncidentOrchestrator


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


def build_app() -> FastAPI:
    from kubernetes import client, config

    from .acr import AcrRegistry
    from .aks import AksActionExecutor, AksDiagnosticProvider
    from .foundry import FoundryDiagnosticProvider
    from .safety import SelfCurePolicy
    from .watcher import PodWatcher

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    namespace = os.environ["AGENTIC_OPS_NAMESPACE"]
    registries = [r for r in os.environ["AGENTIC_OPS_ALLOWED_REGISTRIES"].split(",") if r.strip()]
    config.load_incluster_config()
    core, apps = client.CoreV1Api(), client.AppsV1Api()

    diagnostics = FoundryDiagnosticProvider(
        AksDiagnosticProvider(core, apps),
        endpoint=os.environ["AZURE_AI_FOUNDRY_ENDPOINT"],
        deployment=os.environ["AZURE_AI_FOUNDRY_DEPLOYMENT"],
    )
    policy = SelfCurePolicy(AcrRegistry(), namespace=namespace, allowed_registries=registries)
    orchestrator = IncidentOrchestrator(diagnostics, AksActionExecutor(apps), policy)
    watcher = PodWatcher(core, apps, namespace, orchestrator.handle)
    return create_app(watcher=watcher)
