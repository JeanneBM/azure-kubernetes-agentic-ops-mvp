# Azure Kubernetes Agentic Ops

A safety-first AKS incident agent. The MVP automatically corrects one narrowly defined condition: an image-name typo in one Azure Container Registry Deployment, such as paymnets-api instead of payments-api. All other cases are escalated with collected evidence.

## What the MVP does

1. PodWatcher watches one managed namespace for ImagePullBackOff and ErrImagePull.
2. AksDiagnosticProvider reads the Pod, Kubernetes Events, and the failing image reference.
3. FoundryDiagnosticProvider analyses source-backed evidence and returns validated JSON.
4. SelfCurePolicy treats the model output as untrusted input and applies deterministic checks.
5. AksActionExecutor patches only the affected container image and verifies the rollout.
6. IncidentOrchestrator deduplicates by namespace and workload and emits an audit event.

The only automatic action is fix_image. It is allowed only when:

- exactly one container has an image-pull failure;
- the model supplies exactly one image parameter;
- the registry is allowlisted and unchanged;
- repository and tag differ by no more than two edits;
- the current image is missing from ACR;
- the proposed image exists in ACR;
- the Deployment still has the image seen during diagnosis.

The model cannot choose a namespace, workload, deployment, or container. An incident is resolved only after a healthy rollout. Any policy, model, ACR, Kubernetes, or rollout error results in escalation.

## Requirements

- Python 3.11 or later for development. The container image uses Python 3.12.
- AKS with OIDC issuer and Workload Identity enabled.
- Azure Container Registry reachable from AKS.
- Azure AI Foundry or Azure OpenAI compatible endpoint with a deployed model.
- Azure CLI and kubectl.

## Development

~~~
python -m pip install -c constraints.txt -e ".[dev]"
python -m pytest
~~~

## AKS deployment

The deployment uses two independent workloads:

| Workload | Kubernetes access | Azure access |
| --- | --- | --- |
| Diagnostic agent | Read-only Pods, Events, ReplicaSets and Deployments in the managed namespace. | Azure AI Foundry inference. |
| Remediation agent | Read and patch Deployments in the managed namespace. | ACR tag validation. |

Use a NetworkPolicy-capable CNI. For a production deployment, implement the egress boundary described in [the two-workload guide](docs/two-workload-deployment.md).

### Configure variables

PowerShell:

~~~
$resourceGroup = "rg-agentic-ops"
$aksName = "aks-agentic-ops"
$acrName = "<YOUR_ACR_NAME>"
$managedNamespace = "payments"
$identityResourceGroup = $resourceGroup
$diagnosticIdentityName = "id-agentic-ops-diagnostic"
$remediationIdentityName = "id-agentic-ops-remediation"

$env:ACR_LOGIN_SERVER = "$acrName.azurecr.io"
$env:MANAGED_NAMESPACE = $managedNamespace
$env:AZURE_AI_FOUNDRY_ENDPOINT = "https://<FOUNDRY_RESOURCE>.openai.azure.com"
$env:AZURE_AI_FOUNDRY_DEPLOYMENT = "<FOUNDRY_MODEL_DEPLOYMENT_NAME>"
$env:AGENTIC_OPS_IMAGE = "$env:ACR_LOGIN_SERVER/agentic-ops:0.2.0"

az login
az account set --subscription "<SUBSCRIPTION_ID_OR_NAME>"
~~~

### Configure two Workload Identities

Create both user-assigned identities and obtain the AKS OIDC issuer:

~~~
az identity create --name $diagnosticIdentityName --resource-group $identityResourceGroup
az identity create --name $remediationIdentityName --resource-group $identityResourceGroup

$diagnosticClientId = az identity show --name $diagnosticIdentityName --resource-group $identityResourceGroup --query clientId -o tsv
$diagnosticPrincipalId = az identity show --name $diagnosticIdentityName --resource-group $identityResourceGroup --query principalId -o tsv
$remediationClientId = az identity show --name $remediationIdentityName --resource-group $identityResourceGroup --query clientId -o tsv
$remediationPrincipalId = az identity show --name $remediationIdentityName --resource-group $identityResourceGroup --query principalId -o tsv
$issuer = az aks show --name $aksName --resource-group $resourceGroup --query oidcIssuerProfile.issuerUrl -o tsv
~~~

Grant only the required roles:

~~~
$foundryResourceId = "<FOUNDRY_RESOURCE_ID>"
$acrResourceId = az acr show --name $acrName --resource-group $resourceGroup --query id -o tsv

az role assignment create --assignee-object-id $diagnosticPrincipalId --assignee-principal-type ServicePrincipal --role "Cognitive Services OpenAI User" --scope $foundryResourceId
az role assignment create --assignee-object-id $remediationPrincipalId --assignee-principal-type ServicePrincipal --role AcrPull --scope $acrResourceId
az aks update --name $aksName --resource-group $resourceGroup --attach-acr $acrName
~~~

Create one federated credential for each ServiceAccount:

~~~
az identity federated-credential create --name agentic-ops-diagnostic --identity-name $diagnosticIdentityName --resource-group $identityResourceGroup --issuer $issuer --subject "system:serviceaccount:agentic-ops:agentic-ops-diagnostic" --audiences "api://AzureADTokenExchange"
az identity federated-credential create --name agentic-ops-remediation --identity-name $remediationIdentityName --resource-group $identityResourceGroup --issuer $issuer --subject "system:serviceaccount:agentic-ops:agentic-ops-remediation" --audiences "api://AzureADTokenExchange"
~~~

### Build and deploy

Build the image:

~~~
az acr build --registry $acrName --image agentic-ops:0.2.0 .
~~~

Create namespaces and the token used by the authenticated diagnostic-to-remediation handoff:

~~~
az aks get-credentials --resource-group $resourceGroup --name $aksName --overwrite-existing
kubectl create namespace agentic-ops --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace $env:MANAGED_NAMESPACE --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic agentic-ops-remediation -n agentic-ops --from-literal=token="$(openssl rand -base64 32)"
~~~

Render all seven placeholders in `deploy/aks-agentic-ops.yaml`: `DIAGNOSTIC_AZURE_CLIENT_ID`, `REMEDIATION_AZURE_CLIENT_ID`, `AZURE_AI_FOUNDRY_ENDPOINT`, `AZURE_AI_FOUNDRY_DEPLOYMENT`, `AGENTIC_OPS_IMAGE`, `MANAGED_NAMESPACE`, and `ACR_LOGIN_SERVER`.

~~~
$rendered = Get-Content deploy/aks-agentic-ops.yaml -Raw
$values = @{
  DIAGNOSTIC_AZURE_CLIENT_ID = $diagnosticClientId
  REMEDIATION_AZURE_CLIENT_ID = $remediationClientId
  AZURE_AI_FOUNDRY_ENDPOINT = $env:AZURE_AI_FOUNDRY_ENDPOINT
  AZURE_AI_FOUNDRY_DEPLOYMENT = $env:AZURE_AI_FOUNDRY_DEPLOYMENT
  AGENTIC_OPS_IMAGE = $env:AGENTIC_OPS_IMAGE
  MANAGED_NAMESPACE = $env:MANAGED_NAMESPACE
  ACR_LOGIN_SERVER = $env:ACR_LOGIN_SERVER
}
foreach ($name in $values.Keys) { $rendered = $rendered.Replace("${$name}", $values[$name]) }
Set-Content deploy/aks-agentic-ops.rendered.yaml $rendered

kubectl apply -f deploy/aks-agentic-ops.rendered.yaml
kubectl rollout status deployment/agentic-ops-diagnostic -n agentic-ops
kubectl rollout status deployment/agentic-ops-remediation -n agentic-ops
~~~

## Demo

Import the correct image into ACR:

~~~
az acr import --name $acrName --source docker.io/library/nginx:1.27 --image payments-api:1.4.2
~~~

Render and apply `deploy/demo-payments-api.yaml` with the same managed namespace and ACR login server values. It intentionally references `paymnets-api`. Then watch recovery:

~~~
kubectl get pods -n $env:MANAGED_NAMESPACE -w
kubectl logs -n agentic-ops deploy/agentic-ops-diagnostic
kubectl logs -n agentic-ops deploy/agentic-ops-remediation
kubectl get deployment payments-api -n $env:MANAGED_NAMESPACE -o jsonpath='{.spec.template.spec.containers[0].image}'
~~~

## Webhook

The pod watcher is sufficient for the demo. The manifest exposes the diagnostic API through the cluster-internal `agentic-ops-diagnostic` Service. To enable `POST /api/v1/incidents`, create the optional token Secret and restart only the diagnostic workload:

~~~
$bytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$token = [Convert]::ToBase64String($bytes)
kubectl create secret generic agentic-ops-webhook -n agentic-ops --from-literal=token=$token
kubectl rollout restart deployment/agentic-ops-diagnostic -n agentic-ops
~~~

Every request must include `X-Webhook-Token`. The diagnostic NetworkPolicy permits a webhook sender only from namespaces labelled `agentic-ops/webhook-sender=true`; the remediation API accepts traffic only from the diagnostic workload.

| HTTP status | Meaning |
| --- | --- |
| 200 | Incident handled. Response contains status, reason, summary, and action. The call is synchronous and can take up to two minutes. |
| 401 | Token is missing or invalid. |
| 403 | Namespace is outside this agent instance scope. |
| 409 | Submitted workload does not match the Pod Deployment. |
| 422 | Invalid request, missing Pod, or a Pod not owned by a Deployment. |
| 502 | Kubernetes API failed while ownership was checked. |
| 503 | Webhook is disabled because no token is configured. |

## MVP limitations

- Only image-typo remediation is automatic.
- One namespace, ACR, Deployments, and one failing container per Pod are supported.
- Incident state and deduplication are in memory; the Deployments intentionally run one replica each.
- Tests use fake Kubernetes, ACR, and Foundry endpoints. Validate identity, network policy, and rollout behaviour in a non-production AKS environment before production use.
- RBAC permits Deployment patching in the managed namespace. The image-only limit is enforced in code, not Kubernetes RBAC.
- The model groundedness score is self-reported; registry and policy checks provide the effective safeguards.

~~~

To remove the complete resource group:

~~~
.\scripts\stop-agentic-ops.ps1 -ResourceGroup "rg-agentic-ops" -DeleteResourceGroup
~~~
