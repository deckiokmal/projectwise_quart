# projectwise/utils/response.py
from __future__ import annotations
from quart import jsonify
from .helper import wib_now_iso


def make_response(status: str, message: str, http_status: int = 500):
    """Buat response JSON untuk Humanis Feedback user.

    Args:
        status (str): success | warning | error
        message (str): pesan humanis untuk user.
        http_status (int, optional): HTTP status code. Defaults to 500.

    Returns:
        dict: {status, message, time}
    """
    return jsonify(
        {
            "status": status,
            "message": message,
            "time": wib_now_iso(),
        }
    ), http_status
