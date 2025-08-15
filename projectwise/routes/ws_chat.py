# projectwise/routes/ws_chat.py
from __future__ import annotations

import json
from typing import Any
from quart import Blueprint, websocket, current_app

from projectwise.utils.logger import get_logger
from ..services.workflow.chat_with_memory import ChatWithMemory


logger = get_logger(__name__)
ws_chat_bp = Blueprint("ws_chat", __name__)


active_rooms: dict[str, dict[str, Any]] = {}  # user_id -> ws


def _get_chat_service() -> ChatWithMemory:
    """
    Ambil instance ChatWithMemory yang konsisten dengan app.extensions.
    Tidak ada .init() khusus; gunakan factory from_quart_app().
    """
    if "chat_ai" not in current_app.extensions:
        current_app.extensions["chat_ai"] = ChatWithMemory.from_quart_app(current_app)
    return current_app.extensions["chat_ai"]


@ws_chat_bp.websocket("/ws/chat/<room_id>/<user_id>")
async def chat_ws_room(room_id: str, user_id: str) -> None:
    room_id = str(room_id); user_id = str(user_id)
    active_rooms.setdefault(room_id, {})
    active_rooms[room_id][user_id] = websocket
    logger.info("[ws] connected | room=%s user=%s", room_id, user_id)
    try:
        chat_ai = _get_chat_service()
        while True:
            data_raw = await websocket.receive()
            data = json.loads(data_raw or "{}")
            user_message = (data.get("message") or "").strip()
            if not user_message:
                await websocket.send(json.dumps({"error": "Pesan kosong"}))
                continue

            reply = await chat_ai.chat(user_id=f"{room_id}:{user_id}", user_message=user_message)
            await _broadcast(room_id, {
                "type": "completed",
                "from": "assistant",
                "content": reply,
            })
    except Exception as e:
        logger.exception("[ws] error | room=%s user=%s", room_id, user_id)
        try:
            await websocket.send(json.dumps({
                "error": "Koneksi WS bermasalah",
                "detail": str(e)[:160]  # ringkas, human-readable
            }))
        except Exception:
            pass
    finally:
        try:
            del active_rooms[room_id][user_id]
            if not active_rooms[room_id]:
                active_rooms.pop(room_id, None)
        except Exception:
            pass
        logger.info("[ws] disconnected | room=%s user=%s", room_id, user_id)


async def _broadcast(room_id: str, message: dict) -> None:
    if room_id not in active_rooms:
        return
    payload = json.dumps(message)
    for uid, ws in list(active_rooms[room_id].items()):
        try:
            await ws.send(payload)
        except Exception:
            del active_rooms[room_id][uid]
