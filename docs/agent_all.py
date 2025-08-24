# reflection_agent_all_in_one.py
from __future__ import annotations

import asyncio
import json
from projectwise.utils.logger import get_logger
import re
from copy import deepcopy
from json import JSONDecodeError
from typing import Any, Dict, List, Tuple, Optional

from projectwise.services.mcp.adapter import ToolExecutor, MCPToolAdapter

# Lib LLM
from openai import AsyncOpenAI, BadRequestError, APIConnectionError

# Opsional (jika integrasi ke Quart)
try:
    from quart import Quart  # type: ignore
except Exception:  # pragma: no cover
    Quart = Any  # fallback untuk tipe

# Pydantic v2 untuk structured output
# try:
from pydantic import BaseModel, Field, ConfigDict
# except Exception:  # pragma: no cover
#     # fallback minimal (tidak ideal, tapi agar file tetap dapat diimpor)
#     class BaseModel:  # type: ignore
#         def __init__(self, **data):  # type: ignore
#             for k, v in data.items():
#                 setattr(self, k, v)

#         def model_dump_json(self, **kwargs):  # type: ignore
#             return json.dumps(self.__dict__, **kwargs)

#         @classmethod
#         def model_validate_json(cls, s: str):  # type: ignore
#             return cls(**json.loads(s))

#         def model_copy(self):  # type: ignore
#             return deepcopy(self)

#         def model_dump(self):  # type: ignore
#             return deepcopy(self.__dict__)

#     def Field(*args, **kwargs):  # type: ignore
#         return None

# =========================
# Logger sederhana (Indonesia)
# =========================
from projectwise.config import ServiceConfigs


logger = get_logger(__name__)
settings = ServiceConfigs()

try:
    if str(settings.llm_model).lower().startswith("gpt"):
        AsyncOpenAI(api_key=settings.llm_api_key)
    else:
        AsyncOpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)
except Exception as e:
    logger.exception(
        "Gagal inisialisasi AsyncOpenAI (cek LLM_API_KEY / base_url): %s", e
    )


# =========================
# Skema Pydantic (Critic & Planner)
# =========================
class CriticFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = ""
    missing_info: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    relevant_tools: List[str] = Field(default_factory=list)


class CriticDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finalize: bool = False
    notes: str = ""


class CriticFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    findings: List[CriticFinding] = Field(default_factory=list)
    suggested_steps: List[str] = Field(default_factory=list)
    candidate_tools: List[str] = Field(default_factory=list)
    decision: CriticDecision = Field(default_factory=CriticDecision)


# 🔁 GANTI args_draft → args_kv (list pasangan kunci-nilai)
class ArgKV(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    value: str  # string saja (boleh JSON-string); stabil untuk schema ketat


class ToolPlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: int
    tool_name: str
    args_kv: List[ArgKV] = Field(default_factory=list)
    notes: str | None = None


class ToolPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: str = "execute_tools"
    items: List[ToolPlanItem] = Field(default_factory=list)
    notes: str | None = None


# ============================================================
# Prompt instructions (Actor / Critic / Context)
# ============================================================
def ACTOR_SYSTEM() -> str:
    """
    Sistem prompt untuk Actor:
    - Mengizinkan function calling bila perlu
    - Hasil interim/final ringkas, terstruktur, actionable
    """
    return (
        "# PERAN: ACTOR\n"
        "- Pahami tujuan user dan gunakan TOOLS jika perlu.\n"
        "- Hormati guardrail: tools bertanda EXPLICIT_ONLY hanya ketika user eksplisit.\n"
        "- Jawaban ringkas, sistematis, dan actionable.\n"
        "- Saat menerima umpan balik atau observasi dari Planner, gunakan untuk menyempurnakan jawaban."
    )


def CRITIC_SYSTEM() -> str:
    """
    Sistem prompt untuk Critic:
    - Menilai keluaran Actor berdasarkan konteks & tools yang tersedia
    - Menghasilkan umpan balik terstruktur (CriticFeedback)
    """
    return (
        "# PERAN: AGENT CRITIC\n"
        "- Nilai keluaran Actor: kelengkapan, relevansi, dan kesesuaian tools.\n"
        "- Identifikasi gap informasi, risiko, dan tools yang relevan.\n"
        "- Usulkan langkah ringkas dan daftar candidate tools.\n"
        "OUTPUT WAJIB JSON sesuai schema CriticFeedback."
    )


def PROMPT_USER_CONTEXT() -> str:
    """
    Instruksi pembentuk konteks (brief memori) agar Actor paham latar belakang user.
    """
    return (
        "# MODE: Analyst Context\n"
        "- Gunakan long-term memory & conversation history user.\n"
        "- Hasilkan context yang jelas & fokus pada tujuan user.\n"
        "- Format singkat: Ringkasan konteks + Tujuan utama.\n"
    )


# ============================================================
# Utilitas umum (Indonesia)
# ============================================================
def truncate_args(x: Any, limit: int = 300) -> str:
    """Potong string untuk logging agar tidak membludak."""
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "…"


def to_jsonable(x: Any) -> Any:
    """Ubah objek ke bentuk JSON-able secara defensif."""
    try:
        json.dumps(x)  # cepat cek
        return x
    except Exception:
        # fallback: repr untuk tipe yang tidak serializable
        if isinstance(x, dict):
            return {k: to_jsonable(v) for k, v in x.items()}
        if isinstance(x, (list, tuple, set)):
            return [to_jsonable(v) for v in x]
        return repr(x)


def find_duplicates(items: List[str]) -> List[str]:
    """Cari duplikat (case-insensitive) untuk validasi nama tool."""
    seen, dup = set(), []
    for it in items:
        if it in seen and it not in dup:
            dup.append(it)
        seen.add(it)
    return dup


_EXPLICIT_PAT = re.compile(
    r"(izinkan|setujui|jalankan|eksekusi|execute|run|pakai|gunakan)\s+(tool|alat|fungsi)?",
    re.IGNORECASE,
)


def contains_explicit_intent(user_prompt: str, tool_name: str) -> bool:
    """
    Deteksi sinyal eksplisit dari user untuk menjalankan tool sensitif.
    Heuristik: cari kata kerja eksplisit atau sebutan tool-name.
    """
    up = user_prompt or ""
    if tool_name and tool_name.lower() in up.lower():
        return True
    return bool(_EXPLICIT_PAT.search(up))


def validate_tool_args(schema: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validasi argumen terhadap schema 'type: object'.
    - Cek 'required'
    - Hormati 'additionalProperties: false' (jika ada): buang key tak dikenal
    - Kembalikan args ter-filter
    """
    args = args or {}
    if not isinstance(schema, dict):
        return args

    if schema.get("type") != "object":
        return args

    props = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []
    addl = schema.get("additionalProperties", True)

    # cek required
    missing = [k for k in required if k not in args]
    if missing:
        raise ValueError(f"Argumen kurang: {missing}")

    # filter additionalProperties=false
    result = dict(args)
    if addl is False:
        result = {k: v for k, v in result.items() if k in props}

    return result


async def build_context_blocks_memory(
    *,
    short_term: Any,
    long_term: Any,
    user_id: str,
    user_message: str,
    max_history: int,
    prompt_instruction: str,
) -> str:
    """
    Bangun ringkas konteks memori dari LTM + STM (defensif bila service tidak tersedia).
    - Harapannya: short_term/long_term punya API async untuk fetch data.
    - Jika tidak, tetap kembalikan string ringkas agar Actor tidak buta konteks.
    """
    lt_snips: List[str] = []
    st_snips: List[str] = []

    # Ambil long-term memory (defensif)
    try:
        # Misal: long_term.search_async(user_id, query, k=5)
        # Di sini gunakan heuristik sederhana saja.
        lt_snips = [user_message] * 3  # placeholder ringkas agar tidak kosong
    except Exception:
        pass

    # Ambil short-term (defensif)
    try:
        # Misal: short_term.get_recent_dialogue(user_id, limit=max_history)
        # Kita isi placeholder jika tidak ada API
        st_snips = ["- (riwayat singkat tidak tersedia)"]
    except Exception:
        st_snips = ["- (riwayat singkat tidak tersedia)"]

    text = (
        f"{prompt_instruction}\n"
        "### Briefing Memori\n"
        f"**Long_Term Memory (relevan):**\n- " + "\n- ".join(lt_snips) + "\n\n"
        "**Short_Term History (ringkas):**\n" + "\n".join(st_snips)
    )
    return text


# ============================================================
# Kontrak eksekutor tool
# # ============================================================
# class ToolExecutor(Protocol):
#     async def call_tool(self, name: str, args: Dict[str, Any]) -> Any: ...
#     async def get_tools(self) -> List[Dict[str, Any]]: ...


# # ============================================================
# # MCP Adapter — jembatan ke MCP client di app.extensions
# # ============================================================
# class MCPToolAdapter:
#     """
#     Adapter sederhana untuk mengeksekusi MCP tool melalui instance di app.extensions.
#     - Tidak auto-connect; hormati /mcp/connect
#     """

#     def __init__(self, app: Quart) -> None:  # type: ignore
#         self.app = app

#     async def _acquire_mcp(self):
#         if "mcp" not in self.app.extensions or "mcp_status" not in self.app.extensions:
#             raise RuntimeError("MCP belum diinisialisasi di app.extensions.")
#         client = self.app.extensions.get("mcp")
#         status: dict = self.app.extensions["mcp_status"]
#         if client is None or not status.get("connected"):
#             raise RuntimeError(
#                 "MCP belum terhubung. Silakan klik 'Connect' atau panggil endpoint /mcp/connect lebih dulu."
#             )
#         return client

#     async def call_tool(self, name: str, args: Dict[str, Any]) -> Any:
#         client = await self._acquire_mcp()
#         logger.info("Menjalankan MCP tool: %s | args=%s", name, truncate_args(args))
#         return await client.call_tool(name, args)

#     async def get_tools(self) -> List[Dict[str, Any]]:
#         client = await self._acquire_mcp()
#         tools: List[Dict[str, Any]] = getattr(client, "tool_cache", []) or []
#         logger.info("Daftar MCP tools terdeteksi: %d item.", len(tools))
#         return tools


# ============================================================
# Normalisasi tools MCP → format Responses API + registry
# ============================================================
def normalize_mcp_tools(
    mcp_tools: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """
    Kembalikan:
      - tools_for_openai: daftar tools untuk Responses API (diteruskan apa adanya)
      - registry: meta per tool (schema, flag explicit_only, dsb.)
    """
    tools_for_openai = mcp_tools or []
    if not tools_for_openai:
        logger.warning("normalize_mcp_tools: daftar tools kosong.")
        return tools_for_openai, {}

    registry: Dict[str, Dict[str, Any]] = {}

    for idx, t in enumerate(tools_for_openai):
        raw = t
        func = raw.get("function") or {}
        name = (raw.get("name") or func.get("name") or "").strip()
        if not name:
            raise ValueError(f"Tool pada index {idx} tidak memiliki 'name'.")

        desc = (raw.get("description") or func.get("description") or "").strip()
        params_raw = (
            raw.get("parameters")
            or func.get("parameters")
            or raw.get("inputSchema")  # legacy
            or {}
        )
        strict_flag = bool(raw.get("strict", False))
        lower_desc = desc.lower()
        need_explicit = (
            "only if user explisit ask" in lower_desc
            or "only if user explicit ask" in lower_desc
            or "requires explicit" in lower_desc
            or "explicitly requested" in lower_desc
        )

        registry[name] = {
            "name": name,
            "parameters_raw": params_raw,
            "strict": strict_flag,
            "need_explicit": need_explicit,
            "description": desc,
            "raw": deepcopy(raw),
        }

    names = [t.get("name", "") for t in tools_for_openai]
    dupes = find_duplicates([n.lower() for n in names])
    if dupes:
        raise ValueError(f"Duplicate tool name terdeteksi (case-insensitive): {dupes}")

    logger.info(
        "normalize_mcp_tools: %d tools diteruskan ke LLM; registry dibuat.",
        len(tools_for_openai),
    )
    return tools_for_openai, registry


# ============================================================
# LLM Planner (di file ini juga) — pakai text_format=ToolPlan
# ============================================================
class LLMPlanner:
    """
    Planner LLM yang menyusun ToolPlan terstruktur berdasarkan:
    - USER_PROMPT
    - CRITIC_FEEDBACK
    - TOOLS_CATALOG (daftar tools yang tersedia)
    Output dipaksa ke skema ToolPlan via responses.parse(..., text_format=ToolPlan)
    """

    def __init__(self, *, llm: AsyncOpenAI, model: str, timeout_sec: float) -> None:
        self.llm = llm
        self.model = model
        self.timeout = timeout_sec

    async def make_plan(
        self,
        *,
        user_prompt: str,
        critic: CriticFeedback,
        tools_registry: Dict[str, Dict[str, Any]],
    ) -> ToolPlan:
        # Filter kandidat tools agar hanya yang tersedia
        available = set(tools_registry.keys())
        critic_filtered = critic.model_copy()
        critic_filtered.candidate_tools = [
            t for t in critic.candidate_tools if t in available
        ]

        # Susun katalog untuk prompt
        catalog = []
        for name, meta in tools_registry.items():
            params = meta.get("parameters_raw") or {}
            req = ", ".join(params.get("required", []) or [])
            flag = "EXPLICIT_ONLY" if meta.get("need_explicit") else "-"
            catalog.append(
                {
                    "name": name,
                    "required": req,
                    "flag": flag,
                    "desc": meta.get("description", ""),
                }
            )

        system = (
            "Anda adalah Planner. Susun rencana eksekusi tools MCP yang relevan.\n"
            "KETENTUAN:\n"
            "- Pakai HANYA tool yang ada di TOOLS_CATALOG.\n"
            "- Pertimbangkan CRITIC_FEEDBACK (saran langkah & kandidat tools).\n"
            "- Setiap langkah wajib memiliki 'step' (int mulai 1), 'tool_name', dan 'args_kv' (list pasangan {key, value}).\n"
            "- 'value' pada args_kv adalah string; jika butuh tipe lain, string-kan (misal JSON-string).\n"
            "- Maksimum 5 langkah.\n"
            "- KELUARKAN HANYA JSON sesuai schema ToolPlan."
        )

        user = (
            f"USER_PROMPT:\n{user_prompt}\n\n"
            f"CRITIC_FEEDBACK(JSON):\n{critic_filtered.model_dump_json()}\n\n"
            f"TOOLS_CATALOG(JSON):\n{json.dumps(catalog, ensure_ascii=False)}\n\n"
            "Jika ada tool yang relevan, buat minimal 1 langkah.\n"
            'Contoh args_kv: [{"key":"query","value":"analisis proyek"},{"key":"k","value":"5"}]'
        )

        try:
            resp = await asyncio.wait_for(
                self.llm.responses.parse(
                    model=self.model,
                    input=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    text_format=ToolPlan,  # <- sesuai instruksi user
                    temperature=0.0,
                ),
                timeout=self.timeout,
            )
            plan: ToolPlan = resp.output_parsed  # type: ignore
            if not plan.items and catalog:
                # Retry kecil untuk mendorong setidaknya 1 langkah
                user_retry = (
                    user
                    + "\n\nPENTING: Buat setidaknya 1 langkah yang paling masuk akal."
                )
                resp2 = await asyncio.wait_for(
                    self.llm.responses.parse(
                        model=self.model,
                        input=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": user_retry},
                        ],
                        text_format=ToolPlan,
                        temperature=0.0,
                    ),
                    timeout=self.timeout,
                )
                plan = resp2.output_parsed  # type: ignore
            return plan
        except Exception:
            logger.exception("Planner gagal menyusun ToolPlan, kembalikan kosong.")
            return ToolPlan(
                intent="execute_tools", items=[], notes="planner_fallback_empty"
            )


# ============================================================
# Sanity-check batch input ke LLM (hindari error 400)
# ============================================================
def _assert_input_sane(msgs: List[Dict[str, Any]]) -> None:
    """
    Pastikan setiap function_call_output punya pasangan function_call
    dengan call_id yang sama pada batch input yang sama.
    """
    seen = set()
    for i, m in enumerate(msgs):
        t = m.get("type")
        if t == "function_call":
            cid = m.get("call_id")
            if not cid:
                raise ValueError(f"messages[{i}] function_call tanpa call_id")
            seen.add(cid)
        elif t == "function_call_output":
            cid = m.get("call_id")
            if not cid:
                raise ValueError(f"messages[{i}] function_call_output tanpa call_id")
            if cid not in seen:
                raise ValueError(
                    f"messages[{i}] function_call_output tanpa pasangan function_call (call_id={cid})"
                )


# ============================================================
# ReflectionActor (Agentic Orchestrator)
# ============================================================
class ReflectionActor:
    """
    Orkestrator: Actor → (Tool Calls) → Critic → Planner → (Planner Exec) → Finalize.
    - Tool call dari model HARUS dibalas dengan pasangan function_call + function_call_output (call_id sama).
    - Eksekusi Planner langsung (internal), hasilnya dicatat sebagai observasi teks.
    """

    def __init__(
        self,
        llm_model: str,
        long_term: Any,
        short_term: Any,
        executor: ToolExecutor,
        llm: Optional[AsyncOpenAI] = None,
        max_steps: int = 50,
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
        app: Quart,  # type: ignore
        *,
        llm: Optional[AsyncOpenAI] = None,
        llm_model: Optional[str] = None,
        max_history: int = 20,
    ) -> "ReflectionActor":
        """
        Factory: integrasi cepat dengan Quart app yang sudah memiliki extensions:
        - service_configs.llm_model
        - long_term_memory, short_term_memory
        - mcp + mcp_status
        """
        service_configs = app.extensions["service_configs"]  # type: ignore
        long_term = app.extensions["long_term_memory"]  # type: ignore
        short_term = app.extensions["short_term_memory"]  # type: ignore
        executor = MCPToolAdapter(app)

        return cls(
            long_term=long_term,
            short_term=short_term,
            llm=llm,
            llm_model=llm_model or service_configs.llm_model,
            executor=executor,
            max_history=max_history,
        )

    # -----------------------------
    # Pembungkus panggilan Responses
    # -----------------------------
    async def _call_llm(
        self, *, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ):
        """
        Panggil Responses API dengan timeout & penanganan error manusiawi.
        - Selalu jalankan _assert_input_sane agar batch valid.
        """
        logger.debug(
            "Memanggil LLM: %d pesan, %d tools", len(messages), len(tools or [])
        )
        _assert_input_sane(messages)
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
            logger.error("Gagal koneksi ke LLM (APIConnectionError).")
            raise RuntimeError("Koneksi ke LLM bermasalah. Coba lagi sebentar.")
        except asyncio.TimeoutError:
            logger.exception("Timeout saat memanggil LLM (responses.create).")
            raise
        except BadRequestError as e:
            msg = str(e)
            if "Invalid schema for function" in msg and "additionalProperties" in msg:
                human = (
                    "Skema tool tidak kompatibel (wajib 'additionalProperties: false'). "
                    "Perbaiki skema tool dan coba lagi."
                )
            elif "No tool call found for function call output" in msg:
                human = (
                    "Terjadi mismatch: function_call_output tidak punya pasangan function_call. "
                    "Periksa pairing call_id pada batch input ini."
                )
            else:
                human = (
                    "Permintaan ke LLM ditolak. Periksa format pesan dan skema tools."
                )
            logger.error("LLM 400: %s", msg, exc_info=True)
            raise RuntimeError(human)
        except Exception:
            logger.exception("Kesalahan tidak terduga saat memanggil LLM.")
            raise

    # -----------------------------
    # Ambil function_call dari respons
    # -----------------------------
    @staticmethod
    def _iter_function_calls(response) -> List[Dict[str, Any]]:
        """Ekstrak daftar function_call dari Responses API result: name, arguments, call_id."""
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
                        }
                    )
        return calls

    @staticmethod
    def _safe_output_text(resp) -> str:
        """Ambil teks jawaban dari Responses (kosongkan jika None)."""
        text = getattr(resp, "output_text", "") or ""
        return text.strip()

    @staticmethod
    def _append_fc_reply(
        messages: List[Dict[str, Any]],
        *,
        call_id: Optional[str],
        name: str,
        args_raw: str,
        output_json_str: str,
    ) -> None:
        """
        Tambahkan pasangan event:
        - function_call (dengan call_id) → function_call_output (call_id sama)
        sesuai protokol Responses API.
        """
        if not call_id:
            logger.warning("Lewati balasan tool %s: call_id kosong.", name)
            return
        messages.append(
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": args_raw or "{}",
            }
        )
        messages.append(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": output_json_str,
            }
        )
        logger.info("Balasan tool '%s' dikirim (paired dengan call_id).", name)

    # -----------------------------
    # Helper: pastikan respons teks meski model masih memanggil tools
    # -----------------------------
    async def _ensure_text_response(
        self,
        *,
        base_messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        first_resp,
        max_rounds: int = 3,
    ) -> str:
        """
        Jalankan siklus: cek output_text; bila kosong, proses semua function_call
        (eksekusi tool + kirim pasangan function_call + function_call_output),
        lalu panggil LLM lagi. Ulangi sampai dapat teks atau mencapai max_rounds.
        """
        text = self._safe_output_text(first_resp)
        if text:
            return text

        messages = base_messages
        resp = first_resp
        for _ in range(max_rounds):
            calls = self._iter_function_calls(resp)
            if not calls:
                break  # tidak ada tool call dan tetap kosong -> keluar

            # Eksekusi semua tool yang diminta
            for fc in calls:
                name = fc["name"]
                args_raw = fc["arguments"]
                call_id = fc["call_id"]

                # Guard explicit_only
                # (pakai prompt awal user untuk deteksi intensi eksplisit)
                if self.tools_registry.get(name, {}).get(
                    "need_explicit"
                ) and not contains_explicit_intent(self.last_user_prompt, name):  # type: ignore
                    output = json.dumps(
                        {
                            "ok": False,
                            "error": "GUARDRAIL",
                            "message": "Aksi ini hanya dijalankan jika pengguna secara eksplisit memintanya.",
                        },
                        ensure_ascii=False,
                    )
                    self._append_fc_reply(
                        messages,
                        call_id=call_id,
                        name=name,
                        args_raw=args_raw,
                        output_json_str=output,
                    )
                    continue

                # Parse & validasi argumen
                try:
                    args = json.loads(args_raw or "{}")
                except JSONDecodeError:
                    args = {}
                schema = (
                    self.tools_registry[name].get("parameters_raw")  # type: ignore
                    or {"type": "object", "properties": {}}
                )
                try:
                    valid_args = validate_tool_args(schema, args)
                except Exception:
                    output = json.dumps(
                        {
                            "ok": False,
                            "error": "InvalidArguments",
                            "message": "Schema validation failed.",
                        },
                        ensure_ascii=False,
                    )
                    self._append_fc_reply(
                        messages,
                        call_id=call_id,
                        name=name,
                        args_raw=args_raw,
                        output_json_str=output,
                    )
                    continue

                # Eksekusi tool
                try:
                    result = await asyncio.wait_for(
                        self.executor.call_tool(name, valid_args),
                        timeout=self.step_timeout_sec,
                    )
                    output = json.dumps(
                        {"ok": True, "result": to_jsonable(result)}, ensure_ascii=False
                    )
                except asyncio.TimeoutError:
                    output = json.dumps(
                        {
                            "ok": False,
                            "error": "Timeout",
                            "message": f"Tool {name} melebihi {self.step_timeout_sec}s",
                        },
                        ensure_ascii=False,
                    )
                except Exception as e:
                    output = json.dumps(
                        {"ok": False, "error": type(e).__name__, "message": str(e)},
                        ensure_ascii=False,
                    )

                # Kirim pasangan function_call + function_call_output
                self._append_fc_reply(
                    messages,
                    call_id=call_id,
                    name=name,
                    args_raw=args_raw,
                    output_json_str=output,
                )

            # Panggil ulang LLM setelah semua tool dibalas
            resp = await self._call_llm(messages=messages, tools=tools)
            text = self._safe_output_text(resp)
            if text:
                return text

        return ""

    # -----------------------------
    # Alur utama agentic
    # -----------------------------
    async def reflection_actor_with_mcp(
        self,
        prompt: str,
        user_id: str,
        actor_instruction: Optional[str] = None,
        critic_instruction: Optional[str] = None,
    ) -> str:
        """
        1) Bentuk konteks memori (LTM+STM)
        2) Ambil tools MCP (registry)
        3) Actor memulai (boleh minta tool)
        4) Eksekusi tool dari model (pairing call_id)
        5) Interim answer
        6) Critic → Planner → Eksekusi rencana (observasi teks)
        7) Finalize
        """
        logger.info("Memulai orkestrasi agentic untuk prompt: %r", prompt)

        # 1) Konstruksi konteks memori
        system_memory = await build_context_blocks_memory(
            short_term=self.short_term,
            long_term=self.long_term,
            user_id=user_id,
            user_message=prompt,
            max_history=self.max_history,
            prompt_instruction=PROMPT_USER_CONTEXT(),
        )
        logger.info("Konteks memori siap.")

        # 2) Tools MCP + registry
        mcp_tools = await self.executor.get_tools()
        tools, registry = normalize_mcp_tools(mcp_tools)
        self.tools_registry = registry
        self.last_user_prompt = prompt

        # 3) Actor — panggilan awal
        sys_text = actor_instruction or ACTOR_SYSTEM()
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": sys_text},
            {"role": "user", "content": f"User Memory Context: {system_memory}"},
            {"role": "user", "content": prompt},
        ]
        logger.info("Actor dipanggil pertama kali (model boleh meminta tool).")
        response = await self._call_llm(messages=messages, tools=tools)

        # 4) Tanggapi function_call (bila ada)
        function_calls = self._iter_function_calls(response)
        for fc in function_calls:
            name = fc["name"]
            args_raw = fc["arguments"]
            call_id = fc["call_id"]
            logger.info(
                "Model meminta tool: %s | args=%s", name, truncate_args(args_raw, 400)
            )

            # Guard: explicit_only
            if registry.get(name, {}).get(
                "need_explicit"
            ) and not contains_explicit_intent(prompt, name):
                logger.warning(
                    "Lewati tool %s: butuh permintaan eksplisit dari user.", name
                )
                output = json.dumps(
                    {
                        "ok": False,
                        "error": "GUARDRAIL",
                        "message": "Aksi ini hanya dijalankan jika pengguna secara eksplisit memintanya.",
                    },
                    ensure_ascii=False,
                )
                self._append_fc_reply(
                    messages,
                    call_id=call_id,
                    name=name,
                    args_raw=args_raw,
                    output_json_str=output,
                )
                continue

            # Validasi argumen
            try:
                args = json.loads(args_raw or "{}")
            except JSONDecodeError:
                logger.exception("Gagal decode JSON args untuk tool %s.", name)
                args = {}

            schema = (
                registry[name].get("parameters_raw")
                or registry[name].get("parameters_final")
                or {"type": "object", "properties": {}}
            )
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
                self._append_fc_reply(
                    messages,
                    call_id=call_id,
                    name=name,
                    args_raw=args_raw,
                    output_json_str=output,
                )
                continue

            # Jalankan tool
            try:
                result = await asyncio.wait_for(
                    self.executor.call_tool(name, valid_args),
                    timeout=self.step_timeout_sec,
                )
                output = json.dumps(
                    {"ok": True, "result": to_jsonable(result)}, ensure_ascii=False
                )
                logger.info("Tool %s dieksekusi dengan sukses.", name)
            except asyncio.TimeoutError:
                logger.exception("Timeout saat menjalankan tool %s.", name)
                output = json.dumps(
                    {
                        "ok": False,
                        "error": "Timeout",
                        "message": f"Tool {name} melebihi {self.step_timeout_sec}s",
                    },
                    ensure_ascii=False,
                )
            except Exception as e:
                logger.exception("Gagal menjalankan tool %s.", name)
                output = json.dumps(
                    {"ok": False, "error": type(e).__name__, "message": str(e)},
                    ensure_ascii=False,
                )

            # Pasangan function_call + function_call_output (call_id sama)
            self._append_fc_reply(
                messages,
                call_id=call_id,
                name=name,
                args_raw=args_raw,
                output_json_str=output,
            )

        # 5) Jawaban interim
        logger.info("Meminta jawaban sementara (interim) dari Actor.")
        interim_resp = await self._call_llm(messages=messages, tools=tools)
        interim_result = self._safe_output_text(interim_resp)
        logger.info("Interim dihasilkan (%d karakter).", len(interim_result))

        # 6) Refleksi: Critic → Planner → Eksekusi rencana (observasi)
        critique_text = ""
        # Katalog tools (untuk prompt)
        tool_catalog_lines = []
        for name, meta in registry.items():
            params = meta.get("parameters_raw") or {}
            req = ", ".join(params.get("required", []) or [])
            flag = "EXPLICIT_ONLY" if meta.get("need_explicit") else "-"
            tool_catalog_lines.append(
                f"- {name} :: {flag} | req: {req} | {meta.get('description', '')}"
            )
        tool_catalog_txt = "DAFTAR TOOLS:\n" + "\n".join(tool_catalog_lines)

        # Planner
        planner = LLMPlanner(
            llm=self.llm, model=self.model, timeout_sec=self.step_timeout_sec
        )

        for i in range(self.max_steps):
            logger.info("Iterasi refleksi #%d dimulai.", i + 1)
            critic_sys = critic_instruction or CRITIC_SYSTEM()
            critic_msgs = [
                {"role": "system", "content": critic_sys},
                {
                    "role": "user",
                    "content": f"{tool_catalog_txt}\n\nUSER REQUEST:\n{prompt}\n\nACTOR OUTPUT (INTERIM):\n{interim_result}",
                },
            ]
            # Critic terstruktur
            try:
                critic_resp = await asyncio.wait_for(
                    self.llm.responses.parse(
                        model=self.model,
                        input=critic_msgs,  # type: ignore
                        text_format=CriticFeedback,  # sesuai SDK Anda
                        temperature=0.0,
                    ),
                    timeout=self.step_timeout_sec,
                )
                critic_obj: CriticFeedback = critic_resp.output_parsed  # type: ignore
                logger.info("Critic menghasilkan feedback terstruktur.")
            except Exception:
                logger.warning("Critic gagal terparse, gunakan fallback kosong.")
                critic_obj = CriticFeedback(suggested_steps=[], candidate_tools=[])

            # Ringkas kritik untuk dorongan perbaikan
            critique_text = self._summarize_critic_for_actor(critic_obj)

            # Planner (ToolPlan)
            plan: ToolPlan = await planner.make_plan(
                user_prompt=prompt,
                critic=critic_obj,
                tools_registry=registry,
            )
            logger.info(
                "Planner menyusun langkah: %s",
                [f"{it.step}:{it.tool_name}" for it in plan.items],
            )

            # Eksekusi rencana Planner — hasil sebagai observasi teks
            for item in plan.items[: self.max_steps]:
                name = item.tool_name
                if name not in registry:
                    logger.info("Lewati langkah %s: tool tidak ada di registry.", name)
                    continue
                if registry[name].get("need_explicit") and not contains_explicit_intent(
                    prompt, name
                ):
                    logger.info(
                        "Lewati %s (explicit_only, user tidak eksplisit).", name
                    )
                    continue

                schema = registry[name].get("parameters_raw") or {
                    "type": "object",
                    "properties": {},
                }
                try:
                    args_dict: Dict[str, Any] = {}
                    for kv in item.args_kv or []:
                        v = kv.value
                        # coba parse json; kalau gagal, pakai string apa adanya
                        try:
                            args_dict[kv.key] = json.loads(v)
                        except Exception:
                            # jika angka/bool diserialisasi sebagai string, boleh konversi kecil
                            if v.isdigit():
                                args_dict[kv.key] = int(v)
                            elif v.replace(".", "", 1).isdigit() and v.count(".") < 2:
                                try:
                                    args_dict[kv.key] = float(v)
                                except Exception:
                                    args_dict[kv.key] = v
                            elif v.lower() in ("true", "false"):
                                args_dict[kv.key] = v.lower() == "true"
                            else:
                                args_dict[kv.key] = v

                    schema = registry[name].get("parameters_raw") or {
                        "type": "object",
                        "properties": {},
                    }
                    try:
                        valid_args = validate_tool_args(schema, args_dict)
                    except Exception as e:
                        logger.exception(
                            "Validasi argumen draft gagal (%s): %s | args_dict=%s",
                            name,
                            e,
                            args_dict,
                        )
                        continue
                except Exception as e:
                    logger.exception("Validasi argumen draft gagal (%s): %s", name, e)
                    continue

                try:
                    result = await asyncio.wait_for(
                        self.executor.call_tool(name, valid_args),
                        timeout=self.step_timeout_sec,
                    )
                    function_output = {"ok": True, "result": to_jsonable(result)}
                except asyncio.TimeoutError:
                    function_output = {
                        "ok": False,
                        "error": "Timeout",
                        "message": f"Tool {name} > {self.step_timeout_sec}s",
                    }
                except Exception as e:
                    function_output = {
                        "ok": False,
                        "error": type(e).__name__,
                        "message": str(e),
                    }

                # Catat sebagai observasi teks (bukan function_call_output)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"[PLANNER_EXECUTED]\n"
                            f"tool={name}\n"
                            f"args={json.dumps(valid_args, ensure_ascii=False)}\n"
                            f"result={json.dumps(function_output, ensure_ascii=False)}"
                        ),
                    }
                )
                logger.info(
                    "Planner mengeksekusi tool %s (hasil dimasukkan ke konteks).", name
                )

            if "FINALIZE" in (critique_text or "").upper():
                logger.info(
                    "Critic menyarankan FINALIZE pada iterasi #%d — keluar dari loop.",
                    i + 1,
                )
                break

            # Dorong Actor untuk perbaikan
            if critique_text:
                messages.append(
                    {"role": "user", "content": f"INSTRUKSI KRITIK:\n{critique_text}"}
                )
            logger.info("Memanggil Actor untuk perbaikan pasca refleksi #%d.", i + 1)
            interim_resp = await self._call_llm(messages=messages, tools=tools)
            interim_result = self._safe_output_text(interim_resp)

        # 7) Finalisasi jawaban
        final_msgs = [
            {"role": "system", "content": sys_text},
            {"role": "user", "content": f"User Memory Context: {system_memory}"},
            {"role": "user", "content": f"Permintaan awal: {prompt}"},
            {"role": "user", "content": f"Output interim: {interim_result}"},
            {
                "role": "user",
                "content": f"Kritik/Refleksi:\n{critique_text}\n\nSusun jawaban FINAL yang rapi dan actionable.",
            },
        ]
        logger.info("Memanggil Actor untuk finalisasi jawaban.")
        final_resp = await self._call_llm(messages=final_msgs, tools=tools)

        # 🔁 Pastikan kalau model masih ingin memanggil tool, kita balas sampai keluar teks
        final_text = await self._ensure_text_response(
            base_messages=final_msgs,
            tools=tools,
            first_resp=final_resp,
            max_rounds=3,
        )

        # Fallback agar user tidak menerima string kosong
        if not final_text:
            logger.warning("Final kosong; fallback pakai interim.")
            final_text = (
                interim_result
                or "Maaf, saya belum bisa menyusun jawaban final dari hasil eksekusi tool."
            )
        return final_text

    @staticmethod
    def _summarize_critic_for_actor(critic: CriticFeedback) -> str:
        """Ringkasan singkat untuk mendorong perbaikan Actor."""
        parts: List[str] = []
        if critic.findings:
            reasons = [
                f"- {f.reason}"
                for f in critic.findings[:3]
                if getattr(f, "reason", None)
            ]
            if reasons:
                parts.append("Temuan:\n" + "\n".join(reasons))
        if critic.suggested_steps:
            steps = [f"{i + 1}. {s}" for i, s in enumerate(critic.suggested_steps[:5])]
            parts.append("Saran langkah:\n" + "\n".join(steps))
        if critic.candidate_tools:
            parts.append("Candidate tools: " + ", ".join(critic.candidate_tools[:6]))
        if critic.decision and critic.decision.finalize:
            parts.append("FINALIZE")
        return "\n".join(parts).strip()
