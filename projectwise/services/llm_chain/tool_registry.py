# projectwise/projectwise/services/llm_chain/tool_registry.py
from __future__ import annotations

from typing import Any, Dict, List, Tuple, Callable, Awaitable
from jsonschema import validate, ValidationError


ToolExecutor = Callable[
    [str, Dict[str, Any]], Awaitable[Dict[str, Any]] | Dict[str, Any]
]

# class ToolExecutor(Protocol):
#     async def call_tool(self, name: str, args: Dict[str, Any]) -> Any: ...
#     async def get_tools(self) -> List[Dict[str, Any]]: ...



def _ensure_additional_properties_false(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Tambal schema agar ketat."""
    if isinstance(schema, dict):
        if "type" not in schema:
            schema = {"type": "object", **schema}
        if "properties" not in schema and schema.get("type") == "object":
            schema = {**schema, "properties": {}}
        if "additionalProperties" not in schema and schema.get("type") == "object":
            schema = {**schema, "additionalProperties": False}
    return schema


def normalize_openai_tools_from_cache(
    tool_cache: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Konversi item MCP tool_cache → format OpenAI Chat Completions:
    [{ "type":"function", "function": { "name","description","parameters":<JSONSchema> } }]
    """
    tools: List[Dict[str, Any]] = []
    for item in tool_cache or []:
        params = item.get("parameters") or {"type": "object", "properties": {}}
        params = _ensure_additional_properties_false(params)
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": item["name"],
                    "description": item.get("description", ""),
                    "parameters": params,
                },
            }
        )
    return tools


def build_mcp_tooling(
    mcp,
) -> Tuple[
    List[Dict[str, Any]],
    ToolExecutor,
    Dict[str, Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]],
]:
    """
    Bangun:
      - TOOLS_JSONSCHEMA: daftar tools untuk OpenAI Chat.
      - TOOL_EXECUTOR: dispatcher (name, args) -> dict
      - TOOL_REGISTRY: map "name" -> async (args)->dict
    """
    # Pastikan tool_cache sudah ada (MCPClient __aenter__ memanggil refresh awal)
    # (lihat client.__aenter__ dan _refresh_tool_cache)  # referensi di penjelasan

    tools_openai = normalize_openai_tools_from_cache(mcp.tool_cache)

    async def tool_executor(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Dispatcher eksekusi tool MCP:
        - Validasi args terhadap JSON Schema tool (dari tool_cache)
        - Panggil mcp.call_tool(name, args)
        - Pastikan output berbentuk dict dengan 'status' & 'message'
        """
        spec = next((t for t in mcp.tool_cache if t.get("name") == name), None)
        if not spec:
            return {"status": "error", "message": f"Tool '{name}' tidak ditemukan."}

        schema = spec.get("parameters") or {"type": "object", "properties": {}}
        schema = _ensure_additional_properties_false(schema)

        # Validasi argumen di sisi klien untuk fail-fast
        try:
            validate(instance=args or {}, schema=schema)
        except ValidationError as ve:
            return {
                "status": "error",
                "message": f"Argumen untuk '{name}' tidak valid: {ve.message}",
                "meta": {"path": list(ve.path), "schema_path": list(ve.schema_path)},
            }

        # Eksekusi ke MCP server
        try:
            result = await mcp.call_tool(name, args)
        except Exception as e:
            return {"status": "error", "message": f"Gagal eksekusi '{name}': {e}"}

        # Normalisasi hasil → selalu dict + status/message
        if not isinstance(result, dict):
            result = {"data": result}
        result.setdefault("status", "success")
        result.setdefault("message", "ok")
        return result

    # TOOL_REGISTRY: bind per-nama (hindari late-binding dengan default arg)
    registry: Dict[str, Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]] = {}
    for item in mcp.tool_cache:
        nm = item["name"]

        async def _bound(args: Dict[str, Any], _nm=nm) -> Dict[str, Any]:
            return await tool_executor(_nm, args)

        registry[nm] = _bound

    return tools_openai, tool_executor, registry
