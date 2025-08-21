# projectwise/projectwise/services/llm_chain/llm_chains.py
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple, Type, Union, Literal

from pydantic import BaseModel, ValidationError
from openai import AsyncOpenAI
from openai import (
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
    BadRequestError,
    AuthenticationError,
    InternalServerError,
)

from projectwise.config import ServiceConfigs as Settings
from projectwise.utils.logger import get_logger

# Gunakan alias ToolExecutor dari tool_registry agar konsisten di seluruh project
from projectwise.services.llm_chain.tool_registry import ToolExecutor

logger = get_logger(__name__)
settings = Settings()  # type: ignore

Prefer = Literal["responses", "chat", "auto"]


class LLMChains:
    """
    Pusat pemanggilan LLM (async) untuk:
      - Chat Completions (mendukung JSON Schema `response_format`)
      - Responses API (mendukung Pydantic/JSON Schema)
      - Function Calling (tools) termasuk multi-hop roundtrip (eksekusi & reply)
      - Fallback otomatis "chat" <-> "responses" sesuai preferensi/auto

    Seluruh method mereturn dict konsisten minimal:
        { "status": "success"|"error", "message": str, ... }
    """

    def __init__(
        self,
        model: str = settings.llm_model,
        *,
        prefer: Prefer = "auto",
        client: Optional[AsyncOpenAI] = None,
        llm_base_url: str = settings.llm_base_url,
        api_key: Optional[str] = settings.llm_api_key,
        temperature: float = settings.llm_temperature,
        max_tokens: int = 2048,
        request_timeout: float = 60.0,
        tool_timeout: float = 45.0,
        tool_retries: int = 1,
    ):
        self.model = model
        self.prefer = prefer
        self.temperature = float(temperature or 0.0)
        self.max_tokens = int(max_tokens)
        self.request_timeout = float(request_timeout)
        self.tool_timeout = float(tool_timeout)
        self.tool_retries = int(tool_retries)
        # AsyncOpenAI sudah async—tidak perlu to_thread
        self.client = client or AsyncOpenAI(base_url=llm_base_url, api_key=api_key)

    # ============================== Utils umum ===============================

    @staticmethod
    def _ret(status: str, message: str, **extra: Any) -> Dict[str, Any]:
        out = {"status": status, "message": message}
        out.update(extra or {})
        return out

    @staticmethod
    def _ok(
        message: str,
        *,
        took_ms: float,
        hops: int,
        usage: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # konsisten: "success"
        return {
            "status": "success",
            "message": message,
            "hops": hops,
            "took_ms": round(took_ms, 2),
            "usage": usage,
        }

    @staticmethod
    def _err(message: str, *, took_ms: float) -> Dict[str, Any]:
        return {"status": "error", "message": message, "took_ms": round(took_ms, 2)}

    @staticmethod
    def _safe_json(v: Any) -> Dict[str, Any]:
        if isinstance(v, dict):
            return v
        if not v:
            return {}
        try:
            return json.loads(v)
        except Exception:
            return {}

    def _json_schema_rf(
        self, name: Optional[str], schema: Dict[str, Any], strict: bool = True
    ) -> Dict[str, Any]:
        # Format response_format Chat Completions (JSON Schema)
        return {
            "type": "json_schema",
            "json_schema": {
                "name": name or f"schema_{uuid.uuid4().hex[:8]}",
                "schema": schema,
                "strict": strict,
            },
        }

    # Normalisasi tambahan untuk berjaga-jaga jika ada tools yang belum dipatch
    def _normalize_tools(
        self, tools: Optional[List[Dict[str, Any]]]
    ) -> List[Dict[str, Any]]:
        safe: List[Dict[str, Any]] = []
        for t in tools or []:
            try:
                if t.get("type") == "function":
                    params = (t.get("function") or {}).get("parameters") or {}
                    if (
                        isinstance(params, dict)
                        and "additionalProperties" not in params
                    ):
                        params = {**params, "additionalProperties": False}
                        t = {
                            **t,
                            "function": {
                                **(t.get("function") or {}),
                                "parameters": params,
                            },
                        }
                safe.append(t)
            except Exception:
                logger.warning("Gagal menormalkan tool: %s", t, exc_info=True)
                safe.append(t)
        return safe

    def _extract_calls_from_chat(self, resp) -> List[Dict[str, Any]]:
        """
        Ekstraksi function call dari Chat Completions.
        """
        calls: List[Dict[str, Any]] = []
        try:
            choice = (resp.choices or [None])[0]
            msg = getattr(choice, "message", None)
            for tc in getattr(msg, "tool_calls", []) or []:
                fn = getattr(tc, "function", None)
                args_str = getattr(fn, "arguments", "") or "{}"
                try:
                    args = json.loads(args_str)
                except Exception:
                    args = {"_raw": args_str}
                calls.append(
                    {
                        "name": getattr(fn, "name", ""),
                        "arguments": args,
                        "id": getattr(tc, "id", None),
                        "raw": tc,
                    }
                )
        except Exception:
            logger.exception("Gagal mengekstrak tool_calls (chat).")
        return calls

    def _extract_calls_from_responses(self, resp) -> List[Dict[str, Any]]:
        """
        Ekstraksi function call dari Responses API.
        """
        calls: List[Dict[str, Any]] = []
        try:
            output = getattr(resp, "output", None)
            if isinstance(output, list):
                for it in output:
                    if getattr(it, "type", None) == "function_call":
                        args = getattr(it, "arguments", "") or "{}"
                        try:
                            args_json = json.loads(args)
                        except Exception:
                            args_json = {"_raw": args}
                        calls.append(
                            {
                                "name": getattr(it, "name", ""),
                                "arguments": args_json,
                                "call_id": getattr(it, "call_id", None),
                                "raw": it,
                            }
                        )
        except Exception:
            logger.exception("Gagal mengekstrak function_call (responses).")
        return calls

    # ========================== Chat / Responses Text ========================

    async def chat_completions_text(
        self,
        messages: List[Dict[str, Any]],
        *,
        json_schema: Optional[Dict[str, Any]] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Hasilkan teks via Chat Completions (opsional: JSON Schema).
        """
        logger.info("ChatCompletions: generate text (schema=%s)", bool(json_schema))
        try:
            kwargs: Dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
            }
            if max_tokens or self.max_tokens:
                kwargs["max_tokens"] = max_tokens or self.max_tokens
            if json_schema:
                kwargs["response_format"] = self._json_schema_rf(
                    name=(json_schema.get("$id") or "chat_schema"),
                    schema=json_schema,
                    strict=True,
                )

            resp = await asyncio.wait_for(
                self.client.chat.completions.create(**kwargs),
                timeout=self.request_timeout,
            )

            choice = (getattr(resp, "choices", None) or [None])[0]
            content = getattr(getattr(choice, "message", None), "content", None)

            data: Any = content
            if json_schema and isinstance(content, str):
                # content kemungkinan string JSON
                try:
                    data = json.loads(content)
                except Exception:
                    logger.warning(
                        "Structured content bukan JSON valid, mengembalikan string mentah."
                    )

            return self._ret(
                "success",
                "Berhasil menghasilkan teks (chat).",
                data=data,
                raw=resp,
                meta={"endpoint": "chat.completions"},
            )
        except Exception as e:
            logger.error("ChatCompletions gagal: %s", e, exc_info=True)
            return self._ret("error", self._friendly_error(e))

    async def responses_text(
        self,
        input: Union[str, List[Dict[str, Any]]],
        *,
        pydantic_model: Optional[Type[BaseModel]] = None,
        max_output_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Hasilkan teks/structured via Responses API.
        - Bila tersedia, prefer `responses.parse` untuk Pydantic; fallback ke `responses.create`.
        """
        logger.info("Responses: generate (pydantic=%s)", bool(pydantic_model))
        try:
            # 1) Pakai responses.parse bila tersedia & user minta pydantic
            if pydantic_model is not None and hasattr(self.client.responses, "parse"):
                parsed = await asyncio.wait_for(
                    self.client.responses.parse(
                        model=self.model,
                        input=input,
                        temperature=self.temperature,
                        response_format=pydantic_model,  # type: ignore[arg-type]
                        max_output_tokens=max_output_tokens or self.max_tokens,
                    ),
                    timeout=self.request_timeout,
                )
                data = parsed.model_dump() if hasattr(parsed, "model_dump") else parsed
                return self._ret(
                    "success",
                    "Berhasil menghasilkan structured output (responses.parse).",
                    data=data,
                    raw=None,
                    meta={"endpoint": "responses.parse"},
                )

            # 2) Tanpa parse(): pakai responses.create
            kwargs: Dict[str, Any] = {
                "model": self.model,
                "input": input,
                "temperature": self.temperature,
            }
            if max_output_tokens or self.max_tokens:
                kwargs["max_output_tokens"] = max_output_tokens or self.max_tokens

            if pydantic_model is not None:
                try:
                    schema = pydantic_model.model_json_schema()  # type: ignore[attr-defined]
                except Exception:
                    schema = {}
                kwargs["response_format"] = self._json_schema_rf(
                    name=pydantic_model.__name__,
                    schema=schema or {"type": "object"},
                    strict=True,
                )

            resp = await asyncio.wait_for(
                self.client.responses.create(**kwargs),
                timeout=self.request_timeout,
            )

            text = (getattr(resp, "output_text", "") or "").strip()

            if pydantic_model is not None and text:
                try:
                    data_obj = pydantic_model.model_validate_json(text)  # type: ignore[attr-defined]
                    data = data_obj.model_dump()  # type: ignore[attr-defined]
                    return self._ret(
                        "success",
                        "Berhasil menghasilkan structured output (responses + validate).",
                        data=data,
                        raw=resp,
                        meta={"endpoint": "responses.create"},
                    )
                except ValidationError as ve:
                    logger.error("Validasi Pydantic gagal: %s", ve)
                    return self._ret(
                        "error",
                        f"Parsing structured output gagal: {ve.errors()}",
                        raw=resp,
                        meta={"endpoint": "responses.create"},
                    )

            return self._ret(
                "success",
                "Berhasil menghasilkan teks (responses).",
                data=text,
                raw=resp,
                meta={"endpoint": "responses.create"},
            )

        except Exception as e:
            logger.error("Responses gagal: %s", e, exc_info=True)
            return self._ret("error", self._friendly_error(e))

    async def responses_parse(
        self,
        input: Union[str, List[Dict[str, Any]]],
        *,
        pydantic_model: Type[BaseModel],
    ) -> Dict[str, Any]:
        """Convenience: paksa structured output via Responses API + Pydantic."""
        return await self.responses_text(input=input, pydantic_model=pydantic_model)

    # ============================= Function Calling ==========================

    async def function_call(
        self,
        messages: List[Dict[str, Any]],
        *,
        tools: List[Dict[str, Any]],
        prefer: Prefer = "chat",
        tool_choice: Union[str, Dict[str, Any]] = "auto",
    ) -> Dict[str, Any]:
        """
        Ekstrak function-calls (satu-hop) dengan fallback:
          - prefer="chat" → coba Chat Completions; jika kosong/gagal → Responses.
          - prefer="responses" → kebalikannya.
        """
        tools_norm = self._normalize_tools(tools)

        async def _via_responses() -> Tuple[List[Dict[str, Any]], Any]:
            try:
                input_payload = self._ensure_responses_input(messages)
                resp = await asyncio.wait_for(
                    self.client.responses.create(
                        model=self.model,
                        input=input_payload,  # type: ignore
                        tools=tools_norm,  # type: ignore[arg-type]
                        tool_choice=tool_choice,  # type: ignore
                        temperature=self.temperature,
                        max_output_tokens=self.max_tokens,
                    ),
                    timeout=self.request_timeout,
                )
                calls = self._extract_calls_from_responses(resp)
                return calls, resp
            except Exception as e:
                logger.error("FunctionCall Responses gagal: %s", e, exc_info=True)
                return [], e

        async def _via_chat() -> Tuple[List[Dict[str, Any]], Any]:
            try:
                resp = await asyncio.wait_for(
                    self.client.chat.completions.create(
                        model=self.model,
                        messages=messages,  # type: ignore
                        tools=tools_norm,  # type: ignore[arg-type]
                        tool_choice=tool_choice,  # type: ignore
                        temperature=self.temperature,
                        max_tokens=self.max_tokens,
                    ),
                    timeout=self.request_timeout,
                )
                calls = self._extract_calls_from_chat(resp)
                return calls, resp
            except Exception as e:
                logger.error("FunctionCall Chat gagal: %s", e, exc_info=True)
                return [], e

        order = (
            (_via_chat, _via_responses)
            if prefer == "chat"
            else (_via_responses, _via_chat)
        )

        calls, raw = await order[0]()
        if calls:
            return self._ret(
                "success",
                "Berhasil mengekstrak function call.",
                calls=calls,
                raw=raw,
                meta={
                    "endpoint": "chat.completions"
                    if order[0] is _via_chat
                    else "responses"
                },
            )

        calls_fb, raw_fb = await order[1]()
        if calls_fb:
            return self._ret(
                "success",
                "Berhasil mengekstrak function call (fallback).",
                calls=calls_fb,
                raw=raw_fb,
                meta={
                    "endpoint": "responses"
                    if order[0] is _via_chat
                    else "chat.completions"
                },
            )

        # Gagal keduanya
        msg = (
            self._friendly_error(raw)
            if isinstance(raw, Exception)
            else (
                self._friendly_error(raw_fb)
                if isinstance(raw_fb, Exception)
                else "Tidak ditemukan function call dari kedua endpoint."
            )
        )
        return self._ret("error", msg, meta={"endpoint": "fallback-failed"})

    # ============== Multi-hop Function Calling Roundtrip (Agentic) ===========

    async def run_function_call_roundtrip(
        self,
        messages: List[Dict[str, Any]],
        *,
        tools: List[Dict[str, Any]],
        tool_executor: ToolExecutor,
        tool_choice: Literal["auto", "none"] | Dict[str, Any] = "auto",
        max_hops: int = 6,
        prefer: Optional[Prefer] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Jalankan loop: LLM → (minta tools?) → eksekusi → balas → ulangi sampai final text.
        Return: {status, message, usage?, hops, took_ms}
        """
        t0 = time.perf_counter()
        mode = self._decide_mode(prefer)
        logger.debug("LLMChains mode=%s model=%s", mode, self.model)

        try:
            hops = 0
            last_text: Optional[str] = None
            while True:
                hops += 1
                if hops > max_hops:
                    return self._ok(
                        message=last_text
                        or "Maksimum langkah tercapai tanpa jawaban final.",
                        took_ms=(time.perf_counter() - t0) * 1000,
                        hops=hops - 1,
                    )

                # 1) Panggil model sekali
                resp = await self._call_model_once(
                    mode=mode,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    metadata=metadata,
                )

                # 2) Ekstraksi
                tool_calls = self._extract_tool_calls(mode, resp)
                assistant_text = self._extract_assistant_text(mode, resp)
                usage = self._extract_usage(mode, resp)

                if tool_calls:
                    # 3) Eksekusi semua tools (berurutan, deterministik, defensif)
                    tool_msgs: List[Dict[str, Any]] = []
                    for tc in tool_calls:
                        name = tc["name"]
                        args = tc.get("arguments") or {}
                        result: Dict[str, Any] = await self._exec_tool_defensive(
                            tool_executor, name, args
                        )
                        tool_msgs.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.get("id") or name,
                                "name": name,
                                "content": json.dumps(result, ensure_ascii=False),
                            }
                        )

                    # Simpan jejak panggilan assistant + balasan tool agar konteks lengkap
                    messages.append(
                        self._assistant_stub_for_history(assistant_text, tool_calls)
                    )
                    messages.extend(tool_msgs)
                    last_text = assistant_text or last_text
                    # lanjut loop
                else:
                    final_text = assistant_text or last_text or ""
                    return self._ok(
                        message=final_text,
                        took_ms=(time.perf_counter() - t0) * 1000,
                        hops=hops,
                        usage=usage,
                    )

        except Exception as e:
            return self._err(
                f"Gagal menjalankan LLMChains: {type(e).__name__}: {e}",
                took_ms=(time.perf_counter() - t0) * 1000,
            )

    # ========================= Engine select & one-shot ======================

    def _decide_mode(self, prefer: Optional[Prefer]) -> Prefer:
        mode = prefer or self.prefer or "auto"
        if mode in ("responses", "chat"):
            return mode
        # AUTO heuristik:
        #  - jika base_url mengandung tanda kompatibel (mis. dashscope/vLLM), default "chat"
        #  - selain itu default "responses"
        base_url = getattr(self.client, "_base_url", "") or getattr(
            self.client, "base_url", ""
        )
        s = str(base_url).lower()
        if "dashscope" in s or "compatible" in s:
            return "chat"
        return "responses"

    async def _call_model_once(
        self,
        *,
        mode: Prefer,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        tool_choice: Any,
        metadata: Optional[Dict[str, Any]],
    ) -> Any:
        # Kedua API mendukung tools namun paramnya beda: chat: messages, responses: input
        if mode == "responses":
            return await asyncio.wait_for(
                self.client.responses.create(
                    model=self.model,
                    input=self._ensure_responses_input(messages),  # type: ignore
                    tools=self._normalize_tools(tools),  # type: ignore[arg-type]
                    tool_choice=tool_choice,  # type: ignore
                    metadata=metadata or {},
                    temperature=self.temperature,
                    max_output_tokens=self.max_tokens,
                ),
                timeout=self.request_timeout,
            )
        else:
            return await asyncio.wait_for(
                self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,  # type: ignore
                    tools=self._normalize_tools(tools),  # type: ignore[arg-type]
                    tool_choice=tool_choice,  # type: ignore
                    metadata=metadata or {},
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                ),
                timeout=self.request_timeout,
            )

    # ============================= Parsers & helpers =========================

    def _extract_tool_calls(self, mode: Prefer, resp: Any) -> List[Dict[str, Any]]:
        try:
            if mode == "responses":
                # 1) Bentuk "tool_calls"
                collected: List[Dict[str, Any]] = []
                for item in getattr(resp, "output", []) or []:
                    tc = getattr(item, "tool_calls", None)
                    if tc:
                        collected.extend(
                            [
                                {
                                    "id": x.get("id"),
                                    "name": x.get("function", {}).get("name"),
                                    "arguments": self._safe_json(
                                        x.get("function", {}).get("arguments")
                                    ),
                                }
                                for x in tc
                                if x.get("type") == "function"
                            ]
                        )
                if collected:
                    return collected

                # 2) Fallback: bentuk "function_call" items
                calls = self._extract_calls_from_responses(resp)  # ← sudah ada
                if calls:
                    return [
                        {
                            "id": c.get("call_id"),
                            "name": c.get("name"),
                            "arguments": c.get("arguments") or {},
                        }
                        for c in calls
                    ]
                return []
            else:
                choice = (getattr(resp, "choices", None) or [None])[0]
                if not choice:
                    return []
                msg = getattr(choice, "message", None)
                tc = getattr(msg, "tool_calls", None)
                if not tc:
                    return []
                return [
                    {
                        "id": x.id,
                        "name": x.function.name,
                        "arguments": self._safe_json(x.function.arguments),
                    }
                    for x in tc
                    if getattr(x, "type", "function") == "function"
                ]
        except Exception:
            return []

    def _extract_assistant_text(self, mode: Prefer, resp: Any) -> str:
        if mode == "responses":
            chunks: List[str] = []
            for item in getattr(resp, "output", []) or []:
                content = getattr(item, "content", None) or []
                for c in content:
                    if getattr(c, "type", None) == "output_text":
                        chunks.append(getattr(c, "text", "") or "")
            return "".join(chunks).strip()
        # chat
        choice = (getattr(resp, "choices", None) or [None])[0]
        if not choice:
            return ""
        return (
            getattr(getattr(choice, "message", None), "content", None) or ""
        ).strip()

    def _extract_usage(self, mode: Prefer, resp: Any) -> Optional[Dict[str, Any]]:
        try:
            u = getattr(resp, "usage", None)
            if not u:
                return None
            if isinstance(u, dict):
                return u
            if hasattr(u, "model_dump"):
                return u.model_dump()  # OpenAI SDK v1 objects
            # fallback generic: ambil atribut umum
            out = {}
            for k in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "input_tokens",
                "output_tokens",
            ):
                if hasattr(u, k):
                    out[k] = getattr(u, k)
            return out or None
        except Exception:
            return None

    @staticmethod
    def _assistant_stub_for_history(
        text: Optional[str], tool_calls: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        # Simpan “assistant meminta tools” di riwayat sebelum mendorong balasan tool
        msg: Dict[str, Any] = {"role": "assistant"}
        if text:
            msg["content"] = text
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.get("id") or tc["name"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc.get("arguments") or {}),
                    },
                }
                for tc in tool_calls
            ]
        return msg

    def _ensure_responses_input(
        self, maybe_messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Jika messages sudah gaya Responses (content:list), kembalikan apa adanya.
        Jika gaya Chat (content:str/tool_calls), konversi ke bentuk Responses.
        """
        if not maybe_messages:
            return []
        first = maybe_messages[0]
        if isinstance(first.get("content"), list):  # kemungkinan sudah Responses-style
            return maybe_messages
        return self._to_responses_input(maybe_messages)

    def _to_responses_input(
        self, chat_messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Konversi messages gaya Chat → 'input' Responses API (JSON-serializable):
          - content:str → [{"type":"text","text":...}]
          - assistant.tool_calls → [{"type":"function_call", ...}]
          - tool message → {"role":"tool", "tool_call_id":..., "content":[{"type":"output_text","text":...}]}
        """
        resp_input: List[Dict[str, Any]] = []
        for m in chat_messages:
            role = m.get("role")

            # pesan tool
            if role == "tool":
                resp_input.append(
                    {
                        "role": "tool",
                        "tool_call_id": m.get("tool_call_id"),
                        "name": m.get("name"),
                        "content": [
                            {
                                "type": "output_text",
                                "text": m.get("content")
                                if isinstance(m.get("content"), str)
                                else json.dumps(m.get("content"), ensure_ascii=False),
                            }
                        ],
                    }
                )
                continue

            # assistant dengan tool_calls
            tool_calls = m.get("tool_calls") or []
            if role == "assistant" and tool_calls:
                items = []
                for tc in tool_calls:
                    fn = getattr(tc, "function", None)
                    tc_id = getattr(tc, "id", None) or (
                        tc.get("id") if isinstance(tc, dict) else None
                    )
                    name = getattr(fn, "name", None) or (
                        tc.get("function", {}).get("name")
                        if isinstance(tc, dict)
                        else ""
                    )
                    args = getattr(fn, "arguments", None) or (
                        tc.get("function", {}).get("arguments")
                        if isinstance(tc, dict)
                        else "{}"
                    )
                    items.append(
                        {
                            "type": "function_call",
                            "name": name,
                            "arguments": args or "{}",
                            "call_id": tc_id,
                        }
                    )
                resp_input.append({"role": "assistant", "content": items})
                continue

            # pesan biasa (system/user/assistant text)
            content = m.get("content", "")
            if isinstance(content, str):
                resp_input.append(
                    {"role": role, "content": [{"type": "text", "text": content}]}
                )
            else:
                # jika sudah berbentuk blok, lewatkan apa adanya
                resp_input.append({"role": role, "content": content})
        return resp_input

    # ============================== Error mapping ===========================

    def _friendly_error(self, e: Exception) -> str:
        if isinstance(e, (APIConnectionError, APITimeoutError)):
            return "Koneksi ke LLM bermasalah atau timeout. Coba ulangi."
        if isinstance(e, RateLimitError):
            return "Kutipan penggunaan model terlampaui (rate limit). Coba beberapa saat lagi."
        if isinstance(e, AuthenticationError):
            return "Autentikasi LLM gagal. Periksa API key."
        if isinstance(e, BadRequestError):
            return "Permintaan ke LLM ditolak. Periksa format input/parameter."
        if isinstance(e, InternalServerError):
            return "Layanan LLM mengalami gangguan. Coba ulangi."
        return f"Kesalahan tidak terduga: {e}"

    # ============================== Tool executor ===========================

    async def _exec_tool_defensive(
        self, tool_executor: ToolExecutor, name: str, args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Jalankan tool dengan retry minimal & timeout.
        ToolExecutor boleh async atau sync (alias di tool_registry mendukung keduanya).
        """
        last_err: Optional[BaseException] = None
        for attempt in range(self.tool_retries + 1):
            try:
                return await asyncio.wait_for(
                    tool_executor(name, args),  # type: ignore
                    timeout=self.tool_timeout,
                )
            except Exception as e:
                last_err = e
                await asyncio.sleep(0.05 * (attempt + 1))
        return {
            "status": "error",
            "message": f"Tool '{name}' gagal: {type(last_err).__name__}: {last_err}",
        }
