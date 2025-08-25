# projectwise/routes/chat.py
from __future__ import annotations

from quart import Blueprint, current_app, request, Response, jsonify

from projectwise.utils.logger import get_logger
from projectwise.utils.helper import response_error_toast, response_success_with_toast

from projectwise.services.workflow.intent_classification import route_based_on_intent
from projectwise.services.llm_chain.llm_chains import LLMChains
from projectwise.services.workflow.chat_with_memory import ChatWithMemory


chat_bp = Blueprint("chat", __name__)
logger = get_logger(__name__)


@chat_bp.post("/message")
async def chat_message():
    data = await request.get_json(force=True)
    user_id: str = data.get("user_id") or "default"
    user_message: str = data.get("message") or ""

    # ===============================================
    # App Extensions
    # ===============================================
    app = current_app
    mcp = app.extensions["mcp"]
    llm = LLMChains(prefer="chat")
    stm = app.extensions["short_term_memory"]
    ltm = app.extensions["long_term_memory"]

    service_configs = app.extensions["service_configs"]
    memory = ChatWithMemory(
        long_term=ltm, short_term=stm, service_configs=service_configs, max_history=5
    )

    # ===============================================
    # Handlers untuk tiap intent
    # ===============================================
    async def _h_kak(q, cls):
        # Check status MCP
        if not app.extensions["mcp_status"]["connected"]:
            response: tuple[Response, int] = response_error_toast(
                status="error", message="MCP belum terhubung.", http_status=503
            )
            return response

        # 0) Ambil konteks retrieval dari MCP
        context_search = await llm.chat_completions_text(
            messages=[
                {
                    "role": "system",
                    "content": "Hasilkan maksimal '300 text' query instruction untuk pencarian retrieval vectordb yang tepat berdasarkan informasi yang diberikan. output hanya text query tanpa penjelasan dan format apapun.",
                },
                {
                    "role": "system",
                    "content": f"Conversation History:\n{await stm.get_history(user_id, limit=5)}",
                },
                {
                    "role": "system",
                    "content": f"Relevan memory:\n{await ltm.get_memories_v2(query=q, user_id=user_id, limit=5)}",
                },
                {"role": "user", "content": q},
            ]
        )

        retrieve = await mcp.call_tool(
            "retrieval_tool", {"query": context_search, "k": 5}
        )

        # 1) Pesan awal
        messages = [
            {
                "role": "system",
                "content": "Anda adalah asisten yang membantu menjawab pertanyaan berdasarkan hasil analysis proyek. gunakan tools yang tersedia untuk mendapatkan informasi yang dibutuhkan.",
            },
            {"role": "system", "content": f"context dari MCP:\n{retrieve}"},
            {
                "role": "system",
                "content": f"Relevan memory:\n{await ltm.get_memories_v2(query=q, user_id=user_id, limit=5)}",
            },
            {"role": "user", "content": q},
        ]

        # 2) Skema tools (format OpenAI Chat Completions)
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
                                "description": "Nama project.",
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
        tool_calls, _raw = await llm.chat_function_call(
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )

        # 4) Eksekusi tool yang diminta model
        if tool_calls:
            # eksekutor fungsi
            async def exec_tool(name: str, args: dict) -> dict:
                kak_summaries = await mcp.call_tool(name, args)
                return kak_summaries

            # Jalankan semua panggilan tool dari model, lalu lampirkan kembali hasilnya sebagai pesan role="tool"
            for call in tool_calls:
                fname = call["name"]
                fargs = call["arguments"]  # sudah berupa dict dari LLMChains
                out = await exec_tool(fname, fargs)
                # logger.info(f"Tool {fname} executed with args {fargs}, got: {out}")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": fname,
                        "content": str(
                            out
                        ),  # string/JSON; OpenAI merekomendasikan string
                    }
                )

        # 5) (Opsional) Tambah instruksi kecil agar model merangkum hasil tool
        messages.append(
            {
                "role": "user",
                "content": "Gunakan hasil tool di atas lalu berikan jawaban final.",
            }
        )

        # 6) Minta jawaban final dari model (tanpa tools)
        final_text = await llm.chat_completions_text(messages=messages)
        
        # Persist ke memori (best‑effort)
        await ltm.add_memory_v2(
            [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": str(final_text)},
            ],
            user_id=user_id,
        )
        
        return final_text

    async def _h_web(q, cls):
        # Check status MCP
        if not app.extensions["mcp_status"]["connected"]:
            response: tuple[Response, int] = response_error_toast(
                status="error", message="MCP belum terhubung.", http_status=503
            )
            return response

        context_search = await llm.chat_completions_text(
            messages=[
                {
                    "role": "system",
                    "content": "Hasilkan maksimal '300 text' query instruction untuk pencarian web yang tepat berdasarkan informasi memory. output hanya text query tanpa penjelasan dan format apapun.",
                },
                {
                    "role": "system",
                    "content": f"Memory:\n{await stm.get_history(user_id, limit=5)}",
                },
                {"role": "user", "content": q},
            ]
        )
        search = await mcp.call_tool(
            "websearch_tool", {"query": context_search, "max_results": 7}
        )
        # logger.info(f"Websearch results: {search}")
        result = await llm.chat_completions_text(
            messages=[
                {
                    "role": "system",
                    "content": "Bertindak sebagai asisten yang membantu menjawab pertanyaan berdasarkan hasil pencarian. berikan informasi apapun yang dapat anda temukan beserta sumbernya.",
                },
                {"role": "assistant", "content": f"Hasil pencarian web:\n{search}"},
                {
                    "role": "system",
                    "content": f"Relevan memory:\n{await ltm.get_memories_v2(query=q, user_id=user_id, limit=5)}",
                },
                {"role": "user", "content": q},
            ]
        )
        # Persist ke memori (best‑effort)
        await ltm.add_memory_v2(
            [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": str(result)},
            ],
            user_id=user_id,
        )
        return (
            result or "Maaf, saya tidak dapat menemukan informasi yang Anda butuhkan."
        )

    async def _h_proposal(q, cls):
        # Check status MCP
        if not app.extensions["mcp_status"]["connected"]:
            response: tuple[Response, int] = response_error_toast(
                status="error", message="MCP belum terhubung.", http_status=503
            )
            return response

        return response_success_with_toast(
            reply="Fitur belum tersedia penuh.",
            message="Fitur belum tersedia (mode terbatas).",
            severity="warning",
            http_status=200,
        )

    async def _h_calc(q, cls):
        # Check status MCP
        if not app.extensions["mcp_status"]["connected"]:
            response: tuple[Response, int] = response_error_toast(
                status="error", message="MCP belum terhubung.", http_status=503
            )
            return response

        retrieve_sizing = await mcp.call_tool(
            "read_product_sizing_tool",
            {
                "filename": "internet_dedicated",
                "category": "datacom",
                "product": "internet_dedicated",
                "tahun": "2025",
            },
        )

        result = await llm.chat_completions_text(
            messages=[
                {
                    "role": "system",
                    "content": "Tugas anda adalah menghitung harga produk berdasarkan panduan yang tersedia. Berikan jawaban yang jelas dan ringkas. Jika ada asumsi yang anda buat, sebutkan secara eksplisit.",
                },
                {
                    "role": "system",
                    "content": f"Panduan menghitung harga:\n{retrieve_sizing}",
                },
                {
                    "role": "system",
                    "content": f"Conversation History:\n{await stm.get_history(user_id, limit=5)}",
                },
                {"role": "user", "content": q},
            ]
        )
        
        # Persist ke memori (best‑effort)
        await ltm.add_memory_v2(
            [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": str(result)},
            ],
            user_id=user_id,
        )
        return (
            result or "Maaf, saya tidak dapat menemukan informasi yang Anda butuhkan."
        )

    async def _h_other(q, cls):
        reply = await memory.chat(user_id=user_id, user_message=q)
        # Persist ke memori (best‑effort)
        await ltm.add_memory_v2(
            [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": str(reply)},
            ],
            user_id=user_id,
        )
        return reply

    # ===============================================
    # Klasifikasi intent + routing ke handler terkait
    # ===============================================
    try:
        reply, cls = await route_based_on_intent(
            query=user_message,
            on_proposal_generation=_h_proposal,
            on_kak_analyzer=_h_kak,
            on_product_calculator=_h_calc,
            on_web_search=_h_web,
            on_other=_h_other,
            confidence_threshold=service_configs.intent_classification_threshold,
            prefer="chat",
        )
    except Exception:
        logger.exception("Gagal memproses routing/handler.")
        return jsonify(
            {
                "status": "error",
                "message": "Terjadi kesalahan pada server saat memproses pesan.",
            }
        ), 500

    # ===============================================
    # Persist ke memori (best‑effort)
    # ===============================================
    try:
        await stm.save(user_id, "user", user_message)
        await stm.save(user_id, "assistant", str(reply))
        await ltm.add_memory_v2(
            [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": str(reply)},
            ],
            user_id=user_id,
        )
    except Exception:
        logger.exception("Gagal menyimpan memori.")

    # ===============================================
    # Bentuk response HTTP
    # ===============================================
    # ——— Pass-through untuk Response dari make_response (error) ———
    if isinstance(reply, Response):
        return reply
    if isinstance(reply, tuple) and reply and isinstance(reply[0], Response):
        return reply  # (Response, status) dari make_response

    # ——— Normalisasi tipe data sukses ———
    if isinstance(reply, dict):
        # anggap ini payload sukses yang ingin ditampilkan di chat
        return jsonify({"status": "success", "reply": reply}), 200

    if isinstance(reply, (bytes, bytearray)):
        reply = reply.decode(errors="ignore")
    elif not isinstance(reply, str):
        reply = str(reply)

    return jsonify({"status": "success", "reply": reply}), 200
