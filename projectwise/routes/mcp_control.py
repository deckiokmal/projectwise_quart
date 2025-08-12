"""
Blueprint to monitor and control the MCP client connection.

This blueprint exposes endpoints to check the connection status,
trigger a reconnect and gracefully shutdown the MCP client.  These
are useful for operational monitoring and remote management of the
running server.
"""

from __future__ import annotations

from quart import Blueprint, current_app, jsonify
from ..utils.logger import get_logger


mcp_control_bp = Blueprint("mcp_control", __name__)
logger = get_logger(__name__)


@mcp_control_bp.route("/status", methods=["GET"])
async def status() -> any: # type: ignore
    """Return the status of the MCP client connection."""
    mcp_client = current_app.extensions["mcp"]
    service_configs = current_app.extensions["service_configs"]
    is_connected = getattr(mcp_client, "_connected", False)
    return jsonify(
        {
            "connected": is_connected,
            "mcp_server_url": service_configs.mcp_server_url,
            "llm_model": service_configs.llm_model,
        }
    )


@mcp_control_bp.route("/reconnect", methods=["POST"])
async def reconnect() -> tuple[any, int] | any: # type: ignore
    """Force the MCP client to reconnect."""
    mcp_client = current_app.extensions["mcp"]
    try:
        await mcp_client._ensure_reconnected()
        return jsonify({"status": "reconnected"})
    except Exception as e:
        logger.error(f"Reconnect failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@mcp_control_bp.route("/shutdown", methods=["POST"])
async def shutdown() -> tuple[any, int] | any: # type: ignore
    """Shutdown the MCP client connection."""
    mcp_client = current_app.extensions["mcp"]
    try:
        await mcp_client.shutdown()
        return jsonify({"status": "shutdown"})
    except Exception as e:
        logger.error(f"Shutdown failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
