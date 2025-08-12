# projectwise/routes/chat_ws_room.py
import json
from quart import Blueprint, websocket, current_app

chat_ws_room_bp = Blueprint("chat_ws_room", __name__)

# Menyimpan koneksi aktif: {room_id: {user_id: websocket}}
active_rooms = {}


@chat_ws_room_bp.websocket("/ws/chat/<room_id>/<user_id>")
async def chat_ws_room(room_id, user_id):
    """
    WebSocket chat dengan dukungan multi-room.
    - URL format: /ws/chat/<room_id>/<user_id>
    - Setiap room punya riwayat terpisah di STM & LTM
    """
    room_id = str(room_id)
    user_id = str(user_id)

    # Simpan koneksi di memory
    active_rooms.setdefault(room_id, {})
    active_rooms[room_id][user_id] = websocket
    print(f"[Room {room_id}] User {user_id} connected")

    try:
        while True:
            # Terima pesan dari user
            data_raw = await websocket.receive()
            data = json.loads(data_raw)
            user_message = data.get("message", "").strip()

            if not user_message:
                await websocket.send(json.dumps({"error": "Pesan kosong"}))
                continue

            # Ambil ekstensi
            mcp_client = current_app.extensions["mcp"]
            stm = current_app.extensions["short_term_memory"]
            ltm = current_app.extensions["long_term_memory"]
            service_configs = current_app.extensions["service_configs"]

            # Ambil memori relevan dari LTM
            relevant_memories = await ltm.get_memories(
                user_message, user_id=f"{room_id}:{user_id}"
            )
            memories_block = (
                "\n".join(f"- {m}" for m in relevant_memories) or "[Tidak ada]"
            )

            # Bangun prompt
            system_prompt = (
                "Anda adalah ProjectWise, asisten AI presales & PM.\n"
                f"Memori relevan:\n{memories_block}"
            )
            messages_for_llm = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ]

            # Kirim info start ke semua member room
            await broadcast(
                room_id, {"type": "start", "from": user_id, "message": user_message}
            )

            # Streaming response dari MCPClient
            assistant_reply_parts = []
            async with mcp_client.llm.responses.stream(
                model=service_configs.llm_model,
                input=messages_for_llm,
            ) as stream:
                async for event in stream:
                    if event.type == "response.output_text.delta":
                        assistant_reply_parts.append(event.delta)
                        await broadcast(
                            room_id,
                            {
                                "type": "delta",
                                "from": "assistant",
                                "content": event.delta,
                            },
                        )
                    elif event.type == "response.completed":
                        await broadcast(
                            room_id, {"type": "completed", "from": "assistant"}
                        )
                    elif event.type == "response.error":
                        await broadcast(
                            room_id, {"type": "error", "error": event.error}
                        )

            # Gabungkan jawaban penuh
            assistant_reply = "".join(assistant_reply_parts)

            # Simpan ke STM (room_id:user_id)
            await stm.save(f"{room_id}:{user_id}", "user", user_message)
            await stm.save(f"{room_id}:{user_id}", "assistant", assistant_reply)

            # Simpan ke LTM
            await ltm.add_conversation(
                messages_for_llm + [{"role": "assistant", "content": assistant_reply}],
                user_id=f"{room_id}:{user_id}",
            )

    except Exception as e:
        print(f"[Room {room_id}] Error untuk user {user_id}: {e}")

    finally:
        # Hapus koneksi saat disconnect
        if room_id in active_rooms and user_id in active_rooms[room_id]:
            del active_rooms[room_id][user_id]
        print(f"[Room {room_id}] User {user_id} disconnected")


async def broadcast(room_id, message: dict):
    """Kirim pesan ke semua user dalam 1 room."""
    if room_id not in active_rooms:
        return
    message_json = json.dumps(message)
    for uid, ws in list(active_rooms[room_id].items()):
        try:
            await ws.send(message_json)
        except Exception:
            # Jika gagal kirim, hapus koneksi
            del active_rooms[room_id][uid]
