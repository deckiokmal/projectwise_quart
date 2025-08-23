# projectwise/services/workflow/intent_classification.py
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Literal, Optional, Tuple
from pydantic import BaseModel, Field, ValidationError

from projectwise.utils.logger import get_logger
from projectwise.services.workflow.prompt_instruction import (
    PROMPT_WORKFLOW_INTENT,
    FEW_SHOT_INTENT,
)
from projectwise.services.llm_chain.llm_chains import LLMChains

logger = get_logger(__name__)


# ======================================================
# 1) Data Models
# ======================================================

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


# ======================================================
# 2) Core: Klasifikasi via Json schema chat or Responses
# ======================================================


async def classify_intent_chat(
    llm: LLMChains, query: str, *, timeout_sec: float = 45.0
) -> IntentClassification:
    messages = [
        {"role": "system", "content": PROMPT_WORKFLOW_INTENT()},
        *FEW_SHOT_INTENT(),
        {"role": "user", "content": query},
    ]

    # 1) JSON Schema dari Pydantic → ketatkan
    schema = llm.tighten_json_schema(IntentClassification.model_json_schema())

    # 2) Panggil Chat Completions dengan schema strict
    resp = await asyncio.wait_for(
        llm.chat_completions_text(messages=messages, json_schema=schema),
        timeout=timeout_sec,
    )
    if resp.get("status") != "success":
        # Bisa terjadi refusal atau format-issue
        return IntentClassification(intent="other", confidence=0.0)

    # 3) Parse ke Pydantic (defensif: data bisa dict/string)
    data = resp.get("data")
    try:
        if isinstance(data, dict):
            return IntentClassification.model_validate(data)
        elif isinstance(data, str):
            return IntentClassification.model_validate_json(data)
        else:
            return IntentClassification(intent="other", confidence=0.0)
    except ValidationError:
        # Jika schema mismatch walau strict, amankan
        return IntentClassification(intent="other", confidence=0.0)


# ——— Router dengan 5 intent
OnAny = Callable[[str, IntentClassification], Awaitable[Any]]


# ======================================================
# 3) Controller: Best-Practice Routing dengan Threshold
# ======================================================


async def classify_intent(
    llm: LLMChains, query: str, *, timeout_sec: float = 45.0
) -> IntentClassification:
    # 1) Coba Chat (JSON Schema)
    cls = await classify_intent_chat(llm, query, timeout_sec=timeout_sec)
    if cls.intent != "other" or cls.confidence > 0:
        return cls

    # 2) Fallback → Responses API (Pydantic)
    messages = [
        {"role": "system", "content": PROMPT_WORKFLOW_INTENT()},
        *FEW_SHOT_INTENT(),
        {"role": "user", "content": query},
    ]
    resp = await asyncio.wait_for(
        llm.responses_text(input=messages, pydantic_model=IntentClassification),
        timeout=timeout_sec,
    )
    if resp.get("status") == "success":
        data = resp.get("data")
        try:
            if isinstance(data, dict):
                return IntentClassification.model_validate(data)
            elif isinstance(data, str):
                return IntentClassification.model_validate_json(data)
        except ValidationError:
            pass

    # 3) Last resort
    return IntentClassification(intent="other", confidence=0.0)


async def route_based_on_intent(
    query: str,
    llm: LLMChains = LLMChains(),
    *,
    on_proposal_generation: OnAny,
    on_kak_analyzer: OnAny,
    on_product_calculator: OnAny,
    on_web_search: OnAny,
    on_other: OnAny,
    confidence_threshold: float = 0.60,
    timeout_sec: float = 45.0,
) -> Tuple[Any, IntentClassification]:
    cls = await classify_intent(
        llm,
        query,
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
