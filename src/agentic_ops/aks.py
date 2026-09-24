from __future__ import annotations

import time
from typing import Callable

from kubernetes import client, config

from .contracts import ActionRequest, Evidence, Facts, IncidentTrigger, PullFailure

PULL_REASONS = frozenset({"ImagePullBackOff", "ErrImagePull"})


def _load_apis(core, apps):
    if core is None or apps is None:
        config.load_incluster_config()
    return (
        client.CoreV1Api() if core is None else core,
        client.AppsV1Api() if apps is None else apps,
    )


def workload_of(apps: client.AppsV1Api, pod: client.V1Pod) -> str | None:
    """Name of the Deployment that owns the pod (Pod -> ReplicaSet -> Deployment)."""
    for owner in pod.metadata.owner_references or []:
        if owner.kind == "ReplicaSet":
            replica_set = apps.read_namespaced_replica_set(owner.name, pod.metadata.namespace)
            for parent in replica_set.metadata.owner_references or []:
                if parent.kind == "Deployment":
                    return parent.name
    return None


def resolve_pod_workload(
    core: client.CoreV1Api, apps: client.AppsV1Api, namespace: str, pod_name: str
) -> str | None:
    """Deployment that really owns the named pod, or None if the pod is gone or not owned by one."""
    try:
        pod = core.read_namespaced_pod(pod_name, namespace)
    except client.exceptions.ApiException as error:
        if error.status == 404:
            return None
        raise
    return workload_of(apps, pod)


def find_pull_failures(pod: client.V1Pod) -> tuple[PullFailure, ...]:
    images = {c.name: c.image for c in pod.spec.containers}
    failures = []
    for status in pod.status.container_statuses or []:
        waiting = status.state.waiting if status.state else None
        if waiting and waiting.reason in PULL_REASONS and status.name in images:
            failures.append(PullFailure(status.name, images[status.name]))
    return tuple(failures)


class AksDiagnosticProvider:
    """Read-only AKS diagnostics using the in-cluster Kubernetes identity."""

    def __init__(self, core: client.CoreV1Api | None = None, apps: client.AppsV1Api | None = None) -> None:
        self._core, self._apps = _load_apis(core, apps)

    def collect(self, trigger: IncidentTrigger) -> Facts:
        pod = self._core.read_namespaced_pod(trigger.pod, trigger.namespace)
        events = self._core.list_namespaced_event(
            trigger.namespace, field_selector=f"involvedObject.name={trigger.pod}"
        )
        pod_source = f"k8s://pod/{trigger.namespace}/{trigger.pod}"
        failures = find_pull_failures(pod)
        evidence = [
            Evidence("pod-phase", pod.status.phase or "unknown", pod_source),
            Evidence("pod-containers", self._container_states(pod), f"{pod_source}/status"),
            Evidence("pod-events", self._events_text(events.items), f"k8s://events/{trigger.namespace}/{trigger.pod}"),
        ]
        if failures:
            evidence.append(Evidence(
                "image-pull-failures",
                "; ".join(f"container={f.container} image={f.image}" for f in failures),
                f"{pod_source}/spec",
            ))
        else:
            evidence.extend(self._previous_logs(pod, trigger))
        return Facts(tuple(evidence), groundedness=1.0, pull_failures=failures)

    def _previous_logs(self, pod: client.V1Pod, trigger: IncidentTrigger) -> list[Evidence]:
        result = []
        for container in pod.spec.containers:
            try:
                logs = self._core.read_namespaced_pod_log(
                    trigger.pod, trigger.namespace, container=container.name, previous=True, tail_lines=100
                )
            except client.exceptions.ApiException as error:
                if error.status != 400:
                    raise
                logs = ""
            result.append(Evidence(
                f"previous-logs/{container.name}",
                logs or "No previous container logs available",
                f"k8s://logs/{trigger.namespace}/{trigger.pod}/{container.name}/previous",
            ))
        return result

    @staticmethod
    def _container_states(pod: client.V1Pod) -> str:
        statuses = pod.status.container_statuses or []
        return "; ".join(
            f"{item.name}:ready={item.ready},restarts={item.restart_count},"
            f"state={item.state.to_dict() if item.state else 'unknown'}"
            for item in statuses
        ) or "no container statuses"

    @staticmethod
    def _events_text(events: list[client.V1Event]) -> str:
        return "; ".join(f"{e.reason or 'unknown'}: {e.message or ''}" for e in events) or "No matching events"


class AksActionExecutor:
    """Executes only actions already authorized by SelfCurePolicy."""

    def __init__(
        self,
        apps: client.AppsV1Api | None = None,
        *,
        verify_timeout: float = 120.0,
        poll_interval: float = 3.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if apps is None:
            config.load_incluster_config()
            apps = client.AppsV1Api()
        self._apps = apps
        self._timeout = verify_timeout
        self._poll = poll_interval
        self._sleep = sleep
        self._monotonic = monotonic

    def execute(self, action: ActionRequest) -> None:
        if action.name != "fix_image":
            raise PermissionError(f"Unsupported AKS action: {action.name}")
        p = action.parameters
        deployment = self._apps.read_namespaced_deployment(p["deployment"], p["namespace"])
        container = next(
            (c for c in deployment.spec.template.spec.containers if c.name == p["container"]), None
        )
        if container is None:
            raise RuntimeError(f"Container {p['container']!r} not found in deployment")
        if container.image != p["old_image"]:
            raise RuntimeError("Deployment image changed since diagnosis; refusing to overwrite it")
        patch = {"spec": {"template": {"spec": {"containers": [
            {"name": p["container"], "image": p["new_image"]}
        ]}}}}
        self._apps.patch_namespaced_deployment(p["deployment"], p["namespace"], patch)

    def verify(self, action: ActionRequest) -> bool:
        p = action.parameters
        deadline = self._monotonic() + self._timeout
        while True:
            deployment = self._apps.read_namespaced_deployment(p["deployment"], p["namespace"])
            if self._rolled_out(deployment, p["container"], p["new_image"]):
                return True
            if self._monotonic() >= deadline:
                return False
            self._sleep(self._poll)

    @staticmethod
    def _rolled_out(deployment: client.V1Deployment, container: str, image: str) -> bool:
        spec_image = next(
            (c.image for c in deployment.spec.template.spec.containers if c.name == container), None
        )
        if spec_image != image:
            return False
        wanted = 1 if deployment.spec.replicas is None else deployment.spec.replicas
        status = deployment.status
        return (
            (status.observed_generation or 0) >= (deployment.metadata.generation or 0)
            and (status.replicas or 0) == wanted
            and (status.updated_replicas or 0) == wanted
            and (status.available_replicas or 0) == wanted
        )
