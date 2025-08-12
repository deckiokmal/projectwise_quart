"""
Main blueprint for basic UI endpoints.

This blueprint provides minimal HTML responses for the home page.  In a
real deployment you might serve templates or static assets here.  For
this refactored version we keep it simple and return a JSON message
indicating that the API is available.
"""

from __future__ import annotations

from quart import Blueprint, jsonify


main_bp = Blueprint("main", __name__)


@main_bp.route("/")
async def index() -> any: # type: ignore
    """Health check endpoint for the root of the application."""
    return jsonify({"message": "ProjectWise API is running."})