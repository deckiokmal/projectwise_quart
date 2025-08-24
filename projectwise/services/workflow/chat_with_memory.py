# projectwise/services/workflow/chat_with_memory.py
from __future__ import annotations

from typing import Optional, List, Dict, Any
from openai import AsyncOpenAI, APIConnectionError

from projectwise.utils.logger import get_logger
from projectwise.services.memory.long_term_memory import Mem0Manager
from projectwise.services.memory.short_term_memory import ShortTermMemory
from projectwise.services.llm_chain.llm_utils import build_context_blocks_memory
from projectwise.services.llm_chain.llm_chains import LLMChains
from projectwise.config import ServiceConfigs


logger = get_logger(__name__)
settings = ServiceConfigs()
LLM = LLMChains(prefer="chat")

try:
    if str(settings.llm_model).lower().startswith("gpt"):
        AsyncOpenAI(api_key=settings.llm_api_key)
    else:
        AsyncOpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)
except Exception as e:
    logger.exception(
        "Gagal inisialisasi AsyncOpenAI (cek LLM_API_KEY / base_url): %s", e
    )


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
        Kirim pesan ke LLM dalam mode War Room:
        - Menyuntik blok STM/LTM ke system prompt (briefing).
        - Opsional menyertakan `assistant_message` (mis. output tool) bila ada.
        - Menyimpan percakapan ke STM & LTM.

        Return:
            string balasan dari LLM
        """
        logger.info(
            "[war_room] chat start | user=%s | msg.len=%d",
            user_id,
            len(user_message or ""),
        )

        system_prompt = await build_context_blocks_memory(
            short_term=self.short_term,
            long_term=self.long_term,
            user_id=user_id,
            user_message=user_message,
            max_history=self.max_history,
            # prompt_instruction=PROMPT_WAR_ROOM(),
        )

        messages: List[Dict[str, Any]] = [
            self._shape("system", system_prompt),
            self._shape("user", user_message),
        ]
        if assistant_message:  # hanya jika disuplai
            messages.append(
                self._shape("assistant", f"Tool output: {assistant_message}")
            )

        # Panggil LLM (Responses API)
        try:
            # resp = await self.llm.responses.create(
            #     model=self.llm_model,
            #     input=messages,  # type: ignore
            #     temperature=self.service_configs.llm_temperature,
            # )
            # assistant_reply = (resp.output_text or "").strip() or "[Tidak ada respon]"
            resp = await LLM.chat_completions_text(messages=messages)
            assistant_reply = resp.strip() or "[Tidak ada respon]"
        except APIConnectionError:
            logger.error("LLM APIConnectionError.")
            human = "LLM API Connection Error. Silakan coba lagi."
            raise RuntimeError(human)
        except Exception as e:
            logger.exception("[war_room] LLM error")
            assistant_reply = f"Maaf, terjadi kesalahan saat memproses jawaban: {e}"

        # Persist memori (best-effort; jangan memblokir error ke user)
        try:
            await self.short_term.save(user_id, "user", user_message)
            await self.short_term.save(user_id, "assistant", assistant_reply) # type: ignore
        except Exception:
            logger.exception("[war_room] gagal simpan ke ShortTermMemory")

        try:
            # convo = messages + [self._shape("assistant", assistant_reply)]
            await self.long_term.add_memory(user_message, user_id=user_id)
        except Exception:
            logger.exception("[war_room] gagal simpan ke LongTermMemory")

        logger.info("[war_room] chat done | reply.len=%d", len(assistant_reply)) # type: ignore
        return assistant_reply # type: ignore


# ---- Contoh pemakaian (opsional) ----
"""
# Dari dalam handler Quart:
war = ChatWithMemory.from_quart_app(current_app)
reply = await war.chat(user_id="u-123", user_message="Status risiko jaringan site A?")
"""
