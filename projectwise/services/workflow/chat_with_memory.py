"""
Chat service with integrated long‑term and short‑term memory.

The :class:`ChatWithMemory` class composes a long‑term memory store
(vector store) with a short‑term memory store (SQL database) and an
LLM client.  When a user sends a message it retrieves the latest
messages from short‑term memory, searches the vector store for
relevant documents, builds a prompt that includes both contexts, calls
the LLM and finally persists the conversation back to both memories.

This service is designed to be instantiated once per application and
reused across requests.  Call :meth:`init` once after construction to
prepare the underlying stores.
"""

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
        stm_db_url: str,
        llm_model: Optional[str] = None,
        max_history: int = 20,
    ) -> None:
        # Memory managers
        self.long_term = Mem0Manager(service_configs)
        self.short_term = ShortTermMemory(stm_db_url, max_history=max_history)

        # LLM client
        self.client = AsyncOpenAI()
        self.llm_model = llm_model or service_configs.llm_model

    async def init(self) -> None:
        """Initialise all memory backends.  Must be awaited once."""
        await self.long_term.init()
        await self.short_term.init_models()

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


# ==== Example usage ====
async def main() -> None:
    from projectwise.config import ServiceConfigs

    service_configs = ServiceConfigs()  # load from .env
    chat_ai = ChatWithMemory(
        service_configs,
        stm_db_url="sqlite+aiosqlite:///./short_term.db",
    )
    await chat_ai.init()
    while True:
        user_input = input("You: ")
        if user_input.lower() in {"exit", "quit"}:
            break
        reply = await chat_ai.chat(user_id="user123", user_message=user_input)
        print(f"AI: {reply}")


if __name__ == "__main__":
    asyncio.run(main())