import os

import google.auth
import google.auth.transport.requests
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.genai import types

DEVELOPER_KNOWLEDGE_MCP_URL = "https://developerknowledge.googleapis.com/mcp"


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


root_agent = LlmAgent(
    name="gcp_assistant",
    model="gemini-2.0-flash",
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
    tools=[
        McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url=DEVELOPER_KNOWLEDGE_MCP_URL,
            ),
            header_provider=get_auth_headers,
        )
    ],
)

session_service = InMemorySessionService()
runner = Runner(
    agent=root_agent,
    app_name="slack-gcp-assistant",
    session_service=session_service,
)


async def query_agent(user_id: str, session_id: str, message: str) -> str:
    """Send a message to the agent and return the text response."""

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

    response_text = ""
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=user_content,
    ):
        if event.is_final_response():
            for part in event.content.parts:
                if part.text:
                    response_text += part.text

    return response_text if response_text else "I didn't get a response. Please try again."
