from collections.abc import Sequence

from agent_framework import Agent, Message
from agent_framework.openai import OpenAIChatClient
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import get_bearer_token_provider
from openai import AsyncOpenAI

from .contracts import INSTRUCTIONS, ChatMessage, get_utc_time
from .host import load_settings, run
from .settings import Settings


class MafResponder:
    def __init__(self, settings: Settings, credential: AsyncTokenCredential) -> None:
        self._client = AsyncOpenAI(
            base_url=settings.responses_base_url,
            api_key=get_bearer_token_provider(
                credential, "https://cognitiveservices.azure.com/.default"
            ),
            max_retries=1,
            timeout=20,
        )
        client = OpenAIChatClient(model=settings.deployment, async_client=self._client)
        client.function_invocation_configuration["max_iterations"] = 3
        self._agent = Agent(
            client=client,
            name="MAF Teams Assistant",
            instructions=INSTRUCTIONS,
            tools=[get_utc_time],
            default_options={"store": False},
        )

    async def respond(self, messages: Sequence[ChatMessage]) -> str:
        response = await self._agent.run(
            [Message(role=message.role, contents=[message.content]) for message in messages],
        )
        return response.text

    async def close(self) -> None:
        await self._agent.close()
        await self._client.close()


def main() -> None:
    settings = load_settings("maf")
    credential = settings.credential()
    run(settings, MafResponder(settings, credential), credential)


if __name__ == "__main__":
    main()
