# projectwise/routes/chat.py
from __future__ import annotations
from quart import Blueprint, current_app, request, Response, jsonify
from openai import AsyncOpenAI

from projectwise.utils.logger import get_logger
from projectwise.utils.human_response import make_response
from projectwise.services.workflow.intent_classification import route_based_on_intent
from projectwise.services.workflow.handler_proposal_generation import (
    run as proposal_run,
)
from projectwise.services.workflow.reflexion_actor import ReflectionActor
from projectwise.services.workflow.chat_with_memory import ChatWithMemory
from projectwise.services.workflow.prompt_instruction import (
    ACTOR_SYSTEM,
    CRITIC_SYSTEM,
    PROMPT_KAK_ANALYZER,
    PROMPT_PRODUCT_CALCULATOR,
)


logger = get_logger(__name__)
chat_bp = Blueprint("chat", __name__)


@chat_bp.post("/message")
async def chat_message():
    app = current_app
    data = await request.get_json(force=True)
    user_id: str = data.get("user_id") or "default"
    user_message: str = data.get("message") or ""
    project_name: str | None = data.get("project_name")
    override_template: str | None = data.get("override_template")

    # extensions
    service_configs = app.extensions["service_configs"]
    stm = app.extensions["short_term_memory"]
    ltm = app.extensions["long_term_memory"]

    llm = AsyncOpenAI()
    model = service_configs.llm_model

    # ——— Handlers untuk tiap intent ———
    async def _h_proposal(q, cls):
        # Check status MCP
        if not app.extensions["mcp_status"]["connected"]:
            response = make_response(
                status="error", message="MCP belum terhubung.", http_status=503
            )
            return response

        # Jalankan pipeline Document Generation
        client = type("C", (), {"llm": llm, "model": model})
        return await proposal_run(
            client=client,
            project_name=project_name or "Untitled",
            user_query=q,
            override_template=override_template,
            app=app,
        )

    async def _h_kak(q, cls):
        # Check status MCP
        if not app.extensions["mcp_status"]["connected"]:
            response = make_response(
                status="error", message="MCP belum terhubung.", http_status=503
            )
            return response

        # Jalankan Reflection Actor
        actor = ReflectionActor.from_quart_app(app, llm=llm, llm_model=model)
        return await actor.reflection_actor_with_mcp(
            user_id=user_id,
            prompt=q,
            actor_instruction=ACTOR_SYSTEM() + "\n" + PROMPT_KAK_ANALYZER(),
            critic_instruction=CRITIC_SYSTEM(),
        )

    async def _h_calc(q, cls):
        # Check status MCP
        if not app.extensions["mcp_status"]["connected"]:
            response = make_response(
                status="error", message="MCP belum terhubung.", http_status=503
            )
            return response

        # Jalankan Reflection Actor
        actor = ReflectionActor.from_quart_app(app, llm=llm, llm_model=model)
        return await actor.reflection_actor_with_mcp(
            user_id=user_id,
            prompt=q,
            actor_instruction=ACTOR_SYSTEM() + "\n" + PROMPT_PRODUCT_CALCULATOR(),
            critic_instruction=CRITIC_SYSTEM(),
        )

    async def _h_web(q, cls):
        # Check status MCP
        if not app.extensions["mcp_status"]["connected"]:
            response = make_response(
                status="error", message="MCP belum terhubung.", http_status=503
            )
            return response

        # Jalankan Reflection Actor
        actor = ReflectionActor.from_quart_app(app, llm=llm, llm_model=model)
        return await actor.reflection_actor_with_mcp(
            user_id=user_id,
            prompt=q,
            actor_instruction=ACTOR_SYSTEM() + "\nGunakan tool websearch bila relevan.",
            critic_instruction=CRITIC_SYSTEM(),
        )

    async def _h_other(q, cls):
        # Fallback ke War Room agar tetap memanfaatkan STM/LTM
        general_conversation = ChatWithMemory.from_quart_app(app)
        return await general_conversation.chat(user_id=user_id, user_message=q)

    # ——— Route intent ———
    reply, cls = await route_based_on_intent(
        llm,
        user_message,
        model=model,
        on_proposal_generation=_h_proposal,
        on_kak_analyzer=_h_kak,
        on_product_calculator=_h_calc,
        on_web_search=_h_web,
        on_other=_h_other,
        confidence_threshold=service_configs.intent_classification_threshold,
    )

    # Persist ke memori (best‑effort)
    try:
        await stm.save(user_id, "user", user_message)
        await stm.save(user_id, "assistant", str(reply))
        await ltm.add_conversation(
            [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": str(reply)},
            ],
            user_id=user_id,
        )
    except Exception:
        logger.exception("Gagal menyimpan memori.")

    # jawaban akhir ke user
    # 1) Jika handler mengembalikan Response/tuple Response, kirim apa adanya
    if isinstance(reply, Response):
        return reply
    if isinstance(reply, tuple) and reply and isinstance(reply[0], Response):
        return reply  # (Response, status) dari make_response

    # 2) Jika string/bytes/dll → normalisasi ke string, lalu gunakan format standar
    if isinstance(reply, (bytes, bytearray)):
        reply = reply.decode(errors="ignore")
    elif not isinstance(reply, str):
        reply = str(reply)

    return jsonify({"status": "success", "reply": reply}), 200
