import threading
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from kubernetes import client as k8s

from agentic_ops import IncidentOrchestrator
from agentic_ops.aks import resolve_pod_workload
from agentic_ops.watcher import PodWatcher
from agentic_ops.web import create_app
from conftest import FakeDiagnostics, FakeExecutor, make_facts, make_policy
from test_aks import pod

TOKEN = "s3cret"
AUTH = {"X-Webhook-Token": TOKEN}
PAYLOAD = {"namespace": "payments", "pod": "payments-api-abc-1", "workload": "payments-api",
           "reason": "ImagePullBackOff", "correlation_id": "INC-4821"}


class Env:
    """Holds the collaborators so tests can assert on them."""

    def __init__(self, owner="payments-api", token=TOKEN):
        self.owner = owner
        self.resolver_calls = []
        self.executor = FakeExecutor()
        self.orchestrator = IncidentOrchestrator(FakeDiagnostics(make_facts()), self.executor, make_policy())
        self.app = create_app(self.orchestrator, resolve_workload=self._resolve, webhook_token=token)

    def _resolve(self, namespace, pod_name):
        self.resolver_calls.append((namespace, pod_name))
        if isinstance(self.owner, Exception):
            raise self.owner
        return self.owner


@pytest.fixture
def env():
    return Env()


@pytest.fixture
def http(env):
    # One shared portal/event loop for the whole test instead of one per request.
    with TestClient(env.app) as client:
        yield client


def test_webhook_fixes_typo_and_reports_action(http, env):
    body = http.post("/api/v1/incidents", json=PAYLOAD, headers=AUTH).json()
    assert body["status"] == "resolved" and body["action_executed"]
    assert body["action"]["parameters"]["new_image"].endswith("payments-api:1.4.2")
    assert env.resolver_calls == [("payments", "payments-api-abc-1")]


def test_webhook_is_disabled_without_a_configured_token():
    with TestClient(Env(token=None).app) as http:
        assert http.post("/api/v1/incidents", json=PAYLOAD).status_code == 503
        assert http.post("/api/v1/incidents", json=PAYLOAD, headers=AUTH).status_code == 503


def test_token_is_required_and_checked_before_the_body(http, env):
    assert http.post("/api/v1/incidents", json=PAYLOAD).status_code == 401
    assert http.post("/api/v1/incidents", json=PAYLOAD, headers={"X-Webhook-Token": "wrong"}).status_code == 401
    # an unauthenticated caller must not learn anything from validation errors
    assert http.post("/api/v1/incidents", json={"bad": 1}).status_code == 401
    assert env.resolver_calls == [] and env.executor.actions == []


def test_payload_validation_after_authentication(http):
    assert http.post("/api/v1/incidents", json={**PAYLOAD, "pod": ""}, headers=AUTH).status_code == 422


def test_unmanaged_namespace_is_rejected_without_touching_the_cluster(http, env):
    response = http.post("/api/v1/incidents", json={**PAYLOAD, "namespace": "kube-system"}, headers=AUTH)
    assert response.status_code == 403
    assert env.resolver_calls == []


def test_workload_that_does_not_match_the_pod_owner_is_rejected(http, env):
    response = http.post("/api/v1/incidents", json={**PAYLOAD, "workload": "billing"}, headers=AUTH)
    assert response.status_code == 409
    assert "payments-api" in response.json()["detail"]
    assert env.executor.actions == []


def test_unknown_or_unowned_pod_is_rejected(http, env):
    env.owner = None
    response = http.post("/api/v1/incidents", json=PAYLOAD, headers=AUTH)
    assert response.status_code == 422
    assert env.executor.actions == []


def test_ownership_lookup_failure_is_a_502_and_never_acts(http, env):
    env.owner = RuntimeError("apiserver down")
    assert http.post("/api/v1/incidents", json=PAYLOAD, headers=AUTH).status_code == 502
    assert env.executor.actions == []


def test_healthz_needs_no_token(http):
    assert http.get("/healthz").json() == {"status": "ok"}


def test_lifespan_starts_the_watcher_and_signals_it_to_stop():
    started, stopped = threading.Event(), threading.Event()

    class FakeWatcher:
        def run(self, stop):
            started.set()
            stop.wait(5)
            stopped.set()

    env = Env()
    app = create_app(env.orchestrator, resolve_workload=env._resolve, webhook_token=TOKEN, watcher=FakeWatcher())
    with TestClient(app):
        assert started.wait(5)
        assert not stopped.is_set()
    assert stopped.wait(5)


def test_resolve_pod_workload_maps_missing_pod_to_none_and_other_errors_raise():
    core, apps = MagicMock(), MagicMock()
    core.read_namespaced_pod.side_effect = k8s.exceptions.ApiException(status=404)
    assert resolve_pod_workload(core, apps, "payments", "gone") is None
    core.read_namespaced_pod.side_effect = k8s.exceptions.ApiException(status=500)
    with pytest.raises(k8s.exceptions.ApiException):
        resolve_pod_workload(core, apps, "payments", "x")
    core.read_namespaced_pod.side_effect = None
    core.read_namespaced_pod.return_value = pod(owner=False)
    assert resolve_pod_workload(core, apps, "payments", "x") is None


def test_watcher_raises_incident_for_pull_failures_only():
    apps = MagicMock()
    parent = MagicMock(kind="Deployment")
    parent.name = "payments-api"
    apps.read_namespaced_replica_set.return_value = MagicMock(metadata=MagicMock(owner_references=[parent]))
    handled = []
    watcher = PodWatcher(MagicMock(), apps, "payments", handled.append)
    assert watcher.process_pod(pod()) is True
    assert handled[0].workload == "payments-api" and handled[0].namespace == "payments"
    assert watcher.process_pod(pod(reason="CrashLoopBackOff")) is False
    assert len(handled) == 1
