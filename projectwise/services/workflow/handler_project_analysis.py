# projectwise/services/workflow/handler_project_analysis.py
from __future__ import annotations

import asyncio
import json
import re
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
    Protocol,
    Mapping,
    Sequence,
    Literal,
)

from openai import AsyncOpenAI
from quart import Quart
from pydantic import BaseModel, Field

from projectwise.utils.logger import get_logger
from projectwise.services.memory.long_term_memory import Mem0Manager
from projectwise.services.memory.short_term_memory import ShortTermMemory
from projectwise.utils.llm_io import (
    to_jsonable,
    truncate_args,
)

logger = get_logger(__name__)

# =========================================================
# Konfigurasi tool MCP untuk PROJECT ANALYSIS
# =========================================================
TOOL_RETRIEVAL = "project_product_retrieval_informations"
TOOL_SUMMARY = "project_summaries_analysis"
TOOL_WEB = "websearch"
ALLOWED_TOOLS = {TOOL_RETRIEVAL, TOOL_SUMMARY, TOOL_WEB}
META_KEYS = ("filename", "pelanggan", "tahun", "project")

MAX_REFINEMENT_TRIES = 3  # perbaiki query R0 max n kali
MAX_TOOL_RETRIES = 3  # retry eksekusi tool plan
MAX_STEPS = 6  # iterasi actor-critic dinamis


# =========================================================
# Kontrak eksekutor tool
# =========================================================
class ToolExecutor(Protocol):
    async def call_tool(self, name: str, args: Dict[str, Any]) -> Any: ...
    async def get_tools(self) -> List[Dict[str, Any]]: ...


# =========================================================
# MCP Adapter
# =========================================================
class MCPToolAdapter:
    def __init__(self, app: Quart) -> None:
        self.app = app

    async def _acquire_mcp(self):
        if "mcp" not in self.app.extensions or "mcp_status" not in self.app.extensions:
            raise RuntimeError("MCP belum diinisialisasi di app.extensions.")
        client = self.app.extensions.get("mcp")
        status: dict = self.app.extensions["mcp_status"]
        if client is None or not status.get("connected"):
            raise RuntimeError(
                "MCP belum terhubung. Klik 'Connect' atau panggil /mcp/connect terlebih dahulu."
            )
        return client

    async def call_tool(self, name: str, args: Dict[str, Any]) -> Any:
        client = await self._acquire_mcp()
        logger.info("Eksekusi MCP tool: %s | args=%s", name, truncate_args(args))
        return await client.call_tool(name, args)

    async def get_tools(self) -> List[Dict[str, Any]]:
        client = await self._acquire_mcp()
        tools: List[Dict[str, Any]] = getattr(client, "tool_cache", []) or []
        logger.info("MCP tool_cache terdeteksi: %d tool.", len(tools))
        return tools


# =========================================================
# Prompts
# =========================================================
def ACTOR_SYSTEM() -> str:
    return (
        "ROLE: PROJECT ANALYST (ACTOR)\n"
        "Tugas: susun jawaban HANYA dari SCRATCHPAD (hasil tools). "
        "Jika informasi tidak ditemukan di scratchpad, tulis 'Tidak ditemukan' atau tempatkan di 'Celah Data'. "
        "DILARANG menambah fakta di luar scratchpad.\n\n"
        "FORMAT: Ringkasan; Temuan Penting; Risiko & Mitigasi; "
        "Celah Data (Butuh Konfirmasi); Detail Barang dan Spesifikasi;"
        "Detail Jasa dan Ruang lingkup;"
    )


def CRITIC_SYSTEM() -> str:
    return (
        "ROLE: PROJECT ANALYSIS CRITIC\n"
        "Nilai kelengkapan, relevansi, konsistensi terhadap PERMINTAAN USER. "
        'Keluarkan JSON: {"missing_info":[],"risks":[],"candidate_tools":[],"suggested_steps":[],"decision":{"finalize":bool}}.'
    )


def COMPRESSOR_SYSTEM() -> str:
    return "ROLE: SCRATCHPAD COMPRESSOR — Ringkas ≤1200 karakter, pertahankan entitas & angka penting."


def PLANNER_SYSTEM() -> str:
    return (
        "ROLE: TOOL PLANNER (STRICT)\n"
        f"Pilih SATU tool dari: {', '.join(sorted(ALLOWED_TOOLS))}; atau 'none' bila siap finalize.\n"
        "Berikan args minimal & tepat guna. Kembalikan JSON sesuai schema."
    )


def FINALIZE_ANSWER() -> str:
    return (
        "ROLE: PROJECT ANALYST (ACTOR)\n"
        "Jawab HANYA dari SCRATCHPAD. Jika tidak ada datanya, sebutkan 'Tidak ditemukan' atau minta konfirmasi."
    )


# =========================================================
# Schemas (Pydantic) — extra=forbid agar sesuai Responses API
# =========================================================
class Metadata(BaseModel):
    filename: Optional[str] = None
    pelanggan: Optional[str] = None
    tahun: Optional[str] = None
    project: Optional[str] = None
    model_config = {"extra": "forbid"}


class ToolArgs(BaseModel):
    # retrieval
    query: Optional[str] = None
    k: Optional[int] = None
    metadata: Optional[Metadata] = None
    # websearch
    q: Optional[str] = None
    num: Optional[int] = None
    # summary
    filename: Optional[str] = None
    pelanggan: Optional[str] = None
    tahun: Optional[str] = None
    project: Optional[str] = None
    model_config = {"extra": "forbid"}


class CriticDecision(BaseModel):
    finalize: bool = False


class CriticFeedback(BaseModel):
    missing_info: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    candidate_tools: List[str] = Field(default_factory=list)
    suggested_steps: List[str] = Field(default_factory=list)
    decision: CriticDecision = Field(default_factory=CriticDecision)


class ToolPlan(BaseModel):
    tool: Literal[
        "project_product_retrieval_informations",
        "project_summaries_analysis",
        "websearch",
        "none",
    ]
    args: ToolArgs = Field(default_factory=ToolArgs)
    reason: str = ""
    model_config = {"extra": "forbid"}


class QueryRefinement(BaseModel):
    query: str
    k: int = 6
    metadata: Optional[Metadata] = None
    model_config = {"extra": "forbid"}


class Candidate(BaseModel):
    filename: str
    pelanggan: str
    project: str
    tahun: str


class ChooseSummaryArgs(BaseModel):
    chosen_index: int
    reason: str
    args: Candidate


# ---------- Context Lock (ditentukan oleh LLM, bukan hardcode) ----------
class ContextLock(BaseModel):
    pelanggan: str
    tahun: Optional[str] = None
    # LLM menyediakan varian/alias nama pelanggan (contoh: "bank sumsel babel", "pt bank pembangunan ... bangka belitung")
    pelanggan_variants: List[str] = Field(default_factory=list)
    # Kata kunci proyek untuk membantu seleksi (contoh: ["firewall", "core"])
    project_keywords: List[str] = Field(default_factory=list)
    reason: str = ""


# =========================================================
# Utilitas
# =========================================================
def json_preview(obj: Any, limit: int = 1200) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)[:limit]
    except Exception:
        try:
            return json.dumps(to_jsonable(obj), ensure_ascii=False)[:limit]
        except Exception:
            try:
                return str(obj)[:limit]
            except Exception:
                return "<unserializable>"


def _slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("&", "dan")
    s = re.sub(r"[^a-z0-9\s_.-]", "", s)
    s = re.sub(r"\s+", "_", s)
    return s


def condense_query_for_search(text: str, max_len: int = 256) -> str:
    text = re.sub(r"(?s)### Briefing Memori.*?$", "", text)
    text = re.sub(r"(?s)\*\*Short_Term History.*?$", "", text)
    text = re.sub(r"`{3}.*?`{3}", "", text)
    text = re.sub(r"#{1,6}\s.*", "", text)
    text = re.sub(r"\(<Response \[.*?\]>,\s*\d{3}\)", "", text)
    lines = [i.strip() for i in text.splitlines() if i.strip()]
    seen, kept = set(), []
    for i in lines:
        if i not in seen:
            kept.append(i)
            seen.add(i)
    return " ".join(kept)[:max_len]


def _year_from_query(q: str) -> Optional[str]:
    m = re.search(r"\b(20\d{2})\b", q or "")
    return m.group(1) if m else None


def _contains(s: str, kw: str) -> bool:
    return kw.lower() in (s or "").lower()


def _as_list(rb: Any) -> List[Any]:
    if isinstance(rb, Mapping):
        for key in ("result", "results", "data"):
            if key in rb:
                container = rb[key]
                if isinstance(container, Sequence) and not isinstance(
                    container, (str, bytes, bytearray)
                ):
                    return list(container)
                return [container]
        return [rb]
    if isinstance(rb, Sequence) and not isinstance(rb, (str, bytes, bytearray)):
        return list(rb)
    return []


def _is_valid_tool_output(name: str, out: Any) -> bool:
    if out is None:
        return False
    if isinstance(out, Mapping) and (
        "error" in out or ("detail" in out and "Traceback" in str(out.get("detail")))
    ):
        return False

    # Quick string scan (untuk error pesan berformat teks)
    def _contains_error_text(items: List[Any]) -> bool:
        markers = (
            "Error executing tool",
            "Tidak ditemukan",
            "Folder Markdown tidak ditemukan",
        )
        for it in items:
            s = None
            if isinstance(it, Mapping):
                if isinstance(it.get("text"), str):
                    s = it["text"]
            elif hasattr(it, "text"):
                s = getattr(it, "text", None)
            elif isinstance(it, str):
                s = it
            if isinstance(s, str) and any(m in s for m in markers):
                return True
        return False

    if name == TOOL_RETRIEVAL:
        items = _as_list(out)
        if not items:
            return False
        # valid jika ada metadata yang masuk akal pada salah satu item
        for it in items:
            md = None
            if isinstance(it, Mapping) and isinstance(it.get("metadata"), Mapping):
                md = it["metadata"]
            else:
                text_str = (
                    it.get("text")
                    if isinstance(it, Mapping)
                    else getattr(it, "text", None)
                )
                if isinstance(text_str, str):
                    try:
                        parsed = json.loads(text_str)
                        md = (
                            parsed.get("metadata")
                            if isinstance(parsed, Mapping)
                            else None
                        )
                    except Exception:
                        md = None
            if isinstance(md, Mapping) and any(md.get(k) for k in META_KEYS):
                return True
        return False

    if name == TOOL_SUMMARY:
        items = _as_list(out)
        if _contains_error_text(items):
            return False
        return True

    # default
    return True


def _make_payload_for(tool: str, args: ToolArgs) -> Dict[str, Any]:
    d = args.model_dump(exclude_none=True)
    if tool == TOOL_RETRIEVAL:
        if "query" not in d and "q" in d:
            d["query"] = d.pop("q")
        return {k: v for k, v in d.items() if k in ("query", "k", "metadata")}
    if tool == TOOL_WEB:
        if "q" not in d and "query" in d:
            d["q"] = d.pop("query")
        return {k: v for k, v in d.items() if k in ("q", "num")}
    if tool == TOOL_SUMMARY:
        return {
            k: v
            for k, v in d.items()
            if k in ("filename", "pelanggan", "tahun", "project")
        }
    return {}


# =========================================================
# Ekstraksi kandidat metadata dari retrieval
# =========================================================
def _extract_candidates_from_retrieval(out: Any) -> List[Candidate]:
    cands: List[Candidate] = []
    items = _as_list(out) if out is not None else []
    for it in items:
        md: Optional[Mapping] = None
        if isinstance(it, Mapping) and isinstance(it.get("metadata"), Mapping):
            md = it["metadata"]
        else:
            text_str = (
                it.get("text") if isinstance(it, Mapping) else getattr(it, "text", None)
            )
            if isinstance(text_str, str):
                try:
                    parsed = json.loads(text_str)
                    if isinstance(parsed, Mapping) and isinstance(
                        parsed.get("metadata"), Mapping
                    ):
                        md = parsed["metadata"]
                except Exception:
                    md = None

        if isinstance(md, Mapping):
            f = str(md.get("filename") or md.get("source") or "").strip()
            pel = str(md.get("pelanggan") or "").strip()
            proj = str(md.get("project") or "").strip()
            th = str(md.get("tahun") or "").strip()
            if any([f, pel, proj, th]):
                cands.append(
                    Candidate(filename=f, pelanggan=pel, project=proj, tahun=th)
                )
    return cands


def _score_candidate(c: Candidate, q: str) -> int:
    score = 0
    y = _year_from_query(q)
    if y and c.tahun and c.tahun == y:
        score += 4
    if any(
        _contains(c.pelanggan, x)
        for x in ["sumsel", "babel", "bangka", "belitung", "bank sumsel"]
    ):
        score += 3
    if any(_contains(c.project, x) for x in ["firewall", "core"]):
        score += 2
    if any(
        _contains(c.filename, x) for x in ["firewall", "core", "bsb", "sumsel", "babel"]
    ):
        score += 1
    return score


def _shortlist_candidates(
    candidates: List[Candidate], q: str, k: int = 5
) -> List[Candidate]:
    dedup: Dict[Tuple[str, str, str, str], Candidate] = {}
    for c in candidates:
        key = (
            c.filename.lower(),
            c.pelanggan.lower(),
            c.project.lower(),
            c.tahun.lower(),
        )
        dedup[key] = c
    ranked = sorted(dedup.values(), key=lambda c: _score_candidate(c, q), reverse=True)
    return ranked[:k]


# =========================================================
# ProjectAnalysisActor (LLM-in-the-loop)
# =========================================================
class ProjectAnalysisActor:
    def __init__(
        self,
        *,
        llm_model: str,
        long_term: Mem0Manager,
        short_term: ShortTermMemory,
        executor: ToolExecutor,
        llm: Optional[AsyncOpenAI] = None,
        step_timeout_sec: float = 300.0,  # 5 menit
        max_history: int = 12,
    ) -> None:
        self.llm = llm or AsyncOpenAI()
        self.model = llm_model
        self.long_term = long_term
        self.short_term = short_term
        self.executor = executor
        self.step_timeout_sec = step_timeout_sec
        self.max_history = max_history
        # state
        self._ctx_lock: Optional[ContextLock] = None

    @classmethod
    def from_quart_app(
        cls,
        app: Quart,
        *,
        llm: Optional[AsyncOpenAI] = None,
        llm_model: Optional[str] = None,
        max_history: int = 12,
    ) -> "ProjectAnalysisActor":
        service_configs = app.extensions["service_configs"]  # type: ignore
        long_term: Mem0Manager = app.extensions["long_term_memory"]  # type: ignore
        short_term: ShortTermMemory = app.extensions["short_term_memory"]  # type: ignore
        executor = MCPToolAdapter(app)
        return cls(
            llm_model=llm_model or service_configs.llm_model,
            long_term=long_term,
            short_term=short_term,
            executor=executor,
            llm=llm,
            max_history=max_history,
        )

    # ---------------------------
    # LLM helpers
    # ---------------------------
    async def _llm_text(
        self, *, messages: List[Dict[str, Any]], temperature: float = 0.0
    ) -> str:
        try:
            resp = await asyncio.wait_for(
                self.llm.responses.create(
                    model=self.model,
                    input=messages,  # type: ignore
                    temperature=temperature,  # type: ignore
                ),  # type: ignore
                timeout=self.step_timeout_sec,
            )
            return (getattr(resp, "output_text", "") or "").strip()
        except Exception:
            logger.exception("LLM text call gagal.")
            return ""

    async def _llm_parse(
        self, *, messages: List[Dict[str, Any]], schema: Any
    ) -> Optional[Any]:
        try:
            resp = await asyncio.wait_for(
                self.llm.responses.parse(
                    model=self.model,
                    input=messages,  # type: ignore
                    text_format=schema,
                    temperature=0,  # type: ignore
                ),  # type: ignore
                timeout=self.step_timeout_sec,
            )
            return resp.output_parsed
        except Exception:
            logger.exception("LLM parse gagal.")
            return None

    async def _compress(self, text: str) -> str:
        msgs = [
            {"role": "system", "content": COMPRESSOR_SYSTEM()},
            {"role": "user", "content": text},
        ]
        out = await self._llm_text(messages=msgs, temperature=0.0)
        return (out or text)[:1200]

    # ---------------------------
    # Context Lock: ditentukan LLM dari prompt + kandidat hasil retrieval
    # ---------------------------
    async def _derive_context_lock(
        self, prompt: str, retrieval_out: Any
    ) -> Optional[ContextLock]:
        # bentuk kandidat unik (pelanggan,tahun,project) untuk referensi LLM
        cands = _extract_candidates_from_retrieval(retrieval_out)
        # ringkas daftar pelanggan & tahun yang terlihat
        observed = {
            "pelanggan": sorted({c.pelanggan for c in cands if c.pelanggan}),
            "tahun": sorted({c.tahun for c in cands if c.tahun}),
            "projects": sorted({c.project for c in cands if c.project}),
        }

        system = (
            "ROLE: CONTEXT LOCKER\n"
            "Tentukan *satu* pelanggan dan (jika ada) *satu* tahun target untuk mengunci konteks analisis.\n"
            "- Dasarkan terutama pada USER PROMPT.\n"
            "- Jika ambigu, pilih kandidat yang paling selaras dengan prompt (nama pelanggan dan tahun) dari daftar observed.\n"
            "- Berikan juga 'pelanggan_variants' (alias/variasi penulisan) agar seleksi dokumen konsisten.\n"
            "- 'project_keywords' minimal berisi ['firewall','core'] jika relevan.\n"
            "- Jangan menciptakan organisasi baru yang tidak tersirat.\n"
            "- Kembalikan JSON sesuai schema."
        )
        user = (
            f"USER PROMPT:\n{prompt}\n\n"
            f"OBSERVED CANDIDATES (ringkas):\n{json.dumps(observed, ensure_ascii=False, indent=2)}\n"
            "Pilih pelanggan & tahun untuk Context Lock."
        )
        lock: Optional[ContextLock] = await self._llm_parse(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            schema=ContextLock,
        )
        return lock

    def _match_with_lock(self, md: Mapping, lock: ContextLock) -> bool:
        """Cek apakah metadata cocok dengan Context Lock (pelanggan & tahun)."""
        pel = str(md.get("pelanggan") or "").lower()
        th = str(md.get("tahun") or "").strip()
        # pelanggan cocok jika mengandung salah satu variant
        variants = [lock.pelanggan.lower()] + [
            v.lower() for v in (lock.pelanggan_variants or [])
        ]
        ok_pelanggan = any(v and v in pel for v in variants if v)
        if lock.tahun:
            return ok_pelanggan and (th == lock.tahun)
        return ok_pelanggan

    def _filter_hits_by_lock(self, out: Any, lock: Optional[ContextLock]) -> Any:
        if not lock:
            return out
        items = _as_list(out)
        filtered: List[Any] = []
        for it in items:
            md = None
            if isinstance(it, Mapping) and isinstance(it.get("metadata"), Mapping):
                md = it["metadata"]
            else:
                text_str = (
                    it.get("text")
                    if isinstance(it, Mapping)
                    else getattr(it, "text", None)
                )
                if isinstance(text_str, str):
                    try:
                        parsed = json.loads(text_str)
                        if isinstance(parsed, Mapping) and isinstance(
                            parsed.get("metadata"), Mapping
                        ):
                            md = parsed["metadata"]
                    except Exception:
                        md = None
            if isinstance(md, Mapping) and self._match_with_lock(md, lock):
                filtered.append(it)
        # Kembalikan list terfilter jika ada, else original (jangan kosongkan total agar pipeline tetap berjalan)
        return filtered if filtered else out

    # ---------------------------
    # Metadata extractor
    # ---------------------------
    @staticmethod
    def _collect_meta_from_result(result_block: Any) -> Dict[str, str]:
        meta: Dict[str, str] = {}
        hits: List[Any] = _as_list(result_block)

        def _extract_md_from_hit(hit: Any) -> Optional[Mapping]:
            if isinstance(hit, Mapping):
                md = hit.get("metadata")
                if isinstance(md, Mapping):
                    return md
                t = hit.get("text")
                if isinstance(t, str):
                    try:
                        parsed = json.loads(t)
                        if isinstance(parsed, Mapping) and isinstance(
                            parsed.get("metadata"), Mapping
                        ):
                            return parsed["metadata"]
                    except Exception:
                        return None
                return None
            txt = getattr(hit, "text", None)
            if isinstance(txt, str):
                try:
                    parsed = json.loads(txt)
                    if isinstance(parsed, Mapping) and isinstance(
                        parsed.get("metadata"), Mapping
                    ):
                        return parsed["metadata"]
                except Exception:
                    return None
            return None

        for h in hits:
            md = _extract_md_from_hit(h)
            if isinstance(md, Mapping):
                for k in META_KEYS:
                    v = md.get(k)
                    if v not in (None, ""):
                        meta.setdefault(k, str(v))
        return meta

    # ---------------------------
    # Tool call with retries
    # ---------------------------
    async def _call_tool_retriable(
        self, name: str, args: Dict[str, Any]
    ) -> Tuple[bool, Any, str]:
        last_err = ""
        for attempt in range(1, MAX_TOOL_RETRIES + 1):
            try:
                out = await asyncio.wait_for(
                    self.executor.call_tool(name, args), timeout=self.step_timeout_sec
                )
                logger.info(
                    "output dari mcp tools %s : %s", name, json_preview(out, 500)
                )
                if _is_valid_tool_output(name, out):
                    return True, out, ""
                last_err = f"Invalid output structure on attempt {attempt}"
                logger.warning("%s -> %s", name, last_err)
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                logger.warning("Tool %s failed attempt %d: %s", name, attempt, last_err)
        return False, None, last_err

    # =========================================================
    # PIPELINE: LLM-in-the-loop
    # =========================================================
    async def run(self, *, prompt: str, user_id: str, k: int = 6) -> str:
        logger.info("ProjectAnalysisActor.start: %s", truncate_args(prompt, 160))
        await self.executor.get_tools()

        scratch: List[str] = []
        known_meta: Dict[str, str] = {}
        r0_out: Any = None
        r_refine_out: Any = None

        # 1) R0 — Retrieval awal (query dikondensasi)
        base_query = condense_query_for_search(prompt)
        ok, r0, err = await self._call_tool_retriable(
            TOOL_RETRIEVAL, {"query": base_query, "k": k}
        )
        if ok:
            r0_out = r0
            scratch.append(
                f"[R0]{json.dumps(to_jsonable(r0), ensure_ascii=False)[:1500]}"
            )
            # preview
            top_preview = None
            if isinstance(r0, list) and r0:
                top_preview = r0[0]
            elif isinstance(r0, dict):
                rr = r0.get("result") or r0.get("results") or r0.get("data") or []
                top_preview = rr[0] if isinstance(rr, list) and rr else None
            logger.info(
                "R0 top preview: %s",
                json.dumps(to_jsonable(top_preview), ensure_ascii=False)[:500],
            )
        else:
            scratch.append(f"[R0-ERROR]{err}")

        # 2) Derive Context Lock (LLM) dari prompt + R0 (bukan hardcode)
        self._ctx_lock = await self._derive_context_lock(prompt, r0_out or [])
        logger.info(
            "CONTEXT LOCK: %s",
            json_preview(self._ctx_lock.model_dump() if self._ctx_lock else {}, 400),
        )

        # 3) Terapkan Lock ke hasil R0 dan tarik metadata dari yang lolos
        if r0_out and self._ctx_lock:
            r0_locked = self._filter_hits_by_lock(r0_out, self._ctx_lock)
            # ganti potongan R0 di scratch agar yang tersimpan sudah “terkunci”
            scratch.append(f"[R0-LOCKED]{json_preview(r0_locked, 1500)}")
            known_meta.update(self._collect_meta_from_result(r0_locked))
            logger.info("META R0 (locked): %s", known_meta)
        elif r0_out:
            # fallback tanpa lock
            known_meta.update(self._collect_meta_from_result(r0_out))
            logger.info("META R0 (no lock): %s", known_meta)

        # 4) Jika metadata masih kosong → refine query ke RAG (tetap melekat pada Lock via LLM)
        tries = 0
        while (not known_meta) and (tries < MAX_REFINEMENT_TRIES):
            tries += 1
            refine_msgs = [
                {
                    "role": "system",
                    "content": "ROLE: RETRIEVAL QUERY REFINER\nSusun query singkat & tajam untuk RAG project analysis.",
                },
                {
                    "role": "user",
                    "content": f"USER_PROMPT:\n{prompt}\n\nBASE_QUERY:\n{base_query}\n\nLOCK:\n{json.dumps(self._ctx_lock.model_dump() if self._ctx_lock else {}, ensure_ascii=False)}\n\nKembalikan JSON sesuai schema.",
                },
            ]
            plan: Optional[QueryRefinement] = await self._llm_parse(  # type: ignore
                messages=refine_msgs, schema=QueryRefinement
            )
            if not plan:
                break

            args: Dict[str, Any] = {"query": plan.query, "k": int(plan.k)}  # type: ignore
            # Inject metadata dari Context Lock (LLM decides; code hanya meneruskan)
            if self._ctx_lock:
                args["metadata"] = {
                    "pelanggan": self._ctx_lock.pelanggan,
                    "tahun": self._ctx_lock.tahun,
                }
            elif plan.metadata:  # type: ignore
                args["metadata"] = plan.metadata.model_dump(exclude_none=True)  # type: ignore

            ok2, rX, err2 = await self._call_tool_retriable(TOOL_RETRIEVAL, args)
            scratch.append(f"[R0-REFINE-{tries}] q={plan.query!r} | ok={ok2}")  # type: ignore
            if ok2:
                r_refine_out = rX
                # filter dengan Lock
                rX_locked = self._filter_hits_by_lock(rX, self._ctx_lock)
                scratch.append(f"[R{tries}-LOCKED]{json_preview(rX_locked, 1500)}")
                known_meta.update(self._collect_meta_from_result(rX_locked))
                logger.info("META R0-refine (locked): %s", known_meta)
                break
            else:
                scratch.append(f"[R0-REFINE-{tries}-ERROR]{err2}")

        # 5) Kompres awal (termasuk info LOCK agar tetap hadir di scratch)
        if self._ctx_lock:
            scratch.append(
                f"[LOCK]{json.dumps(self._ctx_lock.model_dump(), ensure_ascii=False)}"
            )
        scratch = [await self._compress("\n".join(scratch))]

        # 6) Actor–Critic loop
        steps = 0
        finalize = False
        while steps < MAX_STEPS and not finalize:
            steps += 1

            planner_msgs = [
                {"role": "system", "content": PLANNER_SYSTEM()},
                {
                    "role": "user",
                    "content": (
                        f"PERMINTAAN USER:\n{prompt}\n\n"
                        f"CONTEXT LOCK (LLM):\n{json.dumps(self._ctx_lock.model_dump() if self._ctx_lock else {}, ensure_ascii=False)}\n\n"
                        f"SCRATCHPAD:\n{scratch[0]}\n\nKNOWN_META:{json.dumps(known_meta, ensure_ascii=False)}\n\n"
                        "Jika perlu, pilih tool & args minimalis."
                    ),
                },
            ]
            plan: Optional[ToolPlan] = await self._llm_parse(
                messages=planner_msgs, schema=ToolPlan
            )
            if not plan:
                break

            if plan.tool == "none":
                critic = await self._llm_parse(
                    messages=[
                        {"role": "system", "content": CRITIC_SYSTEM()},
                        {
                            "role": "user",
                            "content": f"USER:\n{prompt}\n\nSCRATCHPAD:\n{scratch[0]}",
                        },
                    ],
                    schema=CriticFeedback,
                )
                finalize = bool(critic and critic.decision.finalize)
                if not finalize:
                    continue
                else:
                    break

            if plan.tool not in ALLOWED_TOOLS:
                continue

            # Build payload
            payload = _make_payload_for(plan.tool, plan.args)

            if plan.tool == TOOL_RETRIEVAL:
                payload.setdefault("k", k)
                if not payload.get("query"):
                    payload["query"] = condense_query_for_search(prompt)
                # **Kunci dinamis via Lock** (ditentukan LLM, bukan hardcode)
                if self._ctx_lock:
                    payload["metadata"] = {
                        "pelanggan": self._ctx_lock.pelanggan,
                        "tahun": self._ctx_lock.tahun,
                    }
                elif "metadata" not in payload and known_meta:
                    payload["metadata"] = {
                        mk: mv for mk, mv in known_meta.items() if mk in META_KEYS
                    }

            elif plan.tool == TOOL_SUMMARY:
                # Bangun argumen SUMMARY dari retrieval yang sudah di-lock
                source = r_refine_out or r0_out
                source_locked = (
                    self._filter_hits_by_lock(source, self._ctx_lock)
                    if source
                    else None
                )
                built = await self._build_summary_args_from_retrieval(
                    prompt, source_locked
                )
                if built:
                    payload = built
                else:
                    payload = {}
                    for kmeta in META_KEYS:
                        if known_meta.get(kmeta):
                            payload[kmeta] = known_meta[kmeta]
                # pelanggan untuk tool summary perlu slug/normalisasi path — ini bukan mengubah lock, hanya format folder
                if payload.get("pelanggan"):
                    payload["pelanggan"] = _slugify(payload["pelanggan"])

            elif plan.tool == TOOL_WEB:
                if not payload.get("q") and plan.args.query:
                    payload["q"] = plan.args.query
                payload.setdefault("q", condense_query_for_search(prompt))
                payload.setdefault("num", 5)

            # Eksekusi tool
            ok_tool, tool_out, err_tool = await self._call_tool_retriable(
                plan.tool, payload
            )
            if not ok_tool:
                scratch.append(f"[{plan.tool}-ERROR]{err_tool}")
                scratch = [await self._compress("\n".join(scratch))]
                continue

            # Jika retrieval, filter hasil lagi dengan Lock sebelum diserap
            if plan.tool == TOOL_RETRIEVAL and self._ctx_lock:
                tool_out_locked = self._filter_hits_by_lock(tool_out, self._ctx_lock)
                scratch.append(
                    f"[{plan.tool.upper()}-LOCKED]{json_preview(tool_out_locked, 1500)}"
                )
                known_meta.update(self._collect_meta_from_result(tool_out_locked))
            else:
                scratch.append(f"[{plan.tool.upper()}]{json_preview(tool_out, 1500)}")
                if plan.tool == TOOL_RETRIEVAL:
                    known_meta.update(self._collect_meta_from_result(tool_out))

            # Critic
            critic = await self._llm_parse(
                messages=[
                    {"role": "system", "content": CRITIC_SYSTEM()},
                    {
                        "role": "user",
                        "content": f"USER:\n{prompt}\n\nLOCK:\n{json.dumps(self._ctx_lock.model_dump() if self._ctx_lock else {}, ensure_ascii=False)}\n\n"
                        f"TOOL_OUT({plan.tool}):\n{json.dumps(to_jsonable(tool_out), ensure_ascii=False)[:2000]}\n\n"
                        f"SCRATCHPAD_SO_FAR:\n{scratch[0]}",
                    },
                ],
                schema=CriticFeedback,
            )
            scratch.append(f"[CRITIC]{critic.model_dump_json() if critic else '{}'}")
            scratch = [await self._compress("\n".join(scratch))]
            finalize = bool(critic and critic.decision.finalize)

        # 7) Finalize (Actor) — STRICT grounding + Lock awareness
        lock_clause = (
            "Hanya gunakan informasi yang cocok dengan Context Lock (pelanggan & tahun) berikut:\n"
            f"{json.dumps(self._ctx_lock.model_dump() if self._ctx_lock else {}, ensure_ascii=False)}\n"
            "Abaikan konten lain di SCRATCHPAD jika tidak cocok Lock."
        )
        final_msgs = [
            {"role": "system", "content": FINALIZE_ANSWER()},
            {"role": "user", "content": f"Permintaan awal:\n{prompt}"},
            {"role": "user", "content": lock_clause},
            {"role": "user", "content": f"SCRATCHPAD:\n{scratch[0]}"},
            {
                "role": "user",
                "content": "Susun jawaban FINAL bergaya analis profesional yang menjawab permintaan awal dengan tepat.",
            },
        ]
        logger.info(
            "# 7) Finalize — STRICT grounding + LOCK. final_msgs=%s",
            json_preview(final_msgs, 800),
        )
        try:
            resp = await asyncio.wait_for(
                self.llm.responses.create(
                    model=self.model,
                    input=final_msgs,  # type: ignore
                    temperature=0.1,  # type: ignore
                ),  # type: ignore
                timeout=self.step_timeout_sec,
            )
            final_text = (getattr(resp, "output_text", "") or "").strip()
        except Exception:
            logger.exception("Finalize gagal.")
            final_text = ""

        logger.info("Hasil dari responses summary final_msgs adalah %s", final_text)
        return final_text or "Tidak ada jawaban final yang dapat disusun."

    # ---------------------------
    # Summary arg builder (pakai retrieval yang sudah di-lock)
    # ---------------------------
    async def _select_summary_args_via_llm(
        self, query: str, candidates: List[Candidate]
    ) -> Optional[Candidate]:
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        listing = [
            {
                "index": i,
                "filename": c.filename,
                "pelanggan": c.pelanggan,
                "project": c.project,
                "tahun": c.tahun,
            }
            for i, c in enumerate(candidates)
        ]
        system = (
            "Anda adalah selector metadata yang ketat.\n"
            "- Pilih *tepat satu* kandidat yang PALING relevan dengan query user.\n"
            "- Pilih HANYA dari daftar kandidat yang diberikan (berdasar index).\n"
            "- Pertimbangkan kecocokan tahun, pelanggan & project.\n"
            "- Kembalikan hasil sesuai schema."
        )
        user = (
            f"USER QUERY:\n{query}\n\n"
            f"KANDIDAT (0..{len(candidates) - 1}):\n{json.dumps(listing, ensure_ascii=False, indent=2)}\n\n"
            "Pilih kandidat terbaik."
        )
        try:
            parsed = await asyncio.wait_for(
                self.llm.responses.parse(
                    model=self.model,
                    input=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    text_format=ChooseSummaryArgs,
                    temperature=0,
                ),
                timeout=self.step_timeout_sec,
            )
            choice: ChooseSummaryArgs = parsed.output_parsed  # type: ignore
            idx = int(choice.chosen_index)
            if 0 <= idx < len(candidates):
                cand = candidates[idx]
                return cand
        except Exception as e:
            logger.warning("LLM selection gagal: %s", e, exc_info=False)
        return candidates[0]

    async def _build_summary_args_from_retrieval(
        self, query: str, retrieval_out: Any
    ) -> Optional[Dict[str, str]]:
        all_cands = _extract_candidates_from_retrieval(retrieval_out)
        if not all_cands:
            return None
        short = _shortlist_candidates(all_cands, query, k=5)
        selected = await self._select_summary_args_via_llm(query, short)
        if not selected:
            return None
        # pelanggan ke format folder (slug) — ini formatting, bukan keputusan lock
        pel_norm = _slugify(selected.pelanggan or "")
        args = {
            "filename": selected.filename or "",
            "pelanggan": pel_norm,
            "project": selected.project or "",
            "tahun": selected.tahun or "",
        }
        return args
