# projectwise/services/workflow/handler_project_analysis.py — refactor v3 (seeded args + actor)
# =============================================================================
# Tujuan refactor v3:
# - Tetap patuh arsitektur: semua panggilan LLM via LLMChains; tools via registry/MCP.
# - Manfaatkan ReflectionActor untuk loop utama (actor→tools→critic),
#   namun **seed** 1x pemanggilan tool (mis. `retrieval`) dengan **argumen otomatis**
#   sebelum memanggil actor, agar model tidak meminta nama file lebih dulu.
# - Tidak ada append manual coroutine ke JSON (menghindari error serialisasi).
# - Komentar lengkap + logging ramah debug (ringkas, tidak noisy).
# =============================================================================
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Callable, Awaitable
import re
import json

from quart import Quart

from projectwise.utils.logger import get_logger
from projectwise.config import ServiceConfigs

# Prompt policy
from projectwise.services.workflow.prompt_instruction import (
    DEFAULT_SYSTEM_PROMPT,
    PROMPT_USER_CONTEXT,
)
from projectwise.services.llm_chain.tool_registry import build_mcp_tooling
from projectwise.services.mcp.adapter import MCPToolAdapter

# LLM orchestrator & actor
from projectwise.services.llm_chain.llm_chains import LLMChains, Prefer
from projectwise.services.workflow.reflection_actor import ReflectionActor

# MCP tooling
from projectwise.services.llm_chain.tool_registry import (
    build_mcp_tooling,
)
from projectwise.services.mcp.adapter import MCPToolAdapter

# Utils
from projectwise.services.llm_chain.llm_utils import (
    shape_system,
    shape_user,
    build_context_blocks_memory,
    to_jsonable,
    truncate_args,
)

logger = get_logger(__name__)
settings = ServiceConfigs()


# ============================= Tool helpers ============================= #
def _find_tool(tools: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    for t in tools:
        if (
            t.get("type") == "function"
            and (t.get("function") or {}).get("name") == name
        ):
            return t
    return None


def _list_tool_names(tools: List[Dict[str, Any]]) -> List[str]:
    names: List[str] = []
    for t in tools:
        if t.get("type") == "function":
            n = (t.get("function") or {}).get("name")
            if n:
                names.append(n)
    return sorted(set(names))


def _extract_entities_from_prompt(prompt: str) -> Dict[str, Optional[str]]:
    """Ekstrak entitas sederhana dari prompt: pelanggan, project, tahun."""
    q = (prompt or "").lower()
    tahun = None
    m = re.search(r"(20\d{2})", q)
    if m:
        tahun = m.group(1)
    pelanggan = None
    if any(x in q for x in ["sumsel", "babel", "ssb", "sumsel babel"]):
        pelanggan = "Bank Sumsel Babel"
    project = None
    if any(
        x in q for x in ["switch core", "core switch", "switching core", "core network"]
    ):
        project = "Switch Core"
    return {"pelanggan": pelanggan, "project": project, "tahun": tahun}


def _build_seed_args(prompt: str, tool: Dict[str, Any]) -> Dict[str, Any]:
    """Bangun argumen awal yang masuk akal untuk tool *retrieval* (atau sejenis)."""
    props = ((tool.get("function") or {}).get("parameters") or {}).get(
        "properties", {}
    ) or {}
    ent = _extract_entities_from_prompt(prompt)

    # Kandidat umum nama parameter query
    q_keys = [k for k in ("query", "q", "text", "keywords") if k in props]
    # Field metadata
    pel_key = next((k for k in ("pelanggan", "customer", "client") if k in props), None)
    proj_key = next((k for k in ("project", "proyek", "judul") if k in props), None)
    year_key = next((k for k in ("tahun", "year") if k in props), None)
    k_key = next((k for k in ("k", "top_k", "limit") if k in props), None)

    args: Dict[str, Any] = {}
    if q_keys:
        args[q_keys[0]] = prompt.strip()
    if pel_key and ent["pelanggan"]:
        args[pel_key] = ent["pelanggan"]
    if proj_key and ent["project"]:
        args[proj_key] = ent["project"]
    if year_key and ent["tahun"]:
        args[year_key] = ent["tahun"]
    if k_key:
        args[k_key] = 8

    return args


def _tool_priming_block(tool_names: List[str], query: str) -> str:
    names_str = ", ".join(tool_names) if tool_names else "[tidak ada]"
    q = (query or "").lower()
    tahun = re.findall(r"(20\d{2})", q)
    tahun = tahun[0] if tahun else ""
    return (
        "# TOOLS_AVAILABLE (names only)\n"
        f"{names_str}\n\n"
        "# TOOL-USAGE HINTS (internal)\n"
        "- Prioritaskan fungsi: 'retrieval'/'kak'/'tor'/'project_context'/'search' sebelum minta klarifikasi.\n"
        "- Isi parameter 'query/keywords' dengan ringkasan permintaan.\n"
        f"- Kata kunci dari user → pelanggan: {'bank sumsel babel' if 'sumsel' in q or 'babel' in q else '[cari di memori/dok]'}, proyek: {'switch core' if 'switch' in q or 'core' in q else '[cari di memori/dok]'}, tahun: {tahun or '[jika ada]'}\n"
    )


def _build_actor_instruction() -> str:
    rules = (
        "\n# TUGAS: PROJECT ANALYSIS\n"
        "- Jawab berdasarkan: dokumen (RAG), memori (STM/LTM), dan kalkulator produk.\n"
        "- **Jangan meminta nama file KAK/TOR sebelum mencoba tools pencarian/RAG setidaknya 1×.**\n"
        "- Jangan sebut nama tool MCP di jawaban akhir.\n"
        "- Format ringkas, berbasis poin, actionable.\n"
    )
    return (DEFAULT_SYSTEM_PROMPT + rules).strip()


# ============================= Handler utama ============================= #
class ProjectAnalysisHandler:
    """Handler analisis proyek: seed 1x tool → ReflectionActor (actor→critic)."""

    def __init__(
        self,
        app: Quart,
        *,
        model: Optional[str] = None,
        prefer: Prefer = "auto",
        request_timeout: float = 60.0,
        tool_timeout: float = 45.0,
        tool_retries: int = 1,
        max_history: int = 20,
    ) -> None:
        self.app = app
        self.prefer = prefer
        self.max_history = max_history
        self.model = model or settings.llm_model
        # Actor untuk loop utama (tetap LLMChains di dalamnya)
        self.actor = ReflectionActor.from_quart_app(
            app,
            model=self.model,
            prefer=self.prefer,
            max_history=max_history,
        )

    async def _seed_once(self, prompt: str) -> Tuple[str, Dict[str, Any]]:
        """Jalankan 1x tool yang paling relevan (prefer 'retrieval') dengan argumen di‑autofill.
        Mengembalikan (seed_hint_text, seed_meta) untuk disuntik ke extra_hints Actor.
        """
        tools, tool_exec = await _prepare_tooling(self.app)
        if not tools or not tool_exec:
            return "", {"seed": "skipped_no_tools"}

        # Pilih tool kandidat
        tool_names = _list_tool_names(tools)
        name = "retrieval" if "retrieval" in tool_names else None
        if not name:
            # fallback: cari yang mengandung kata kunci
            for kw in ("kak", "tor", "project_context", "search", "rag"):
                name = next((n for n in tool_names if kw in n.lower()), None)
                if name:
                    break
        if not name:
            return "", {"seed": "skipped_no_match"}

        tool = _find_tool(tools, name)
        if not tool:
            return "", {"seed": "skipped_missing_tool"}

        logger.info("[analysis] seed tool %s args=%s", name, truncate_args(args))

        try:
            res = await tool_exec(name, args)
        except Exception as e:
            logger.warning(
                "[analysis] seed tool '%s' gagal: %s", name, e, exc_info=True
            )
            return "", {"seed": "error", "tool": name, "error": str(e)}

        # Susun seed hint ringkas untuk disuntikkan ke Actor (tanpa JSON besar)
        safe = to_jsonable(res)
        # ambil ringkasan kunci saja
        keys = (
            ", ".join(list(safe.keys())[:6])
            if isinstance(safe, dict)
            else type(safe).__name__
        )
        hint = (
            "# SEED_RESULT (internal)\n"
            f"tool: {name}\n"
            f"args: {json.dumps(args, ensure_ascii=False)}\n"
            f"result_keys: {keys}\n"
            "→ Gunakan hasil ini sebagai jangkar awal; lanjutkan analisis dengan tools RAG lain bila perlu.\n"
        )
        return hint, {"seed": "ok", "tool": name, "args": args}

    async def run(
        self,
        *,
        prompt: str,
        user_id: Optional[str] = None,
        k: int = 6,
        prefer: Optional[Prefer] = None,
        extra_hints: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # 0) Engine heuristik (ramah function-calling untuk model populer non‑OpenAI)
        engine: Prefer = (
            prefer
            or (
                "chat"
                if any(
                    m in (self.model or "").lower()
                    for m in ["qwen", "glm", "yi", "deepseek"]
                )
                else self.prefer
            )
            or "auto"
        ) # type: ignore

        # 1) Seed sekali (agar tidak minta file terlebih dahulu)
        seed_hint, seed_meta = await self._seed_once(prompt)

        # 2) Tool hints (daftar nama) sebagai steering tambahan
        tools, _ = await _prepare_tooling(self.app)
        tool_names = _list_tool_names(tools)
        tool_hints = _tool_priming_block(tool_names, prompt) if tool_names else ""

        # 3) Gabungkan hints untuk Actor
        all_hints = "\n\n".join(
            h for h in [extra_hints or "", tool_hints, seed_hint] if h
        )

        # 4) Jalankan Actor (multi‑hop via LLMChains di dalam ReflectionActor)
        res_text = await self.actor.run(
            user_id=user_id or "default",
            prompt=prompt,
            actor_instruction=_build_actor_instruction(),
            critic_instruction=None,
            extra_hints=all_hints or None,
        )

        # 5) Bungkus hasil ke format standar (LLMChains style)
        result: Dict[str, Any] = {
            "status": "success",
            "message": res_text,
            "hops": 2,  # minimal 2 (seed + actor), actual hop detail ada di log LLMChains
            "meta": {
                "model": self.actor.llm.model,  # type: ignore[attr-defined]
                "engine": engine,
                "tools": len(tool_names),
                **seed_meta,
            },
        }
        return result


# ============================= Entry-point fungsional ============================= #
async def run(
    app: Quart,
    *,
    prompt: str,
    user_id: Optional[str] = None,
    k: int = 6,
    prefer: Optional[Prefer] = None,
    extra_hints: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    handler = ProjectAnalysisHandler(app, prefer=prefer or "auto")
    return await handler.run(
        prompt=prompt,
        user_id=user_id,
        k=k,
        prefer=prefer,
        extra_hints=extra_hints,
        metadata=metadata,
    )
