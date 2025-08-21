# projectwise/services/mcp/adapter.py
from __future__ import annotations

from typing import Any, Dict, List, Protocol
from quart import Quart
from projectwise.utils.logger import get_logger

from projectwise.utils.llm_io import (
    truncate_args,
)


logger = get_logger(__name__)


# =========================
# 1) Kontrak eksekutor tool
# =========================
class ToolExecutor(Protocol):
    async def call_tool(self, name: str, args: Dict[str, Any]) -> Any: ...
    async def get_tools(self) -> List[Dict[str, Any]]: ...


# =========================
# 2) MCP Adapter (sederhana)
# =========================
class MCPToolAdapter:
    """
    Adapter yang mengeksekusi MCP tool via instance di app.extensions.

    Catatan:
    - Tidak membuat MCPClient baru.
    - mcp_status di extensions.py.
    - Menyediakan get_tools() agar ReflectionActor tidak bergantung pada detail internal.
    """

    def __init__(self, app: Quart) -> None:
        self.app = app

    async def _acquire_mcp(self):
        # Pastikan state tersedia
        if "mcp" not in self.app.extensions or "mcp_status" not in self.app.extensions:
            raise RuntimeError("MCP belum diinisialisasi di app.extensions.")

        client = self.app.extensions.get("mcp")
        status: dict = self.app.extensions["mcp_status"]

        # Jangan auto‑connect di sini. Hormati kontrol via /mcp/connect
        if client is None or not status.get("connected"):
            raise RuntimeError(
                "MCP belum terhubung. Silakan klik 'Connect' atau panggil endpoint /mcp/connect terlebih dahulu."
            )
        return client

    async def call_tool(self, name: str, args: Dict[str, Any]) -> Any:
        client = await self._acquire_mcp()
        logger.info("Eksekusi MCP tool: %s | args=%s", name, truncate_args(args))
        return await client.call_tool(name, args)

    async def get_tools(self) -> List[Dict[str, Any]]:
        """Kembalikan daftar tool MCP (gunakan cache bila tersedia)."""
        client = await self._acquire_mcp()
        tools: List[Dict[str, Any]] = getattr(client, "tool_cache", []) or []
        logger.info("MCP tool_cache terdeteksi: %d tool.", len(tools))
        return tools
