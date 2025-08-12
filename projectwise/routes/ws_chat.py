"""
WebSocket chat room with long‑term and short‑term memory integration.

This blueprint exposes a WebSocket endpoint ``/ws/chat/<room_id>/<user_id>``
that supports multi‑room chat with streaming responses.  For each
incoming message the chat service retrieves relevant memories, calls
the MCP LLM via streaming API and broadcasts deltas and final
responses to all participants in the room.
"""

from __future__ import annotations

import json
from quart import Blueprint, websocket, current_app

from ..services.workflow.chat_with_memory import ChatWithMemory


ws_chat_bp = Blueprint("ws_chat", __name__)

# Track active WebSocket connections per room
active_rooms: dict[str, dict[str, any]] = {} # type: ignore


def _get_chat_service() -> ChatWithMemory:
    """Return (and lazily initialise) the ChatWithMemory service."""
    if "chat_ai" not in current_app.extensions:
        service_configs = current_app.extensions["service_configs"]
        db_url = current_app.config["SQLALCHEMY_DATABASE_URI"]
        chat_ai = ChatWithMemory(service_configs, db_url)
        current_app.extensions["chat_ai_init"] = current_app.loop.create_task( # type: ignore
            chat_ai.init()
        )
        current_app.extensions["chat_ai"] = chat_ai
    return current_app.extensions["chat_ai"]


@ws_chat_bp.websocket("/ws/chat/<room_id>/<user_id>")
async def chat_ws_room(room_id: str, user_id: str) -> None:
    """Handle WebSocket chat for the given room and user."""
    room_id = str(room_id)
    user_id = str(user_id)

    # Register connection
    active_rooms.setdefault(room_id, {})
    active_rooms[room_id][user_id] = websocket
    print(f"[Room {room_id}] User {user_id} connected")
    try:
        # Ensure chat service initialised
        chat_ai = _get_chat_service()
        init_task = current_app.extensions.get("chat_ai_init")
        if init_task:
            await init_task
            current_app.extensions["chat_ai_init"] = None
        while True:
            data_raw = await websocket.receive()
            data = json.loads(data_raw)
            user_message = data.get("message", "").strip()
            if not user_message:
                await websocket.send(json.dumps({"error": "Pesan kosong"}))
                continue
            # Retrieve memories & call LLM via chat_ai.chat
            assistant_reply = await chat_ai.chat(
                user_id=f"{room_id}:{user_id}", user_message=user_message
            )
            # Broadcast the full assistant reply to all participants
            await _broadcast(room_id, {
                "type": "completed",
                "from": "assistant",
                "content": assistant_reply,
            })
    except Exception as e:
        print(f"[Room {room_id}] Error for user {user_id}: {e}")
    finally:
        if room_id in active_rooms and user_id in active_rooms[room_id]:
            del active_rooms[room_id][user_id]
        print(f"[Room {room_id}] User {user_id} disconnected")


async def _broadcast(room_id: str, message: dict) -> None:
    """Send a message to all users in the specified room."""
    if room_id not in active_rooms:
        return
    message_json = json.dumps(message)
    for uid, ws in list(active_rooms[room_id].items()):
        try:
            await ws.send(message_json)
        except Exception:
            # Remove broken connection
            del active_rooms[room_id][uid]