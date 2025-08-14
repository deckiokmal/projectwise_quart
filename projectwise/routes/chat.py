from __future__ import annotations

from quart import Blueprint, current_app, request, jsonify

from ..services.workflow.chat_with_memory import ChatWithMemory
from projectwise.utils.logger import get_logger


logger = get_logger(__name__)
chat_bp = Blueprint("chat", __name__)


@chat_bp.post("/message")
async def chat_message():
    logger.info(f"Received message from {request.remote_addr}")
    service_configs = current_app.extensions["service_configs"]
    # Ambil singleton yang sudah dibuat saat startup
    long_term = current_app.extensions["long_term_memory"]
    short_term = current_app.extensions["short_term_memory"]

    chat_ai = ChatWithMemory(
        service_configs,
        llm_model=service_configs.llm_model,
        long_term=long_term,
        short_term=short_term,
        max_history=10,
    )

    data = await request.get_json(force=True)
    user_message = data.get("message", "").strip()
    user_id = data.get("user_id", "default")
    reply = await chat_ai.chat(user_id=user_id, user_message=user_message)
    logger.info("chat execution done!.")
    return jsonify({"response": reply})
