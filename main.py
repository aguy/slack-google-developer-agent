import os
import certifi
os.environ["SSL_CERT_FILE"] = certifi.where()
import asyncio
import logging
import os
import threading
import re

import google.auth
from flask import Flask, request, jsonify
from google.cloud import secretmanager
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler

from app.gcp_agent import query_agent

# --- Configure logging ---
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

# --- GCP Project Fallback ---
def get_project_id():
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        try:
            _, project_id = google.auth.default()
        except Exception:
            pass
    return project_id

PROJECT_ID = get_project_id()
logger.info(f"Project ID: {PROJECT_ID}")

# --- Secret Management ---
_secrets_cache = {}

def get_secret(secret_id: str) -> str:
    """Fetch secret synchronously. Safe to call during app startup."""
    if secret_id not in _secrets_cache:
        env_val = os.environ.get(secret_id.upper().replace("-", "_"))
        if env_val:
            _secrets_cache[secret_id] = env_val
        else:
            if not PROJECT_ID:
                raise ValueError("PROJECT_ID is not set.")
            client = secretmanager.SecretManagerServiceClient()
            name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
            response = client.access_secret_version(request={"name": name})
            _secrets_cache[secret_id] = response.payload.data.decode("UTF-8")
    return _secrets_cache[secret_id]

# Eagerly pre-fetch secrets before the app starts handling requests
logger.info("Pre-fetching secrets...")
SLACK_BOT_TOKEN = get_secret("slack-bot-token")
SLACK_SIGNING_SECRET = get_secret("slack-signing-secret")

# --- Shared Event Loop for Async GCP Agent ---
_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()

def run_async(coro):
    """Schedule a coroutine on the background event loop and block until complete."""
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=120)

# --- Slack App Initialization ---
# Bolt automatically handles signature verification, parsing, and background threading.
bolt_app = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)

@bolt_app.event("app_mention")
@bolt_app.event("message")
def handle_message(event, say, logger, context):
    # Ignore messages sent by bots
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return

    text = event.get("text", "")
    # Strip user mentions (e.g., <@U12345>)
    text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()
    
    if not text:
        return

    channel_id = event.get("channel")
    user_id = event.get("user")
    thread_ts = event.get("thread_ts") or event.get("ts")
    team_id = event.get("team") or context.get("team_id")
    session_id = f"slack-{channel_id}-{user_id}"

    try:
        agent = query_agent(user_id=user_id, session_id=session_id, message=text)
        
        # Using Slack-Bolt built-in streaming (from slack-sdk/slack-bolt >= 1.26)
        # This creates a ChatStream instance that automatically manages 
        # API calls: chat.startStream, chat.appendStream, chat.stopStream.
        stream = bolt_app.client.chat_stream(
            channel=channel_id,
            thread_ts=thread_ts,
            recipient_team_id=team_id,
            recipient_user_id=user_id
        )
        
        while True:
            try:
                # Wait for next chunk from the async agent
                future = asyncio.run_coroutine_threadsafe(anext(agent), _loop)
                chunk = future.result(timeout=120)
                
                if chunk:
                    stream.append(markdown_text=chunk)
                        
            except StopAsyncIteration:
                break

        # Let the SDK finalize the message and stop the "thinking..." state
        stream.stop()

    except Exception as e:
        logger.error(f"Error handling Slack message: {e}", exc_info=True)
        if hasattr(e, "response"):
            logger.error(f"Slack API Error response: {e.response}")
        try:
            say(text="❌ Something went wrong while processing your request.", thread_ts=thread_ts)
        except Exception as say_e:
            logger.error(f"Failed to send fallback error message: {say_e}")

# --- Flask Web Server Setup ---
app = Flask(__name__)
handler = SlackRequestHandler(bolt_app)

@app.route("/slack/events", methods=["POST"])
def slack_events():
    # Pass incoming requests to the Bolt handler
    return handler.handle(request)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))