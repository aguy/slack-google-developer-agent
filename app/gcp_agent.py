import asyncio
import logging
import os
import time

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

# Model
MODEL = os.environ.get("MODEL")

# Retry configuration
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds

# Global credentials cache
_cached_credentials = None
_cached_project_id = None

def get_auth_headers(request_context=None):
    global _cached_credentials, _cached_project_id
    
    # Initialize credentials only once
    if _cached_credentials is None:
        logger.info("Initializing Google Auth credentials...")
        _cached_credentials, _cached_project_id = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    
    # Only refresh if the token is expired or missing
    if not _cached_credentials.valid:
        logger.info("Refreshing Google Auth token...")
        auth_request = google.auth.transport.requests.Request()
        _cached_credentials.refresh(auth_request)

    final_project = os.environ.get("GOOGLE_CLOUD_PROJECT", _cached_project_id)

    return {
        "Authorization": f"Bearer {_cached_credentials.token}",
        "X-Goog-User-Project": final_project or "",
    }

def create_mcp_toolset():
    """Create a fresh MCP toolset instance."""
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=DEVELOPER_KNOWLEDGE_MCP_URL,
            timeout=30,
        ),
        header_provider=get_auth_headers,
        errlog=logging.getLogger(__name__)
    )


def create_agent():
    """Create a fresh agent with a new MCP toolset."""
    return LlmAgent(
        name="gcp_assistant",
        model=MODEL,
        description="Google Cloud Developer Assistant",
        instruction=(
            "You are an expert Google Cloud Solutions Architect and Developer Advocate. "
            "Your goal is to provide precise, high-quality technical assistance to developers building on Google Cloud."
            "You answer questions about Google Products and only Google Products."
            "If you get questions about AWS or Azure, tell the user that you are not qualified to answer those questions."
            "If you don't know the answer, tell the user that you don't know and ask for help."
            "Guidelines:\n"
            "1. **Technical Accuracy**: Provide up-to-date commands and API usage. Use your search tools to verify recent changes.\n"
            "2. **Actionable Examples**: Prefer providing gcloud commands, code snippets, or Terraform blocks over long descriptions.\n"
            "3. **Best Practices**: Focus on security, cost-optimization, and following the Google Cloud Architecture Framework.\n"
            "4. **Conciseness**: Give direct answers and speak developer to developer."
        ),
        tools=[create_mcp_toolset()],
    )


session_service = InMemorySessionService()

# Global runner state and lock to prevent race conditions during rebuilds
_runner = None
_runner_lock = asyncio.Lock()


async def get_runner() -> Runner:
    """Get the current runner or initialize it if it doesn't exist."""
    global _runner
    async with _runner_lock:
        if _runner is None:
            logger.info("Initializing agent and runner on the async loop...")
            _agent = create_agent()
            _runner = Runner(
                agent=_agent,
                app_name="slack-gcp-assistant",
                session_service=session_service,
            )
        return _runner


async def rebuild_runner() -> Runner:
    """Rebuild the agent and runner with a fresh MCP connection."""
    global _runner
    async with _runner_lock:
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
    request_id = f"{session_id}-{int(time.time())}"
    logger.info(f"[{request_id}] Agent query start | user={user_id} | message={message[:100]}")

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
    runner = await get_runner()

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"[{request_id}] Attempt {attempt}/{MAX_RETRIES}")
            start_time = time.time()

            response_text = ""
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=user_content,
            ):
                if event.is_final_response() and event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.text:
                            response_text += part.text

            elapsed = time.time() - start_time
            logger.info(
                f"[{request_id}] Success | "
                f"attempt={attempt} | "
                f"elapsed={elapsed:.2f}s | "
                f"response_len={len(response_text)}"
            )

            return response_text if response_text else "I didn't get a response. Please try again."

        except ConnectionError as e:
            last_error = e
            logger.warning(
                f"[{request_id}] MCP ConnectionError | "
                f"attempt={attempt}/{MAX_RETRIES} | "
                f"error={e}",
                exc_info=True,
            )
            runner = await rebuild_runner()

            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY * attempt)

        except Exception as e:
            last_error = e
            error_str = str(e)

            # Detect and log 403 specifically
            if "403" in error_str:
                logger.error(
                    f"[{request_id}] 🔴 403 FORBIDDEN | "
                    f"attempt={attempt}/{MAX_RETRIES} | "
                    f"error={e}",
                    exc_info=True,
                )
                # Force token refresh on 403
                runner = await rebuild_runner()
            else:
                logger.error(
                    f"[{request_id}] Agent error | "
                    f"attempt={attempt}/{MAX_RETRIES} | "
                    f"error_type={type(e).__name__} | "
                    f"error={e}",
                    exc_info=True,
                )

            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY * attempt)

    logger.error(f"[{request_id}] All retries exhausted | last_error={last_error}")
    return f"⚠️ I'm having trouble connecting to my knowledge tools. Please try again in a moment. (Error: {last_error})"