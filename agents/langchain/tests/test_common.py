import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain_agent.contracts import ChatMessage, get_utc_time
from langchain_agent.host import configure_logging, create_app, handle_message
from langchain_agent.settings import Settings
from langchain_agent.storage import ConversationData, MemoryConversationStore, conversation_key
from microsoft_agents.activity import Activity, ChannelAccount, ChannelId, ConversationAccount
from microsoft_agents.hosting.core import TurnContext


def settings(mode: str = "local") -> Settings:
    return Settings.from_env(
        {
            "APP_ENV": mode,
            "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
            "AZURE_OPENAI_DEPLOYMENT_NAME": "demo",
            "AZURE_CLIENT_ID": "11111111-1111-4111-8111-111111111111",
            "AZURE_TENANT_ID": "22222222-2222-4222-8222-222222222222",
        },
        "langchain",
    )


def context(activity_id: str = "activity-1", text: str = "hello") -> Any:
    result = AsyncMock(spec=TurnContext)
    result.activity = Activity(
        type="message",
        id=activity_id,
        text=text,
        channel_id=ChannelId("msteams"),
        conversation=ConversationAccount(id="conversation-1"),
        from_property=ChannelAccount(id="user-1"),
        channel_data={"tenant": {"id": settings("azure").tenant_id}},
    )
    return result


def test_production_auth_is_not_anonymous() -> None:
    assert settings("azure").bot_auth().ANONYMOUS_ALLOWED is False
    assert settings().bot_auth().ANONYMOUS_ALLOWED is True


def test_log_severity_selects_stdout_or_stderr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", root.level)
    configure_logging("langchain")
    root.info("normal-operation")
    root.warning("warning-operation")
    root.error("failed-operation")
    output = capsys.readouterr()
    assert "INFO langchain" in output.out
    assert "normal-operation" in output.out
    assert "warning-operation" not in output.out
    assert "failed-operation" not in output.out
    assert "normal-operation" not in output.err
    assert "WARNING langchain" in output.err
    assert "warning-operation" in output.err
    assert "ERROR langchain" in output.err
    assert "failed-operation" in output.err


def test_responses_endpoint_uses_v1_without_api_version() -> None:
    assert settings().responses_base_url == "https://example.openai.azure.com/openai/v1/"


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"APP_ENV": "prod"},
        {"APP_ENV": "local", "AZURE_OPENAI_ENDPOINT": "http://example"},
        {
            "APP_ENV": "local",
            "AZURE_OPENAI_ENDPOINT": "https://example/openai/v1",
            "AZURE_OPENAI_DEPLOYMENT_NAME": "demo",
        },
        {
            "AZURE_OPENAI_ENDPOINT": "https://example",
            "AZURE_OPENAI_DEPLOYMENT_NAME": "demo",
        },
    ],
)
def test_invalid_configuration_fails(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        Settings.from_env(env, "langchain")


def test_tool_and_conversation_partitioning() -> None:
    assert datetime.fromisoformat(get_utc_time()).utcoffset() == timedelta(0)
    keys = {
        conversation_key("maf", "tenant", "msteams", "chat", "user"),
        conversation_key("langchain", "tenant", "msteams", "chat", "user"),
        conversation_key("maf", "different", "msteams", "chat", "user"),
        conversation_key("maf", "tenant", "msteams", "chat", "another-user"),
    }
    assert len(keys) == 4
    assert all(len(key) == 64 for key in keys)


def test_history_and_deduplication_are_bounded() -> None:
    data = ConversationData()
    for number in range(30):
        data.remember(str(number), "question", "answer")
    assert len(data.messages) == 20
    assert len(data.replies) == 20
    assert "0" not in data.replies
    assert ConversationData.model_validate_json(data.model_dump_json()) == data


async def test_multiturn_and_duplicate_do_not_repeat_model_call() -> None:
    store = MemoryConversationStore()
    responder = AsyncMock()
    responder.respond.return_value = "answer"
    await handle_message(context(), settings(), responder, store)
    await handle_message(context(), settings(), responder, store)
    assert responder.respond.await_count == 1
    followup = context("activity-2", "follow up")
    await handle_message(followup, settings(), responder, store)
    assert responder.respond.call_args.args[0] == [
        ChatMessage(role="user", content="hello"),
        ChatMessage(role="assistant", content="answer"),
        ChatMessage(role="user", content="follow up"),
    ]


async def test_wrong_tenant_and_empty_input_do_not_call_model() -> None:
    responder = AsyncMock()
    invalid = context()
    invalid.activity.channel_data = {"tenant": {"id": "different"}}
    await handle_message(invalid, settings("azure"), responder, MemoryConversationStore())
    await handle_message(context(text=""), settings(), responder, MemoryConversationStore())
    responder.respond.assert_not_awaited()


@pytest.mark.parametrize("channel_data", [{}, {"tenant": {}}, {"tenant": None}, None])
async def test_missing_tenant_does_not_call_model(channel_data: Any) -> None:
    responder = AsyncMock()
    invalid = context()
    invalid.activity.channel_data = channel_data
    await handle_message(invalid, settings("azure"), responder, MemoryConversationStore())
    responder.respond.assert_not_awaited()
    invalid.send_activity.assert_awaited_once_with("This agent is not enabled for your tenant.")


async def test_personal_chats_do_not_share_history_across_conversations_or_users() -> None:
    responder = AsyncMock()
    responder.respond.return_value = "answer"
    store = MemoryConversationStore()
    await handle_message(context(), settings("azure"), responder, store)
    other_chat = context()
    other_chat.activity.conversation = ConversationAccount(id="another-chat")
    await handle_message(other_chat, settings("azure"), responder, store)
    assert responder.respond.call_args.args[0] == [ChatMessage(role="user", content="hello")]
    other_user = context()
    other_user.activity.from_property = ChannelAccount(id="another-user")
    await handle_message(other_user, settings("azure"), responder, store)
    assert responder.respond.call_args.args[0] == [ChatMessage(role="user", content="hello")]


async def test_error_does_not_persist_success_history() -> None:
    responder = AsyncMock()
    responder.respond.side_effect = RuntimeError("model unavailable")
    store = MemoryConversationStore()
    with pytest.raises(RuntimeError, match="model unavailable"):
        await handle_message(context(), settings(), responder, store)
    responder.respond.side_effect = None
    responder.respond.return_value = "recovered"
    await handle_message(context(), settings(), responder, store)
    assert len(responder.respond.call_args.args[0]) == 1


async def test_concurrent_turns_are_serialized() -> None:
    store = MemoryConversationStore()

    async def increment() -> None:
        async with store.turn("test") as turn:
            await asyncio.sleep(0)
            count = len(turn.data.messages)
            turn.data.remember(str(count), "q", "a")
            await turn.save()

    await asyncio.gather(increment(), increment(), increment())
    async with store.turn("test") as turn:
        assert len(turn.data.messages) == 6


async def test_probes_bypass_auth_but_messages_require_auth(aiohttp_client: Any) -> None:
    responder, store, credential = AsyncMock(), AsyncMock(), AsyncMock()
    client = await aiohttp_client(create_app(settings("azure"), responder, store, credential))
    for endpoint in ("/health/live", "/health/ready", "/privacy", "/terms"):
        response = await client.get(endpoint)
        assert response.status == 200
    assert (await client.post("/api/messages", json={})).status == 401
    malformed = await client.post(
        "/api/messages", json={}, headers={"Authorization": "NotBearer token"}
    )
    assert malformed.status == 401
    responder.respond.assert_not_awaited()


async def test_request_diagnostics_include_auth_rejections_without_sensitive_data(
    aiohttp_client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="langchain_agent.host")
    responder = AsyncMock()
    client = await aiohttp_client(
        create_app(settings("azure"), responder, MemoryConversationStore(), AsyncMock())
    )
    await client.get("/health/ready")
    assert not caplog.records
    response = await client.post(
        "/api/messages?private=query-secret",
        json={"text": "private-prompt", "conversation": {"id": "private-conversation"}},
        headers={"Authorization": "NotBearer private-token"},
    )
    assert response.status == 401
    messages = [
        record.getMessage() for record in caplog.records if record.name == "langchain_agent.host"
    ]
    assert len(messages) == 2
    assert "Messaging request received: reference=" in messages[0]
    assert "Messaging request completed: reference=" in messages[1]
    assert "status=401" in messages[1]
    assert "duration_ms=" in messages[1]
    assert messages[0].split("reference=")[1] == messages[1].split("reference=")[1].split()[0]
    assert "private-" not in " ".join(messages)
    assert "query-secret" not in " ".join(messages)
    responder.respond.assert_not_awaited()


async def test_request_diagnostics_preserve_unhandled_failures(
    aiohttp_client: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.INFO, logger="langchain_agent.host")
    monkeypatch.setattr(
        "langchain_agent.host.start_agent_process",
        AsyncMock(side_effect=RuntimeError("adapter failure")),
    )
    client = await aiohttp_client(
        create_app(settings(), AsyncMock(), MemoryConversationStore(), AsyncMock())
    )
    response = await client.post("/api/messages", json={})
    assert response.status == 500
    assert any(
        record.name == "langchain_agent.host" and "status=500" in record.getMessage()
        for record in caplog.records
    )


async def test_azure_readiness_and_privacy_use_memory_only(aiohttp_client: Any) -> None:
    client = await aiohttp_client(
        create_app(settings("azure"), AsyncMock(), MemoryConversationStore(), AsyncMock())
    )
    response = await client.get("/health/ready")
    assert response.status == 200
    assert await response.json() == {"status": "ready", "agent": "langchain"}
    privacy = await (await client.get("/privacy")).text()
    assert "process memory" in privacy
    assert "lost on restart" in privacy
    assert "Teams and Microsoft 365 Copilot may separately retain chat transcripts" in privacy
    assert "Microsoft 365 retention policies" in privacy


async def test_new_process_store_starts_without_history() -> None:
    first = MemoryConversationStore()
    async with first.turn("chat") as turn:
        turn.data.remember("message", "hello", "answer")
        await turn.save()
    async with MemoryConversationStore().turn("chat") as turn:
        assert turn.data.messages == []
        assert turn.data.replies == {}
    await first.close()
    async with first.turn("chat") as turn:
        assert turn.data.messages == []


@pytest.mark.parametrize("channel_id", ["emulator", "msteams"])
async def test_local_sdk_activity_roundtrip(
    aiohttp_client: Any, channel_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    from aiohttp import web

    caplog.set_level(logging.INFO, logger="langchain_agent.host")
    received: list[dict[str, Any]] = []

    async def capture(request: web.Request) -> web.Response:
        received.append(await request.json())
        return web.json_response({"id": "reply-1"})

    channel = web.Application()
    channel.router.add_post("/v3/conversations/chat/activities", capture)
    channel.router.add_post("/v3/conversations/chat/activities/{activity_id}", capture)
    channel_client = await aiohttp_client(channel)
    responder = AsyncMock()
    responder.respond.return_value = "SDK round trip"
    client = await aiohttp_client(
        create_app(settings(), responder, MemoryConversationStore(), AsyncMock())
    )
    response = await client.post(
        "/api/messages",
        json={
            "type": "message",
            "id": "activity-1",
            "text": "hello",
            "channelId": channel_id,
            "channelData": {"tenant": {"id": settings("azure").tenant_id}},
            "serviceUrl": str(channel_client.make_url("/")),
            "conversation": {"id": "chat"},
            "from": {"id": "user"},
            "recipient": {"id": "bot"},
        },
    )
    assert response.status in {200, 201, 202}
    responder.respond.assert_awaited_once()
    assert any(activity.get("text") == "SDK round trip" for activity in received)
    messages = [
        record.getMessage() for record in caplog.records if record.name == "langchain_agent.host"
    ]
    assert any(message.startswith("Messaging request received: reference=") for message in messages)
    assert any(
        message.startswith("Messaging request completed:")
        and f"status={response.status}" in message
        for message in messages
    )
    assert f"Message activity received: channel={channel_id!r}" in messages
    assert "SDK round trip" not in " ".join(messages)
