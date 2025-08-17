# projectwise/routes/main.py
from __future__ import annotations

from projectwise.utils.logger import get_logger
from quart import Blueprint, render_template, current_app, jsonify


logger = get_logger(__name__)
main_bp = Blueprint("main", __name__)


@main_bp.route("/")
async def index() -> any:  # type: ignore
    """Render the home page with a brief description and links."""
    return await render_template("index.html")


@main_bp.get("/rooms")
async def war_rooms() -> any:  # type: ignore
    """Render the WebSocket rooms page."""
    return await render_template("ws_room.html")


@main_bp.get("/proposal")
async def proposal() -> any:  # type: ignore
    """Render the HTTP chat interface."""
    return await render_template("proposal.html")


@main_bp.get("/analysis")
async def analysis() -> any:  # type: ignore
    """Render the analysis tools page."""
    return await render_template("analysis.html")


@main_bp.get("/healt/memory")
async def memory_health():
    ltm = current_app.extensions["long_term_memory"]
    return jsonify(ltm.health()), 200
