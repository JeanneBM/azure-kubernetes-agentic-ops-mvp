from __future__ import annotations

import logging
import os

from .agents import SafeRemediationAgent
from .remote import HttpRemediationAgent, create_remediation_app
from .web import build_app as build_legacy_app, create_app


def build_app():
    """Build one of the explicitly separated MVP workloads.

    AGENTIC_OPS_ROLE=diagnostic owns watcher, AKS reads, Foundry diagnosis, and
    the authenticated internal handoff. AGENTIC_OPS_ROLE=remediation owns the
    narrow write capability. The legacy all-in-one mode remains only for local
    compatibility and is never used by the supplied AKS manifest.
    """
    role = os.environ.get("AGENTIC_OPS_ROLE", "all-in-one")
    if role == "all-in-one":
        return build_legacy_app()

    from kubernetes import client, config

    from .acr import AcrRegistry
    from .aks import AksActionExecutor, AksDiagnosticProvider, resolve_pod_workload
    from .foundry import FoundryDiagnosticProvider
    from .orchestrator import IncidentOrchestrator
    from .safety import SelfCurePolicy
    from .watcher import PodWatcher

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config.load_incluster_config()
    namespace = os.environ["AGENTIC_OPS_NAMESPACE"]
    registries = [
        value for value in os.environ["AGENTIC_OPS_ALLOWED_REGISTRIES"].split(",") if value.strip()
    ]
    core, apps = client.CoreV1Api(), client.AppsV1Api()

    if role == "remediation":
        policy = SelfCurePolicy(AcrRegistry(), namespace=namespace, allowed_registries=registries)
        return create_remediation_app(
            SafeRemediationAgent(policy, AksActionExecutor(apps)),
            agent_token=os.environ["AGENTIC_OPS_REMEDIATION_TOKEN"],
        )

    if role == "diagnostic":
        diagnostics = FoundryDiagnosticProvider(
            AksDiagnosticProvider(core, apps),
            endpoint=os.environ["AZURE_AI_FOUNDRY_ENDPOINT"],
            deployment=os.environ["AZURE_AI_FOUNDRY_DEPLOYMENT"],
        )
        remediation = HttpRemediationAgent(
            os.environ["AGENTIC_OPS_REMEDIATION_URL"],
            os.environ["AGENTIC_OPS_REMEDIATION_TOKEN"],
            namespace,
        )
        orchestrator = IncidentOrchestrator(diagnostics, remediation_agent=remediation)
        watcher = PodWatcher(core, apps, namespace, orchestrator.handle)
        return create_app(
            orchestrator,
            resolve_workload=lambda ns, pod: resolve_pod_workload(core, apps, ns, pod),
            webhook_token=os.environ.get("AGENTIC_OPS_WEBHOOK_TOKEN") or None,
            watcher=watcher,
        )

    raise ValueError("AGENTIC_OPS_ROLE must be diagnostic, remediation, or all-in-one")
