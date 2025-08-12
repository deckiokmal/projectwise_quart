# projectwise/routes/main.py
from quart import Blueprint, render_template

main_bp = Blueprint("main", __name__)


@main_bp.route("/main")
async def index():
    # Render halaman utama
    return await render_template("index.html")
