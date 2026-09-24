from __future__ import annotations

import logging
import threading
import uuid
from typing import Callable

from kubernetes import client, watch

from .aks import find_pull_failures, workload_of
from .contracts import IncidentTrigger

log = logging.getLogger("agentic_ops.watcher")


class PodWatcher:
    """Turns image-pull failures in one namespace into incident triggers.

    This is the event source that makes the MVP automatic: no external alerting
    is required. The webhook remains available for manual or external triggers.
    """

    def __init__(
        self,
        core: client.CoreV1Api,
        apps: client.AppsV1Api,
        namespace: str,
        handle: Callable[[IncidentTrigger], object],
    ) -> None:
        self._core = core
        self._apps = apps
        self._namespace = namespace
        self._handle = handle

    def process_pod(self, pod: client.V1Pod) -> bool:
        """Handle one pod event. Returns True if an incident was raised."""
        failures = find_pull_failures(pod)
        if not failures:
            return False
        workload = workload_of(self._apps, pod)
        if workload is None:
            log.warning("pod %s is not owned by a Deployment; skipping", pod.metadata.name)
            return False
        trigger = IncidentTrigger(
            namespace=pod.metadata.namespace,
            pod=pod.metadata.name,
            workload=workload,
            reason="ImagePullBackOff",
            correlation_id=f"watch-{uuid.uuid4().hex[:8]}",
        )
        self._handle(trigger)
        return True

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                stream = watch.Watch().stream(
                    self._core.list_namespaced_pod, self._namespace, timeout_seconds=300
                )
                for event in stream:
                    if stop.is_set():
                        return
                    if event["type"] in {"ADDED", "MODIFIED"}:
                        try:
                            self.process_pod(event["object"])
                        except Exception:  # noqa: BLE001 - one bad pod must not stop the watch
                            log.exception("failed to process pod event")
            except Exception:  # noqa: BLE001 - reconnect on API/watch errors
                log.exception("pod watch interrupted; reconnecting")
                stop.wait(5)
