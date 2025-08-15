# projectwise/utils/llm_io.py
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

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

def extract_output_text(resp) -> str:
    # Responses API common variants
    text = getattr(resp, "output_text", None)
    if text:
        return text.strip()
    outputs = getattr(resp, "output", None) or []
    buf = []
    for o in outputs:
        for c in getattr(o, "content", []) or []:
            ctype = getattr(c, "type", None) or (isinstance(c, dict) and c.get("type"))
            if ctype in ("output_text", "text"):
                buf.append(getattr(c, "text", None) or (isinstance(c, dict) and c.get("text")) or "")
    return "".join(buf).strip()

def extract_tool_calls(resp) -> List[Dict[str, Any]]:
    calls = []
    outputs = getattr(resp, "output", None) or []
    for o in outputs:
        for c in getattr(o, "content", []) or []:
            ctype = getattr(c, "type", None) or (isinstance(c, dict) and c.get("type"))
            if ctype in ("tool_use", "function_call", "tool_call"):
                name = getattr(c, "name", None) or (isinstance(c, dict) and c.get("name"))
                args = getattr(c, "input", None) or (isinstance(c, dict) and c.get("input")) or {}
                call_id = getattr(c, "id", None) or (isinstance(c, dict) and c.get("id")) or ""
                calls.append({
                    "id": call_id or f"call_{len(calls)+1}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}
                })
    # fallback old choices.* if needed
    if calls:
        return calls
    try:
        choice0 = resp.choices[0]
        msg = choice0.message
        if getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                calls.append({
                    "id": getattr(tc, "id", None) or f"call_{len(calls)+1}",
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }
                })
    except Exception:
        pass
    return calls

def extract_assistant_and_tool_calls_from_responses(resp) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """
    Ekstrak teks assistant & panggilan tool dari Responses API.
    Karena variasi SDK/versi, kita buat parser yang toleran.

    Return:
      (assistant_text, tool_calls)
      - assistant_text: str | None
      - tool_calls: list of {"id","type":"function","function":{"name","arguments"}}
    """
    assistant_text: Optional[str] = None
    tool_calls: List[Dict[str, Any]] = []

    # ==== Pola 1 (SDK baru): resp.output[].content[] ====
    try:
        outputs = getattr(resp, "output", None) or []
        for o in outputs:
            # o biasanya punya .content (list)
            content_list = getattr(o, "content", None) or []
            for c in content_list:
                ctype = getattr(c, "type", None) or c.get("type")
                if ctype in ("output_text", "text"):
                    # SDK baru: {"type":"output_text","text":"..."}
                    txt = getattr(c, "text", None) or c.get("text") or ""
                    assistant_text = (assistant_text or "") + txt
                elif ctype in ("tool_use", "function_call", "tool_call"):
                    # SDK baru: {"type":"tool_use","name":"...","input":{...},"id":"..."}
                    name = getattr(c, "name", None) or c.get("name")
                    args = getattr(c, "input", None) or c.get("input") or {}
                    call_id = getattr(c, "id", None) or c.get("id") or ""
                    tool_calls.append({
                        "id": call_id or f"call_{len(tool_calls)+1}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                    })
        if assistant_text is not None or tool_calls:
            return assistant_text, tool_calls
    except Exception:
        pass

    # ==== Pola 2 fallback (SDK lama): resp.choices[0].message ====
    try:
        choice0 = resp.choices[0]
        msg = choice0.message
        # message.content bisa string
        if getattr(msg, "content", None):
            assistant_text = msg.content
        # message.tool_calls (daftar)
        if getattr(msg, "tool_calls", None):
            # Format sudah mirip Chat Completions
            for tc in msg.tool_calls:
                tool_calls.append({
                    "id": getattr(tc, "id", None) or f"call_{len(tool_calls)+1}",
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                })
        return assistant_text, tool_calls
    except Exception:
        pass

    return assistant_text, tool_calls