from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .contracts import ActionRequest, Facts, IncidentTrigger
from .safety import ActionExecutor, SelfCurePolicy


@dataclass(frozen=True)
class RemediationOutcome:
    """The independently verified outcome of the network-isolated second agent."""

    action: ActionRequest | None
    executed: bool
    successful: bool
    reason: str = ""


class RemediationAgent(Protocol):
    """Network-isolated agent that authorizes, acts, and verifies."""

    def manages(self, namespace: str) -> bool:
        """Return whether the namespace is in this agent's fixed scope."""

    def remediate(self, trigger: IncidentTrigger, facts: Facts) -> RemediationOutcome:
        """Attempt one safe remediation. Raise on policy or infrastructure errors."""


class SafeRemediationAgent:
    """Second agent with no model, browser, or network-search dependency.

    It accepts only the typed handoff from the diagnostic agent. Its policy
    derives target namespace, workload, container, and current image from AKS
    facts, then verifies the rollout independently.
    """

    def __init__(self, policy: SelfCurePolicy, executor: ActionExecutor) -> None:
        self._policy = policy
        self._executor = executor

    def manages(self, namespace: str) -> bool:
        return self._policy.namespace_allowed(namespace)

    def remediate(self, trigger: IncidentTrigger, facts: Facts) -> RemediationOutcome:
        action = self._policy.authorize(trigger, facts)
        self._executor.execute(action)
        if not self._executor.verify(action):
            return RemediationOutcome(
                action=action,
                executed=True,
                successful=False,
                reason="second agent action executed but the workload did not become healthy",
            )
        return RemediationOutcome(action=action, executed=True, successful=True)
