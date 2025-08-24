# projectwise/services/mcp/adapter.py
from __future__ import annotations

from typing import Any, Dict, List, Callable, Awaitable
from quart import Quart
from projectwise.utils.logger import get_logger

logger = get_logger(__name__)


class MCPToolAdapter:
    """
    Adapter ringan untuk akses MCP.
    - Tidak mengubah/menormalisasi schema maupun hasil tool.
    - Satu-satunya pintu untuk: get_tools() dan call_tool().
    """

    def __init__(self, app: Quart) -> None:
        self.app = app

    async def _acquire_mcp(self):
        mcp = self.app.extensions.get("mcp")
        status = self.app.extensions.get("mcp_status", {}) or {}
        if not mcp or not status.get("connected"):
            raise RuntimeError("MCP belum siap/terhubung.")
        return mcp

    # === PANGGIL TOOL TANPA NORMALISASI HASIL ===
    async def call_tool(self, name: str, args: Dict[str, Any]) -> Any:
        client = await self._acquire_mcp()
        logger.info("Eksekusi MCP tool: %s | args=%s", name, args)
        return await client.call_tool(name, args or {})

    # === AMBIL TOOLS APA ADANYA DARI MCP ===
    async def get_tools(self) -> List[Dict[str, Any]]:
        client = await self._acquire_mcp()
        tools: List[Dict[str, Any]] = getattr(client, "tool_cache", []) or []
        logger.info("MCP tool_cache terdeteksi: %d tool.", len(tools))
        return tools

    # === OPSIONAL: KONVERSI SHAPE KE OPENAI "tools" TANPA UBAH SCHEMA ===
    async def get_openai_tools(self) -> List[Dict[str, Any]]:
        """
        Kembalikan daftar tools format OpenAI:
        [{"type":"function","function":{"name":..,"description":..,"parameters":<inputSchema>}}]
        Tanpa menyetel additionalProperties, tanpa memangkas/menambah field.
        """
        tools = await self.get_tools()
        tools_openai: List[Dict[str, Any]] = []
        for item in tools:
            name = item.get("name")
            if not name:
                continue
            fn: Dict[str, Any] = {"name": name}
            if item.get("description"):
                fn["description"] = item["description"]
            if "inputSchema" in item and item["inputSchema"] is not None:
                fn["parameters"] = item["inputSchema"]  # pass-through apa adanya

            tools_openai.append({"type": "function", "function": fn})
        return tools_openai

    # === OPSIONAL: REGISTRY MAP UNTUK PLANNER ===
    async def build_registry_map(
        self,
    ) -> Dict[str, Callable[[Dict[str, Any]], Awaitable[Any]]]:
        """
        Peta {tool_name: async(args)->Any} agar modul lain bisa memanggil langsung.
        """
        tools = await self.get_tools()
        registry: Dict[str, Callable[[Dict[str, Any]], Awaitable[Any]]] = {}

        for item in tools:
            nm = item.get("name")
            if not nm:
                continue

            async def _bound(args: Dict[str, Any], _nm=nm) -> Any:
                return await self.call_tool(_nm, args)

            registry[nm] = _bound

        return registry
