from collections.abc import Sequence

from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import get_bearer_token_provider
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI

from .contracts import INSTRUCTIONS, ChatMessage, get_utc_time
from .host import load_settings, run
from .settings import Settings


class LangChainResponder:
    def __init__(self, settings: Settings, credential: AsyncTokenCredential) -> None:
        self._model = ChatOpenAI(
            base_url=settings.responses_base_url,
            model=settings.deployment,
            api_key=get_bearer_token_provider(
                credential, "https://cognitiveservices.azure.com/.default"
            ),
            use_responses_api=True,
            use_previous_response_id=False,
            store=False,
            include=["reasoning.encrypted_content"],
            max_retries=1,
            timeout=20,
        )
        self._agent = create_agent(self._model, tools=[get_utc_time], system_prompt=INSTRUCTIONS)

    async def respond(self, messages: Sequence[ChatMessage]) -> str:
        result = await self._agent.ainvoke(
            {
                "messages": [
                    HumanMessage(content=message.content)
                    if message.role == "user"
                    else AIMessage(content=message.content)
                    for message in messages
                ]
            },
            config={"recursion_limit": 10},
        )
        reply = result["messages"][-1]
        if not isinstance(reply, AIMessage):
            raise TypeError("LangChain did not finish with an assistant message")
        return reply.text

    async def close(self) -> None:
        if self._model.root_async_client is not None:
            await self._model.root_async_client.close()
        if self._model.root_client is not None:
            self._model.root_client.close()


def main() -> None:
    settings = load_settings("langchain")
    credential = settings.credential()
    run(settings, LangChainResponder(settings, credential), credential)


if __name__ == "__main__":
    main()
