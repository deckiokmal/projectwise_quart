"""
Chat HTTP API using the ChatWithMemory service.

This blueprint defines endpoints for sending chat messages to the AI
assistant.  The chat service maintains both long‑term and short‑term
memory and is initialised lazily on first use.  Responses are
returned as JSON.
"""

from __future__ import annotations

from quart import Blueprint, current_app, request, jsonify

from ..services.workflow.chat_with_memory import ChatWithMemory


chat_bp = Blueprint("chat", __name__)


def _get_chat_service() -> ChatWithMemory:
    """Return a cached ChatWithMemory instance attached to the app."""
    if "chat_ai" not in current_app.extensions:
        service_configs = current_app.extensions["service_configs"]
        db_url = current_app.config["SQLALCHEMY_DATABASE_URI"]
        chat_ai = ChatWithMemory(service_configs, db_url)
        # Initialise asynchronously and store the task; callers will await
        current_app.extensions["chat_ai_init"] = current_app.loop.create_task(
            chat_ai.init()
        )
        current_app.extensions["chat_ai"] = chat_ai
    return current_app.extensions["chat_ai"]


@chat_bp.post("/message")
async def chat_message() -> tuple[any, int] | any:
    """Process a chat message and return the assistant's reply."""
    data = await request.get_json(force=True)
    user_message = data.get("message", "").strip()
    user_id = data.get("user_id", "default")
    if not user_message:
        return jsonify({"error": "message is required"}), 400
    chat_ai = _get_chat_service()
    # Await initialisation if still in progress
    init_task = current_app.extensions.get("chat_ai_init")
    if init_task:
        await init_task
        current_app.extensions["chat_ai_init"] = None
    reply = await chat_ai.chat(user_id=user_id, user_message=user_message)
    return jsonify({"reply": reply})
