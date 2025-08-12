# projectwise/services/workflow/manager.py
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from jsonschema import Draft202012Validator, FormatChecker
from helper import get_validator, make_fingerprint, CircuitBreaker
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)
from projectwise.utils.logger import get_logger


logger = get_logger(__name__)
format_checker = FormatChecker()


# =========================
# Data models (typed)
# =========================


@dataclass(frozen=True)
class ValidationErrorItem:
    path: str
    message: str
    schema_path: str
    validator: str


@dataclass(frozen=True)
class ToolEntry:
    name: str
    title: Optional[str]
    description: Optional[str]
    input_schema: Dict[str, Any]
    function: Dict[str, Any]  # {"name","description","parameters"}


@dataclass(frozen=True)
class ToolCallRequest:
    tool: str
    args: Dict[str, Any]
    timeout_s: Optional[float] = None
    retry: int = 0  # override default retry if needed


@dataclass
class ToolCallResponse:
    success: bool
    result: Any = None
    reason: Optional[str] = None
    validation_errors: List[ValidationErrorItem] = field(default_factory=list)
    example_schema: Optional[Dict[str, Any]] = None
    suggestion: Optional[str] = None
    coerced_args: Optional[Dict[str, Any]] = None
    tool_name: Optional[str] = None


# ==================================
# JSON Schema validation & coercion
# ==================================


def _coerce_basic_types(arg: Any, schema: Dict[str, Any]) -> Any:
    """
    Coercion ringan & aman (level-1):
      - integer/number/boolean dari string
      - array/object dari string JSON (jika valid)
    Hindari over-coercion. Biarkan invalid tetap gagal agar cepat diperbaiki.
    """
    import json as _json

    t = schema.get("type")
    if t == "integer":
        if isinstance(arg, str) and arg.strip().isdigit():
            return int(arg)
    if t == "number":
        if isinstance(arg, str):
            try:
                return float(arg)
            except ValueError:
                pass
    if t == "boolean":
        if isinstance(arg, str):
            s = arg.strip().lower()
            if s in ("true", "1", "yes", "y"):
                return True
            if s in ("false", "0", "no", "n"):
                return False
    if t == "array":
        if isinstance(arg, str):
            try:
                x = _json.loads(arg)
                if isinstance(x, list):
                    return x
            except Exception:
                pass
    if t == "object":
        if isinstance(arg, str):
            try:
                x = _json.loads(arg)
                if isinstance(x, dict):
                    return x
            except Exception:
                pass
    return arg


def _apply_shallow_coercion(
    args: Dict[str, Any], schema: Dict[str, Any]
) -> Dict[str, Any]:
    if not isinstance(args, dict):
        return args
    props = (schema or {}).get("properties") or {}
    out = dict(args)
    for k, v in list(out.items()):
        sub_schema = props.get(k, {})
        out[k] = _coerce_basic_types(v, sub_schema)
    return out


def validate_tool_args(
    tool_name: str,
    args: Dict[str, Any],
    input_schema: Dict[str, Any],
    *,
    coerce: bool = True,
) -> Tuple[bool, List[ValidationErrorItem], Dict[str, Any], Dict[str, Any]]:
    """
    Validasi Draft 2020-12.
    Return: ok, errors, coerced_args, normalized_schema
    """
    schema = input_schema or {}
    if "type" not in schema:
        schema = {"type": "object", **schema}

    fingerprint = make_fingerprint(schema)
    validator = get_validator(fingerprint)

    coerced = _apply_shallow_coercion(args, schema) if coerce else args
    validator = Draft202012Validator(schema, format_checker=format_checker)

    errors: List[ValidationErrorItem] = []
    for e in validator.iter_errors(coerced):
        errors.append(
            ValidationErrorItem(
                path=".".join(str(x) for x in e.path) or "<root>",
                message=e.message,
                schema_path="/".join(str(x) for x in e.schema_path),
                validator=str(e.validator),
            )
        )

    return (len(errors) == 0, errors, coerced, schema)


# =========================
# AIManager
# =========================


class AIManager:
    """
    AI Agent Manager:
    - Sinkronisasi cache tool dari MCP (name/desc/schema/LLM-function)
    - Validasi argumen terhadap JSON Schema sebelum eksekusi
    - Eksekusi tool MCP dengan retry & timeout
    - Event hooks/telemetry untuk observability
    - Siap diintegrasikan dengan planner/LLM reasoning di modul terpisah
    """

    # Defaults (override lewat __init__)
    DEFAULT_TIMEOUT_S: float = 30.0
    DEFAULT_MAX_RETRY: int = 2

    def __init__(
        self,
        mcp_client: Any,
        *,
        enable_coercion: bool = True,
        default_timeout_s: float | None = None,
        default_max_retry: int | None = None,
        on_event: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
    ) -> None:
        """
        mcp_client: objek klien MCP yang menyediakan:
            - session (opsional)
            - list_tools() → metadata
            - call_tool_mcp(tool_name: str, args: Dict[str, Any]) → Any
            - (opsional) _refresh_tool_cache() jika sudah ada
        on_event: hook async dipanggil dengan (event_name, payload) untuk telemetry.
        """
        self.mcp_client = mcp_client
        self.enable_coercion = enable_coercion
        self.DEFAULT_TIMEOUT_S = default_timeout_s or self.DEFAULT_TIMEOUT_S
        self.DEFAULT_MAX_RETRY = (
            default_max_retry
            if default_max_retry is not None
            else self.DEFAULT_MAX_RETRY
        )
        self.on_event = on_event
        self._tool_cache: Dict[str, ToolEntry] = {}

    # --------- Observability helpers ---------
    async def _emit(self, event: str, payload: Dict[str, Any]) -> None:
        if self.on_event:
            try:
                await self.on_event(event, payload)
            except Exception:
                logger.debug("on_event hook error for '%s'", event, exc_info=True)

    # --------- Tools cache ---------
    async def refresh_tools(self) -> None:
        """
        Muat ulang daftar tools dari MCP server dan rebuild cache internal.
        Harus dipanggil setelah koneksi session MCP aktif.
        """
        # Jika mcp_client sudah punya _refresh_tool_cache & tool_cache → gunakan itu
        if hasattr(self.mcp_client, "_refresh_tool_cache"):
            await self.mcp_client._refresh_tool_cache()  # type: ignore

            tool_cache = getattr(self.mcp_client, "tool_cache", None)
            if isinstance(tool_cache, list):
                self._rebuild_cache_from_raw_list(tool_cache)
                await self._emit("tools.refreshed", {"count": len(self._tool_cache)})
                return

        # Fallback: panggil list_tools() manual jika tidak ada method di atas
        try:
            resp = await self.mcp_client.session.list_tools()  # type: ignore
            raw = []
            for t in resp.tools:
                raw.append(
                    {
                        "name": t.name,
                        "title": getattr(t, "title", None),
                        "description": t.description,
                        "inputSchema": t.inputSchema,
                        "function": {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.inputSchema,
                        },
                    }
                )
            self._rebuild_cache_from_raw_list(raw)
            await self._emit("tools.refreshed", {"count": len(self._tool_cache)})
        except Exception as e:
            logger.warning("refresh_tools failed: %s", e, exc_info=True)
            await self._emit("tools.refresh_failed", {"error": str(e)})

    def _rebuild_cache_from_raw_list(self, raw: List[Dict[str, Any]]) -> None:
        cache: Dict[str, ToolEntry] = {}
        for item in raw:
            name = item.get("name")
            if not name:
                continue
            cache[name] = ToolEntry(
                name=name,
                title=item.get("title"),
                description=item.get("description"),
                input_schema=item.get("inputSchema")
                or item.get("function", {}).get("parameters")
                or {},
                function=item.get("function") or {},
            )
        self._tool_cache = cache
        logger.debug("Tool cache built with %d tools", len(cache))

    def get_tool_entry(self, tool_name: str) -> Optional[ToolEntry]:
        return self._tool_cache.get(tool_name)

    def list_tools(self) -> List[str]:
        return list(self._tool_cache.keys())

    # --------- Validation + Execution ---------
    async def execute_tool(
        self,
        req: ToolCallRequest,
    ) -> ToolCallResponse:
        """
        Validasi argumen → eksekusi tool MCP dengan retry & timeout.
        """
        tool = self.get_tool_entry(req.tool)
        if not tool:
            return ToolCallResponse(
                success=False,
                reason=f"Tool '{req.tool}' tidak ditemukan. Panggil refresh_tools() terlebih dahulu.",
                suggestion="Periksa ejaan nama tool atau panggil refresh_tools().",
                tool_name=req.tool,
            )

        ok, errors, coerced, normalized_schema = validate_tool_args(
            tool_name=req.tool,
            args=req.args,
            input_schema=tool.input_schema,
            coerce=self.enable_coercion,
        )

        if not ok:
            logger.warning("Schema validation failed for tool %s: %s", req.tool, errors)
            return ToolCallResponse(
                success=False,
                reason="Argumen tidak valid terhadap schema tool.",
                validation_errors=errors,
                example_schema=normalized_schema,
                suggestion=(
                    "Perbaiki argumen sesuai schema. Pastikan field 'required' terisi "
                    "dan tipenya benar (integer/number/boolean/array/object)."
                ),
                coerced_args=coerced if coerced != req.args else None,
                tool_name=req.tool,
            )

        # Lolos validasi → eksekusi MCP tool dengan retry & timeout
        max_attempts = (
            req.retry if req.retry and req.retry > 0 else self.DEFAULT_MAX_RETRY
        )
        timeout_s = req.timeout_s or self.DEFAULT_TIMEOUT_S

        await self._emit(
            "tool.before_execute",
            {
                "tool": req.tool,
                "args": coerced,
                "timeout_s": timeout_s,
                "max_attempts": max_attempts,
            },
        )

        async def _call_once() -> Any:
            return await asyncio.wait_for(
                self.mcp_client.call_tool_mcp(req.tool, coerced),
                timeout=timeout_s,
            )

        try:
            result = None
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(max_attempts),
                wait=wait_exponential_jitter(initial=0.5, max=3.0),
                retry=retry_if_exception_type(
                    (asyncio.TimeoutError, ConnectionError, OSError)
                ),
                reraise=True,
            ):
                with attempt:
                    try:
                        self.breaker = getattr(self, "breaker", CircuitBreaker())
                        if not self.breaker.allow(req.tool):
                            return ToolCallResponse(
                                success=False,
                                reason=f"Tool '{req.tool}' sementara ditutup (circuit open).",
                                tool_name=req.tool,
                            )
                        result = await _call_once()
                    except asyncio.TimeoutError as te:
                        logger.warning("Timeout executing tool '%s': %s", req.tool, te)
                        raise
                    except (ConnectionError, OSError) as ne:
                        logger.warning(
                            "Network error executing tool '%s': %s", req.tool, ne
                        )
                        raise
                    except Exception as e:
                        # Non-retryable by default: langsung propagasi
                        logger.exception(
                            "Tool '%s' execution error (non-retry): %s", req.tool, e
                        )
                        raise

                    self.breaker.record(req.tool, ok=True)
            await self._emit(
                "tool.after_execute",
                {
                    "tool": req.tool,
                    "ok": True,
                },
            )
            return ToolCallResponse(success=True, result=result, tool_name=req.tool)

        except RetryError as re:
            # Retries habis
            cause = re.last_attempt.exception() if re.last_attempt else re
            await self._emit(
                "tool.after_execute",
                {
                    "tool": req.tool,
                    "ok": False,
                    "error": str(cause),
                    "retries": max_attempts,
                },
            )
            return ToolCallResponse(
                success=False,
                reason=f"Gagal mengeksekusi tool '{req.tool}' setelah {max_attempts} percobaan: {cause}",
                suggestion="Periksa koneksi MCP Server/transport, atau coba tingkatkan timeout.",
                tool_name=req.tool,
            )
        except Exception as e:
            await self._emit(
                "tool.after_execute",
                {
                    "tool": req.tool,
                    "ok": False,
                    "error": str(e),
                },
            )
            return ToolCallResponse(
                success=False,
                reason=f"Gagal mengeksekusi tool '{req.tool}': {e}",
                suggestion="Cek log MCP Server dan validasi ketersediaan tool.",
                tool_name=req.tool,
            )

    # --------- High-level helper (opsional) ---------
    async def safe_call(
        self,
        tool: str,
        args: Dict[str, Any],
        *,
        timeout_s: Optional[float] = None,
        retry: Optional[int] = None,
    ) -> Tuple[bool, Any]:
        """
        Helper ringkas untuk workflow:
          True, result -> sukses
          False, error_dict -> gagal (berisi reason/validation_errors/suggestion)
        """
        resp = await self.execute_tool(
            ToolCallRequest(
                tool=tool, args=args, timeout_s=timeout_s, retry=(retry or 0)
            )
        )
        if resp.success:
            return True, resp.result
        # kembalikan error yang ramah untuk dipakai regenerate args/planner
        return False, {
            "reason": resp.reason,
            "validation_errors": [vars(e) for e in resp.validation_errors],
            "example_schema": resp.example_schema,
            "suggestion": resp.suggestion,
            "coerced_args": resp.coerced_args,
            "tool_name": resp.tool_name,
        }
