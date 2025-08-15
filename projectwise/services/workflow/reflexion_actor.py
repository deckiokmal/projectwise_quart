# projectwise/services/workflow/reflexion_actor.py
from __future__ import annotations

import asyncio
import json
from json import JSONDecodeError
from typing import Any, Dict, List, Tuple, Optional, Protocol

from jsonschema import Draft202012Validator
from openai import AsyncOpenAI
from quart import Quart

from projectwise.utils.logger import get_logger


logger = get_logger(__name__)


# =========================
# 1) Kontrak eksekutor tool
# =========================
class ToolExecutor(Protocol):
    async def call_tool(self, name: str, args: Dict[str, Any]) -> Any: ...
    async def get_tools(self) -> List[Dict[str, Any]]: ...


# =========================
# 2) MCP Adapter (sederhana)
# =========================
class MCPToolAdapter:
    """
    Adapter yang mengeksekusi MCP tool via instance di app.extensions.

    Catatan:
    - Tidak membuat MCPClient baru.
    - Menghormati mcp_lock & mcp_status di extensions.py.
    - Menyediakan get_tools() agar ReflectionActor tidak bergantung pada detail internal.
    """

    def __init__(self, app: Quart) -> None:
        self.app = app

    async def _acquire_mcp(self):
        # Pastikan state tersedia
        if "mcp" not in self.app.extensions or "mcp_status" not in self.app.extensions:
            raise RuntimeError("MCP belum diinisialisasi di app.extensions.")

        client = self.app.extensions.get("mcp")
        status: dict = self.app.extensions["mcp_status"]

        # Jangan auto‑connect di sini. Hormati kontrol via /mcp/connect
        if client is None or not status.get("connected"):
            raise RuntimeError(
                "MCP belum terhubung. Silakan klik 'Connect' atau panggil endpoint /mcp/connect terlebih dahulu."
            )
        return client

    async def call_tool(self, name: str, args: Dict[str, Any]) -> Any:
        client = await self._acquire_mcp()
        logger.info("Eksekusi MCP tool: %s | args=%s", name, _truncate_args(args))
        return await client.call_tool(name, args)

    async def get_tools(self) -> List[Dict[str, Any]]:
        """Kembalikan daftar tool MCP (gunakan cache bila tersedia)."""
        client = await self._acquire_mcp()
        tools: List[Dict[str, Any]] = getattr(client, "tool_cache", []) or []
        logger.info("MCP tool_cache terdeteksi: %d tool.", len(tools))
        return tools


# ==================================
# 3) Normalisasi tools dari MCP → LLM
# ==================================
def normalize_mcp_tools(
    mcp_tools: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """
    Kembalikan:
      tools_for_openai: list tools utk Responses API (function calling)
      registry: meta per alat (schema, guardrail, dll)
    """
    tools_for_openai: List[Dict[str, Any]] = []
    registry: Dict[str, Dict[str, Any]] = {}

    if not mcp_tools:
        logger.warning("normalize_mcp_tools: mcp_tools kosong.")

    for t in mcp_tools:
        func = t.get("function") or {}
        params = func.get("parameters") or t.get("inputSchema") or {"type": "object", "properties": {}}
        name = func.get("name") or t.get("name")
        desc = func.get("description") or t.get("description") or ""

        # Pastikan schema bertipe object agar valid utk function calling
        if params.get("type") != "object":
            logger.warning("Tool %s memiliki schema non-object. Memaksa ke object {}.", name)
            params = {"type": "object", "properties": {}}

        tools_for_openai.append({
            "type": "function",
            "name": str(name),
            "description": str(desc),
            "parameters": params,
            "strict": True,
        })

        need_explicit = "only if user explisit ask" in desc.lower() or "only if user explicit ask" in desc.lower()
        registry[str(name)] = {
            "name": str(name),
            "parameters": params,
            "need_explicit": need_explicit,
            "description": desc,
            "raw": t
        }

    # Pastikan nama unik
    names = [x["name"] for x in tools_for_openai]
    if len(set(names)) != len(names):
        dupes = _find_duplicates(names)
        raise ValueError(f"Duplicate tool name terdeteksi: {dupes}")

    logger.info("normalize_mcp_tools: %d tools siap dikirim ke LLM.", len(tools_for_openai))
    return tools_for_openai, registry


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


# ==========================================
# 5) Reflection–Actor Orchestrator (singkat)
# ==========================================
class ReflectionActor:
    def __init__(
        self,
        llm: AsyncOpenAI,
        model: str,
        executor: ToolExecutor,
        max_steps: int = 3,
        step_timeout_sec: float = 60.0,
    ) -> None:
        self.llm = llm
        self.model = model
        self.executor = executor
        self.max_steps = max_steps
        self.step_timeout_sec = step_timeout_sec

    @classmethod
    def from_quart_app(
        cls,
        app: Quart,
        llm: Optional[AsyncOpenAI] = None,
        model: Optional[str] = None,
    ) -> "ReflectionActor":
        service_configs = app.extensions["service_configs"]  # type: ignore
        _llm = llm or AsyncOpenAI()
        _model = model or service_configs.llm_model
        executor = MCPToolAdapter(app)
        return cls(llm=_llm, model=_model, executor=executor)

    # -------- util LLM ----------
    async def _call_llm(self, *, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]):
        """Pembungkus pemanggilan Responses API + timeout + logging."""
        logger.debug("LLM call: tools=%d, msg=%d", len(tools or []), len(messages))
        try:
            return await asyncio.wait_for(
                self.llm.responses.create(model=self.model, input=messages, tools=tools), # type: ignore
                timeout=self.step_timeout_sec,
            )
        except asyncio.TimeoutError:
            logger.exception("Timeout memanggil LLM (responses.create).")
            raise
        except Exception:
            logger.exception("Gagal memanggil LLM (responses.create).")
            raise

    # -------- ekstraksi function_call ----------
    @staticmethod
    def _iter_function_calls(response) -> List[Dict[str, Any]]:
        """Ekstrak function_call secara defensif."""
        calls: List[Dict[str, Any]] = []
        output = getattr(response, "output", None)
        if isinstance(output, list):
            for it in output:
                if getattr(it, "type", None) == "function_call":
                    calls.append({
                        "name": getattr(it, "name", ""),
                        "arguments": getattr(it, "arguments", "") or "{}",
                        "call_id": getattr(it, "call_id", None),
                        "raw": it,
                    })
        return calls

    @staticmethod
    def _safe_output_text(resp) -> str:
        text = getattr(resp, "output_text", "") or ""
        return text.strip()

    async def reflection_actor_with_mcp(self, prompt: str, actor_instruction: Optional[str] = None, critic_instruction: Optional[str] = None) -> str:
        logger.info("Mulai loop untuk prompt: %r", prompt)

        # 1) Ambil daftar tools dari MCP
        mcp_tools = await self.executor.get_tools()
        tools, registry = normalize_mcp_tools(mcp_tools)

        # ===== Actor: panggil model dgn tools =====
        messages = [
            {"role": "system", "content": actor_instruction},
            {"role": "user", "content": prompt},
        ]

        # Step 1: Panggilan awal Actor (bisa mengeluarkan function_call)
        response = await self._call_llm(messages=messages, tools=tools)

        # Step 2: Tangani function_call (jika ada)
        function_calls = self._iter_function_calls(response)
        for fc in function_calls:
            name = fc["name"]
            args_raw = fc["arguments"]
            logger.info("LLM meminta function_call: %s args=%s", name, _truncate_args(args_raw, 400))

            # Guardrail: alat yang butuh explicit ask
            if registry.get(name, {}).get("need_explicit"):
                if not _contains_explicit_intent(prompt, name):
                    logger.warning("Guardrail: menolak eksekusi tool %s (tidak ada eksplisit perintah).", name)
                    output = json.dumps({
                        "ok": False,
                        "error": "GUARDRAIL",
                        "message": "Aksi ini hanya dijalankan jika pengguna secara eksplisit memintanya."
                    }, ensure_ascii=False)
                    messages.append(fc["raw"])  # catat function_call
                    messages.append({"type": "function_call_output", "call_id": fc["call_id"], "output": output})
                    continue

            # Validasi argumen
            try:
                args = json.loads(args_raw or "{}")
            except JSONDecodeError:
                logger.exception("Gagal decode JSON arguments untuk tool %s.", name)
                args = {}

            schema = registry[name]["parameters"]
            try:
                valid_args = validate_tool_args(schema, args)
            except Exception:
                logger.exception("Validasi argumen gagal untuk tool %s.", name)
                output = json.dumps({"ok": False, "error": "InvalidArguments", "message": "Schema validation failed."}, ensure_ascii=False)
                messages.append(fc["raw"])
                messages.append({"type": "function_call_output", "call_id": fc["call_id"], "output": output})
                continue

            # Eksekusi MCP tool (dengan timeout)
            try:
                result = await asyncio.wait_for(self.executor.call_tool(name, valid_args), timeout=self.step_timeout_sec)
                output = json.dumps({"ok": True, "result": result}, ensure_ascii=False)
                logger.info("Tool %s sukses.", name)
            except asyncio.TimeoutError:
                logger.exception("Timeout eksekusi tool %s.", name)
                output = json.dumps({"ok": False, "error": "Timeout", "message": f"Tool {name} melebihi {self.step_timeout_sec}s"}, ensure_ascii=False)
            except Exception as e:
                logger.exception("Gagal eksekusi tool %s.", name)
                output = json.dumps({"ok": False, "error": type(e).__name__, "message": str(e)}, ensure_ascii=False)

            # Tambahkan call & output ke messages
            messages.append(fc["raw"])
            messages.append({"type": "function_call_output", "call_id": fc["call_id"], "output": output})

        # Step 3: Dapatkan jawaban sementara (setelah tool-outputs di-append)
        interim_resp = await self._call_llm(messages=messages, tools=tools)
        interim_result = self._safe_output_text(interim_resp)
        logger.info("Interim length=%d chars", len(interim_result))

        # ===== Reflection: iterasi singkat =====
        critique_text = ""
        for i in range(self.max_steps):
            critic_msgs = [
                {"role": "system", "content": critic_instruction},
                {"role": "user", "content": f"USER REQUEST:\n{prompt}\n\nACTOR OUTPUT (INTERIM):\n{interim_result}"}
            ]
            critic_resp = await self._call_llm(messages=critic_msgs, tools=[])
            critique_text = self._safe_output_text(critic_resp)
            logger.info("Critic[%d]: %s", i + 1, _truncate_args(critique_text, 300))

            if "FINALIZE" in critique_text.upper():
                logger.info("Critic meminta FINALIZE pada iterasi %d.", i + 1)
                break

            # Dorong Actor memperbaiki singkat berdasarkan kritik
            messages.append({"role": "user", "content": f"INSTRUKSI KRITIK: {critique_text}"})
            interim_resp = await self._call_llm(messages=messages, tools=tools)
            interim_result = self._safe_output_text(interim_resp)

        # ===== Finalisasi oleh Actor =====
        final_msgs = [
            {"role": "system", "content": actor_instruction},
            {"role": "user", "content": f"Permintaan awal: {prompt}"},
            {"role": "user", "content": f"Output interim: {interim_result}"},
            {"role": "user", "content": f"Kritik/Refleksi: {critique_text}\n\nSusun jawaban FINAL yang rapi dan actionable."}
        ]
        final_resp = await self._call_llm(messages=final_msgs, tools=tools)
        final_text = self._safe_output_text(final_resp)
        logger.info("Final length=%d chars", len(final_text))

        return final_text


# =================
# 6) Utilitas kecil
# =================
def _truncate_args(v: Any, n: int = 200) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[: n - 3] + "..."

def _find_duplicates(items: List[str]) -> List[str]:
    seen, dup = set(), []
    for x in items:
        if x in seen:
            dup.append(x)
        else:
            seen.add(x)
    return dup

def _contains_explicit_intent(prompt: str, tool_name: str) -> bool:
    p = (prompt or "").lower()
    return any(k in p for k in [
        "ingest","upload","unggah","masukkan","parse","ekstrak","convert", tool_name.lower()
    ])
