# projectwise/services/memory/long_term_memory.py
from __future__ import annotations

import asyncio
import time
import contextlib
from typing import List, Dict, Any, Optional, Tuple, Deque

from collections import deque
from mem0 import AsyncMemory
from mem0.configs.base import MemoryConfig
from .noop_memory import NoOpAsyncMemory
from projectwise.utils.logger import get_logger
from projectwise.services.workflow.prompt_instruction import DEFAULT_SYSTEM_PROMPT


logger = get_logger(__name__)


# helper deteksi koneksi
def _is_connect_error(exc: BaseException) -> bool:
    msg = str(exc)
    # Pattern yang muncul di Windows + httpx/qdrant
    return (
        "actively refused" in msg.lower()
        or "connecterror" in msg.lower()
        or "failed to establish a new connection" in msg.lower()
        or "connection refused" in msg.lower()
    )


# helper item untuk antrian tulis
class _WriteItem:
    __slots__ = ("text", "user_id", "metadata")

    def __init__(self, text: str, user_id: str, metadata: Optional[Dict[str, Any]]):
        self.text = text
        self.user_id = user_id
        self.metadata = metadata


def _extract_text_from_mem_item(item: Dict[str, Any]) -> Optional[str]:
    """
    Normalisasi hasil memori dari berbagai bentuk schema:
    coba 'memory', 'text', 'value', 'content' secara berurutan.
    """
    for k in ("memory", "text", "value", "content"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v
    # fallback: jika item langsung string
    if isinstance(item, str) and item.strip():
        return item
    return None


class Mem0Manager:
    """Wrapper asinkron untuk mem0 AsyncMemory agar lebih modular & defensif."""

    def __init__(self, service_configs, config: Optional[Dict[str, Any]] = None):
        self._service_configs = service_configs
        self._config = config or self._default_config()
        self._memory: Optional[AsyncMemory] = None
        self._init_lock = asyncio.Lock()

        # ADD: status & resilience state
        self._ready: bool = False
        self._degraded: bool = False
        self._last_error: Optional[str] = None
        self._init_attempts: int = 0

        # ADD: circuit breaker & fail counters
        self._consec_failures: int = 0
        self._cb_open_until: float = 0.0  # epoch detik
        self._cb_fail_threshold: int = (
            2  # CHANGE: batas buka circuit setelah 2 gagal berturut
        )
        self._cb_open_seconds: int = 10  # CHANGE: selama 10 detik jangan retry

        # ADD: write-queue saat degraded
        self._pending: Deque[_WriteItem] = deque()
        self._max_pending: int = 500  # CHANGE: batasi memori

        # ADD: guard agar flush tidak double
        self._flush_lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None

    def _default_config(self) -> MemoryConfig:
        custom_fact_extraction_prompt = """
        You are an extraction engine for Lintasarta presales & project management. 
        Extract ONLY concrete, atomic facts related to these categories:
        - User Persona (role, unit/tim, pihak terkait)
        - KAK/TOR Proyek (tujuan, ruang lingkup, HPS/budget, jadwal, SLA/MTTR, skema evaluasi, bobot teknis:harga)
        - Product Standard Lintasarta (nama produk, parameter teknis, SLA, jaminan layanan)
        - Competitor Analysis (nama kompetitor, produk/fitur yang dibandingkan, kelebihan/kekurangan ringkas)
        - Project Risk (risiko implementasi/operasional)
        - Tender Risk (risiko administratif/komersial/kontrak)
        - Compliance & Licensing/Perizinan (TKDN, K3LL/HSSE, ISO, PSE Kominfo, NIB, IUJK/IUJPTL dsb.)
        - Delivery & Implementation Cases (lesson learned, kendala lapangan, mitigasi)

        Guidelines:
        - Output ONLY in JSON: {"facts":[...]}
        - Each fact must be a single concise string, no reasoning, no explanations.
        - Use the pattern: "<Category>: <content> [| key=value | key=value ...]" for consistency.
        - If the input is irrelevant or lacks extractable facts, return {"facts": []}.
        - Do NOT infer or hallucinate. Extract strictly from the input text.
        - Avoid duplicates; split multi-point sentences into separate atomic facts.
        - Language of facts should follow the input language (ID or EN).

        Few-shot examples:

        Input: Hi.
        Output: {"facts": []}

        Input: The weather is nice today.
        Output: {"facts": []}

        Input: Saya melihat risiko proyek bank sumsel.
        Output: {"facts": ["ProjectRisk: Risiko proyek Bank Sumsel (detail tidak spesifik)"]}

        Input: KAK: Pengadaan WAF untuk Bank Sumsel Babel, nilai HPS ~1,83 M IDR, durasi 100 hari kerja, kontrak lumpsum, evaluasi 70:30 teknis:harga.
        Output: {"facts": [
        "KAK_TOR: Pengadaan WAF untuk Bank Sumsel Babel",
        "KAK_TOR: HPS≈1,83 M IDR",
        "KAK_TOR: Durasi 100 hari kerja",
        "KAK_TOR: Kontrak lumpsum",
        "KAK_TOR: Evaluasi 70:30 teknis:harga"
        ]}

        Input: Product Standard Internet Dedicated Lintasarta: rasio 1:1, uptime 99.5%, latency domestik <60ms.
        Output: {"facts": [
        "ProductStandard: Internet Dedicated | ratio=1:1 | uptime=99.5% | latency_domestic<60ms"
        ]}

        Input: Kompetitor A menawarkan Fortinet 100F; unggul throughput, namun biaya lebih tinggi dari kami.
        Output: {"facts": [
        "Competitor: A | product=Fortinet 100F | strength=throughput | weakness=higher cost"
        ]}

        Input: Tender mensyaratkan TKDN minimal 40% dan ISO 27001 untuk penyedia layanan; penalti keterlambatan 1/1000 nilai kontrak per hari.
        Output: {"facts": [
        "Compliance: TKDN>=40%",
        "Compliance: ISO 27001 (required)",
        "TenderRisk: Penalty delay=1/1000 per day"
        ]}

        Input: Kasus implementasi Palembang: izin crossing fiber terlambat, solusi jalur alternatif via FO existing.
        Output: {"facts": [
        "DeliveryCase: Palembang | issue=permit delay fiber crossing | mitigation=alternate FO route"
        ]}

        Return ONLY the JSON as shown and use Indonesian language.
        """
        custom_update_memory_prompt = """
        You are a smart memory manager for Lintasarta presales & project management.
        Your job: reconcile newly retrieved FACTS (atomic strings) with the existing MEMORY list.
        You can perform four operations on each existing item: ADD, UPDATE, DELETE, NONE.

        Context (domain categories you will encounter in text):
        - Persona (role, unit, stakeholders)
        - KAK/TOR (project scope, HPS/budget, duration, contract type, SLA/MTTR, evaluation ratio)
        - Product Standard (product name + params: ratio, uptime, latency, etc.)
        - Competitor Analysis (name, product, strengths, weaknesses)
        - Project Risk (implementation/operational risks)
        - Tender Risk (admin/commercial/contractual risks)
        - Compliance & Permits (TKDN, ISO, PSE Kominfo, NIB, IUJK/IUJPTL, K3LL/HSSE)
        - Delivery & Implementation Cases (issues, mitigations, lessons)

        INPUTS YOU WILL RECEIVE:
        - Old Memory (list of objects): [{"id": "...", "text": "..."}]
        - Retrieved facts (list of strings) — each string is atomic and formatted like:
        "<Category>: <content> | key=value | key=value"

        YOUR OUTPUT FORMAT (JSON):
        {
        "memory": [
            { "id": "<existing-or-new>", "text": "<final text>", "event": "ADD|UPDATE|DELETE|NONE", "old_memory": "<old text if UPDATE>" }
        ]
        }

        OPERATION RULES:
        1) ADD:
        - If a retrieved fact is NEW (no semantically equivalent memory exists), ADD it with a NEW id.
        - “Semantically equivalent” means: same Category and same primary subject.
            Examples of primary subject:
            • KAK_TOR: (project/customer) or same field topic (e.g., Duration).
            • ProductStandard: same product name.
            • Competitor: same competitor name (+ optional product).
            • Risk: same risk description theme.
            • Compliance: same regulation name (e.g., TKDN, ISO 27001).
            • DeliveryCase: same location + issue theme.
        - If memory is empty, ADD all retrieved facts.

        2) UPDATE (keep the SAME id):
        - If an existing memory item is about the same subject but the new fact is MORE SPECIFIC, MORE RECENT, or CORRECTS a field → UPDATE.
        - For ProductStandard, MERGE params (e.g., add latency if previously missing).
        - For Risks (Project/Tender), if severity/likelihood changes, UPDATE to the more informative one; if both exist, prefer WORSE severity (High > Medium > Low). Include mitigation if provided.
        - For Compliance, normalize keys (e.g., "TKDN>=40%") and UPDATE thresholds if changed.
        - For KAK/TOR numeric fields (duration, HPS), prefer the NEWER or MORE SPECIFIC value (e.g., “100 hari kerja” → “120 hari kerja”).
        - When updating, include the previous text in "old_memory".

        3) DELETE (keep the SAME id, just mark DELETE):
        - If a retrieved fact contradicts an existing memory item (e.g., “Penalty 1/1000” vs “No penalty”), mark the conflicting older one as DELETE.
        - If direction explicitly says to remove a requirement, mark DELETE.

        4) NONE:
        - If a retrieved fact is already fully captured (same content/semantics), or it’s irrelevant to our categories, mark NONE.

        RESOLUTION & NORMALIZATION:
        - Prefer inputs with timestamps or explicit “latest” indicators if present.
        - Prefer more specific over generic (e.g., “Evaluasi 70:30 teknis:harga” > “Ada evaluasi teknis:harga”).
        - Merge additive parameters for ProductStandard into one line: 
        "ProductStandard: Internet Dedicated | ratio=1:1 | uptime=99.5% | latency_domestic<60ms".
        - Keep concise; avoid duplicates.
        - Do not hallucinate fields not present in facts.

        EXAMPLES (Indonesian):
        Old Memory:
        [
        {"id":"m1","text":"KAK_TOR: Pengadaan WAF | customer=Bank Sumsel Babel"},
        {"id":"m2","text":"ProductStandard: Internet Dedicated | ratio=1:1 | uptime=99.5%"},
        {"id":"m3","text":"TenderRisk: Penalty delay=1/1000 per day"}
        ]
        Retrieved facts:
        [
        "KAK_TOR: Durasi 120 hari kerja",
        "ProductStandard: Internet Dedicated | latency_domestic<60ms",
        "Compliance: TKDN>=40%"
        ]
        New Memory (illustrative):
        {
        "memory":[
            {"id":"m1","text":"KAK_TOR: Pengadaan WAF | customer=Bank Sumsel Babel", "event":"NONE"},
            {"id":"m2","text":"ProductStandard: Internet Dedicated | ratio=1:1 | uptime=99.5% | latency_domestic<60ms", "event":"UPDATE", "old_memory":"ProductStandard: Internet Dedicated | ratio=1:1 | uptime=99.5%"},
            {"id":"m3","text":"TenderRisk: Penalty delay=1/1000 per day", "event":"NONE"},
            {"id":"m4","text":"KAK_TOR: Durasi 120 hari kerja", "event":"ADD"},
            {"id":"m5","text":"Compliance: TKDN>=40%", "event":"ADD"}
        ]
        }
        Return ONLY the JSON described above and use Indonesian language.
        """

        from mem0.configs.base import (
            VectorStoreConfig,
            LlmConfig,
            EmbedderConfig,
            # GraphStoreConfig,
            # Neo4jConfig,
        )

        custom_config = MemoryConfig(
            vector_store=VectorStoreConfig(
                provider="qdrant",
                config={
                    "collection_name": getattr(
                        self._service_configs, "collection_name", "mem0"
                    ),
                    "host": getattr(self._service_configs, "qdrant_host", "localhost"),
                    "port": getattr(self._service_configs, "qdrant_port", 6333),
                },
            ),
            llm=LlmConfig(
                provider="openai",
                config={
                    "openai_base_url": self._service_configs.llm_base_url,
                    "api_key": self._service_configs.llm_api_key,
                    "model": self._service_configs.llm_model,
                },
            ),
            embedder=EmbedderConfig(
                provider="openai",
                config={
                    "api_key": self._service_configs.embedding_model_api_key,
                    "model": self._service_configs.embedding_model,
                },
            ),
            # history_db_path=":memory:",  # not used
            # graph_store=GraphStoreConfig(
            #     provider="neo4j",
            #     config=Neo4jConfig(
            #         uri="bolt://localhost:7687",
            #         user="neo4j",
            #         password="password",
            #     ),
            # ),
            custom_fact_extraction_prompt=custom_fact_extraction_prompt.strip(),
            custom_update_memory_prompt=custom_update_memory_prompt.strip(),
            version="v1.1",
        )

        return custom_config

    # ---------------- lifecycle ----------------
    async def init(self) -> None:
        """Inisialisasi; jangan raise saat gagal (masuk degraded)."""
        # SHORT-CIRCUIT: jika circuit breaker sedang open, skip init
        now = time.time()
        if now < self._cb_open_until:
            # CHANGE: hindari retry terlalu sering
            logger.debug("Circuit open; skip init until %.0f", self._cb_open_until)
            return

        if self._ready and not self._degraded:
            return

        async with self._init_lock:
            # cek lagi di dalam lock
            if self._ready and not self._degraded:
                return

            self._init_attempts += 1
            logger.info(
                "Inisialisasi Mem0 AsyncMemory... (attempt=%s)", self._init_attempts
            )
            try:
                self._memory = AsyncMemory(config=self._default_config())
                self._ready = True
                self._degraded = False
                self._last_error = None
                self._consec_failures = 0  # ADD: reset
                logger.info("Mem0 AsyncMemory siap digunakan")
                # ADD: kalau ada pending, flush di background
                self._schedule_flush()
            except Exception as e:
                self._memory = NoOpAsyncMemory()  # type: ignore
                self._ready = False
                self._degraded = True
                self._last_error = str(e)
                self._consec_failures += 1  # ADD
                # OPEN circuit bila melewati ambang
                if self._consec_failures >= self._cb_fail_threshold:
                    self._cb_open_until = time.time() + self._cb_open_seconds
                logger.warning(
                    "Mem0 in DEGRADE mode (vector store unavailable): %s",
                    self._last_error,
                )

    # ADD: schedule flush pending jika belum berjalan
    def _schedule_flush(self) -> None:
        if self._flush_task and not self._flush_task.done():
            return
        # jalankan sebagai background task
        self._flush_task = asyncio.create_task(self._flush_pending_safe())

    # ADD: wrapper aman flush
    async def _flush_pending_safe(self) -> None:
        try:
            await self._flush_pending()
        except Exception as e:
            logger.exception("Flush pending gagal: %s", e)

    # ADD: flush antrian ketika sudah siap (ready & !degraded)
    async def _flush_pending(self) -> None:
        if not self._ready or self._degraded:
            return
        async with self._flush_lock:
            if not self._pending:
                return
            logger.info(
                "Mulai flush %d memori tertunda ke vector store...", len(self._pending)
            )
            flushed = 0
            while self._pending:
                item = self._pending[0]
                try:
                    await self._add_direct(
                        item.text, user_id=item.user_id, metadata=item.metadata
                    )
                    flushed += 1
                    self._pending.popleft()
                except Exception as e:
                    # jika connect error lagi, hentikan flush & degrade kembali
                    if _is_connect_error(e):
                        logger.warning(
                            "Flush terhenti (koneksi gagal), kembali ke degraded."
                        )
                        await self._set_degraded(e)
                        break
                    # error lain: log & buang item ini (hindari loop buntu)
                    logger.error("Gagal flush 1 item (drop): %s", e)
                    self._pending.popleft()
            logger.info(
                "Flush selesai. Berhasil=%d; Sisa queue=%d", flushed, len(self._pending)
            )

    # ADD: set state degraded + circuit semi-open
    async def _set_degraded(self, exc: BaseException) -> None:
        self._memory = NoOpAsyncMemory()  # type: ignore
        self._ready = False
        self._degraded = True
        self._last_error = str(exc)
        self._consec_failures += 1
        # buka circuit untuk menahan retry sebentar
        if self._consec_failures >= self._cb_fail_threshold:
            self._cb_open_until = time.time() + self._cb_open_seconds
        logger.warning("Switch to DEGRADE due to error: %s", self._last_error)

    # ADD: panggilan add langsung ke backend aktif (NoOp atau Mem0)
    async def _add_direct(
        self, text: str, *, user_id: str, metadata: Optional[Dict[str, Any]]
    ) -> None:
        role = "user"
        await self.memory.add(
            messages=[{"role": role, "content": text}],
            user_id=user_id,
            metadata=metadata or {},
            infer=False,
        )

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def degraded(self) -> bool:
        return self._degraded

    def health(self) -> Dict[str, Any]:
        # UPDATE: tambah metrik baru
        return {
            "ready": self._ready,
            "degraded": self._degraded,
            "init_attempts": self._init_attempts,
            "last_error": self._last_error,
            "consec_failures": self._consec_failures,  # ADD
            "circuit_open_until": self._cb_open_until,  # ADD (epoch; 0 jika tertutup)
            "pending_queue_len": len(self._pending),  # ADD
        }

    async def _ensure_ready_or_retry(self) -> None:
        # Jika ready & tidak degraded: tidak perlu retry
        if self._ready and not self._degraded:
            return
        # Jika circuit masih open: skip
        if time.time() < self._cb_open_until:
            return
        # small delay + coba init lagi
        with contextlib.suppress(Exception):
            await asyncio.sleep(0.3)
            await self.init()
            if self._ready and not self._degraded:
                logger.info("Mem0 recovered from degraded mode")
                # saat pulih, flush pending
                self._schedule_flush()

    @property
    def memory(self) -> AsyncMemory:
        # Selalu non-None (NoOp jika gagal)
        return self._memory or NoOpAsyncMemory()  # type: ignore

    # ---------------- helpers ----------------
    async def add_memory(
        self,
        text: str,
        *,
        user_id: str = "default",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Tambahkan satu potong memori teks.
        Return: (ok, error_message)
        """
        await self._ensure_ready_or_retry()
        if not isinstance(text, str) or not text.strip():
            return False, "Teks memori kosong."

        try:
            # Jika sedang degraded → antrikan & return ok=True(queued)
            if self._degraded or not self._ready:
                if len(self._pending) >= self._max_pending:
                    # queue penuh → tolak halus
                    logger.warning("Queue memori penuh; menolak 1 item baru.")
                    return False, "degraded:queue_full"
                self._pending.append(_WriteItem(text, user_id, metadata))  # ADD
                return True, "degraded:queued"

            # Normal path (backend siap)
            await self._add_direct(text, user_id=user_id, metadata=metadata)
            # sukses → reset failure count
            self._consec_failures = 0
            return True, None

        except Exception as e:
            # CHANGE: jika connect error → degrade + enqueue + jangan error-kan user
            if _is_connect_error(e):
                await self._set_degraded(e)
                if len(self._pending) < self._max_pending:
                    self._pending.append(_WriteItem(text, user_id, metadata))
                    logger.warning(
                        "Gagal konek; item di-queue. Total queue=%d", len(self._pending)
                    )
                    return True, "degraded:queued"
                logger.warning("Queue memori penuh saat koneksi gagal.")
                return False, "degraded:queue_full"

            # Error non-koneksi → log exception (tetap seperti sebelumnya)
            logger.exception("Gagal menambah 1 memori (non-connect error): %s", e)
            return False, str(e)

    async def get_memories(
        self, query: str, *, user_id: str = "default", limit: int = 5
    ) -> List[str]:
        await self._ensure_ready_or_retry()
        try:
            result = await self.memory.search(query, user_id=user_id, limit=limit)
            raw = result.get("results", result) or []
            out: List[str] = []
            for item in raw:
                text = (
                    _extract_text_from_mem_item(item)
                    if isinstance(item, dict)
                    else str(item)
                )
                if text:
                    out.append(text)
            return out
        except Exception as e:
            # CHANGE: jika connect error → degrade & kembalikan kosong
            if _is_connect_error(e):
                await self._set_degraded(e)
                logger.warning("Search memory gagal konek; masuk degraded. return [].")
                return []
            logger.error("Gagal search memory (degraded=%s): %s", self._degraded, e)
            return []

    async def add_conversation(
        self, messages: List[Dict[str, str]], *, user_id: str = "default"
    ) -> Dict[str, Any]:
        await self._ensure_ready_or_retry()
        clean = [
            m
            for m in messages
            if isinstance(m, dict)
            and isinstance(m.get("role"), str)
            and isinstance(m.get("content"), str)
            and m["content"].strip()
        ]
        if not clean:
            logger.warning("Lewati add_conversation: messages kosong/tidak valid")
            return {"ok": True, "saved": 0, "errors": []}

        saved = 0
        queued = 0  # ADD
        errs: List[str] = []
        for m in clean:
            ok, err = await self.add_memory(
                m["content"], user_id=user_id, metadata={"role": m["role"]}
            )
            if ok and (err is None):
                saved += 1
            elif ok and err and err.startswith("degraded:queued"):
                queued += 1  # ADD
            elif err:
                errs.append(err)

        # UPDATE: expose queued count via error list ringan
        return {"ok": len(errs) == 0, "saved": saved, "queued": queued, "errors": errs}

    async def chat_with_memories(
        self, llm_client, *, user_message: str, user_id: str = "default"
    ) -> Dict[str, Any]:
        try:
            memories = await self.get_memories(user_message, user_id=user_id)
            memories_block = "\n".join(f"- {m}" for m in memories) or "[Tidak ada]"

            system_prompt = (
                DEFAULT_SYSTEM_PROMPT
                + "\nAnda adalah ProjectWise, asisten AI presales & PM.\n"
                + f"Memori relevan:\n{memories_block}"
            )

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ]

            response = await llm_client.responses.create(
                model=self._service_configs.llm_model,
                input=messages,
            )
            assistant_reply = getattr(response, "output_text", None) or ""

            messages.append({"role": "assistant", "content": assistant_reply})
            save_res = await self.add_conversation(messages, user_id=user_id)

            return {
                "ok": True,
                "reply": assistant_reply,
                "memories_used": memories,
                "save_result": save_res,
                "error": None,
                "memory_health": self.health(),
            }
        except Exception as e:
            logger.exception("chat_with_memories gagal: %s", e)
            return {
                "ok": False,
                "reply": "",
                "memories_used": [],
                "save_result": {"ok": False, "saved": 0, "errors": [str(e)]},
                "error": str(e),
                "memory_health": self.health(),
            }

    async def add_memory_v2(
        self,
        messages: List[Dict[str, str]],
        *,
        user_id: str = "default",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        await self._ensure_ready_or_retry()
        await self.memory.add(
            messages=messages,
            user_id=user_id,
            metadata=metadata or {},
            infer=True,
        )
        # logger.info("add_memory_v2 result: %s", result)

    async def get_memories_v2(
        self, query: str, *, user_id: str = "default", limit: int = 5
    ) -> str:
        await self._ensure_ready_or_retry()
        result = await self.memory.search(query=query, user_id=user_id, limit=limit)
        # logger.info("get_memories_v2 result: %s", result["results"])

        text_memory = []
        for item in result["results"]:
            text_memory.append(item["memory"])

        return "\n".join(text_memory)

    async def reset_memory(self) -> Tuple[Dict[str, str], int]:
        """Reset state memori (untuk testing)."""
        await self._ensure_ready_or_retry()
        await self.memory.reset()
        return {"status": "success", "message": "Memory reset."}, 200
