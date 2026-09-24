from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from .contracts import (
    ActionRequest,
    Facts,
    Incident,
    IncidentStatus,
    IncidentTrigger,
    Recommendation,
)
from .safety import ActionExecutor, OutOfScope, PolicyViolation, SelfCurePolicy

audit = logging.getLogger("agentic_ops.audit")


class DiagnosticProvider(Protocol):
    def collect(self, trigger: IncidentTrigger) -> Facts:
        """Collect read-only, source-backed facts."""


@dataclass(frozen=True)
class IncidentResult:
    incident: Incident
    deduplicated: bool = False
    action_executed: bool = False


class IncidentOrchestrator:
    """Coordinates incident handling.

    Every failure path ends in ESCALATED with a reason; nothing here raises
    except OutOfScope, which is raised before any cluster or model access.
    """

    def __init__(
        self,
        diagnostics: DiagnosticProvider,
        executor: ActionExecutor,
        policy: SelfCurePolicy,
        *,
        groundedness_threshold: float = 0.85,
        cooldown: timedelta = timedelta(minutes=15),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 0 <= groundedness_threshold <= 1:
            raise ValueError("groundedness_threshold must be between 0 and 1")
        if cooldown < timedelta(0):
            raise ValueError("cooldown cannot be negative")
        self._diagnostics = diagnostics
        self._executor = executor
        self._policy = policy
        self._groundedness_threshold = groundedness_threshold
        self._cooldown = cooldown
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._incidents: dict[str, tuple[Incident, datetime]] = {}
        self._lock = threading.Lock()

    def manages(self, namespace: str) -> bool:
        return self._policy.namespace_allowed(namespace)

    def handle(self, trigger: IncidentTrigger) -> IncidentResult:
        if not self.manages(trigger.namespace):
            raise OutOfScope(trigger.namespace)

        key = trigger.deduplication_key
        now = self._clock()
        with self._lock:
            existing = self._incidents.get(key)
            if existing and (
                existing[0].status is IncidentStatus.OPEN or now - existing[1] < self._cooldown
            ):
                return IncidentResult(existing[0], deduplicated=True)
            # Reserve the key before doing any work so concurrent signals dedupe.
            self._incidents[key] = (
                Incident(trigger, IncidentStatus.OPEN, Facts((), 0.0)),
                now,
            )

        incident, executed = self._process(trigger)
        with self._lock:
            self._incidents[key] = (incident, self._clock())
        self._audit(incident, executed)
        return IncidentResult(incident, action_executed=executed)

    def _process(self, trigger: IncidentTrigger) -> tuple[Incident, bool]:
        facts = Facts((), 0.0)
        action: ActionRequest | None = None
        executed = False
        try:
            facts = self._diagnostics.collect(trigger)
            if facts.groundedness < self._groundedness_threshold:
                return self._escalate(
                    trigger, facts,
                    f"groundedness {facts.groundedness:.2f} is below {self._groundedness_threshold:.2f}",
                ), False
            if facts.safe_action is None:
                return self._escalate(trigger, facts, "no safe action proposed"), False

            action = self._policy.authorize(trigger, facts)
            self._executor.execute(action)
            executed = True
            if self._executor.verify(action):
                return Incident(trigger, IncidentStatus.RESOLVED, facts, action=action), True
            return self._escalate(
                trigger, facts, "action executed but the workload did not become healthy", action
            ), True
        except PolicyViolation as violation:
            return self._escalate(trigger, facts, f"policy: {violation}"), executed
        except Exception as error:  # noqa: BLE001 - any failure must end in escalation
            audit.exception("incident handling failed")
            return self._escalate(
                trigger, facts, f"{type(error).__name__}: {error}", action
            ), executed

    @staticmethod
    def _escalate(
        trigger: IncidentTrigger,
        facts: Facts,
        reason: str,
        action: ActionRequest | None = None,
    ) -> Incident:
        recommendation = None
        if facts.items:
            recommendation = Recommendation(
                summary=facts.summary or f"Investigate {trigger.reason} for {trigger.deduplication_key}",
                action=ActionRequest(
                    "human_review",
                    {"namespace": trigger.namespace, "workload": trigger.workload},
                ),
                evidence_sources=tuple(item.source for item in facts.items),
            )
        return Incident(
            trigger, IncidentStatus.ESCALATED, facts, recommendation, reason=reason, action=action
        )

    @staticmethod
    def _audit(incident: Incident, executed: bool) -> None:
        audit.info(json.dumps({
            "event": "incident_decision",
            "correlation_id": incident.trigger.correlation_id,
            "key": incident.trigger.deduplication_key,
            "trigger_reason": incident.trigger.reason,
            "status": incident.status.value,
            "reason": incident.reason,
            "groundedness": incident.facts.groundedness,
            "summary": incident.facts.summary,
            "action": (
                {"name": incident.action.name, "parameters": dict(incident.action.parameters)}
                if incident.action else None
            ),
            "action_executed": executed,
            "evidence_sources": [item.source for item in incident.facts.items],
        }))
