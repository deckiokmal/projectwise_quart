# projectwise/routes/ws_chat.py
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Dict

from quart import Blueprint, websocket, current_app
from projectwise.utils.logger import get_logger

# Broker: fallback import jika file Anda berada di root project
try:
    from projectwise.utils.websocket_broker import Broker
except Exception:  # pragma: no cover
    from websocket_broker import Broker  # type: ignore

logger = get_logger(__name__)
ws_chat_bp = Blueprint("ws_chat", __name__)

# Broker per-room agar isolasi pesan rapi dan mudah dibersihkan
_room_brokers: Dict[str, Broker] = {}


def _get_room_broker(room_id: str) -> Broker:
    """Ambil/buat Broker khusus untuk room tertentu."""
    broker = _room_brokers.get(room_id)
    if broker is None:
        broker = Broker()
        _room_brokers[room_id] = broker
    return broker


def _is_conn_reset(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        isinstance(exc, (ConnectionResetError, BrokenPipeError))
        or "10054" in msg
        or "forcibly closed" in msg
        or "connection reset" in msg
    )


@ws_chat_bp.websocket("/ws/chat/<room_id>/<user_id>")
async def chat_ws_room(room_id: str, user_id: str) -> None:
    """
    WebSocket sederhana berbasis room dengan broker internal.
    Kontrak pesan:
      - Client -> Server: {"message": "<teks>"}
      - Server -> Client (broadcast): {
            "room": "<room_id>",
            "type": "message" | "system",
            "from": "<user_id|system>",
            "content": "<teks>"
        }

    Catatan:
    - Tidak ada ChatWithMemory, murni WS broadcast per-room.
    - Pesan pengirim tidak di-echo kembali ke pengirim (hindari duplikasi di UI).
    """
    room_id = str(room_id)
    user_id = str(user_id)
    broker = _get_room_broker(room_id)

    db = current_app.extensions["db"]

    logger.info("[ws] connected | room=%s user=%s", room_id, user_id)

    async def _outbound_to_client() -> None:
        """Kirim semua pesan broker room ke klien ini, kecuali pesan yang ia kirim sendiri."""
        async for raw in broker.subscribe():
            # Broker menyimpan string; pastikan JSON dan filter room/echo
            try:
                payload = json.loads(raw)
            except Exception:
                # Jika format tidak valid, balut sebagai system message
                payload = {
                    "room": room_id,
                    "type": "system",
                    "from": "system",
                    "content": str(raw),
                }

            if payload.get("room") != room_id:
                continue  # abaikan pesan room lain (jika ada)
            if payload.get("type") == "message" and payload.get("from") == user_id:
                continue  # hindari echo ke pengirim

            try:
                await websocket.send(json.dumps(payload))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if _is_conn_reset(e):
                    # putus koneksi = normal; hentikan loop
                    logger.info(
                        "client closed while sending | room=%s user=%s",
                        room_id,
                        user_id,
                    )
                    break
                # selain itu tidak fatal
                logger.debug(
                    "[ws] drop outbound | room=%s user=%s err=%r", room_id, user_id, e
                )
                break

    # Task background untuk menyalurkan pesan dari broker ke klien
    outbound_task = asyncio.create_task(_outbound_to_client())

    # kirim history ke client
    try:
        items = await db.query_ws_recent(room_id=room_id)
        if items:
            await websocket.send(
                json.dumps({"room": room_id, "type": "history", "items": items})
            )
    except Exception:
        logger.exception("Send history gagal | room=%s user=%s", room_id, user_id)

    # Umumkan join (broadcast system)
    await broker.publish(
        json.dumps(
            {
                "room": room_id,
                "type": "system",
                "from": "system",
                "content": f"{user_id} joined",
            }
        )
    )

    # Simpan ke db
    asyncio.create_task(
        db.save_ws_message(room_id, "system", "system", f"{user_id} joined")
    )

    try:
        while True:
            raw = await websocket.receive()
            try:
                data = json.loads(raw or "{}")
            except Exception:
                data = {"message": str(raw or "").strip()}

            # explicit LEAVE
            if data.get("type") == "leave":
                await broker.publish(
                    json.dumps(
                        {
                            "room": room_id,
                            "type": "system",
                            "from": "system",
                            "content": f"{user_id} left",
                        }
                    )
                )
                asyncio.create_task(
                    db.save_ws_message(room_id, "system", "system", f"{user_id} left")
                )
                break

            # normal message
            msg = (data.get("message") or "").strip()
            if not msg:
                continue
            await broker.publish(
                json.dumps(
                    {
                        "room": room_id,
                        "type": "message",
                        "from": user_id,
                        "content": msg,
                    }
                )
            )
            asyncio.create_task(db.save_ws_message(room_id, user_id, "message", msg))

    except asyncio.CancelledError:
        raise
    except Exception as e:
        if _is_conn_reset(e):
            logger.info(
                "[ws] client disconnected (reset) | room=%s user=%s", room_id, user_id
            )
        else:
            logger.exception("[ws] error | room=%s user=%s", room_id, user_id)
    finally:
        # hentikan outbound task & lepas subscriber queue dari broker
        with contextlib.suppress(Exception):
            outbound_task.cancel()
            await outbound_task
