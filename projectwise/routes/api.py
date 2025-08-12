"""
API blueprint exposing analysis and calculation endpoints.

This blueprint provides REST‑style endpoints for the various analysis
tools implemented in :mod:`projectwise.services.analysis`.  Each
endpoint accepts JSON input, invokes the corresponding async
function and returns a JSON response.  The blueprint is mounted
under the ``/api`` prefix in the application factory.
"""

from __future__ import annotations

from quart import Blueprint, request, jsonify

from ..services.analysis.competitor_analysis import competitor_analysis
from ..services.analysis.price_analysis import price_analysis
from ..services.analysis.project_risk_analysis import project_risk_analysis
from ..services.analysis.product_calculator import product_calculator


api_bp = Blueprint("api", __name__)


@api_bp.post("/competitor")
async def api_competitor() -> tuple[any, int] | any: # type: ignore
    """Analyse competitors based on a project description."""
    data = await request.get_json(force=True)
    description = data.get("project_description", "").strip()
    if not description:
        return jsonify({"error": "project_description is required"}), 400
    result = await competitor_analysis(description)
    return jsonify(result)


@api_bp.post("/price")
async def api_price() -> tuple[any, int] | any: # type: ignore
    """Perform a price analysis for a set of product details."""
    data = await request.get_json(force=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object expected"}), 400
    result = await price_analysis(data)
    return jsonify(result)


@api_bp.post("/risk")
async def api_risk() -> tuple[any, int] | any: # type: ignore
    """Assess project risks based on a description."""
    data = await request.get_json(force=True)
    description = data.get("project_description", "").strip()
    if not description:
        return jsonify({"error": "project_description is required"}), 400
    result = await project_risk_analysis(description)
    return jsonify(result)


@api_bp.post("/calculate")
async def api_calculate() -> tuple[any, int] | any: # type: ignore
    """Compute total and discounted price for a list of items."""
    data = await request.get_json(force=True)
    items = data.get("items")
    discount = data.get("discount_rate", 0.0)
    if not isinstance(items, list):
        return jsonify({"error": "items must be a list of objects"}), 400
    try:
        discount_rate = float(discount)
    except Exception:
        return jsonify({"error": "discount_rate must be numeric"}), 400
    result = await product_calculator(items, discount_rate)
    return jsonify(result)
