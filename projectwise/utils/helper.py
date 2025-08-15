# projectwise/utils/helper.py
from __future__ import annotations

import json
import tiktoken
from typing import Any


def truncate_by_tokens(text: str, max_tokens: int, model: str = "gpt-4o-mini") -> str:
    """
    Potong teks berdasarkan jumlah token agar aman untuk dimasukkan ke prompt.
    Fallback encoding: cl100k_base bila model tidak dikenali tiktoken.
    """
    try:
        enc = tiktoken.encoding_for_model(model)
    except Exception:
        enc = tiktoken.get_encoding("cl100k_base")

    tokens = enc.encode(text or "")
    if len(tokens) <= max_tokens:
        return text or ""

    return enc.decode(tokens[:max_tokens])

def safe_args(obj: Any) -> str:
    try:
        s = json.dumps(obj)
        return s[:1000]
    except Exception:
        return str(obj)[:1000]
    

def stringify(obj: Any, limit: int = 4000) -> str:
    try:
        s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
        if len(s) > limit:
            return s[: limit - 3] + "..."
        return s
    except Exception:
        return str(obj)[:limit]