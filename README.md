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

Install the tested dependency set and run all tests:

~~~
python -m pip install -c constraints.txt -e ".[dev]"
python -m pytest
~~~

constraints.txt pins the dependency set used by the project. The test suite has a 60-second timeout per test.

## AKS deployment

The supplied production-style MVP manifest runs two separate workloads: a read-only diagnostic agent and a remediation agent with the narrow Deployment patch permission. Use the authoritative [two-workload deployment guide](docs/two-workload-deployment.md) to create separate identities, configure the authenticated handoff, and apply the manifest. The legacy single-identity commands below are retained for local compatibility only and must not be used for the two-workload deployment.

### Configure variables

PowerShell:

~~~
$resourceGroup = "rg-agentic-ops"
$aksName = "aks-agentic-ops"
$acrName = "<YOUR_ACR_NAME>"
$managedNamespace = "payments"
$identityName = "id-agentic-ops"
$identityResourceGroup = $resourceGroup

$env:ACR_LOGIN_SERVER = "$acrName.azurecr.io"
$env:MANAGED_NAMESPACE = $managedNamespace
$env:AZURE_AI_FOUNDRY_ENDPOINT = "https://<FOUNDRY_RESOURCE>.openai.azure.com"
$env:AZURE_AI_FOUNDRY_DEPLOYMENT = "<FOUNDRY_MODEL_DEPLOYMENT_NAME>"
$env:AGENTIC_OPS_IMAGE = "$env:ACR_LOGIN_SERVER/agentic-ops:0.2.0"

az login
az account set --subscription "<SUBSCRIPTION_ID_OR_NAME>"
~~~

### Configure Workload Identity

Create or look up the user-assigned identity:

~~~
az identity create --name $identityName --resource-group $identityResourceGroup
$clientId = az identity show --name $identityName --resource-group $identityResourceGroup --query clientId -o tsv
$principalId = az identity show --name $identityName --resource-group $identityResourceGroup --query principalId -o tsv
$issuer = az aks show --name $aksName --resource-group $resourceGroup --query oidcIssuerProfile.issuerUrl -o tsv
$env:AZURE_CLIENT_ID = $clientId
~~~

Grant Foundry inference and ACR pull access. Replace the Foundry resource ID with the ID of the resource hosting the model deployment:

~~~
$foundryResourceId = "<FOUNDRY_RESOURCE_ID>"
$acrResourceId = az acr show --name $acrName --resource-group $resourceGroup --query id -o tsv
az role assignment create --assignee-object-id $principalId --assignee-principal-type ServicePrincipal --role "Cognitive Services OpenAI User" --scope $foundryResourceId
az role assignment create --assignee-object-id $principalId --assignee-principal-type ServicePrincipal --role AcrPull --scope $acrResourceId
~~~

Create the federated credential used by the ServiceAccount:

~~~
az identity federated-credential create --name agentic-ops --identity-name $identityName --resource-group $identityResourceGroup --issuer $issuer --subject "system:serviceaccount:agentic-ops:agentic-ops" --audiences "api://AzureADTokenExchange"
az aks update --name $aksName --resource-group $resourceGroup --attach-acr $acrName
~~~

### Build and deploy

Build the image in ACR:

~~~
az acr build --registry $acrName --image agentic-ops:0.2.0 .
~~~

Create the agent and managed namespaces. Render deploy/aks-agentic-ops.yaml by substituting the six documented values before applying it: AZURE_CLIENT_ID, AZURE_AI_FOUNDRY_ENDPOINT, AZURE_AI_FOUNDRY_DEPLOYMENT, AGENTIC_OPS_IMAGE, MANAGED_NAMESPACE, and ACR_LOGIN_SERVER.

~~~
az aks get-credentials --resource-group $resourceGroup --name $aksName --overwrite-existing
kubectl create namespace agentic-ops --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace $env:MANAGED_NAMESPACE --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f deploy/aks-agentic-ops.rendered.yaml
kubectl rollout status deployment/agentic-ops -n agentic-ops
~~~

## Demo

Import the correct image into ACR:

~~~
az acr import --name $acrName --source docker.io/library/nginx:1.27 --image payments-api:1.4.2
~~~

Render and apply deploy/demo-payments-api.yaml with the same managed namespace and ACR login-server values. It intentionally references paymnets-api. Then watch recovery:

~~~
kubectl get pods -n $env:MANAGED_NAMESPACE -w
kubectl logs -n agentic-ops deploy/agentic-ops
kubectl get deployment payments-api -n $env:MANAGED_NAMESPACE -o jsonpath='{.spec.template.spec.containers[0].image}'
~~~

## Webhook

The pod watcher is sufficient for the demo. To enable POST /api/v1/incidents, create a token Secret and restart the agent:

~~~
$bytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$token = [Convert]::ToBase64String($bytes)
kubectl create secret generic agentic-ops-webhook -n agentic-ops --from-literal=token=$token
kubectl rollout restart deployment/agentic-ops -n agentic-ops
~~~

Every request must include X-Webhook-Token. The service verifies the namespace and confirms that the submitted workload is the Deployment that owns the Pod.

The supplied Service is cluster-internal. The NetworkPolicy permits traffic only from namespaces labelled agentic-ops/webhook-sender=true and is effective only with a NetworkPolicy-capable CNI. An internet-facing sender requires an explicitly configured and secured ingress or gateway.

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
- Incident state and deduplication are in memory; the Deployment intentionally runs one replica.
- Tests use fake Kubernetes, ACR, and Foundry endpoints. Validate identity, network policy, and rollout behaviour in a non-production AKS environment before production use.
- RBAC permits Deployment patching in the managed namespace. The image-only limit is enforced in code, not Kubernetes RBAC.
- The model groundedness score is self-reported; registry and policy checks provide the effective safeguards.

## Shutdown

Remove the application and its namespace-scoped RBAC:

~~~
.\scripts\stop-agentic-ops.ps1 -ResourceGroup "rg-agentic-ops" -AksName "aks-agentic-ops" -ManagedNamespace "payments"
~~~

To remove the complete resource group:

~~~
.\scripts\stop-agentic-ops.ps1 -ResourceGroup "rg-agentic-ops" -DeleteResourceGroup
~~~

