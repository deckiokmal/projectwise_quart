"""
Main blueprint for basic UI endpoints.

This blueprint provides minimal HTML responses for the home page.  In a
real deployment you might serve templates or static assets here.  For
this refactored version we keep it simple and return a JSON message
indicating that the API is available.
"""

from __future__ import annotations

from quart import Blueprint, render_template


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
