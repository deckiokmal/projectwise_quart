# projectwise/services/workflow/intent_classification.py
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Tuple
from pydantic import BaseModel, Field, ValidationError
from openai import AsyncOpenAI, APIConnectionError

from projectwise.utils.logger import get_logger
from projectwise.utils.llm_io import short_str
from projectwise.services.workflow.prompt_instruction import (
    PROMPT_WORKFLOW_INTENT,
    FEW_SHOT_INTENT,
)


logger = get_logger(__name__)


# =================
# 1) Data Models
# =================

IntentLabel = Literal[
    "other",
    "product_calculator",
    "kak_analyzer",
    "proposal_generation",
    "web_search",
]


class IntentClassification(BaseModel):
    """
    Hasil klasifikasi intent yang siap dipakai untuk routing.
    - Tambah 'reasoning' untuk observabilitas (audit/tracing).
    - Gunakan nama field 'confidence' (seragam di codebase).
    """

    intent: IntentLabel
    confidence: float = Field(ge=0, le=1)
    reasoning: Optional[str] = None


# =================
# 2) Utilities
# =================


def _build_messages(user_query: str) -> List[Dict[str, Any]]:
    """
    Susun pesan untuk Responses API dengan gaya ChatML (kompatibel dengan banyak SDK).
    - Pertahankan few-shot dari implementasi Anda agar akurasi meningkat.
    """
    return [
        {"role": "system", "content": PROMPT_WORKFLOW_INTENT()},
        *FEW_SHOT_INTENT(),
        {"role": "user", "content": user_query},
    ]


# =================
# 3) Core: Klasifikasi via OpenAI Responses API
# =================


async def classify_intent_responses(
    llm: AsyncOpenAI,
    query: str,
    *,
    model: str,
    temperature: float = 0.0,
    top_p: float = 0.0,
    timeout_sec: float = 45.0,
) -> IntentClassification:
    logger.info("[intent] start | model=%s | q=%s", model, short_str(query, 200))
    messages = _build_messages(query)

    # Prefer parse → IntentClassification
    try:
        resp = await asyncio.wait_for(
            llm.responses.parse(
                model=model,
                input=messages,  # type: ignore
                text_format=IntentClassification,
                temperature=temperature,
                top_p=top_p,
            ),
            timeout=timeout_sec,
        )
        parsed = getattr(resp, "output_parsed", None)
        if isinstance(parsed, IntentClassification):
            logger.info(
                "[intent] parsed via responses.parse | %s %.2f",
                parsed.intent,
                parsed.confidence,
            )
            return parsed
    except APIConnectionError:
        logger.error("LLM APIConnectionError.")
        human = "LLM API Connection Error. Silakan coba lagi."
        raise RuntimeError(human)
    except Exception:
        logger.exception("[intent] responses.parse failed, fallback to create")

    # Fallback create → parse manual
    try:
        resp = await asyncio.wait_for(
            llm.responses.create(
                model=model, input=messages, temperature=temperature, top_p=top_p # type: ignore
            ),
            timeout=timeout_sec,
        )
        # Try output_text → pydantic
        raw = getattr(resp, "output_text", None) or ""
        if raw.strip():
            try:
                return IntentClassification.model_validate_json(raw)
            except ValidationError:
                logger.warning("[intent] manual-parse failed: %s", short_str(raw))
    except APIConnectionError:
        logger.error("LLM APIConnectionError.")
        human = "LLM API Connection Error. Silakan coba lagi."
        raise RuntimeError(human)
    except Exception:
        logger.exception("[intent] responses.create failed")

    logger.warning("[intent] fallback → other (0.00)")
    return IntentClassification(intent="other", confidence=0.0)


# ——— Router dengan 5 intent
OnAny = Callable[[str, IntentClassification], Awaitable[Any]]

# =================
# 4) Controller: Best-Practice Routing dengan Threshold
# =================


async def route_based_on_intent(
    llm: AsyncOpenAI,
    query: str,
    *,
    model: str,
    on_proposal_generation: OnAny,
    on_kak_analyzer: OnAny,
    on_product_calculator: OnAny,
    on_web_search: OnAny,
    on_other: OnAny,
    confidence_threshold: float = 0.60,
    temperature: float = 0.0,
    top_p: float = 0.0,
    timeout_sec: float = 45.0,
) -> Tuple[Any, IntentClassification]:
    cls = await classify_intent_responses(
        llm,
        query,
        model=model,
        temperature=temperature,
        top_p=top_p,
        timeout_sec=timeout_sec,
    )
    logger.info(
        "[intent] decision | %s %.2f thr=%.2f",
        cls.intent,
        cls.confidence,
        confidence_threshold,
    )

    intent = cls.intent if cls.confidence >= confidence_threshold else "other"
    try:
        if intent == "proposal_generation":
            return await on_proposal_generation(query, cls), cls
        if intent == "kak_analyzer":
            return await on_kak_analyzer(query, cls), cls
        if intent == "product_calculator":
            return await on_product_calculator(query, cls), cls
        if intent == "web_search":
            return await on_web_search(query, cls), cls
        return await on_other(query, cls), cls
    except Exception as e:
        logger.exception("[intent] handler error: %s", e)
        return {"status": "error", "message": str(e)}, cls
