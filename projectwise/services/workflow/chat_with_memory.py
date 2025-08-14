from __future__ import annotations

import asyncio
from typing import Optional
from openai import AsyncOpenAI

from projectwise.services.memory.long_term_memory import Mem0Manager
from projectwise.services.memory.short_term_memory import ShortTermMemory


class ChatWithMemory:
    """High‑level chat service combining long‑term and short‑term memory."""

    def __init__(
        self,
        service_configs,
        stm_db_url: str | None = None,
        llm_model: Optional[str] = None,
        max_history: int = 20,
        *,
        long_term: Mem0Manager | None = None,
        short_term: ShortTermMemory | None = None,
    ) -> None:
        # Pakai instance injeksi jika ada, kalau tidak buat baru (backward-compatible)
        self.long_term = long_term or Mem0Manager(service_configs)
        if short_term is not None:
            self.short_term = short_term
        else:
            if not stm_db_url:
                raise ValueError("stm_db_url wajib jika short_term tidak diinjeksikan.")
            self.short_term = ShortTermMemory(stm_db_url, max_history=max_history)

        self.client = AsyncOpenAI()
        self.llm_model = llm_model or service_configs.llm_model


    async def chat(self, user_id: str, user_message: str) -> str:
        """Send a message to the AI assistant and return its reply."""
        # Retrieve chat history for context
        stm_history = await self.short_term.get_history(user_id)
        stm_block = stm_history or "[Tidak ada riwayat percakapan]"
        
        # Retrieve relevant long‑term memories
        ltm_results = await self.long_term.get_memories(user_message, user_id=user_id)
        ltm_block = (
            "\n".join(f"- {m}" for m in ltm_results) or "[Tidak ada memori relevan]"
        )
        
        # Build system prompt
        system_prompt = (
            "Anda adalah ProjectWise, asisten AI presales & PM.\n"
            "Gunakan informasi berikut untuk menjawab dengan akurat.\n\n"
            f"### Long Term Memory:\n{ltm_block}\n\n"
            f"### Short Term Memory:\n{stm_block}"
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        
        # Call the language model
        response = await self.client.responses.create(
            model=self.llm_model,
            input=messages,  # type: ignore
        )
        assistant_reply = response.output_text or "[Tidak ada respon]"
        
        # Persist to short‑term memory
        await self.short_term.save(user_id, "user", user_message)
        await self.short_term.save(user_id, "assistant", assistant_reply)
        # Persist to long‑term memory
        await self.long_term.add_conversation(
            messages + [{"role": "assistant", "content": assistant_reply}],
            user_id=user_id,
        )
        return assistant_reply
