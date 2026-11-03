import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from os import environ
from time import monotonic
from uuid import uuid4

from aiohttp import web
from azure.core.credentials_async import AsyncTokenCredential
from dotenv import load_dotenv
from microsoft_agents.activity import Activity, ActivityTypes
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import (
    CloudAdapter,
    jwt_authorization_middleware,
    start_agent_process,
)
from microsoft_agents.hosting.core import AgentApplication, MemoryStorage, TurnContext, TurnState

from .contracts import ChatMessage, Responder
from .settings import Settings
from .storage import (
    ConversationStore,
    MemoryConversationStore,
    conversation_key,
)

logger = logging.getLogger(__name__)


def configure_logging(agent_name: str) -> None:
    stdout = logging.StreamHandler(sys.stdout)
    stdout.addFilter(lambda record: record.levelno < logging.WARNING)
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(logging.WARNING)
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s %(levelname)s {agent_name} %(name)s %(message)s",
        handlers=[stdout, stderr],
    )


@web.middleware
async def log_messaging_request(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    if request.path != "/api/messages":
        return await handler(request)
    reference = str(uuid4())
    started = monotonic()
    status = 500
    logger.info("Messaging request received: reference=%s", reference)
    try:
        response = await handler(request)
        status = response.status
        return response
    except web.HTTPException as error:
        status = error.status
        raise
    finally:
        log = logger.warning if status >= 400 else logger.info
        log(
            "Messaging request completed: reference=%s status=%s duration_ms=%.0f",
            reference,
            status,
            (monotonic() - started) * 1000,
        )


def load_settings(agent_name: str) -> Settings:
    load_dotenv()
    return Settings.from_env(environ, agent_name)


async def handle_message(
    context: TurnContext,
    settings: Settings,
    responder: Responder,
    store: ConversationStore,
) -> None:
    activity = context.activity
    logger.info("Message activity received: channel=%r", str(activity.channel_id or ""))
    text = (activity.text or "").strip()
    if not text:
        await context.send_activity("Please send a text message.")
        return
    if len(text) > 8000:
        await context.send_activity("Please keep messages below 8000 characters.")
        return
    channel = str(activity.channel_id or "")
    channel_data = activity.channel_data
    tenant = ""
    if isinstance(channel_data, dict):
        tenant_data = channel_data.get("tenant")
        if isinstance(tenant_data, dict):
            tenant = str(tenant_data.get("id", ""))
    if channel == "msteams" and settings.environment == "azure":
        if tenant.lower() != settings.tenant_id:
            logger.warning("Rejected Teams activity from a different or missing tenant")
            await context.send_activity("This agent is not enabled for your tenant.")
            return
    conversation = activity.conversation.id if activity.conversation else None
    user = activity.from_property.id if activity.from_property else None
    if not activity.id or not conversation or not user or not channel:
        logger.warning("Rejected activity missing required conversation identifiers")
        await context.send_activity("The message is missing required conversation information.")
        return
    key = conversation_key(settings.agent_name, tenant, channel, conversation, user)
    async with store.turn(key) as turn:
        reply = turn.data.replies.get(activity.id)
        if reply is None:
            await context.send_activity(Activity(type=ActivityTypes.typing))
            async with asyncio.timeout(25):
                reply = await responder.respond(
                    [*turn.data.messages, ChatMessage(role="user", content=text)]
                )
            reply = reply.strip()
            if not reply:
                raise ValueError("Model returned an empty reply")
            if len(reply) > 3500:
                reply = reply[:3480] + "\n[Reply truncated]"
            turn.data.remember(activity.id, text, reply)
            # Cache before delivery so retries within this process reuse the reply.
            await turn.save()
        async with asyncio.timeout(10):
            await context.send_activity(reply)


def create_app(
    settings: Settings,
    responder: Responder,
    store: ConversationStore,
    credential: AsyncTokenCredential,
) -> web.Application:
    auth = settings.bot_auth()
    manager = MsalConnectionManager(connections_configurations={"SERVICE_CONNECTION": auth})
    adapter = CloudAdapter(connection_manager=manager)
    agent = AgentApplication[TurnState](
        storage=MemoryStorage(), adapter=adapter, connection_manager=manager
    )

    @agent.activity("message")
    async def on_message(context: TurnContext, _state: TurnState) -> None:
        await handle_message(context, settings, responder, store)

    @agent.conversation_update("membersAdded")
    async def on_members_added(context: TurnContext, _state: TurnState) -> None:
        await context.send_activity(
            f"Welcome to the {settings.agent_name} assistant in Teams and Microsoft 365 Copilot. "
            "Ask me anything, or ask for the current UTC time. "
            "AI-generated answers may be inaccurate. Microsoft 365 file access is not configured."
        )

    @agent.error
    async def on_error(context: TurnContext, error: Exception) -> None:
        correlation = str(uuid4())
        logger.error("Turn failed: %s", correlation, exc_info=error)
        await context.send_activity(
            f"Unable to process your message. Error reference: {correlation}"
        )

    async def messages(request: web.Request) -> web.StreamResponse:
        response = await start_agent_process(request, agent, adapter)
        if response is None:
            raise RuntimeError("The channel adapter did not return an HTTP response")
        return response

    async def live(_request: web.Request) -> web.Response:
        return web.json_response({"status": "alive", "agent": settings.agent_name})

    async def ready(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ready", "agent": settings.agent_name})

    async def privacy(_request: web.Request) -> web.Response:
        return web.Response(
            text="This demo sends chat messages to Azure OpenAI and retains the last 10 "
            "conversation turns in process memory. History and cached replies are lost on "
            "restart or deployment. No conversation data is written to Azure Storage. "
            "Teams and Microsoft 365 Copilot may separately retain chat transcripts under "
            "your organization's Microsoft 365 retention policies. "
            "Do not submit sensitive information. This demo does not access Microsoft Graph."
        )

    async def terms(_request: web.Request) -> web.Response:
        return web.Response(text="Development demonstration only. No warranty or production SLA.")

    async def cleanup(_app: web.Application) -> AsyncIterator[None]:
        yield
        try:
            await responder.close()
        finally:
            try:
                await store.close()
            finally:
                await credential.close()

    # Scope channel JWT middleware to messages; probes must remain unauthenticated.
    channel_app = web.Application(middlewares=[jwt_authorization_middleware])
    channel_app["agent_configuration"] = auth
    channel_app["agent_app"] = agent
    channel_app["adapter"] = adapter
    channel_app.router.add_post("/messages", messages)
    app = web.Application(client_max_size=128 * 1024, middlewares=[log_messaging_request])
    app.add_subapp("/api", channel_app)
    app.router.add_get("/health/live", live)
    app.router.add_get("/health/ready", ready)
    app.router.add_get("/", terms)
    app.router.add_get("/privacy", privacy)
    app.router.add_get("/terms", terms)
    app.cleanup_ctx.append(cleanup)
    return app


def run(settings: Settings, responder: Responder, credential: AsyncTokenCredential) -> None:
    configure_logging(settings.agent_name)
    # Never allow anonymous local mode on an externally reachable interface.
    host = "127.0.0.1" if settings.environment == "local" else "0.0.0.0"
    web.run_app(
        create_app(settings, responder, MemoryConversationStore(), credential),
        host=host,
        port=settings.port,
        access_log=None,
    )
