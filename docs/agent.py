# projectwise/services/workflow/agent.py
from __future__ import annotations

from projectwise.services.mcp.client import MCPClient
from projectwise.services.llm_chain.tool_registry import build_mcp_tooling
from projectwise.services.llm_chain.llm_chains import LLMChains


async def run():
    async with MCPClient() as mcp:
        TOOLS_JSONSCHEMA, TOOL_EXECUTOR, TOOL_REGISTRY = build_mcp_tooling(mcp)

        llm = LLMChains(model=mcp.model)  # atau pakai Settings Anda
        messages = [
            {"role": "system", "content": "Anda adalah AI agent perusahaan."},
            {"role": "user", "content": "Cari tiket termurah ke Bali minggu depan."},
        ]

        # Jalur produksi → prefer="chat"
        result = await llm.run_function_call_roundtrip(
            messages=messages,
            tools=TOOLS_JSONSCHEMA,
            tool_executor=TOOL_EXECUTOR, # type: ignore
            prefer="chat",
            max_hops=4,
            tool_choice="auto",
        )

        if result["status"] == "success":
            print("Final:", result.get("data"))
        else:
            print("Error:", result["message"])
