import asyncio
import logging
import os

import google.auth
import google.auth.transport.requests
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.genai import types

logger = logging.getLogger(__name__)

DEVELOPER_KNOWLEDGE_MCP_URL = "https://developerknowledge.googleapis.com/mcp"

# Retry configuration
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds


def get_auth_headers(request_context=None):
    credentials, project_id = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    final_project = os.environ.get("GOOGLE_CLOUD_PROJECT", project_id)

    auth_request = google.auth.transport.requests.Request()
    credentials.refresh(auth_request)

    headers = {"Authorization": f"Bearer {credentials.token}"}
    if final_project:
        headers["X-Goog-User-Project"] = final_project

    return headers


def create_mcp_toolset():
    """Create a fresh MCP toolset instance."""
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=DEVELOPER_KNOWLEDGE_MCP_URL,
            timeout=30,
        ),
        header_provider=get_auth_headers,
    )


def create_agent():
    """Create a fresh agent with a new MCP toolset."""
    return LlmAgent(
        name="gcp_assistant",
        model="gemini-2.0-flash",
        description="Google Cloud Developer Assistant",
        instruction=(
            "You are an expert Google Cloud Solutions Architect. "
            "Provide precise technical assistance with actionable gcloud commands, "
            "code snippets, and best practices. Be concise and direct. "
            "Use your tools to look up the latest documentation when needed."
        ),
        tools=[create_mcp_toolset()],
    )


session_service = InMemorySessionService()

# Initial agent and runner
_agent = create_agent()
_runner = Runner(
    agent=_agent,
    app_name="slack-gcp-assistant",
    session_service=session_service,
)


def _rebuild_runner():
    """Rebuild the agent and runner with a fresh MCP connection."""
    global _agent, _runner
    logger.info("Rebuilding agent and runner with fresh MCP connection...")
    _agent = create_agent()
    _runner = Runner(
        agent=_agent,
        app_name="slack-gcp-assistant",
        session_service=session_service,
    )
    return _runner


async def query_agent(user_id: str, session_id: str, message: str) -> str:
    """Send a message to the agent and return the text response with retry logic."""
    global _runner

    session = await session_service.get_session(
        app_name="slack-gcp-assistant",
        user_id=user_id,
        session_id=session_id,
    )

    if session is None:
        session = await session_service.create_session(
            app_name="slack-gcp-assistant",
            user_id=user_id,
            session_id=session_id,
        )

    user_content = types.Content(
        role="user",
        parts=[types.Part(text=message)],
    )

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response_text = ""
            async for event in _runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=user_content,
            ):
                if event.is_final_response() and event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.text:
                            response_text += part.text

            return response_text if response_text else "I didn't get a response. Please try again."

        except ConnectionError as e:
            last_error = e
            logger.warning(
                f"MCP connection failed (attempt {attempt}/{MAX_RETRIES}): {e}"
            )

            # Rebuild the runner with a fresh MCP toolset
            _runner = _rebuild_runner()

            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY * attempt)  # Exponential-ish backoff

        except Exception as e:
            last_error = e
            logger.error(f"Agent query failed (attempt {attempt}/{MAX_RETRIES}): {e}")

            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
            else:
                break

    return f"⚠️ I'm having trouble connecting to my knowledge tools. Please try again in a moment. (Error: {last_error})"