# Two-Workload MVP Deployment

The supplied manifest runs the MVP as two independently authenticated workloads.

| Workload | Identity and Kubernetes permissions | External capability |
| --- | --- | --- |
| Diagnostic agent | Read-only access to Pods, Events, ReplicaSets, and Deployments. | Azure AI Foundry inference and the authenticated internal remediation handoff. |
| Remediation agent | Read and patch access to Deployments only. | ACR tag validation, Kubernetes rollout verification, and the internal remediation API. It has no Foundry configuration. |

## Required Azure identities

Create two user-assigned identities and two federated credentials:

- diagnostic identity subject: system:serviceaccount:agentic-ops:agentic-ops-diagnostic
- remediation identity subject: system:serviceaccount:agentic-ops:agentic-ops-remediation

Grant Cognitive Services OpenAI User only to the diagnostic identity. Grant AcrPull only to the remediation identity. Neither identity should receive the other role.

Set DIAGNOSTIC_AZURE_CLIENT_ID and REMEDIATION_AZURE_CLIENT_ID when rendering deploy/aks-agentic-ops.yaml.

## Internal handoff

Create the required token Secret before applying the manifest:

~~~sh
kubectl create secret generic agentic-ops-remediation   --namespace agentic-ops   --from-literal=token="$(openssl rand -base64 32)"
~~~

The diagnostic agent sends typed Facts to the cluster-internal remediation Service. The remediation API rejects requests without this token. The NetworkPolicy allows its ingress only from the diagnostic pod label; it also retains the optional webhook ingress.

## Egress boundary

The code and identities prevent the remediation agent from using Foundry or a web-search tool. Standard Kubernetes NetworkPolicy cannot safely express an allowlist for Azure services whose addresses can change without knowing the cluster's egress design. Therefore this portable manifest does not make a false claim of a public-egress block.

Before a production deployment, enforce the final boundary at the network layer: allow remediation egress only to the AKS API, ACR, DNS, and the workload's required Azure identity endpoints, using Azure Firewall, an egress gateway, or a CNI policy with FQDN support. The diagnostic workload may additionally reach the approved research and Foundry endpoints.

## Rollback

Switching AGENTIC_OPS_ROLE back to all-in-one is supported only for local compatibility. The supplied AKS manifest always uses the two-workload design.
