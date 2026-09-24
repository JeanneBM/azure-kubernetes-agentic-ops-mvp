from __future__ import annotations

import os

import httpx
from azure.identity import DefaultAzureCredential

from .safety import ImageRef

_MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


class RegistryError(Exception):
    """The registry could not give a definite answer (auth, network, unexpected status)."""


class AcrRegistry:
    """Checks whether an image tag exists in Azure Container Registry.

    Uses the workload identity of the pod: AAD token -> ACR refresh token ->
    repository-scoped pull token -> manifest lookup. The identity needs the
    AcrPull role on the registry. Only 200 (exists) and 404 (missing) are
    treated as answers; everything else raises so the incident is escalated.
    """

    def __init__(self, *, credential=None, http_client: httpx.Client | None = None) -> None:
        self._credential = credential or DefaultAzureCredential()
        self._client = http_client or httpx.Client(timeout=20)

    def exists(self, image: ImageRef) -> bool:
        token = self._pull_token(image)
        try:
            response = self._client.head(
                f"https://{image.registry}/v2/{image.repository}/manifests/{image.tag}",
                headers={"Authorization": f"Bearer {token}", "Accept": _MANIFEST_TYPES},
            )
        except httpx.HTTPError as error:
            raise RegistryError(f"Registry request failed: {error}") from error
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise RegistryError(f"Unexpected registry status {response.status_code} for {image}")

    def _pull_token(self, image: ImageRef) -> str:
        aad_token = self._credential.get_token("https://management.azure.com/.default").token
        exchange = {
            "grant_type": "access_token",
            "service": image.registry,
            "access_token": aad_token,
        }
        tenant = os.environ.get("AZURE_TENANT_ID")
        if tenant:
            exchange["tenant"] = tenant
        try:
            refresh = self._client.post(f"https://{image.registry}/oauth2/exchange", data=exchange)
            refresh.raise_for_status()
            access = self._client.post(
                f"https://{image.registry}/oauth2/token",
                data={
                    "grant_type": "refresh_token",
                    "service": image.registry,
                    "scope": f"repository:{image.repository}:pull",
                    "refresh_token": refresh.json()["refresh_token"],
                },
            )
            access.raise_for_status()
            return access.json()["access_token"]
        except (httpx.HTTPError, KeyError, ValueError) as error:
            raise RegistryError(f"Could not obtain a registry token: {error}") from error
