# projectwise/services/workflow/reflexion_actor.py
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from json import JSONDecodeError
from typing import Any, Dict, List, Tuple, Optional

from openai import AsyncOpenAI, BadRequestError, APIConnectionError
from quart import Quart

from projectwise.utils.logger import get_logger
from projectwise.services.memory.long_term_memory import Mem0Manager
from projectwise.services.memory.short_term_memory import ShortTermMemory
from projectwise.services.workflow.prompt_instruction import PROMPT_USER_CONTEXT
from projectwise.utils.llm_io import build_context_blocks_memory
from projectwise.utils.llm_io import (
    find_duplicates,
    truncate_args,
    to_jsonable,
    contains_explicit_intent,
    validate_tool_args,
)
from projectwise.services.mcp.adapter import ToolExecutor, MCPToolAdapter


logger = get_logger(__name__)


# # =========================
# # 1) Kontrak eksekutor tool
# # =========================
# class ToolExecutor(Protocol):
#     async def call_tool(self, name: str, args: Dict[str, Any]) -> Any: ...
#     async def get_tools(self) -> List[Dict[str, Any]]: ...


# # =========================
# # 2) MCP Adapter (sederhana)
# # =========================
# class MCPToolAdapter:
#     """
#     Adapter yang mengeksekusi MCP tool via instance di app.extensions.

#     Catatan:
#     - Tidak membuat MCPClient baru.
#     - mcp_status di extensions.py.
#     - Menyediakan get_tools() agar ReflectionActor tidak bergantung pada detail internal.
#     """

#     def __init__(self, app: Quart) -> None:
#         self.app = app

#     async def _acquire_mcp(self):
#         # Pastikan state tersedia
#         if "mcp" not in self.app.extensions or "mcp_status" not in self.app.extensions:
#             raise RuntimeError("MCP belum diinisialisasi di app.extensions.")

#         client = self.app.extensions.get("mcp")
#         status: dict = self.app.extensions["mcp_status"]

#         # Jangan auto‑connect di sini. Hormati kontrol via /mcp/connect
#         if client is None or not status.get("connected"):
#             raise RuntimeError(
#                 "MCP belum terhubung. Silakan klik 'Connect' atau panggil endpoint /mcp/connect terlebih dahulu."
#             )
#         return client

#     async def call_tool(self, name: str, args: Dict[str, Any]) -> Any:
#         client = await self._acquire_mcp()
#         logger.info("Eksekusi MCP tool: %s | args=%s", name, truncate_args(args))
#         return await client.call_tool(name, args)

#     async def get_tools(self) -> List[Dict[str, Any]]:
#         """Kembalikan daftar tool MCP (gunakan cache bila tersedia)."""
#         client = await self._acquire_mcp()
#         tools: List[Dict[str, Any]] = getattr(client, "tool_cache", []) or []
#         logger.info("MCP tool_cache terdeteksi: %d tool.", len(tools))
#         return tools


# ==================================
# 3) Normalisasi tools dari MCP → LLM
# ==================================
def normalize_mcp_tools(
    mcp_tools: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """
    Return:
      tools_for_openai: daftar tools untuk Responses API (function calling) - DIKEMBALIKAN APA ADANYA.
      registry: meta per alat (schema, guardrail, dll).

    Catatan:
    - Fungsi ini TIDAK melakukan normalisasi lagi. Asumsinya, input sudah berbentuk:
        {"type":"function","name","description","parameters":{...},"strict"?:bool}
      namun tetap dibuat defensif agar aman jika ada variasi kecil.
    """
    tools_for_openai = mcp_tools or []
    if not tools_for_openai:
        logger.warning("normalize_mcp_tools: daftar tools kosong.")
        return tools_for_openai, {}

    registry: Dict[str, Dict[str, Any]] = {}

    for idx, t in enumerate(tools_for_openai):
        raw = t  # sudah siap pakai; tidak diubah
        func = raw.get("function") or {}  # fallback jika ada format lama

        # Ambil nama & deskripsi apa adanya (defensif untuk format lama)
        name = (raw.get("name") or func.get("name") or "").strip()
        if not name:
            # Biarkan errornya eksplisit: tool tanpa name tak valid untuk OpenAI Responses
            raise ValueError(f"Tool pada index {idx} tidak memiliki 'name'.")

        desc = (raw.get("description") or func.get("description") or "").strip()

        # Ambil schema parameters (tanpa harden/normalisasi)
        params_raw = (
            raw.get("parameters")
            or func.get("parameters")
            or raw.get("inputSchema")  # extremely legacy
            or {}
        )

        strict_flag = bool(raw.get("strict", False))

        lower_desc = desc.lower()
        need_explicit = (
            "only if user explisit ask" in lower_desc
            or "only if user explicit ask" in lower_desc
        )

        # Simpan meta lengkap untuk routing/guardrail/debug
        registry[name] = {
            "name": name,
            "parameters_raw": params_raw,  # schema yang dipakai caller
            "strict": strict_flag,
            "need_explicit": need_explicit,
            "description": desc,
            "raw": deepcopy(raw),  # simpan snapshot jika perlu inspeksi
        }

    # Validasi nama unik (case-insensitive)
    names = [t.get("name", "") for t in tools_for_openai]
    lowered = [n.lower() for n in names]
    dupes = find_duplicates(lowered)
    if dupes:
        raise ValueError(f"Duplicate tool name terdeteksi (case-insensitive): {dupes}")

    logger.info(
        "normalize_mcp_tools: %d tools diteruskan tanpa normalisasi; registry dibuat.",
        len(tools_for_openai),
    )
    return tools_for_openai, registry


# ==========================================
# 5) Reflection–Actor Orchestrator (singkat)
# ==========================================
class ReflectionActor:
    def __init__(
        self,
        llm_model: str,
        long_term: Mem0Manager,
        short_term: ShortTermMemory,
        executor: ToolExecutor,
        llm: Optional[AsyncOpenAI] = None,
        max_steps: int = 3,
        step_timeout_sec: float = 60.0,
        max_history: int = 20,
    ) -> None:
        self.llm = llm or AsyncOpenAI()
        self.model = llm_model
        self.long_term = long_term
        self.short_term = short_term
        self.max_history = max_history
        self.executor = executor
        self.max_steps = max_steps
        self.step_timeout_sec = step_timeout_sec

    @classmethod
    def from_quart_app(
        cls,
        app: Quart,
        *,
        llm: Optional[AsyncOpenAI] = None,
        llm_model: Optional[str] = None,
        max_history: int = 20,
    ) -> "ReflectionActor":
        service_configs = app.extensions["service_configs"]  # type: ignore
        long_term: Mem0Manager = app.extensions["long_term_memory"]  # type: ignore
        short_term: ShortTermMemory = app.extensions["short_term_memory"]  # type: ignore
        executor = MCPToolAdapter(app)

        return cls(
            long_term=long_term,
            short_term=short_term,
            llm=llm,
            llm_model=llm_model or service_configs.llm_model,
            executor=executor,
            max_history=max_history,
        )

    # -------- util LLM ----------
    async def _call_llm(
        self, *, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ):
        """Pembungkus pemanggilan Responses API + timeout + logging."""
        logger.debug("LLM call: tools=%d, msg=%d", len(tools or []), len(messages))
        try:
            return await asyncio.wait_for(
                self.llm.responses.create(
                    model=self.model,
                    input=messages,  # type: ignore
                    tools=tools,  # type: ignore
                ),
                timeout=self.step_timeout_sec,
            )
        except APIConnectionError:
            logger.error("LLM APIConnectionError.")
            human = "LLM API Connection Error. Silakan coba lagi."
            raise RuntimeError(human)
        except asyncio.TimeoutError:
            logger.exception("Timeout memanggil LLM (responses.create).")
            raise

        except BadRequestError as e:
            msg = str(e)
            if "Invalid schema for function" in msg and "additionalProperties" in msg:
                human = (
                    "Gagal menjalankan tool karena skema tidak kompatibel dengan OpenAI "
                    "(parameter tool harus menyetel 'additionalProperties: false'). "
                    "Saya sudah menormalkan skemanya, coba ulangi perintah."
                )
            else:
                human = "Permintaan ke LLM ditolak. Periksa format dan skema tools."

            logger.error("LLM 400: %s", msg, exc_info=True)
            raise RuntimeError(human)  # agar UI menampilkan pesan ramah
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
                    calls.append(
                        {
                            "name": getattr(it, "name", ""),
                            "arguments": getattr(it, "arguments", "") or "{}",
                            "call_id": getattr(it, "call_id", None),
                            "raw": it,
                        }
                    )
        return calls

    @staticmethod
    def _safe_output_text(resp) -> str:
        text = getattr(resp, "output_text", "") or ""
        return text.strip()

    async def reflection_actor_with_mcp(
        self,
        prompt: str,
        user_id: str,
        actor_instruction: Optional[str] = None,
        critic_instruction: Optional[str] = None,
    ) -> str:
        logger.info("Mulai loop context untuk prompt: %r", prompt)
        system_memory = await build_context_blocks_memory(
            short_term=self.short_term,
            long_term=self.long_term,
            user_id=user_id,
            user_message=prompt,
            max_history=self.max_history,
            prompt_instruction=PROMPT_USER_CONTEXT(),
        )

        logger.info("System memory generated: %r", system_memory)

        # 1) Ambil daftar tools dari MCP
        mcp_tools = await self.executor.get_tools()
        tools, registry = normalize_mcp_tools(mcp_tools)

        # ===== Actor: panggil model dgn tools =====
        messages = [
            {"role": "system", "content": actor_instruction},
            {"role": "user", "content": f"User Memory Context: {system_memory}"},
            {"role": "user", "content": prompt},
        ]

        # Step 1: Panggilan awal Actor (bisa mengeluarkan function_call)
        response = await self._call_llm(messages=messages, tools=tools)

        # Step 2: Tangani function_call (jika ada)
        function_calls = self._iter_function_calls(response)
        for fc in function_calls:
            name = fc["name"]
            args_raw = fc["arguments"]
            logger.info(
                "LLM meminta function_call: %s args=%s",
                name,
                truncate_args(args_raw, 400),
            )

            # Guardrail: alat yang butuh explicit ask
            if registry.get(name, {}).get("need_explicit"):
                if not contains_explicit_intent(prompt, name):
                    logger.warning(
                        "Guardrail: menolak eksekusi tool %s (tidak ada eksplisit perintah).",
                        name,
                    )
                    output = json.dumps(
                        {
                            "ok": False,
                            "error": "GUARDRAIL",
                            "message": "Aksi ini hanya dijalankan jika pengguna secara eksplisit memintanya.",
                        },
                        ensure_ascii=False,
                    )
                    messages.append(fc["raw"])  # catat function_call
                    messages.append(
                        {
                            "type": "function_call_output",
                            "call_id": fc["call_id"],
                            "output": output,
                        }
                    )
                    continue

            # Validasi argumen
            try:
                args = json.loads(args_raw or "{}")
            except JSONDecodeError:
                logger.exception("Gagal decode JSON arguments untuk tool %s.", name)
                args = {}

            schema = (
                registry[name].get("parameters_final")
                or registry[name].get("parameters_raw")
                or {"type": "object", "properties": {}}
            )
            valid_args = validate_tool_args(schema, args)

            try:
                valid_args = validate_tool_args(schema, args)
            except Exception:
                logger.exception("Validasi argumen gagal untuk tool %s.", name)
                output = json.dumps(
                    {
                        "ok": False,
                        "error": "InvalidArguments",
                        "message": "Schema validation failed.",
                    },
                    ensure_ascii=False,
                )
                messages.append(fc["raw"])
                messages.append(
                    {
                        "type": "function_call_output",
                        "call_id": fc["call_id"],
                        "output": output,
                    }
                )
                continue

            # Eksekusi MCP tool (dengan timeout)
            try:
                result = await asyncio.wait_for(
                    self.executor.call_tool(name, valid_args),
                    timeout=self.step_timeout_sec,
                )
                output = json.dumps(
                    {"ok": True, "result": to_jsonable(result)}, ensure_ascii=False
                )
                logger.info("Tool %s sukses.", name)
            except asyncio.TimeoutError:
                logger.exception("Timeout eksekusi tool %s.", name)
                output = json.dumps(
                    {
                        "ok": False,
                        "error": "Timeout",
                        "message": f"Tool {name} melebihi {self.step_timeout_sec}s",
                    },
                    ensure_ascii=False,
                )
            except Exception as e:
                logger.exception("Gagal eksekusi tool %s.", name)
                output = json.dumps(
                    {"ok": False, "error": type(e).__name__, "message": str(e)},
                    ensure_ascii=False,
                )

            # Tambahkan call & output ke messages
            messages.append(fc["raw"])
            messages.append(
                {
                    "type": "function_call_output",
                    "call_id": fc["call_id"],
                    "output": output,
                }
            )

        # Step 3: Dapatkan jawaban sementara (setelah tool-outputs di-append)
        interim_resp = await self._call_llm(messages=messages, tools=tools)
        interim_result = self._safe_output_text(interim_resp)
        logger.info("Interim length=%d chars", len(interim_result))

        # ===== Reflection: iterasi singkat =====
        critique_text = ""
        for i in range(self.max_steps):
            critic_msgs = [
                {"role": "system", "content": critic_instruction},
                {
                    "role": "user",
                    "content": f"USER REQUEST:\n{prompt}\n\nACTOR OUTPUT (INTERIM):\n{interim_result}",
                },
            ]
            critic_resp = await self._call_llm(messages=critic_msgs, tools=[])
            critique_text = self._safe_output_text(critic_resp)
            logger.info("Critic[%d]: %s", i + 1, truncate_args(critique_text, 300))

            if "FINALIZE" in critique_text.upper():
                logger.info("Critic meminta FINALIZE pada iterasi %d.", i + 1)
                break

            # Dorong Actor memperbaiki singkat berdasarkan kritik
            messages.append(
                {"role": "user", "content": f"INSTRUKSI KRITIK: {critique_text}"}
            )
            interim_resp = await self._call_llm(messages=messages, tools=tools)
            interim_result = self._safe_output_text(interim_resp)

        # ===== Finalisasi oleh Actor =====
        final_msgs = [
            {"role": "system", "content": actor_instruction},
            {"role": "user", "content": f"User Memory Context: {system_memory}"},
            {"role": "user", "content": f"Permintaan awal: {prompt}"},
            {"role": "user", "content": f"Output interim: {interim_result}"},
            {
                "role": "user",
                "content": f"Kritik/Refleksi: {critique_text}\n\nSusun jawaban FINAL yang rapi dan actionable.",
            },
        ]
        final_resp = await self._call_llm(messages=final_msgs, tools=tools)
        final_text = self._safe_output_text(final_resp)
        logger.info("Final length=%d chars", len(final_text))

        return final_text
