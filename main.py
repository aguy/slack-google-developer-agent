import asyncio
import hashlib
import hmac
import logging
import os
import re
import time
import threading

from flask import Flask, request, jsonify
from google.cloud import secretmanager
from slack_sdk import WebClient

from app.gcp_agent import query_agent

# --- Configure logging levels ---
log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

# --- Enable HTTP-level tracing to catch raw 403 responses ---
# Set via env var: MCP_HTTP_DEBUG=true
if os.environ.get("MCP_HTTP_DEBUG", "").lower() == "true":
    # httpx (used by MCP SDK)
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    logging.getLogger("httpcore").setLevel(logging.DEBUG)

    # urllib3 (used by google-auth)
    logging.getLogger("urllib3").setLevel(logging.DEBUG)

    # MCP SDK internals
    logging.getLogger("mcp").setLevel(logging.DEBUG)
    logging.getLogger("google.adk.tools.mcp_tool").setLevel(logging.DEBUG)

    logger.info("🔍 MCP HTTP debug logging ENABLED")
else:
    # Keep HTTP libraries quiet in production
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

app = Flask(__name__)

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")

# --- Shared Event Loop ---
# MCP toolset holds async state; we need a single persistent loop
_loop = asyncio.new_event_loop()
_loop_thread = threading.Thread(target=_loop.run_forever, daemon=True)
_loop_thread.start()


def run_async(coro):
    """Schedule a coroutine on the shared event loop and wait for the result."""
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=120)


# --- Secret Management ---

_secrets_cache = {}


def get_secret(secret_id: str) -> str:
    if secret_id not in _secrets_cache:
        env_key = secret_id.upper().replace("-", "_")
        env_val = os.environ.get(env_key)
        if env_val:
            _secrets_cache[secret_id] = env_val
        else:
            client = secretmanager.SecretManagerServiceClient()
            name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
            resp = client.access_secret_version(request={"name": name})
            _secrets_cache[secret_id] = resp.payload.data.decode("UTF-8")
    return _secrets_cache[secret_id]


# --- Slack Helpers ---

_slack_client = None


def get_slack_client() -> WebClient:
    global _slack_client
    if _slack_client is None:
        _slack_client = WebClient(token=get_secret("slack-bot-token"))
    return _slack_client


def verify_slack_request(req) -> bool:
    signing_secret = get_secret("slack-signing-secret")
    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")

    if not timestamp or abs(time.time() - int(timestamp)) > 300:
        return False

    sig_basestring = b"v0:" + timestamp.encode("utf-8") + b":" + req.get_data()
    expected = (
        "v0="
        + hmac.new(
            signing_secret.encode("utf-8"), sig_basestring, hashlib.sha256
        ).hexdigest()
    )
    actual = req.headers.get("X-Slack-Signature", "")
    return hmac.compare_digest(expected, actual)


# --- Deduplication ---

_processed_events = {}
DEDUP_TTL = 60


def is_duplicate(event_id: str) -> bool:
    now = time.time()
    expired = [k for k, v in _processed_events.items() if now - v > DEDUP_TTL]
    for k in expired:
        del _processed_events[k]

    if event_id in _processed_events:
        return True
    _processed_events[event_id] = now
    return False


# --- Message Processing ---

def process_message(channel_id: str, user_id: str, text: str, thread_ts: str):
    try:
        slack = get_slack_client()
        session_id = f"slack-{channel_id}-{user_id}"

        # Use the shared event loop instead of creating a new one
        response_text = run_async(
            query_agent(
                user_id=user_id,
                session_id=session_id,
                message=text,
            )
        )

        max_len = 3900
        chunks = [
            response_text[i: i + max_len]
            for i in range(0, len(response_text), max_len)
        ]

        for chunk in chunks:
            slack.chat_postMessage(
                channel=channel_id,
                text=chunk,
                thread_ts=thread_ts,
            )

    except Exception as e:
        logger.error(f"Error processing message: {e}", exc_info=True)
        try:
            get_slack_client().chat_postMessage(
                channel=channel_id,
                text="❌ Something went wrong while processing your request. ",
                thread_ts=thread_ts,
            )
        except Exception:
            pass


# --- Routes ---

@app.route("/slack/events", methods=["POST"])
def slack_events():
    logger.info(f"Received Slack event: {request.json}")
    if not verify_slack_request(request):
        return jsonify({"error": "invalid signature"}), 403

    data = request.json

    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    if data.get("type") == "event_callback":
        event = data.get("event", {})

        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return jsonify({"ok": True})

        event_id = data.get("event_id", "")
        if is_duplicate(event_id):
            return jsonify({"ok": True})

        event_type = event.get("type")
        if event_type not in ("app_mention", "message"):
            return jsonify({"ok": True})

        user_id = event.get("user", "unknown")
        channel_id = event.get("channel")
        text = event.get("text", "")
        thread_ts = event.get("thread_ts") or event.get("ts")

        text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()

        if not text:
            return jsonify({"ok": True})

        threading.Thread(
            target=process_message,
            args=(channel_id, user_id, text, thread_ts),
            daemon=True,
        ).start()

    return jsonify({"ok": True})


@app.route("/health", methods=["GET"])
def health():
    logger.info("Health check")
    return jsonify({"status": "healthy"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)