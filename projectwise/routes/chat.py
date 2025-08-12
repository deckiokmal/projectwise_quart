# projectwise/routes/chat.py
from quart import Blueprint, request, jsonify, current_app, render_template

chat_bp = Blueprint(
    "ui", __name__, template_folder="../templates", static_folder="../static"
)


@chat_bp.route("/")
async def index():
    # Render halaman utama
    return await render_template("ws_room.html")


@chat_bp.route("/chat_index")
async def chat_index():
    # Render halaman utama
    return await render_template("chat.html")


@chat_bp.route("/chat_message", methods=["POST"])
async def chat():
    data = await request.get_json()
    user_message = data.get("message", "").strip()
    if not user_message:
        return jsonify({"error": "Message is required"}), 400

    # TODO: Integrasikan ke MCP atau AI agent
    return jsonify({"response": f"Echo: {user_message}"})


@chat_bp.route("/chat_mem", methods=["POST"])
async def chat_mem():
    """
    Endpoint chat gabungan:
    1. Ambil pesan user dari request
    2. Cari memori relevan di LongTermMemory
    3. Panggil MCPClient untuk jawaban
    4. Simpan ke ShortTermMemory & LongTermMemory
    """
    data = await request.get_json()
    user_id = data.get("user_id", "default")
    user_message = data.get("message", "").strip()

    if not user_message:
        return jsonify({"error": "Pesan kosong"}), 400

    # Ambil semua ekstensi dari app
    mcp_client = current_app.extensions["mcp"]
    stm = current_app.extensions["short_term_memory"]
    ltm = current_app.extensions["long_term_memory"]
    service_configs = current_app.extensions["service_configs"]

    # Ambil memori relevan dari LTM
    relevant_memories = await ltm.get_memories(user_message, user_id=user_id)
    memories_block = "\n".join(f"- {m}" for m in relevant_memories) or "[Tidak ada]"

    # Bangun prompt untuk AI
    system_prompt = (
        "Anda adalah ProjectWise, asisten AI presales & PM.\n"
        f"Memori relevan:\n{memories_block}"
    )
    messages_for_llm = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    # Panggil LLM via MCPClient
    try:
        
        response = await mcp_client.llm.responses.create(
            model=service_configs.llm_model,
            input=messages_for_llm,
        )
        assistant_reply = response.output_text or ""
    except Exception as e:
        return jsonify({"error": f"Gagal memanggil LLM: {e}"}), 500

    # Simpan ke ShortTermMemory
    await stm.save(user_id, "user", user_message)
    await stm.save(user_id, "assistant", assistant_reply)

    # Simpan ke LongTermMemory
    await ltm.add_conversation(
        messages_for_llm + [{"role": "assistant", "content": assistant_reply}],
        user_id=user_id,
    )

    return jsonify(
        {
            "user_id": user_id,
            "user_message": user_message,
            "assistant_reply": assistant_reply,
            "relevant_memories": relevant_memories,
        }
    )


@chat_bp.route("/history/<user_id>")
async def get_history(user_id):
    """Ambil history chat dari ShortTermMemory."""
    stm = current_app.extensions["short_term_memory"]
    history = await stm.get_history(user_id)
    return jsonify({"user_id": user_id, "history": history})
