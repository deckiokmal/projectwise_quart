"""
Analysis tools for ProjectWise.

This package contains asynchronous functions that implement various
analysis tasks used by the presales AI agent.  These include
competitor analysis, price analysis, risk assessment and product
cost calculations.  Each function returns a structured result that
can easily be serialised as JSON in API responses.
"""

__all__ = [
    "competitor_analysis",
    "price_analysis",
    "project_risk_analysis",
    "product_calculator",
]