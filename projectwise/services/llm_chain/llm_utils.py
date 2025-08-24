# projectwise/services/llm_chain/llm_utils.py
from __future__ import annotations

import json
from pydantic import BaseModel
from typing import Any, Dict, List, Optional, Type, TypeVar

from projectwise.utils.logger import get_logger
from projectwise.services.memory.long_term_memory import Mem0Manager
from projectwise.services.memory.short_term_memory import ShortTermMemory


logger = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)


# ---------------- Pydantic ↔ JSON Schema & Parsing ----------------
def json_schema_from_pydantic(model: Type[T], *, strict: bool = True) -> dict:
    """Bentuk response_format JSON Schema dari Pydantic (fallback)."""
    schema = model.model_json_schema()
    return {
        "type": "json_schema",
        "json_schema": {
            "name": getattr(model, "__name__", "Schema"),
            "schema": schema,
            "strict": bool(strict),
        },
    }


def pydantic_parse(model: Type[T], payload: object) -> T:
    """Parse payload (str/dict/serializable) ke instance Pydantic."""
    if isinstance(payload, str):
        return model.model_validate_json(payload)
    if isinstance(payload, dict):
        return model.model_validate(payload)
    import json as _json

    return model.model_validate_json(_json.dumps(payload, ensure_ascii=False))


# ---------------- Small utils ----------------
def short_str(obj: Any, n: int = 400) -> str:
    try:
        s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    except Exception:
        s = str(obj)
    return s if len(s) <= n else s[: n - 3] + "..."


def json_loads_safe(s: Optional[str]) -> Any:
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}


def shape_system(content: str) -> Dict[str, Any]:
    return {"role": "system", "content": content}


def shape_user(content: str) -> Dict[str, Any]:
    return {"role": "user", "content": content}


def shape_assistant_text(text: str) -> Dict[str, Any]:
    return {"role": "assistant", "content": text}


# ---------------- Extractors (raw SDK) ----------------
def extract_output_text_responses(resp: Any) -> str:
    # OpenAI Responses: resp.output[].content[].text atau resp.output_text
    txt = getattr(resp, "output_text", None)
    if txt:
        return (txt or "").strip()
    out = []
    for it in getattr(resp, "output", []) or []:
        for c in getattr(it, "content", []) or []:
            if getattr(c, "type", None) in ("output_text", "text") or (
                isinstance(c, dict) and c.get("type") in ("output_text", "text")
            ):
                out.append(
                    getattr(c, "text", None)
                    or (isinstance(c, dict) and c.get("text"))
                    or ""
                )
    return "".join(out).strip()


def extract_assistant_text_chat(resp: Any) -> str:
    choice0 = (getattr(resp, "choices", None) or [None])[0]
    if not choice0:
        return ""
    return (getattr(getattr(choice0, "message", None), "content", None) or "").strip()


def extract_tool_calls_responses(resp: Any) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    for it in getattr(resp, "output", []) or []:
        for c in getattr(it, "content", []) or []:
            ctype = getattr(c, "type", None) or (isinstance(c, dict) and c.get("type"))
            if ctype in ("tool_use", "function_call", "tool_call"):
                name = getattr(c, "name", None) or (
                    isinstance(c, dict) and c.get("name")
                )
                args = (
                    getattr(c, "input", None)
                    or (isinstance(c, dict) and c.get("input"))
                    or {}
                )
                call_id = (
                    getattr(c, "id", None)
                    or (isinstance(c, dict) and c.get("id"))
                    or ""
                )
                calls.append(
                    {
                        "id": call_id or f"call_{len(calls) + 1}",
                        "name": name,
                        "arguments": args,
                    }
                )
    return calls


def extract_tool_calls_chat(resp: Any) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    choice0 = (getattr(resp, "choices", None) or [None])[0]
    if not choice0:
        return calls
    msg = getattr(choice0, "message", None)
    for tc in getattr(msg, "tool_calls", []) or []:
        fn = getattr(tc, "function", None)
        args = getattr(fn, "arguments", "") or "{}"
        try:
            args_json = json.loads(args)
        except Exception:
            args_json = {"_raw": args}
        calls.append(
            {
                "id": getattr(tc, "id", None) or f"call_{len(calls) + 1}",
                "name": getattr(fn, "name", None),
                "arguments": args_json,
            }
        )
    return calls


def extract_assistant_and_tool_calls_from_responses(resp: Any):
    # kompatibel dengan handler_proposal_generation lama
    text = extract_output_text_responses(resp)
    calls = []
    for it in getattr(resp, "output", []) or []:
        for c in getattr(it, "content", []) or []:
            ctype = getattr(c, "type", None) or (isinstance(c, dict) and c.get("type"))
            if ctype in ("tool_use", "function_call", "tool_call"):
                name = getattr(c, "name", None) or (
                    isinstance(c, dict) and c.get("name")
                )
                args = (
                    getattr(c, "input", None)
                    or (isinstance(c, dict) and c.get("input"))
                    or {}
                )
                call_id = (
                    getattr(c, "id", None)
                    or (isinstance(c, dict) and c.get("id"))
                    or ""
                )
                calls.append(
                    {
                        "id": call_id or f"call_{len(calls) + 1}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
                    }
                )
    return (text or None), calls


# ---------------- Messages → Responses.input converter ----------------
def to_responses_input(chat_messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    resp_input: List[Dict[str, Any]] = []
    for m in chat_messages:
        role = m.get("role")
        if role == "tool":
            resp_input.append(
                {
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id"),
                    "name": m.get("name"),
                    "content": [
                        {
                            "type": "output_text",
                            "text": m.get("content")
                            if isinstance(m.get("content"), str)
                            else json.dumps(m.get("content"), ensure_ascii=False),
                        }
                    ],
                }
            )
            continue
        tool_calls = m.get("tool_calls") or []
        if role == "assistant" and tool_calls:
            items = []
            for tc in tool_calls:
                fn = getattr(tc, "function", None) or tc.get("function", {})
                tc_id = getattr(tc, "id", None) or tc.get("id")
                name = getattr(fn, "name", None) or fn.get("name")
                args = getattr(fn, "arguments", None) or fn.get("arguments") or "{}"
                items.append(
                    {
                        "type": "function_call",
                        "name": name,
                        "arguments": args,
                        "call_id": tc_id,
                    }
                )
            resp_input.append({"role": "assistant", "content": items})
            continue
        content = m.get("content", "")
        if isinstance(content, str):
            resp_input.append(
                {"role": role, "content": [{"type": "text", "text": content}]}
            )
        else:
            resp_input.append({"role": role, "content": content})
    return resp_input


def ensure_responses_input(
    maybe_messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not maybe_messages:
        return []
    first = maybe_messages[0]
    if isinstance(first.get("content"), list):
        return maybe_messages
    return to_responses_input(maybe_messages)


# ---------------- JSON-safety ----------------
def to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if hasattr(obj, "model_dump") and callable(getattr(obj, "model_dump")):
        try:
            return to_jsonable(obj.model_dump())  # type: ignore
        except Exception:
            return str(obj)
    try:
        from dataclasses import asdict, is_dataclass

        if is_dataclass(obj):
            return to_jsonable(asdict(obj))  # type: ignore
    except Exception:
        pass
    try:
        json.dumps(obj)
        return obj
    except Exception:
        return str(obj)


# ---------------- Memory briefing ----------------
async def build_context_blocks_memory(
    *,
    short_term: ShortTermMemory,
    long_term: Mem0Manager,
    user_id: str,
    user_message: str,
    max_history: int = 3,
    prompt_instruction: str = "",
) -> str:
    stm_block = await short_term.get_history(user_id=user_id, limit=max_history)
    stm_block = stm_block or "[Tidak ada riwayat percakapan]"
    ltm_results = await long_term.get_memories(
        query=user_message, user_id=user_id, limit=max_history
    )
    ltm_block = (
        "\n".join(f"- {m}" for m in ltm_results)
        if ltm_results
        else "[Tidak ada memori relevan]"
    )
    return (
        prompt_instruction
        + "\n\n### Briefing Memori\n"
        + f"**Long_Term Memory (relevan):**\n{ltm_block}\n\n"
        + f"**Short_Term History (ringkas):**\n{stm_block}\n"
    )
