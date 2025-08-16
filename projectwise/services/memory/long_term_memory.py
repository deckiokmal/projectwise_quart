# projectwise/services/memory/long_term_memory.py
from __future__ import annotations

import asyncio
from typing import List, Dict, Any, Optional, Tuple

from mem0 import AsyncMemory
from projectwise.utils.logger import get_logger
from projectwise.services.workflow.prompt_instruction import DEFAULT_SYSTEM_PROMPT


logger = get_logger(__name__)


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


    def _default_config(self) -> Dict[str, Any]:
        return {
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "host": getattr(self._service_configs, "qdrant_host", "localhost"),
                    "port": getattr(self._service_configs, "qdrant_port", 6333),
                },
            },
            "llm": {
                "provider": "openai",
                "config": {
                    "api_key": self._service_configs.openai_api_key,
                    "model": self._service_configs.llm_model,
                },
            },
            "embedder": {
                "provider": "openai",
                "config": {
                    "api_key": self._service_configs.openai_api_key,
                    "model": self._service_configs.embed_model,
                },
            },
            "version": "v1.1",
        }

    # ---------------- lifecycle ----------------
    async def init(self) -> None:
        """Inisialisasi *lazily*; aman dipanggil berkali-kali."""
        if self._memory is None:
            async with self._init_lock:
                if self._memory is None:
                    logger.info("Inisialisasi Mem0 AsyncMemory...")
                    self._memory = await AsyncMemory.from_config(self._config)
                    logger.info("Mem0 AsyncMemory siap digunakan")


    @property
    def memory(self) -> AsyncMemory:
        if self._memory is None:
            raise RuntimeError("Mem0Manager belum di-init. Panggil await init() dahulu.")
        return self._memory

    # ---------------- helpers ----------------
    async def add_memory(
        self,
        text: str,
        *,
        user_id: str = "default",
        metadata: Optional[Dict[str, Any]] = None,
        agent_id: str = "projectwise",
    ) -> Tuple[bool, Optional[str]]:
        """
        Tambahkan satu potong memori teks.
        Return: (ok, error_message)
        """
        await self.init()
        try:
            if not isinstance(text, str) or not text.strip():
                return False, "Teks memori kosong."
            role = (metadata or {}).get("role") or "user"
            await self.memory.add(
                messages=[{"role": role, "content": text}],
                user_id=user_id,
                agent_id=agent_id,
                metadata=metadata or {},
                infer=False,
            )
            return True, None
        except Exception as e:
            logger.exception("Gagal menambah 1 memori: %s", e)
            return False, str(e)

    # ---------------- operasi utama ----------------
    async def get_memories(
        self, query: str, *, user_id: str = "default", limit: int = 5
    ) -> List[str]:
        """Cari memori relevan untuk *query* dan kembalikan list string (selalu aman)."""
        await self.init()
        try:
            result = await self.memory.search(query=query, user_id=user_id, limit=limit)
            raw = result.get("results", result) or []
            out: List[str] = []
            for item in raw:
                text = _extract_text_from_mem_item(item) if isinstance(item, dict) else str(item)
                if text:
                    out.append(text)
            return out
        except Exception as e:
            logger.error("Gagal search memory: %s", e)
            return []


    async def add_conversation(
        self, messages: List[Dict[str, str]], *, user_id: str = "default"
    ) -> Dict[str, Any]:
        """
        Simpan *messages* (urutan dialog) ke memori, satu per satu, aman.
        Return bentuk konsisten: {"ok": bool, "saved": int, "errors": [..]}.
        """
        await self.init()

        # filter minimal {role, content} string
        clean = [
            m for m in messages
            if isinstance(m, dict)
            and isinstance(m.get("role"), str)
            and isinstance(m.get("content"), str)
            and m["content"].strip()
        ]
        if not clean:
            logger.warning("Lewati add_conversation: messages kosong/tidak valid")
            return {"ok": True, "saved": 0, "errors": []}

        saved = 0
        errs: List[str] = []
        for m in clean:
            ok, err = await self.add_memory(
                m["content"],
                user_id=user_id,
                metadata={"role": m["role"]},
            )
            if ok:
                saved += 1
            elif err:
                errs.append(err)

        return {"ok": len(errs) == 0, "saved": saved, "errors": errs}


    async def chat_with_memories(
        self, llm_client, *, user_message: str, user_id: str = "default"
    ) -> Dict[str, Any]:
        """
        Satu-pintu: ambil memories, panggil LLM, simpan hasil.
        Return SELALU konsisten:
        {
          "ok": bool,
          "reply": str,
          "memories_used": List[str],
          "save_result": {"ok": bool, "saved": int, "errors": [...]},
          "error": Optional[str]
        }
        """
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

            # panggil OpenAI Responses
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
            }
        except Exception as e:
            logger.exception("chat_with_memories gagal: %s", e)
            return {
                "ok": False,
                "reply": "",
                "memories_used": [],
                "save_result": {"ok": False, "saved": 0, "errors": [str(e)]},
                "error": str(e),
            }
