# projectwise/services/workflow/llm_reasoning_action.py
import asyncio
from typing import Any, Dict
from quart import current_app
from mcp import JSONRPCError
from projectwise.services.mcp.client import MCPClient


class LLMWithMCPTools:
    """
    LLM agent yang bisa memanggil MCP tools via MCPClient.call_tool().
    Menggunakan strategi recovery untuk retry dan fallback.
    """

    def __init__(self, mcp_client: MCPClient):
        service_configs = current_app.extensions["service_configs"]
        self.mcp_client = mcp_client
        self.llm = mcp_client.llm
        self.llm_model = service_configs.llm_model

    async def ensure_connected(self):
        """Pastikan MCP client terkoneksi sebelum dipakai."""
        if not self.mcp_client._connected:
            await self.mcp_client.connect()

    async def call_mcp_tool_with_recovery(
        self,
        tool_name: str,
        args: Dict[str, Any],
        retries: int = 2,
        retry_delay: float = 2.0,
    ) -> Any:
        """
        Panggil tool MCP dengan retry logic & error handling.
        """
        await self.ensure_connected()

        attempt = 0
        while attempt <= retries:
            try:
                return await self.mcp_client.call_tool(tool_name, args)
            except JSONRPCError as rpc_err:  # type: ignore  # noqa: F841
                # Error dari server MCP
                if attempt >= retries:
                    raise
                attempt += 1
                await asyncio.sleep(retry_delay)
            except Exception as e:  # noqa: F841
                # Error lain (misal koneksi putus)
                if attempt >= retries:
                    raise
                attempt += 1
                await asyncio.sleep(retry_delay)

    async def llm_decide_and_execute_tool(
        self, user_prompt: str, available_tools: Dict[str, str]
    ) -> str:
        """
        Gunakan LLM untuk memilih tool MCP yang tepat, lalu eksekusi.
        available_tools: dict {tool_name: deskripsi}
        """
        # Buat prompt untuk LLM
        system_prompt = (
            "Anda adalah AI agent yang dapat memanggil MCP tools berikut:\n"
            + "\n".join([f"- {name}: {desc}" for name, desc in available_tools.items()])
            + "\n\n"
            "Pilih tool terbaik untuk menyelesaikan perintah user."
            " Balas hanya dengan JSON berisi: {tool_name: string, args: object}."
        )

        response = await self.llm.responses.create(
            model=self.llm_model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

        # Parsing output JSON
        try:
            import json

            decision = json.loads(response.output_text)
            tool_name = decision.get("tool_name")
            args = decision.get("args", {})
        except Exception:
            return "[Error] Gagal membaca keputusan tool dari LLM."

        if not tool_name:
            return "[Error] LLM tidak memilih tool."

        # Eksekusi tool dengan recovery
        try:
            tool_result = await self.call_mcp_tool_with_recovery(tool_name, args)
            return f"[Tool: {tool_name}]\n{tool_result}"
        except Exception as e:
            return f"[Error] Gagal memanggil tool {tool_name}: {e}"


# ==== Contoh penggunaan ====
async def main():
    async with MCPClient() as mcp:
        agent = LLMWithMCPTools(mcp)
        tools = {
            t["function"]["name"]: t["function"]["description"] for t in mcp.tool_cache
        }

        hasil = await agent.llm_decide_and_execute_tool(
            "Cari data cuaca untuk Jakarta", available_tools=tools
        )
        print(hasil)


if __name__ == "__main__":
    asyncio.run(main())
