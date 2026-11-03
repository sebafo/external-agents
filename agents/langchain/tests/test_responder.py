from unittest.mock import AsyncMock, MagicMock

from langchain_agent.app import LangChainResponder
from langchain_agent.contracts import ChatMessage
from langchain_agent.settings import Settings
from langchain_core.messages import AIMessage, HumanMessage
from openai.types.responses import Response


async def test_real_framework_constructs_and_maps_history() -> None:
    settings = Settings.from_env(
        {
            "APP_ENV": "local",
            "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
            "AZURE_OPENAI_DEPLOYMENT_NAME": "demo",
        },
        "langchain",
    )
    responder = LangChainResponder(settings, MagicMock())
    responder._agent.ainvoke = AsyncMock(
        return_value={"messages": [AIMessage(content="hello from LangChain")]}
    )
    result = await responder.respond([ChatMessage(role="user", content="hello")])
    assert result == "hello from LangChain"
    mapped = responder._agent.ainvoke.call_args.args[0]["messages"]
    assert isinstance(mapped[0], HumanMessage)
    assert mapped[0].content == "hello"
    await responder.close()


async def test_real_langchain_tool_loop_with_mocked_model_transport() -> None:
    settings = Settings.from_env(
        {
            "APP_ENV": "local",
            "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
            "AZURE_OPENAI_DEPLOYMENT_NAME": "demo",
        },
        "langchain",
    )
    responder = LangChainResponder(settings, MagicMock())
    outputs = [
        [
            {
                "id": "rs_1",
                "type": "reasoning",
                "summary": [],
                "encrypted_content": "encrypted-test-reasoning",
            },
            {
                "id": "fc_1",
                "call_id": "call-1",
                "type": "function_call",
                "name": "get_utc_time",
                "arguments": "{}",
                "status": "completed",
            },
        ],
        [
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": "The time was fetched.", "annotations": []}
                ],
            }
        ],
    ]
    responses = [
        Response.model_validate(
            {
                "id": f"resp_{index}",
                "created_at": 1,
                "model": "demo",
                "object": "response",
                "status": "completed",
                "parallel_tool_calls": False,
                "tool_choice": "auto",
                "tools": [],
                "output": output,
            }
        )
        for index, output in enumerate(outputs)
    ]
    raw_responses = []
    for response in responses:
        raw = MagicMock()
        raw.parse.return_value = response
        raw.headers = {}
        raw_responses.append(raw)
    transport = AsyncMock(side_effect=raw_responses)
    assert responder._model.root_async_client is not None
    responder._model.root_async_client.responses.with_raw_response.create = transport
    result = await responder.respond([ChatMessage(role="user", content="What time is it?")])
    assert result == "The time was fetched."
    assert transport.await_count == 2
    assert str(responder._model.root_async_client.base_url) == (
        "https://example.openai.azure.com/openai/v1/"
    )
    for call in transport.call_args_list:
        assert call.kwargs["store"] is False
        assert call.kwargs["model"] == "demo"
        assert "previous_response_id" not in call.kwargs
        assert "reasoning.encrypted_content" in call.kwargs["include"]
    tool_messages = transport.call_args.kwargs["input"]
    assert any(
        message.get("type") == "function_call_output" and "+00:00" in message["output"]
        for message in tool_messages
    )
    assert any(
        message.get("type") == "reasoning"
        and message.get("encrypted_content") == "encrypted-test-reasoning"
        for message in tool_messages
    )
    await responder.close()
