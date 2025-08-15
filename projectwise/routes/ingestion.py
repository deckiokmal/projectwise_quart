# projectwise/routes/ingestion.py
from __future__ import annotations

import httpx
from urllib.parse import urljoin
from projectwise.config import ServiceConfigs

from projectwise.utils.logger import get_logger
from quart import Blueprint, current_app, request, jsonify


logger = get_logger(__name__)
ingestion_bp = Blueprint("ingestion", __name__)


# di module scope, tapi lewat app context saat pertama dipakai:
def _endpoints():
    cfg: ServiceConfigs = current_app.extensions["service_configs"]
    base = cfg.mcp_server_url.rstrip("/") + "/"
    return {
        "upload": urljoin(base, "api/upload-kak-tor/"),
        "check": urljoin(base, "api/check-status/?job_id="),
    }


# Timeout total (detik) untuk koneksi ke MCP
HTTP_TIMEOUT = httpx.Timeout(360.0)   # connect+read+write+pool total
HTTP_LIMITS  = httpx.Limits(max_connections=10, max_keepalive_connections=5)


@ingestion_bp.post("/upload-kak/")
async def upload_kak():
    """
    Menerima multipart/form-data:
      - project_name (str)
      - pelanggan    (str)
      - tahun        (str)
      - file         (file PDF/DOCX KAK/TOR)

    Lalu forward ke MCP (async, httpx) dan kembalikan job_id untuk dipolling.
    """
    eps = _endpoints()
    # === 1) Ambil form & file dari request (Quart) ===
    form = await request.form
    files_in = await request.files

    project_name = (form.get("project_name") or "").strip()
    pelanggan    = (form.get("pelanggan") or "").strip()
    tahun        = (form.get("tahun") or "").strip()
    kak_file     = files_in.get("file")

    # Validasi input minimal
    if not (project_name and pelanggan and tahun and kak_file):
        return jsonify({"error": "Semua field wajib diisi (project_name, pelanggan, tahun, file)."}), 400

    # Siapkan body untuk MCP
    data = {
        "project_name": project_name,
        "pelanggan": pelanggan,
        "tahun": tahun,
    }

    # FileStorage dari Quart punya .filename, .mimetype, .stream
    # Untuk httpx, boleh kirim (filename, fileobj, content_type)
    files = {"file": (kak_file.filename, kak_file.stream, kak_file.mimetype)}

    # === 2) Kirim ke MCP (async) ===
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, limits=HTTP_LIMITS) as client:
            resp = await client.post(eps["upload"], data=data, files=files)
            resp.raise_for_status()
            
    except httpx.HTTPError as e:
        current_app.logger.exception("Gagal mengirim ke MCP")
        return jsonify({"error": f"Gagal mengirim ke MCP: {e}"}), 502

    # === 3) Ambil job_id dari respons MCP ===
    try:
        mcp_json = resp.json()
    except ValueError:
        return jsonify({"error": "Response MCP bukan JSON yang valid."}), 502

    job_id = mcp_json.get("job_id")
    if not job_id:
        return jsonify({"error": "MCP tidak mengembalikan job_id."}), 502

    # === 4) Kembalikan job_id + URL polling status (proxy kita) ===
    return jsonify(
        {
            "job_id": job_id,
            "status_url": f"/proxy-check-status/{job_id}",
            "message": "Upload diterima, silakan polling status ingestion.",
        }
    ), 202


@ingestion_bp.get("/proxy-check-status/<job_id>")
async def proxy_check_status(job_id: str):
    """
    Proxy GET → MCP /check-status, lalu sederhanakan payload untuk frontend.
    """
    eps = _endpoints()
    url = f"{eps['check']}{job_id}"
    
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, limits=HTTP_LIMITS) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        current_app.logger.exception("Gagal mengambil status dari MCP Server")
        return jsonify({"error": f"Gagal fetch status MCP: {e}"}), 502

    try:
        data = resp.json()
    except ValueError:
        return jsonify({"error": "Response MCP bukan JSON yang valid."}), 502

    # Contoh bentuk data MCP yang diharapkan:
    # {
    #   "status": "pending|processing|success|failed",
    #   "message": "Keterangan",
    #   "result": { "summary": "...", "summary_file": "..." }
    # }
    status  = data.get("status")
    message = data.get("message")
    result  = data.get("result") or {}
    summary = result.get("summary")
    summary_file = result.get("summary_file")

    return jsonify(
        {
            "status": status,
            "message": message,
            "summary": summary,
            "summary_location": summary_file,
        }
    ), 200
