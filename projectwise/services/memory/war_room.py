# projectwise/services/memory/war_room.py
from __future__ import annotations

from datetime import datetime
from collections import defaultdict
from mem0 import AsyncMemory
from mem0.configs.base import MemoryConfig, VectorStoreConfig, LlmConfig, EmbedderConfig

from projectwise.services.llm_chain.llm_chains import LLMChains
from projectwise.config import ServiceConfigs
from projectwise.utils.logger import get_logger

logger = get_logger(__name__)
settings = ServiceConfigs()

# Shared project context
RUN_ID = "project-war-room"

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

        Return ONLY the JSON as shown.
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
        Return ONLY the JSON described above.
        """

custom_config = MemoryConfig(
    vector_store=VectorStoreConfig(
        provider="qdrant",
        config={
            "collection_name": getattr(settings, "collection_name", "mem0"),
            "host": getattr(settings, "qdrant_host", "localhost"),
            "port": getattr(settings, "qdrant_port", 6333),
        },
    ),
    llm=LlmConfig(
        provider="openai",
        config={
            "openai_base_url": settings.llm_base_url,
            "api_key": settings.llm_api_key,
            "model": settings.llm_model,
        },
    ),
    embedder=EmbedderConfig(
        provider="openai",
        config={
            "api_key": settings.embedding_model_api_key,
            "model": settings.embedding_model,
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

mem = AsyncMemory(config=custom_config)


class CollaborativeAgent:
    def __init__(self, run_id):
        self.run_id = run_id
        self.mem = mem

    async def add_message(self, role, name, content):
        msg = {"role": role, "name": name, "content": content}
        await self.mem.add([msg], run_id=self.run_id, infer=False)

    async def brainstorm(self, prompt):
        # Get recent messages for context
        memories = await self.mem.search(prompt, run_id=self.run_id, limit=5)
        memories = memories.get("results", [])
        context = "\n".join(
            f"- {m['memory']} (by {m.get('actor_id', 'Unknown')})" for m in memories
        )
        client = LLMChains(prefer="chat")
        messages = [
            {"role": "system", "content": "You are a helpful project assistant."},
            {"role": "user", "content": f"Prompt: {prompt}\nContext:\n{context}"},
        ]
        reply = await client.chat_completions_text(messages=messages)
        await self.add_message("assistant", "assistant", reply)
        return reply

    async def get_all_messages(self):
        message = await self.mem.get_all(run_id=self.run_id)
        return await message["results"]

    async def print_sorted_by_time(self):
        messages = await self.get_all_messages()
        messages.sort(key=lambda m: m.get("created_at", ""))
        print("\n--- Messages (sorted by time) ---")
        for m in messages:
            who = m.get("actor_id") or "Unknown"
            ts = m.get("created_at", "Timestamp N/A")
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+07:00"))
                ts_fmt = dt.strftime("%d-%m-%Y %H:%M:%S")
            except Exception:
                ts_fmt = ts
            print(f"[{ts_fmt}] [{who}] {m['memory']}")

    async def print_grouped_by_actor(self):
        messages = await self.get_all_messages()
        grouped = defaultdict(list)
        for m in messages:
            grouped[m.get("actor_id") or "Unknown"].append(m)
        print("\n--- Messages (grouped by actor) ---")
        for actor, mems in grouped.items():
            print(f"\n=== {actor} ===")
            for m in mems:
                ts = m.get("created_at", "Timestamp N/A")
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+07:00"))
                    ts_fmt = dt.strftime("%d-%m-%Y %H:%M:%S")
                except Exception:
                    ts_fmt = ts
                print(f"[{ts_fmt}] {m['memory']}")


# Example usage
async def main():
    agent = CollaborativeAgent(RUN_ID)
    await agent.add_message(
        "user", "alice", "Let's list tasks for the new landing page."
    )
    await agent.add_message("user", "bob", "I'll own the hero section copy.")
    await agent.add_message("user", "carol", "I'll choose product screenshots.")

    # Brainstorm with context
    print(
        "\nAssistant reply:\n",
        await agent.brainstorm("What are the current open tasks?"),
    )

    # Print all messages sorted by time
    await agent.print_sorted_by_time()

    # Print all messages grouped by actor
    await agent.print_grouped_by_actor()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
