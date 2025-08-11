# /services/memory/long_term_memory.py
from __future__ import annotations
from typing import List, Dict, Any, Optional
import asyncio

from mem0 import AsyncMemory
from projectwise.utils.logger import get_logger

logger = get_logger(__name__)


class Mem0Manager:
    """Wrapper asinkron untuk mem0 AsyncMemory agar lebih modular."""

    def __init__(self, service_configs, config: Optional[Dict[str, Any]] = None):
        """
        service_configs: instance ServiceConfigs (hasil load .env)
        config: override config dict (opsional)
        """
        self._service_configs = service_configs
        self._config = config or self._default_config()
        self._memory: Optional[AsyncMemory] = None
        self._init_lock = asyncio.Lock()

    def _default_config(self) -> Dict[str, Any]:
        """Bangun konfigurasi default mem0 dari service_configs."""
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

    # ------------- lifecycle -----------------------------------------
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
            raise RuntimeError(
                "Mem0Manager belum di-init. Panggil await init() dahulu."
            )
        return self._memory

    # ------------- operasi utama -------------------------------------
    async def get_memories(
        self, query: str, *, user_id: str = "default", limit: int = 5
    ) -> List[str]:
        """Cari memori relevan untuk *query* dan kembalikan list string."""
        await self.init()
        try:
            result = await self.memory.search(query=query, user_id=user_id, limit=limit)
            return [item["memory"] for item in result.get("results", [])]
        except Exception as e:
            logger.error(f"Gagal search memory: {e}")
            return []

    async def add_conversation(
        self, messages: List[Dict[str, str]], *, user_id: str = "default"
    ) -> None:
        """Simpan *messages* (urutan dialog) ke memori."""
        await self.init()
        try:
            await self.memory.add(messages=messages, user_id=user_id)
        except Exception as e:
            logger.error(f"Gagal menambah memori: {e}")

    async def chat_with_memories(
        self, llm_client, *, user_message: str, user_id: str = "default"
    ) -> str:
        """Contoh util satu-pintu: ambil memories, panggil LLM, simpan hasil."""
        memories = await self.get_memories(user_message, user_id=user_id)
        memories_block = "\n".join(f"- {m}" for m in memories) or "[Tidak ada]"

        system_prompt = (
            "Anda adalah ProjectWise, asisten AI presales & PM.\n"
            f"Memori relevan:\n{memories_block}"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        response = await llm_client.responses.create(
            model=self._config["llm"]["config"]["model"],
            input=messages,
        )
        assistant_reply = response.output_text or ""
        messages.append({"role": "assistant", "content": assistant_reply})  # type: ignore
        await self.add_conversation(messages, user_id=user_id)
        return assistant_reply
