# projectwise/services/workflow/intent_classification.py
from __future__ import annotations

from typing import Optional, Tuple, Callable, Awaitable, Any, Literal
from pydantic import BaseModel, Field

from projectwise.utils.logger import get_logger
from projectwise.config import ServiceConfigs
from projectwise.services.llm_chain.llm_chains import LLMChains, Prefer
from projectwise.services.workflow.prompt_instruction import (
    PROMPT_WORKFLOW_INTENT,
    FEW_SHOT_INTENT,
)


logger = get_logger(__name__)
settings = ServiceConfigs()


# ==========================================
# pydantic model untuk klasifikasi intent
# ==========================================
class IntentResult(BaseModel):
    intent: Literal[
        "kak_analyzer",
        "proposal_generation",
        "product_calculator",
        "web_search",
        "other",
    ] = Field(..., description="Intent hasil klasifikasi")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Skor kepercayaan 0..1")
    reasoning: Optional[str] = Field(
        None, description="Alasan singkat (opsional, boleh kosong)"
    )


# ==========================================
# Utils untuk membangun pesan klasifikasi
# ==========================================
_DEF_FEWSHOT = FEW_SHOT_INTENT()


def _build_messages(query: str) -> list[dict]:
    """Bangun daftar pesan untuk klasifikasi intent.

    - system: PROMPT_WORKFLOW_INTENT (instruksi tegas + format JSON wajib)
    - few-shot: contoh tanya-jawab (pesan role user/assistant)
    - user: pesan query aktual
    """
    msgs: list[dict] = []
    msgs.append({"role": "system", "content": PROMPT_WORKFLOW_INTENT()})
    msgs.extend(_DEF_FEWSHOT)
    msgs.append({"role": "user", "content": query})
    return msgs


# ==========================================
# Klasifikasi Intent
# ==========================================
async def classify_intent(
    query: str,
    *,
    prefer: Prefer = "auto",
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    timeout: Optional[float] = None,
    llm: Optional[LLMChains] = None,
) -> IntentResult:
    """Lakukan klasifikasi intent menggunakan LLMChains dengan fallback kuat.

    Strategi:
    - Jika `prefer==\"chat\"` → pakai chat.completions.parse (native) → fallback schema.
    - Jika `prefer==\"responses\"` → pakai responses.parse (native) → fallback schema.
    - Jika `prefer==\"auto\"` → heuristik model, namun tetap jatuh ke fallback bila perlu.
    """
    # Siapkan LLMChains (satu-satunya gateway ke model)
    chain = llm or LLMChains(
        model=model or settings.llm_model,
        prefer=prefer,
        temperature=temperature
        if temperature is not None
        else settings.llm_temperature,
        request_timeout=timeout or 60.0,
    )

    messages = _build_messages(query)

    # Pilih jalur eksekusi berdasarkan preferensi, namun biarkan LLMChains
    # melakukan fallback native→schema secara internal.
    try:
        if (prefer or chain.prefer) == "responses":
            parsed = await chain.responses_parse(
                input=messages, pydantic_model=IntentResult
            )
            logger.info("Intent parsed via responses_parse")
        elif (prefer or chain.prefer) == "chat":
            parsed = await chain.chat_completions_parse(
                messages=messages, pydantic_model=IntentResult
            )
            logger.info("Intent parsed via chat_completions_parse")
        else:
            # AUTO: heuristik ringan → gunakan chat untuk model yang umum FC kuat
            logger.info("Intent classify via AUTO heuristik based on model name")
            name = (model or chain.model or "").lower()
            if any(x in name for x in ["qwen", "glm", "yi", "deepseek"]):
                parsed = await chain.chat_completions_parse(
                    messages=messages, pydantic_model=IntentResult
                )
            else:
                parsed = await chain.responses_parse(
                    input=messages, pydantic_model=IntentResult
                )
    except Exception:
        logger.exception("Intent parse gagal; fallback ekstra via chat schema")
        # Double-fallback defensif jika keduanya gagal: pakai chat + schema
        parsed = await chain.chat_completions_parse(
            messages=messages, pydantic_model=IntentResult
        )

    logger.info(
        "Intent classified: %s (confidence=%.2f)", parsed.intent, parsed.confidence # type: ignore
    )
    return parsed  # type: ignore


# ==========================================
# Routing berdasarkan intent
# ==========================================
async def route_based_on_intent(
    *,
    query: str,
    # Handler async opsional untuk tiap intent. Signature: async def handler(q: str, cls: IntentResult) -> Any
    on_kak_analyzer: Optional[Callable[[str, IntentResult], Awaitable[Any]]] = None,
    on_proposal_generation: Optional[
        Callable[[str, IntentResult], Awaitable[Any]]
    ] = None,
    on_product_calculator: Optional[
        Callable[[str, IntentResult], Awaitable[Any]]
    ] = None,
    on_web_search: Optional[Callable[[str, IntentResult], Awaitable[Any]]] = None,
    on_other: Optional[Callable[[str, IntentResult], Awaitable[Any]]] = None,
    # Konfigurasi
    confidence_threshold: float = settings.intent_classification_threshold,
    prefer: Prefer = "auto",
    model: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Tuple[Any, IntentResult]:
    # 1) Klasifikasi
    cls = await classify_intent(
        query,
        prefer=prefer,
        model=model,
        timeout=timeout or 45.0,
    )

    logger.info(
        "[intent] decision | %s %.2f thr=%.2f",
        cls.intent,
        cls.confidence,
        confidence_threshold,
    )

    # 2) Tentukan target handler berdasarkan ambang dan ketersediaan handler
    effective_intent = cls.intent
    low_conf = cls.confidence < float(confidence_threshold or 0.0)

    if low_conf:
        # Kepercayaan rendah → arahkan ke on_other jika tersedia
        if on_other is not None:
            return await on_other(query, cls), cls
        # Jika on_other tidak ada, teruskan sesuai prediksi model (best effort)

    # 3) Routing ke handler sesuai intent
    if effective_intent == "kak_analyzer" and on_kak_analyzer is not None:
        return await on_kak_analyzer(query, cls), cls
    if effective_intent == "proposal_generation" and on_proposal_generation is not None:
        return await on_proposal_generation(query, cls), cls
    if effective_intent == "product_calculator" and on_product_calculator is not None:
        return await on_product_calculator(query, cls), cls
    if effective_intent == "web_search" and on_web_search is not None:
        return await on_web_search(query, cls), cls

    # 4) Fallback akhir ke on_other bila ada
    if on_other is not None:
        return await on_other(query, cls), cls

    # 5) Jika tidak ada handler apapun, kembalikan hasil klasifikasi saja
    return {
        "status": "success",
        "message": f"Intent: {cls.intent} (confidence={cls.confidence:.2f})",
    }, cls
