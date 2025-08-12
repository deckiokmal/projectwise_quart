# projectwise/utils/helper.py
from __future__ import annotations

import tiktoken


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
