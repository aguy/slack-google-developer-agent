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

# Global State
_runner = None
_runner_lock = asyncio.Lock()
_cached_credentials = None
_cached_project_id = None
_session_service = InMemorySessionService()

def get_auth_headers(request_context=None):
    """Retrieve and cache Google Auth credentials, refreshing only when necessary."""
    global _cached_credentials, _cached_project_id
    
    if _cached_credentials is None:
        _cached_credentials, _cached_project_id = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    
    if not _cached_credentials.valid:
        _cached_credentials.refresh(google.auth.transport.requests.Request())

    return {
        "Authorization": f"Bearer {_cached_credentials.token}",
        "X-Goog-User-Project": os.environ.get("GOOGLE_CLOUD_PROJECT", _cached_project_id) or "",
    }

def create_runner() -> Runner:
    """Instantiate the MCP Toolset, Agent, and Runner."""
    toolset = McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url="https://developerknowledge.googleapis.com/mcp",
            timeout=30,
        ),
        header_provider=get_auth_headers,
        errlog=logger
    )
    
    agent = LlmAgent(
        name="gcp_assistant",
        model=os.environ.get("MODEL"),
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
        tools=[toolset],
    )
    
    return Runner(
        agent=agent,
        app_name="slack-gcp-assistant",
        session_service=_session_service,
    )

async def get_runner(force_rebuild: bool = False) -> Runner:
    """Thread-safe access to the Runner singleton."""
    global _runner
    async with _runner_lock:
        if _runner is None or force_rebuild:
            logger.info("Building new MCP runner...")
            _runner = create_runner()
        return _runner

async def query_agent(user_id: str, session_id: str, message: str) -> str:
    """Send a message to the agent, handling sessions and connection retries."""
    for attempt in range(1, 4):
        try:
            runner = await get_runner()
            
            # Ensure the user session exists
            if not await _session_service.get_session(app_name="slack-gcp-assistant", user_id=user_id, session_id=session_id):
                await _session_service.create_session(app_name="slack-gcp-assistant", user_id=user_id, session_id=session_id)

            response_text = ""
            user_content = types.Content(role="user", parts=[types.Part(text=message)])

            # Stream the response
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=user_content,
            ):
                if event.is_final_response() and event.content and event.content.parts:
                    response_text += "".join(p.text for p in event.content.parts if p.text)

            return response_text or "No response generated."

        except Exception as e:
            logger.error(f"Agent query failed on attempt {attempt}: {e}", exc_info=attempt == 3)
            
            # Force rebuild runner if we hit network/auth issues
            if "403" in str(e) or isinstance(e, ConnectionError):
                await get_runner(force_rebuild=True)
                
            if attempt < 3:
                await asyncio.sleep(2)
            else:
                return f"⚠️ I'm having trouble connecting to my knowledge tools."