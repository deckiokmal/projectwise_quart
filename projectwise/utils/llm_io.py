# projectwise/utils/llm_io.py
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple
from jsonschema import Draft202012Validator

from projectwise.services.memory.long_term_memory import Mem0Manager
from projectwise.services.memory.short_term_memory import ShortTermMemory


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


def shape(role: str, content: str) -> Dict[str, Any]:
    return {"role": role, "content": content}


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
                buf.append(
                    getattr(c, "text", None)
                    or (isinstance(c, dict) and c.get("text"))
                    or ""
                )
    return "".join(buf).strip()


def extract_tool_calls(resp) -> List[Dict[str, Any]]:
    calls = []
    outputs = getattr(resp, "output", None) or []
    for o in outputs:
        for c in getattr(o, "content", []) or []:
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
    # fallback old choices.* if needed
    if calls:
        return calls
    try:
        choice0 = resp.choices[0]
        msg = choice0.message
        if getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                calls.append(
                    {
                        "id": getattr(tc, "id", None) or f"call_{len(calls) + 1}",
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                )
    except Exception:
        pass
    return calls


def extract_assistant_and_tool_calls_from_responses(
    resp,
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
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
                    tool_calls.append(
                        {
                            "id": call_id or f"call_{len(tool_calls) + 1}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args, ensure_ascii=False),
                            },
                        }
                    )
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
                tool_calls.append(
                    {
                        "id": getattr(tc, "id", None) or f"call_{len(tool_calls) + 1}",
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                )
        return assistant_text, tool_calls
    except Exception:
        pass

    return assistant_text, tool_calls


def find_duplicates(items: List[str]) -> List[str]:
    """Check duplikat mcp tools

    Args:
        items (List[str]): List tools name

    Returns:
        List[str]: Return duplikat tools name
    """
    seen, dup = set(), set()
    for x in items:
        if x in seen:
            dup.add(x)
        else:
            seen.add(x)
    return sorted(list(dup))


def looks_double_nested(schema: Dict[str, Any]) -> bool:
    """Check double nested schema properties function call schema

    Args:
        schema (Dict[str, Any]): mcp schema

    Returns:
        bool: Status double nested atau tidak
    """
    return (
        isinstance(schema, dict)
        and schema.get("type") == "object"
        and isinstance(schema.get("properties"), dict)
        and "properties" in schema["properties"]
        and isinstance(schema["properties"]["properties"], dict)
    )


def flatten_double_nested(schema: Dict[str, Any]) -> Dict[str, Any]:
    s = deepcopy(schema)
    inner = s["properties"]["properties"]
    new_s = {"type": "object", "properties": deepcopy(inner)}
    # tarik required dari inner jika ada
    if isinstance(s["properties"].get("required"), list):
        new_s["required"] = deepcopy(s["properties"]["required"])
    # pertahankan title luar kalau ada
    if isinstance(s.get("title"), str):
        new_s["title"] = s["title"]
    return new_s


def has_flexible_fields(schema: Dict[str, Any]) -> bool:
    """
    Heuristik: jika ada field yang jelas fleksibel (object bebas / union null / additionalProperties true),
    kita TIDAK akan memaksa strict/additionalProperties:false di level top.
    """
    if not isinstance(schema, dict):
        return True
    if schema.get("type") != "object":
        return True

    props = schema.get("properties") or {}
    if not isinstance(props, dict):
        return True

    for k, v in props.items():
        if not isinstance(v, dict):
            return True
        # object bebas
        if (
            v.get("type") == "object"
            and v.get("properties") is None
            and v.get("additionalProperties") is None
        ):
            return True
        # union yang mengandung object atau null (fleksibel)
        for key in ("anyOf", "oneOf", "type"):
            val = v.get(key)
            if key in ("anyOf", "oneOf") and isinstance(val, list):
                if any(
                    isinstance(e, dict) and e.get("type") in ("object", "null")
                    for e in val
                ):
                    return True
            elif key == "type":
                if isinstance(val, list) and any(t in ("object", "null") for t in val):
                    return True
        # nama field yang umum fleksibel
        if k in ("metadata_filter", "ctx", "context", "extra"):
            return True

        # additionalProperties:true di field anak → indikasi fleksibel
        if v.get("additionalProperties") is True:
            return True

    return False


def harden_schema(schema_in: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """
    Kembalikan: (schema_akhir, strict_flag)
    - Pastikan schema object valid.
    - Perbaiki double-nesting bila ada.
    - Tambahkan additionalProperties:false + strict=True hanya jika tidak fleksibel.
    """
    s = deepcopy(schema_in) if isinstance(schema_in, dict) else {}
    # default minimal
    if not s:
        s = {"type": "object", "properties": {}}

    # ratahkan jika model MCP menghasilkan properties.properties
    if looks_double_nested(s):
        s = flatten_double_nested(s)

    # pastikan type object & properties ada
    if s.get("type") != "object":
        s["type"] = "object"
    if not isinstance(s.get("properties"), dict):
        s["properties"] = {}

    # putuskan apakah fleksibel
    is_flexible = has_flexible_fields(s)

    strict = False
    if not is_flexible:
        # untuk argumen fixed → kunci rapat
        if "additionalProperties" not in s:
            s["additionalProperties"] = False
        strict = True
    else:
        # jangan paksa strict, biarkan additionalProperties default
        # (kalau sebelumnya ada False dari input, tidak diubah)
        pass

    return s, strict


def truncate_args(v: Any, n: int = 200) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[: n - 3] + "..."


def to_jsonable(obj: Any) -> Any:
    """
    Ubah hasil tool menjadi bentuk yang pasti bisa di-serialize ke JSON.
    - Dict/list: rekursif.
    - Objek dengan .model_dump(): gunakan itu.
    - Tipe OpenAI content (mis. TextContent): fallback ke str(obj).
    - Primitive: biarkan apa adanya.
    """
    # primitive aman
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    # mapping
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    # list/tuple
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    # pydantic-like
    if hasattr(obj, "model_dump") and callable(getattr(obj, "model_dump")):
        try:
            return to_jsonable(obj.model_dump())
        except Exception:
            return str(obj)
    # dataclass
    try:
        from dataclasses import asdict, is_dataclass

        if is_dataclass(obj):
            return to_jsonable(asdict(obj))  # type: ignore
    except Exception:
        pass
    # terakhir: coba json langsung
    try:
        json.dumps(obj)
        return obj
    except Exception:
        return str(obj)


def contains_explicit_intent(prompt: str, tool_name: str) -> bool:
    p = (prompt or "").lower()
    return any(
        k in p
        for k in [
            "ingest",
            "upload",
            "unggah",
            "masukkan",
            "parse",
            "ekstrak",
            "convert",
            tool_name.lower(),
        ]
    )


# ==========================
# 4) Validasi argumen tool
# ==========================
def _fill_defaults(schema: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    props = (schema or {}).get("properties", {}) or {}
    out = dict(data or {})
    for k, spec in props.items():
        if k not in out and isinstance(spec, dict) and "default" in spec:
            out[k] = spec["default"]
    return out


def validate_tool_args(schema: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    args = _fill_defaults(schema, args or {})
    Draft202012Validator(schema or {"type": "object"}).validate(args)
    return args


async def build_context_blocks_memory(
    long_term: Mem0Manager,
    short_term: ShortTermMemory,
    user_id: str,
    user_message: str,
    max_history: int = 3,
    prompt_instruction: str = "",
) -> str:
    # STM
    stm_block = await short_term.get_history(user_id, limit=max_history)
    stm_block = stm_block or "[Tidak ada riwayat percakapan]"

    # LTM (relevansi terhadap user_message)
    ltm_results = await long_term.get_memories(user_message, user_id=user_id)
    if not ltm_results:
        # balas ke UI dengan pesan manusiawi (bukan [object Object])
        system_prompt = (
            prompt_instruction
            + "\n\n### Briefing Memori\n"
            + "**Long_Term Memory (relevan):**\n[Gagal memproses LTM]\n\n"
            + f"**Short_Term History (ringkas):**\n{stm_block}\n"
        )

    ltm_block = (
        "\n".join(f"- {m}" for m in ltm_results)
        if ltm_results
        else "[Tidak ada memori relevan]"
    )

    system_prompt = (
        prompt_instruction
        + "\n\n### Briefing Memori\n"
        + f"**Long_Term Memory (relevan):**\n{ltm_block}\n\n"
        + f"**Short_Term History (ringkas):**\n{stm_block}\n"
    )

    return system_prompt
