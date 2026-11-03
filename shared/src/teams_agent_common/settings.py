from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse
from uuid import UUID

from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential
from microsoft_agents.hosting.core import AgentAuthConfiguration
from microsoft_agents.hosting.core.authorization import AuthTypes


@dataclass(frozen=True)
class Settings:
    agent_name: str
    environment: str
    endpoint: str
    deployment: str
    client_id: str | None
    tenant_id: str | None
    port: int

    @property
    def responses_base_url(self) -> str:
        return f"{self.endpoint}/openai/v1/"

    @classmethod
    def from_env(cls, env: Mapping[str, str], agent_name: str) -> "Settings":
        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ValueError(f"{name} is required")
            return value

        environment = env.get("APP_ENV", "azure")
        if environment not in {"local", "azure"}:
            raise ValueError("APP_ENV must be local or azure")
        endpoint = required("AZURE_OPENAI_ENDPOINT").rstrip("/")
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
            raise ValueError("AZURE_OPENAI_ENDPOINT must be an HTTPS resource root URL")
        client_id = tenant_id = None
        if environment == "azure":
            client_id = str(UUID(required("AZURE_CLIENT_ID")))
            tenant_id = str(UUID(required("AZURE_TENANT_ID")))
        port = int(env.get("PORT", "3978"))
        if not 1 <= port <= 65535:
            raise ValueError("PORT must be between 1 and 65535")
        return cls(
            agent_name=agent_name,
            environment=environment,
            endpoint=endpoint,
            deployment=required("AZURE_OPENAI_DEPLOYMENT_NAME"),
            client_id=client_id,
            tenant_id=tenant_id,
            port=port,
        )

    def credential(self) -> AzureCliCredential | ManagedIdentityCredential:
        if self.environment == "local":
            return AzureCliCredential()
        return ManagedIdentityCredential(client_id=self.client_id)

    def bot_auth(self) -> AgentAuthConfiguration:
        return AgentAuthConfiguration(
            auth_type=AuthTypes.user_managed_identity,
            client_id=self.client_id,
            tenant_id=self.tenant_id,
            anonymous_allowed=self.environment == "local",
            validate_issuer=self.environment == "azure",
        )
