# projectwise/routes/chat.py
from __future__ import annotations

from typing import Any, Dict, Tuple, Union, Optional

from quart import Blueprint, current_app, request, Response, jsonify

from projectwise.utils.logger import get_logger
from projectwise.utils.helper import response_error_toast, response_success_with_toast

from projectwise.services.workflow.intent_classification import route_based_on_intent
from projectwise.services.llm_chain.llm_chains import LLMChains
from projectwise.services.workflow.chat_with_memory import ChatWithMemory


chat_bp = Blueprint("chat", __name__)
logger = get_logger(__name__)

# Tipe alias untuk hasil handler
HandlerReply = Union[str, Dict[str, Any], Response, Tuple[Response, int]]

# Konstanta lokal
HISTORY_LIMIT = 5


@chat_bp.post("/message")
async def chat_message() -> Tuple[Response, int]:
    """
    Endpoint utama percakapan.

    High level flow:
      1) Ambil payload user.
      2) Siapkan dependency dari app.extensions (MCP, LLM, STM/LTM, service configs).
      3) Definisikan handler per-intent (KAK, Web, Proposal, Calc, Other).
      4) Klasifikasikan intent & route ke handler terkait.
      5) Persist ke Short-Term & Long-Term memory (disentralisasi di sini).
      6) Normalisasi & kirim HTTP response.

    Returns:
        (Response, http_status)
    """
    # ----------------------------
    # 1) Ambil payload user
    # ----------------------------
    try:
        request_data = await request.get_json(force=True)
    except Exception:
        logger.exception("[chat] Payload bukan JSON valid.")
        return jsonify({"status": "error", "message": "Payload harus JSON."}), 400

    user_id: str = (request_data.get("user_id") or "default").strip()
    user_text: str = (request_data.get("message") or "").strip()

    if not user_text:
        return jsonify({"status": "error", "message": "Pesan tidak boleh kosong."}), 400

    logger.info(
        "[chat] POST /message start | user=%s | msg.len=%d", user_id, len(user_text)
    )

    # ----------------------------
    # 2) Ambil dependency dari app
    # ----------------------------
    app = current_app
    mcp_client = app.extensions["mcp"]
    llm_client = LLMChains(prefer="chat")
    short_term = app.extensions["short_term_memory"]
    long_term = app.extensions["long_term_memory"]
    service_configs = app.extensions["service_configs"]
    mcp_status = app.extensions.get("mcp_status", {"connected": False})

    memory_orchestrator = ChatWithMemory(
        long_term=long_term,
        short_term=short_term,
        service_configs=service_configs,
        max_history=HISTORY_LIMIT,
    )

    # ----------------------------
    # Helper: MCP connection check
    # ----------------------------
    def mcp_required() -> Optional[Tuple[Response, int]]:
        """Early-return Response apabila MCP belum terhubung."""
        if not mcp_status.get("connected"):
            logger.warning("[chat] MCP belum terhubung.")
            return response_error_toast(
                status="error", message="MCP belum terhubung.", http_status=503
            )
        return None

    # ----------------------------
    # Helper: safe call MCP tool
    # ----------------------------
    async def safe_call_mcp_tool(tool_name: str, args: Dict[str, Any]) -> Any:
        """
        Membungkus pemanggilan MCP tool dengan penanganan error dan logging standard.

        Args:
            tool_name: Nama tool MCP.
            args: Argumen untuk tool tersebut.

        Returns:
            Hasil dari MCP tool (apa adanya). Jika gagal, mengembalikan dict error ringkas.
        """
        try:
            logger.info(
                "[chat] MCP call_tool start | tool=%s | args=%s", tool_name, args
            )
            result = await mcp_client.call_tool(tool_name, args)
            logger.info("[chat] MCP call_tool done  | tool=%s", tool_name)
            return result
        except Exception:
            logger.exception("[chat] Gagal memanggil MCP tool '%s'.", tool_name)
            return {
                "status": "error",
                "message": f"Gagal memanggil tool '{tool_name}'. Silakan coba lagi.",
            }

    # ============================================================
    # 3) Handlers per-intent
    # ============================================================
    async def _handle_kak(q: str, _cls: Any) -> HandlerReply:
        """
        Handler untuk intent 'KAK Analyzer':
        - Membangun query retrieval berbasis history & long-term memory.
        - Mengambil konteks dari MCP 'retrieval_tool'.
        - Meminta LLM melakukan function-call 'read_kak_analysis_tool' bila perlu.
        - Menghasilkan jawaban final dari LLM.
        """
        # Pastikan MCP terhubung
        mcp_error = mcp_required()
        if mcp_error:
            return mcp_error

        # 0) Bangun query pencarian untuk retrieval (maks 300 char, hanya text)
        try:
            search_query_text = await llm_client.chat_completions_text(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Hasilkan maksimal '300 text' query instruction untuk pencarian retrieval "
                            "vectordb yang tepat berdasarkan informasi yang diberikan. "
                            "Output hanya text query tanpa penjelasan dan format apapun."
                        ),
                    },
                    {
                        "role": "system",
                        "content": f"Conversation History:\n{await short_term.get_history(user_id, limit=HISTORY_LIMIT)}",
                    },
                    {
                        "role": "system",
                        "content": f"Relevan memory:\n{await long_term.get_memories_v2(query=q, user_id=user_id, limit=HISTORY_LIMIT)}",
                    },
                    {"role": "user", "content": q},
                ]
            )
        except Exception:
            logger.exception("[chat] Gagal membangun query retrieval (LLM).")
            return "Maaf, terjadi kendala saat menyiapkan pencarian konteks."

        # 0.1) Ambil dokumen relevan dari MCP
        retrieve = await safe_call_mcp_tool(
            "retrieval_tool", {"query": search_query_text, "k": 5}
        )

        # 1) Siapkan pesan awal untuk LLM
        messages = [
            {
                "role": "system",
                "content": (
                    "Anda adalah asisten yang membantu menjawab pertanyaan berdasarkan hasil analysis proyek. "
                    "Gunakan tools yang tersedia untuk mendapatkan informasi yang dibutuhkan."
                ),
            },
            {"role": "system", "content": f"Context dari MCP:\n{retrieve}"},
            {
                "role": "system",
                "content": f"Relevan memory:\n{await long_term.get_memories_v2(query=q, user_id=user_id, limit=HISTORY_LIMIT)}",
            },
            {"role": "user", "content": q},
        ]

        # 2) Skema tools (OpenAI Chat Completions)
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_kak_analysis_tool",
                    "description": "Read KAK analysis.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "filename": {
                                "type": "string",
                                "description": "Nama file KAK.",
                            },
                            "pelanggan": {
                                "type": "string",
                                "description": "Nama pelanggan.",
                            },
                            "project": {
                                "type": "string",
                                "description": "Nama project.",
                            },
                            "tahun": {
                                "type": "string",
                                "description": "Tahun project KAK (YYYY).",
                            },
                        },
                        "required": ["filename", "pelanggan", "project", "tahun"],
                        "additionalProperties": False,
                    },
                },
            }
        ]

        # 3) Minta model melakukan function-call
        try:
            tool_calls, _raw = await llm_client.chat_function_call(
                messages=messages, tools=tools, tool_choice="auto"
            )
        except Exception:
            logger.exception("[chat] Gagal menghasilkan function-call (LLM).")
            return "Maaf, terjadi kendala saat menyiapkan langkah-langkah analisis."

        # 4) Eksekusi tool yang diminta model dan lampirkan hasilnya
        if tool_calls:
            for call in tool_calls:
                tool_name = call["name"]
                tool_args = call["arguments"]  # sudah dict
                out = await safe_call_mcp_tool(tool_name, tool_args)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": tool_name,
                        "content": str(out),  # OpenAI merekomendasikan string
                    }
                )

        # 5) Tambahkan instruksi kecil untuk merangkum hasil tool
        messages.append(
            {
                "role": "user",
                "content": "Gunakan hasil tool di atas lalu berikan jawaban final.",
            }
        )

        # 6) Jawaban final dari model (tanpa tools)
        try:
            final_text = await llm_client.chat_completions_text(messages=messages)
        except Exception:
            logger.exception("[chat] Gagal menghasilkan jawaban final (LLM).")
            return "Maaf, terjadi kendala saat menghasilkan jawaban final."

        return final_text

    async def _handle_web(q: str, _cls: Any) -> HandlerReply:
        """
        Handler untuk intent 'Web Search':
        - Membangun query pencarian web dari history/memori.
        - Memanggil MCP 'websearch_tool'.
        - Menggabungkan hasil pencarian + memori untuk merumuskan jawaban.
        """
        mcp_error = mcp_required()
        if mcp_error:
            return mcp_error

        try:
            search_query_text = await llm_client.chat_completions_text(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Hasilkan maksimal '300 text' query instruction untuk pencarian web yang tepat "
                            "berdasarkan informasi memory. Output hanya text query tanpa penjelasan dan format apapun."
                        ),
                    },
                    {
                        "role": "system",
                        "content": f"Memory:\n{await short_term.get_history(user_id, limit=HISTORY_LIMIT)}",
                    },
                    {"role": "user", "content": q},
                ]
            )
        except Exception:
            logger.exception("[chat] Gagal membangun query web (LLM).")
            return "Maaf, terjadi kendala saat menyiapkan pencarian web."

        search_results = await safe_call_mcp_tool(
            "websearch_tool", {"query": search_query_text, "max_results": 7}
        )

        try:
            result_text = await llm_client.chat_completions_text(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Bertindak sebagai asisten yang membantu menjawab pertanyaan berdasarkan hasil pencarian. "
                            "Berikan informasi apapun yang dapat Anda temukan beserta sumbernya."
                        ),
                    },
                    {
                        "role": "assistant",
                        "content": f"Hasil pencarian web:\n{search_results}",
                    },
                    {
                        "role": "system",
                        "content": f"Relevan memory:\n{await long_term.get_memories_v2(query=q, user_id=user_id, limit=HISTORY_LIMIT)}",
                    },
                    {"role": "user", "content": q},
                ]
            )
        except Exception:
            logger.exception(
                "[chat] Gagal merumuskan jawaban dari hasil pencarian (LLM)."
            )
            return "Maaf, terjadi kendala saat merangkum hasil pencarian."

        return (
            result_text
            or "Maaf, saya tidak dapat menemukan informasi yang Anda butuhkan."
        )

    async def _handle_proposal(_q: str, _cls: Any) -> HandlerReply:
        """
        Handler untuk intent 'Proposal Generation' (mode terbatas / placeholder).
        """
        mcp_error = mcp_required()
        if mcp_error:
            return mcp_error

        return response_success_with_toast(
            reply="Fitur belum tersedia penuh.",
            message="Fitur belum tersedia (mode terbatas).",
            severity="warning",
            http_status=200,
        )

    async def _handle_calc(q: str, _cls: Any) -> HandlerReply:
        """
        Handler untuk intent 'Product Calculator':
        - Membaca panduan pricing via MCP 'read_product_sizing_tool'.
        - Meminta LLM melakukan perhitungan berdasarkan panduan.
        """
        mcp_error = mcp_required()
        if mcp_error:
            return mcp_error

        # Ambil panduan pricing
        sizing = await safe_call_mcp_tool(
            "read_product_sizing_tool",
            {
                "filename": "internet_dedicated",
                "category": "datacom",
                "product": "internet_dedicated",
                "tahun": "2025",
            },
        )

        try:
            result_text = await llm_client.chat_completions_text(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Tugas Anda adalah menghitung harga produk berdasarkan panduan yang tersedia. "
                            "Berikan jawaban yang jelas dan ringkas. Jika ada asumsi yang Anda buat, sebutkan secara eksplisit."
                        ),
                    },
                    {
                        "role": "system",
                        "content": f"Panduan menghitung harga:\n{sizing}",
                    },
                    {
                        "role": "system",
                        "content": f"Conversation History:\n{await short_term.get_history(user_id, limit=HISTORY_LIMIT)}",
                    },
                    {"role": "user", "content": q},
                ]
            )
        except Exception:
            logger.exception("[chat] Gagal menghasilkan hasil perhitungan (LLM).")
            return "Maaf, terjadi kendala saat menghitung harga berdasarkan panduan."

        return (
            result_text
            or "Maaf, saya tidak dapat menemukan informasi yang Anda butuhkan."
        )

    async def _handle_other(q: str, _cls: Any) -> HandlerReply:
        """
        Handler fallback untuk intent 'Other':
        - Menggunakan ChatWithMemory untuk percakapan kontekstual berbasis STM/LTM.
        """
        try:
            reply = await memory_orchestrator.chat(user_id=user_id, user_message=q)
            return reply
        except Exception:
            logger.exception("[chat] Gagal menjalankan ChatWithMemory.")
            return "Maaf, terjadi kendala saat menjalankan percakapan berbasis memori."

    # ============================================================
    # 4) Klasifikasi intent & routing ke handler
    # ============================================================
    try:
        reply, cls_info = await route_based_on_intent(
            query=user_text,
            on_proposal_generation=_handle_proposal,
            on_kak_analyzer=_handle_kak,
            on_product_calculator=_handle_calc,
            on_web_search=_handle_web,
            on_other=_handle_other,
            confidence_threshold=service_configs.intent_classification_threshold,
            prefer="chat",
        )
        logger.info(
            "[chat] intent routed | cls=%s | reply.type=%s",
            repr(cls_info),
            type(reply).__name__,
        )
    except Exception:
        logger.exception("[chat] Gagal memproses routing/handler.")
        return jsonify(
            {
                "status": "error",
                "message": "Terjadi kesalahan pada server saat memproses pesan.",
            }
        ), 500

    # ============================================================
    # 5) Persist ke memori (best-effort, disentralisasi)
    # ============================================================
    try:
        await short_term.save(user_id, "user", user_text)
        await short_term.save(user_id, "assistant", str(reply))
        await long_term.add_memory_v2(
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": str(reply)},
            ],
            user_id=user_id,
        )
        logger.info("[chat] memory persisted | user=%s", user_id)
    except Exception:
        logger.exception("[chat] Gagal menyimpan memori (STM/LTM).")

    # ============================================================
    # 6) Normalisasi & kirim response
    # ============================================================
    return _normalize_reply_to_http(reply)


def _normalize_reply_to_http(reply: HandlerReply) -> Tuple[Response, int]:
    """
    Menormalkan berbagai tipe hasil handler menjadi (Response, status).

    Aturan:
      - Jika sudah Response / (Response, status) → pass-through.
      - Jika dict → bungkus sebagai payload sukses.
      - Jika bytes/bytearray → decode utf-8 'ignore'.
      - Jika bukan string → cast ke string.
    """
    if isinstance(reply, Response):
        return reply, getattr(reply, "status_code", 200)

    if isinstance(reply, tuple) and reply and isinstance(reply[0], Response):
        return reply  # type: ignore[return-value]

    if isinstance(reply, dict):
        return jsonify({"status": "success", "reply": reply}), 200

    if isinstance(reply, (bytes, bytearray)):
        try:
            reply = reply.decode(errors="ignore")
        except Exception:
            reply = str(reply)

    if not isinstance(reply, str):
        reply = str(reply)

    return jsonify({"status": "success", "reply": reply}), 200
