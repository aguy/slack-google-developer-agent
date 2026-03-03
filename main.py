import asyncio
import concurrent.futures
import hashlib
import hmac
import logging
import os
import re
import time
import threading

import google.auth
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
if os.environ.get("MCP_HTTP_DEBUG", "").lower() == "true":
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    logging.getLogger("httpcore").setLevel(logging.DEBUG)
    logging.getLogger("urllib3").setLevel(logging.DEBUG)
    logging.getLogger("mcp").setLevel(logging.DEBUG)
    logging.getLogger("google.adk.tools.mcp_tool").setLevel(logging.DEBUG)
    logger.info("🔍 MCP HTTP debug logging ENABLED")
else:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

app = Flask(__name__)

# --- GCP Project Fallback ---
def get_project_id():
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        try:
            _, project_id = google.auth.default()
        except Exception as e:
            logger.warning(f"Failed to get default GCP project: {e}")
    return project_id

PROJECT_ID = get_project_id()

# --- Shared Event Loop & Thread Pool ---
_loop = asyncio.new_event_loop()
_loop_thread = threading.Thread(target=_loop.run_forever, daemon=True)
_loop_thread.start()

# Use ThreadPoolExecutor to prevent thread explosion
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)

def run_async(coro):
    """Schedule a coroutine on the shared event loop and wait for the result."""
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=120)

# --- Secret Management ---
_secrets_cache = {}
_secrets_locks = {}
_global_secrets_lock = threading.Lock()

def get_secret(secret_id: str) -> str:
    # Fast path: return immediately if cached
    if secret_id in _secrets_cache:
        return _secrets_cache[secret_id]

    # Get or create a lock specific to this secret
    with _global_secrets_lock:
        if secret_id not in _secrets_locks:
            _secrets_locks[secret_id] = threading.Lock()
            
    with _secrets_locks[secret_id]:
        # Double-check pattern
        if secret_id not in _secrets_cache:
            env_key = secret_id.upper().replace("-", "_")
            env_val = os.environ.get(env_key)
            if env_val:
                _secrets_cache[secret_id] = env_val
            else:
                if not PROJECT_ID:
                    raise ValueError("PROJECT_ID is not set, cannot fetch from Secret Manager.")
                client = secretmanager.SecretManagerServiceClient()
                name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
                resp = client.access_secret_version(request={"name": name})
                _secrets_cache[secret_id] = resp.payload.data.decode("UTF-8")
        return _secrets_cache[secret_id]

# --- Slack Helpers ---
_slack_client = None
_slack_client_lock = threading.Lock()

def get_slack_client() -> WebClient:
    global _slack_client
    with _slack_client_lock:
        if _slack_client is None:
            _slack_client = WebClient(token=get_secret("slack-bot-token"))
        return _slack_client

# --- Eager Initialization ---
def _preload_resources():
    """Pre-fetch secrets concurrently during app startup to eliminate first-request latency."""
    try:
        logger.info("Pre-fetching secrets and initializing clients...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as exc:
            exc.submit(get_secret, "slack-signing-secret")
            exc.submit(get_slack_client)
        logger.info("Resources pre-loaded successfully.")
    except Exception as e:
        logger.error(f"Failed to pre-load resources: {e}")

# Run preloader when module is imported by Gunicorn
_preload_resources()

def verify_slack_request(req) -> bool:
    signing_secret = get_secret("slack-signing-secret")
    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")

    if not timestamp or abs(time.time() - int(timestamp)) > 300:
        return False

    # req.get_data() reads and caches the raw payload
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
_dedup_lock = threading.Lock()

def is_duplicate(event_id: str) -> bool:
    now = time.time()
    with _dedup_lock:
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
                text="❌ Something went wrong while processing your request.",
                thread_ts=thread_ts,
            )
        except Exception:
            pass

# --- Routes ---
@app.route("/slack/events", methods=["POST"])
def slack_events():
    # Verify request signature before parsing JSON to prevent payload parsing attacks
    if not verify_slack_request(request):
        return jsonify({"error": "invalid signature"}), 403

    # Safe to parse JSON now
    data = request.json or {}
    logger.info(f"Received Slack event: {data.get('type')}")

    if data.get("type") == "url_verification":
        return jsonify({"challenge": data.get("challenge")})

    if data.get("type") == "event_callback":
        event = data.get("event", {})

        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return jsonify({"ok": True})

        event_id = data.get("event_id", "")
        if event_id and is_duplicate(event_id):
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

        # Submit task to bounded thread pool
        _executor.submit(process_message, channel_id, user_id, text, thread_ts)

    return jsonify({"ok": True})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"})

if __name__ == "__main__":
    # Removed debug=True for security
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))