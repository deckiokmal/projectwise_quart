# projectwise/routes/mcp_control.py
from __future__ import annotations

import asyncio
from quart import Blueprint, current_app, jsonify

from ..utils.logger import get_logger
from ..services.mcp.client import MCPClient


logger = get_logger(__name__)
mcp_control_bp = Blueprint("mcp_control", __name__)


CONNECT_TIMEOUT_SECS = 7

@mcp_control_bp.post("/connect")
async def connect():
    """Connect to the MCP server."""
    lock = current_app.extensions["mcp_lock"]
    status = current_app.extensions["mcp_status"]

    if current_app.extensions["mcp"]:
        status.update({"connected": True, "connecting": False, "error": None})
        return jsonify({"status": "already_connected"})

    async with lock:
        if current_app.extensions["mcp"] is not None:
            status.update({"connected": True, "connecting": False, "error": None})
            return jsonify({"status": "already_connected"})

        status.update({"connecting": True, "error": None})

        try:
            client = MCPClient()
            client = await asyncio.wait_for(
                client.__aenter__(), timeout=CONNECT_TIMEOUT_SECS
            )
            current_app.extensions["mcp"] = client
            status.update({"connected": True})
            return jsonify({"status": "connected"})
        except asyncio.TimeoutError:
            status.update({"connected": False, "error": f"Connect timeout {CONNECT_TIMEOUT_SECS}s"})
            logger.exception("MCP connect timeout")
            return jsonify({
                "error": status["error"],
                "code": "TIMEOUT",
                "hint": "Periksa MCP Server & jaringan. Coba lagi sebentar."
            }), 504, {"Retry-After": "5"}

        except Exception as e:
            status.update({"connected": False, "error": str(e)})
            logger.exception("MCP connect failed")
            return jsonify({
                "error": str(e),
                "code": "CONNECT_FAILED",
                "hint": "Cek URL MCP, kredensial, atau log server MCP."
            }), 500
        finally:
            # ANTI-STUCK: apapun hasilnya, pastikan connecting=False
            status.update({"connecting": False})


@mcp_control_bp.post("/disconnect")
async def disconnect():
    """Disconnect from the MCP server."""
    client = current_app.extensions["mcp"]
    if client is None:
        current_app.extensions["mcp_status"].update(
            {"connected": False, "connecting": False, "error": None}
        )
        return jsonify({"status": "already_disconnected"})

    try:
        await client.__aexit__(None, None, None)
    finally:
        current_app.extensions["mcp"] = None
        current_app.extensions["mcp_status"].update(
            {"connected": False, "connecting": False, "error": None}
        )

    return jsonify({"status": "disconnected"})


@mcp_control_bp.get("/status")
async def status():
    st = current_app.extensions["mcp_status"]
    cfg = current_app.extensions["service_configs"]
    return jsonify(
        {
            "connected": bool(current_app.extensions.get("mcp")),
            "connecting": bool(st.get("connecting")),
            "error": st.get("error"),
            "mcp_server_url": cfg.mcp_server_url,
            "llm_model": cfg.llm_model,
        }
    )


@mcp_control_bp.post("/reconnect")
async def reconnect():
    """Reconnect to the MCP server."""
    await disconnect()
    return await connect()
