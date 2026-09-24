# Rationale for the Two-Agent Architecture

## Separation of responsibilities

| Agent | Permissions | Output |
| --- | --- | --- |
| Diagnostic agent | May use internet search and read-only AKS data. | Structured facts, a confidence score, and one remediation proposal. |
| Remediation agent | Has no browser, search capability, or dependency on external results. It receives only the Facts contract. | Independently authorizes the operation, executes it, and verifies the rollout. |

## Why this design

Internet-search output can help diagnose an incident, but it is external and untrusted input. It can be outdated, incorrect, or contain instructions unrelated to the incident. The first agent therefore has no access to the executor and cannot choose the namespace, Deployment, or container.

The second agent is intentionally narrower. It does not use the network or research tools, so a web page or model response cannot expand the operation's scope. It verifies local facts and safety rules: an allowed registry, a small image-name change, the corrected image's existence in ACR, and the correct AKS target.

## Completion condition

An incident has status resolved only when the second agent successfully authorizes the operation, executes it, and independently confirms a healthy rollout. A policy rejection, execution error, infrastructure error, or failed verification by the second agent ends the incident as escalated, with the reason and evidence available for human review.
