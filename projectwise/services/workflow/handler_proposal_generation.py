# projectwise/services/workflow/handler_proposal_generation.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Callable, Awaitable

from quart import Quart

from projectwise.utils.logger import get_logger
from projectwise.config import ServiceConfigs

# Prompt policy terpusat
from .prompt_instruction import PROMPT_PROPOSAL_GUIDELINES

# Orkestrator LLM terpadu (chat/responses + tools)
from projectwise.services.llm_chain.llm_chains import LLMChains, Prefer

# Registry & normalizer MCP tools
from projectwise.services.llm_chain.tool_registry import (
    build_mcp_tooling,
)

# Adapter sederhana bila ingin akses via app.extensions dengan lapisan aman
from projectwise.services.mcp.adapter import MCPToolAdapter

# Util format pesan (opsional; bisa juga manual)
try:
    from projectwise.services.llm_chain.llm_utils import shape_system, shape_user  # type: ignore
except Exception:  # tetap aman jika util belum ada

    def shape_system(s: str) -> Dict[str, str]:
        return {"role": "system", "content": s}

    def shape_user(s: str) -> Dict[str, str]:
        return {"role": "user", "content": s}


logger = get_logger(__name__)
settings = ServiceConfigs()


# ============================= Tooling builder ============================= #
async def _prepare_tooling(
    app: Quart,
) -> Tuple[
    List[Dict[str, Any]],
    Optional[Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]],
]:
    """Siapkan (tools_for_llm, tool_executor) atau ([], None) jika MCP tidak siap.

    Strategi bertingkat:
      1) Gunakan `app.extensions['mcp']` jika tersambung dan bangun tooling via
         `build_mcp_tooling(mcp)` (validasi schema + executor standar).
      2) Fallback ke `MCPToolAdapter(app)` — ambil tool_cache dan normalisasi ke
         format OpenAI tools; buat executor sederhana tanpa validasi ketat.
      3) Jika semua gagal, kembalikan tanpa tools.
    """
    # 1) Prefer MCPClient langsung
    try:
        mcp = app.extensions.get("mcp")  # MCPClient
        status = app.extensions.get("mcp_status", {}) or {}
        if mcp is not None and status.get("connected"):
            tools, tool_executor, _registry = build_mcp_tooling(mcp)
            logger.info("[proposal] Tooling via MCPClient: %d tool", len(tools))
            return tools, tool_executor
    except Exception as e:
        logger.warning("[proposal] build_mcp_tooling gagal: %s", e, exc_info=True)

    # 2) Fallback adapter
    try:
        adapter = MCPToolAdapter(app)
        tools = await adapter.get_tools()  # list dari tool_cache

        async def _exec(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
            try:
                return await adapter.call_tool(name, args)
            except Exception as e:
                return {"status": "error", "message": f"Gagal eksekusi '{name}': {e}"}

        logger.info("[proposal] Tooling via MCPToolAdapter: %d tool", len(tools))
        return tools, _exec
    except Exception as e:
        logger.warning("[proposal] Adapter MCP tidak tersedia: %s", e)

    # 3) Tanpa tools
    return [], None


def _build_system_prompt(extra_rules: Optional[str] = None) -> str:
    """System prompt untuk Proposal Generation (mengacu ke prompt terpusat)."""
    rules = "- Ikuti prosedur 1→4 dengan tertib."
    return (PROMPT_PROPOSAL_GUIDELINES() + rules + (extra_rules or "")).strip()


# ============================= API utama ============================= #
class ProposalGenerationHandler:
    """Orkestrator Proposal Generation berbasis `LLMChains` + MCP tools.

    Contoh pakai (di route Quart):
        handler = ProposalGenerationHandler(app, model=model, prefer="auto")
        result = await handler.run(project_name, user_query, override_template)
        # `result` adalah dict: {status, message, hops, took_ms, usage?, meta}
    """

    def __init__(
        self,
        app: Quart,
        *,
        model: Optional[str] = None,
        prefer: Prefer = "auto",
        request_timeout: float = 90.0,
        tool_timeout: float = 60.0,
        tool_retries: int = 1,
        client=None,
    ) -> None:
        self.app = app
        self.prefer = prefer  # default engine preference (auto/chat/responses)
        self.llm = LLMChains(
            model=model or settings.llm_model,
            prefer=prefer,  # boleh dioverride per-run
            request_timeout=request_timeout,
            tool_timeout=tool_timeout,
            tool_retries=tool_retries,
            client=client,  # injeksi AsyncOpenAI jika dipasang oleh caller
        )

    async def run(
        self,
        project_name: str,
        user_query: str,
        override_template: Optional[str] = None,
        *,
        user_id: Optional[str] = None,
        prefer: Optional[Prefer] = None,
        metadata: Optional[Dict[str, Any]] = None,
        max_hops: int = 8,
    ) -> Dict[str, Any]:
        """Jalankan autonomous proposal generation.

        Args:
            project_name: Nama proyek (wajib) untuk penarikan konteks awal.
            user_query: Instruksi/permintaan pengguna (mis. penekanan fitur, SLA, dll.).
            override_template: Path template alternatif (.docx) bila ingin override.
            user_id: Opsional untuk telemetri/observabilitas.
            prefer: Paksa mode ("responses"|"chat"|"auto").
            metadata: Metadata dikirim ke Responses API (jika dipakai).
            max_hops: Batas putaran function-calling.
        Returns:
            Dict {status, message, hops, took_ms, usage?, meta}.
        """
        # 1) Siapkan tools & executor (bisa kosong jika MCP off)
        tools_for_llm, tool_executor = await _prepare_tooling(self.app)

        # 2) Susun pesan awal
        system_prompt = _build_system_prompt()
        hints = []
        hints.append(f"PROJECT_NAME: {project_name}")
        if override_template:
            hints.append("OVERRIDE_TEMPLATE: tersedia — gunakan bila perlu.")
        if user_query:
            hints.append(f"USER_NOTES: {user_query}")
        hints_str = "".join(hints)

        messages: List[Dict[str, Any]] = [
            shape_system(system_prompt),
            shape_user(
                (
                    "Ikuti prosedur 1→4. Panggil fungsi sesuai kebutuhan sampai dokumen jadi."
                    "KONTEKS INPUT:"
                    f"{hints_str}"
                    "CATATAN:"
                    '- Jika ada placeholder yang masih kosong/kritis, minta saya melengkapi dalam format JSON {"context":{...}}.'
                    "- Setelah semua lengkap, hasilkan proposal .docx dan beritahu lokasi file."
                ).strip()
            ),
        ]

        # 3) Jalankan autonomous loop via LLMChains
        async def _no_tool_exec(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
            return {"status": "error", "message": "no-tools"}

        tool_exec = tool_executor or _no_tool_exec
        tool_choice = "auto" if len(tools_for_llm) > 0 else "none"

        result = await self.llm.run_function_call_roundtrip(
            messages,
            tools=tools_for_llm,
            tool_executor=tool_exec,
            tool_choice=tool_choice,
            max_hops=max(1, int(max_hops)),
            prefer=prefer,  # override kalau diberikan
            metadata={
                "user_id": user_id,
                "project_name": project_name,
                **(metadata or {}),
            }
            if metadata
            else {"user_id": user_id, "project_name": project_name}
            if user_id
            else {"project_name": project_name},
        )

        # 4) Fallback endpoint: jika gagal/hasil kosong → coba endpoint kebalikan
        need_fb = (
            result.get("status") != "success"
            or not (result.get("message") or "").strip()
        )
        if need_fb:
            alt: Prefer = (
                "chat"
                if (prefer or self.prefer or "auto") == "responses"
                else "responses"
            )
            logger.warning("[proposal] Fallback ke endpoint: %s", alt)
            fb = await self.llm.run_function_call_roundtrip(
                messages,
                tools=tools_for_llm,
                tool_executor=tool_exec,
                tool_choice=tool_choice,
                max_hops=max(1, int(max_hops)),
                prefer=alt,
                metadata={
                    "user_id": user_id,
                    "project_name": project_name,
                    **(metadata or {}),
                }
                if metadata
                else {"user_id": user_id, "project_name": project_name}
                if user_id
                else {"project_name": project_name},
            )
            # Pakai yang lebih baik
            if (
                fb.get("status") == "success" and (fb.get("message") or "").strip()
            ) or (fb.get("hops", 0) > result.get("hops", 0)):
                result = fb

        # 5) Tambahkan meta ringkas (untuk observabilitas di UI)
        result.setdefault("meta", {})
        result["meta"].update(
            {
                "model": self.llm.model,
                "engine": prefer or self.prefer or "auto",
                "tools": len(tools_for_llm),
                "project_name": project_name,
                "override_template": bool(override_template),
            }
        )
        return result


# ============================= Helper fungsional ============================= #
async def run(
    *,
    client: Any,
    project_name: str,
    user_query: str,
    app: Quart,
    override_template: Optional[str] = None,
    user_id: Optional[str] = None,
    prefer: Optional[Prefer] = None,
) -> Dict[str, Any]:
    """Entry‑point fungsional agar route bisa memanggil langsung tanpa membuat instance.

    Contoh di `routes/chat.py`:
        client = type("C", (), {"llm": llm, "model": model})
        data = await handler_proposal_generation.run(
            client=client,
            project_name=project_name or "Untitled",
            user_query=q,
            override_template=override_template,
            app=current_app,
        )
    """
    # Ambil model & client LLM (jika disediakan oleh caller); jatuh ke default settings jika tidak.
    model = getattr(client, "model", settings.llm_model)
    llm_client = getattr(client, "llm", None)

    handler = ProposalGenerationHandler(
        app,
        model=model,
        prefer=prefer or "auto",
        client=llm_client,
    )

    return await handler.run(
        project_name=project_name,
        user_query=user_query,
        override_template=override_template,
        user_id=user_id,
        prefer=prefer,
    )
