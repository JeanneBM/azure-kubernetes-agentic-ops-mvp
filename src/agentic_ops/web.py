from __future__ import annotations

import hmac
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .contracts import IncidentTrigger
from .orchestrator import IncidentOrchestrator
from .safety import OutOfScope

log = logging.getLogger("agentic_ops.web")


class AlertPayload(BaseModel):
    namespace: str = Field(min_length=1)
    pod: str = Field(min_length=1)
    workload: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)


def create_app(
    orchestrator: IncidentOrchestrator,
    *,
    resolve_workload: Callable[[str, str], str | None],
    webhook_token: str | None = None,
    watcher=None,
) -> FastAPI:
    """Build the HTTP app.

    The webhook is enabled only when a token is configured and every request must present it.
    `resolve_workload(namespace, pod)` returns the Deployment that really owns the pod; the
    payload's `workload` is never trusted, only compared against it.
    """
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

    def require_token(x_webhook_token: str | None = Header(default=None)) -> None:
        # A dependency, so authentication is decided before the body is validated.
        if not webhook_token:
            raise HTTPException(
                status_code=503, detail="webhook disabled: AGENTIC_OPS_WEBHOOK_TOKEN is not set"
            )
        if not hmac.compare_digest(x_webhook_token or "", webhook_token):
            raise HTTPException(status_code=401, detail="invalid webhook token")

    @app.post("/api/v1/incidents", dependencies=[Depends(require_token)])
    def receive_incident(payload: AlertPayload):
        if not orchestrator.manages(payload.namespace):
            # Checked first so nothing is read from the cluster for foreign namespaces.
            raise HTTPException(status_code=403, detail="namespace is not managed by this instance")
        try:
            actual = resolve_workload(payload.namespace, payload.pod)
        except Exception:  # noqa: BLE001
            log.exception("could not verify pod ownership")
            raise HTTPException(status_code=502, detail="could not verify pod ownership")
        if actual is None:
            raise HTTPException(
                status_code=422, detail="pod not found in namespace or not owned by a Deployment"
            )
        if actual != payload.workload:
            raise HTTPException(
                status_code=409,
                detail=f"workload {payload.workload!r} does not match the pod's Deployment {actual!r}",
            )
        try:
            result = orchestrator.handle(IncidentTrigger(**payload.model_dump()))
        except OutOfScope:
            raise HTTPException(status_code=403, detail="namespace is not managed by this instance")
        incident = result.incident
        return {
            "status": incident.status.value,
            "deduplicated": result.deduplicated,
            "action_executed": result.action_executed,
            "reason": incident.reason,
            "summary": incident.facts.summary,
            "action": (
                {"name": incident.action.name, "parameters": dict(incident.action.parameters)}
                if incident.action else None
            ),
            "correlation_id": payload.correlation_id,
        }

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    return app


def build_app() -> FastAPI:
    from kubernetes import client, config

    from .acr import AcrRegistry
    from .aks import AksActionExecutor, AksDiagnosticProvider, resolve_pod_workload
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
    token = os.environ.get("AGENTIC_OPS_WEBHOOK_TOKEN") or None
    if token is None:
        logging.getLogger("agentic_ops").warning(
            "AGENTIC_OPS_WEBHOOK_TOKEN is not set: the webhook is disabled, only the pod watcher is active"
        )
    return create_app(
        orchestrator,
        resolve_workload=lambda ns, pod: resolve_pod_workload(core, apps, ns, pod),
        webhook_token=token,
        watcher=watcher,
    )
