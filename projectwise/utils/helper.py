import re
import difflib
import unicodedata
from typing import Optional
from tiktoken import encoding_for_model
from projectwise.config import ServiceConfigs


settings = ServiceConfigs()


def safe_args(d: dict, redact_keys=("api_key", "password", "token")) -> dict:
    """Redact sensitive keys for logging."""
    return {k: ("***" if k in redact_keys else v) for k, v in d.items()}


ENC = encoding_for_model(settings.llm_model)  # sesuaikan
MAX_MEM_TOKENS = 150


def truncate_by_tokens(text: str, max_tokens: int = MAX_MEM_TOKENS) -> str:
    """Potong string agar ≤ max_tokens."""
    ids = ENC.encode(text)
    return ENC.decode(ids[:max_tokens])


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return _SLUG_RE.sub("_", text.lower()).strip("_")


def infer_kak_md(query: str) -> Optional[str]:
    """
    Contoh heuristik: ambil kata setelah 'proyek' (jika ada),
    otherwise seluruh kalimat → slug → tanpa .md
    """
    q = query.lower()
    if "proyek" in q:
        q = q.split("proyek", 1)[1]
    slug = slugify(q)
    return f"{slug}" if slug else None


def best_match(
    filename_candidates: list[str], query_slug: str, cutoff: float = 0.5
) -> str | None:
    """
    Pilih nama file paling mirip (rasio > cutoff) dibanding slug.
    """
    matches = difflib.get_close_matches(
        query_slug, filename_candidates, n=1, cutoff=cutoff
    )
    return matches[0] if matches else None
