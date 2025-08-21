# projectwise/services/memory/long_term_memory.py
from __future__ import annotations

import asyncio
import time
import contextlib
from typing import List, Dict, Any, Optional, Tuple, Deque

from collections import deque
from mem0 import AsyncMemory
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

    def _default_config(self) -> Dict[str, Any]:
        return {
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": getattr(
                        self._service_configs, "collection_name", "mem0"
                    ),
                    "host": getattr(self._service_configs, "qdrant_host", "localhost"),
                    "port": getattr(self._service_configs, "qdrant_port", 6333),
                },
            },
            "llm": {
                "provider": "openai",
                "config": {
                    "api_key": self._service_configs.llm_api_key,
                    "model": self._service_configs.llm_model,
                },
            },
            "embedder": {
                "provider": "openai",
                "config": {
                    "api_key": self._service_configs.llm_api_key,
                    "model": self._service_configs.embedding_model,
                },
            },
            "version": "v1.1",
        }

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
                self._memory = await asyncio.wait_for(
                    AsyncMemory.from_config(self._config), timeout=10
                )
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
                model=self._config["llm"]["config"]["model"],
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
