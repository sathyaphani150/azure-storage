"""Reliable local runner when Azure Functions Core Tools is unavailable.

Runs the same FastAPI dashboard and calls the same process_event function as the
Azure Queue trigger. The deployed Function App still uses process_document.
"""
import json
import os
import socket
import base64
import threading
import time
from pathlib import Path

import uvicorn
from azure.storage.queue import QueueClient


def load_local_settings() -> None:
    """Load uncommitted Function settings for direct local execution."""
    settings_file = Path(__file__).with_name("local.settings.json")
    if not settings_file.exists():
        raise RuntimeError("Create local.settings.json from local.settings.example.json first.")
    values = json.loads(settings_file.read_text(encoding="utf-8")).get("Values", {})
    os.environ.update({key: str(value) for key, value in values.items()})


load_local_settings()
os.environ.setdefault("LOCAL_QUEUE_ON_UPLOAD", "true")

from function_app import api, connection_string, ensure_infrastructure, process_event


def decode_queue_event(content: str) -> dict:
    """Decode raw SDK messages and Azure Functions' Base64 queue format."""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        decoded = base64.b64decode(content, validate=True).decode("utf-8")
        payload = json.loads(decoded)
    event = payload[0] if isinstance(payload, list) else payload
    if not isinstance(event, dict):
        raise ValueError("Queue message must contain a JSON event object.")
    return event


def poll_queue() -> None:
    ensure_infrastructure()
    queue = QueueClient.from_connection_string(connection_string(), "pdf-processing")
    while True:
        try:
            for message in queue.receive_messages(messages_per_page=1, visibility_timeout=60):
                process_event(decode_queue_event(message.content))
                queue.delete_message(message.id, message.pop_receipt)
        except Exception as exc:
            print(f"Queue worker error: {exc}", flush=True)
        time.sleep(2)


def require_available_port(host: str, port: int) -> None:
    """Fail with an actionable message before starting worker threads."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError as exc:
        raise SystemExit(
            f"Port {port} is already in use. StockLens may already be running at "
            f"http://{host}:{port}/. Stop that process before starting another server."
        ) from exc
    finally:
        probe.close()


if __name__ == "__main__":
    require_available_port("127.0.0.1", 7071)
    threading.Thread(target=poll_queue, daemon=True).start()
    uvicorn.run(api, host="127.0.0.1", port=7071)
