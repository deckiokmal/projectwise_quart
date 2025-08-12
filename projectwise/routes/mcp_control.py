# projectwise/routes/mcp_control.py
from quart import Blueprint, current_app, jsonify
from ..utils.logger import get_logger

mcp_control_bp = Blueprint("mcp_control", __name__)
logger = get_logger(__name__)


@mcp_control_bp.route("/status", methods=["GET"])
async def status():
    """
    Cek status koneksi MCP.
    """
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
async def reconnect():
    """
    Lakukan reconnect MCPClient.
    """
    mcp_client = current_app.extensions["mcp"]

    try:
        await mcp_client._ensure_reconnected()
        return jsonify({"status": "reconnected"})
    except Exception as e:
        logger.error(f"Reconnect failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@mcp_control_bp.route("/shutdown", methods=["POST"])
async def shutdown():
    """
    Tutup koneksi MCPClient.
    """
    mcp_client = current_app.extensions["mcp"]

    try:
        await mcp_client.shutdown()
        return jsonify({"status": "shutdown"})
    except Exception as e:
        logger.error(f"Shutdown failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
