# projectwise/services/workflow/chat_with_memory.py
from __future__ import annotations

from typing import Optional, List, Dict, Any
from openai import AsyncOpenAI, APIConnectionError

from projectwise.utils.logger import get_logger
from projectwise.services.memory.long_term_memory import Mem0Manager
from projectwise.services.memory.short_term_memory import ShortTermMemory
from projectwise.services.llm_chain.llm_chains import LLMChains
from projectwise.config import ServiceConfigs


logger = get_logger(__name__)
settings = ServiceConfigs()
LLM = LLMChains(prefer="chat")


class ChatWithMemory:
    """
    War Room chat helper: menggabungkan Long‑Term Memory (mem0) + Short‑Term Memory (SQLite)
    dengan pemanggilan LLM yang konsisten dengan stack ProjectWise.

    Gunakan `from_quart_app(app, ...)` agar dependensi diambil dari `app.extensions`.
    """

    def __init__(
        self,
        *,
        service_configs: ServiceConfigs,
        long_term: Mem0Manager,
        short_term: ShortTermMemory,
        llm: Optional[AsyncOpenAI] = None,
        llm_model: Optional[str] = None,
        max_history: int = 20,
    ) -> None:
        # Dependensi wajib (diinjeksikan)
        self.service_configs = service_configs
        self.long_term = long_term
        self.short_term = short_term

        # LLM
        self.llm = llm or AsyncOpenAI(
            api_key=self.service_configs.llm_api_key,
            base_url=self.service_configs.llm_base_url,
        )
        self.llm_model = llm_model or service_configs.llm_model
        self.max_history = max_history

        logger.info(
            "ChatWithMemory initialized | model=%s | max_history=%d",
            self.llm_model,
            self.max_history,
        )

    # ---------- Factory agar konsisten dengan extensions ----------
    @classmethod
    def from_quart_app(
        cls,
        app,
        *,
        llm: Optional[AsyncOpenAI] = None,
        llm_model: Optional[str] = None,
        max_history: int = 20,
    ) -> "ChatWithMemory":
        service_configs: ServiceConfigs = app.extensions["service_configs"]  # type: ignore
        long_term: Mem0Manager = app.extensions["long_term_memory"]  # type: ignore
        short_term: ShortTermMemory = app.extensions["short_term_memory"]  # type: ignore

        # Pastikan LTM siap (idempotent)
        # Mem0Manager.init() sudah dipanggil saat init_extensions, tapi aman jika dipanggil lagi
        # tanpa await di sini untuk menghindari blocking tak perlu.

        return cls(
            service_configs=service_configs,
            long_term=long_term,
            short_term=short_term,
            llm=llm,
            llm_model=llm_model or service_configs.llm_model,
            max_history=max_history,
        )

    # ---------- Util internal ----------
    @staticmethod
    def _shape(role: str, content: str) -> Dict[str, Any]:
        return {"role": role, "content": content}

    # ---------- API utama ----------
    async def chat(
        self,
        *,
        user_id: str,
        user_message: str,
        assistant_message: Optional[str] = None,
    ) -> str:
        """
        Kirim pesan ke LLM dengan memanfaatkan STM & LTM.
        Alur:
        1. Ambil LTM (mem0) relevan berdasarkan user_message.
        2. Ambil STM (SQLite) berdasarkan user_id.
        3. Bentuk messages: system (LTM) + history (STM) + user_message + (opsional) assistant_message.
        4. Panggil LLM chat completions.
        5. Kembalikan balasan assistant.

        Return:
            string balasan dari LLM
        """
        logger.info(
            "chat start | user=%s | msg.len=%d",
            user_id,
            len(user_message or ""),
        )
        # Siapkan messages Relevan Memory, history, user message, (opsional) assistant message
        # 1. Ambil LTM (mem0)
        ltm = await self.long_term.get_memories_v2(
            query=user_message, user_id=user_id, limit=5
        )
        # 2. Ambil STM (SQLite)
        messages: List[Dict[str, Any]] = await self.short_term.get_history(
            user_id, limit=5
        )
        # 3. Bentuk messages
        messages.insert(0, self._shape("system", f"Relevan memory:\n{ltm}"))
        # 4. User message
        messages.append(self._shape("user", user_message))
        # 5. Assistant message (opsional)
        if assistant_message:
            messages.append(
                self._shape("assistant", f"Tool output: {assistant_message}")
            )

        # Panggil LLM
        try:
            resp = await LLM.chat_completions_text(messages=messages)
            assistant_reply = resp.strip() or "[Tidak ada respon]"
        except APIConnectionError:
            logger.error("LLM APIConnectionError.")
            human = "LLM API Connection Error. Silakan coba lagi."
            raise RuntimeError(human)
        except Exception as e:
            logger.exception("[war_room] LLM error")
            assistant_reply = f"Maaf, terjadi kesalahan saat memproses jawaban: {e}"

        logger.info("chat done | reply.len=%d", len(assistant_reply))
        return assistant_reply
