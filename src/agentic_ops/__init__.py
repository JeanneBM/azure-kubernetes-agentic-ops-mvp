"""Core contracts and two-agent orchestration for Azure Kubernetes Agentic Ops."""

from .agents import RemediationAgent, RemediationOutcome, SafeRemediationAgent
from .contracts import (
    ActionRequest,
    Evidence,
    Facts,
    IncidentTrigger,
    IncidentStatus,
    PullFailure,
    Recommendation,
)
from .orchestrator import IncidentOrchestrator, IncidentResult
from .safety import OutOfScope, PolicyViolation, SelfCurePolicy

__all__ = [
    "ActionRequest",
    "Evidence",
    "Facts",
    "IncidentTrigger",
    "IncidentStatus",
    "PullFailure",
    "Recommendation",
    "RemediationAgent",
    "RemediationOutcome",
    "SafeRemediationAgent",
    "IncidentOrchestrator",
    "IncidentResult",
    "OutOfScope",
    "PolicyViolation",
    "SelfCurePolicy",
]
