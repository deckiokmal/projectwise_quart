# projectwise/services/llm_chain/llm_chains.py
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Literal, Callable, Awaitable, Type, Tuple
from pydantic import BaseModel
from openai import AsyncOpenAI
from openai import (
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
    BadRequestError,
    AuthenticationError,
    InternalServerError,
)
from projectwise.utils.logger import get_logger
from projectwise.config import ServiceConfigs
from .llm_utils import (
    ensure_responses_input,
    extract_assistant_text_chat,
    extract_output_text_responses,
    json_schema_from_pydantic,
    pydantic_parse,
    extract_tool_calls_chat,
    extract_tool_calls_responses,
)

logger = get_logger(__name__)
settings = ServiceConfigs()
Prefer = Literal["responses", "chat", "auto"]

ToolExecutor = Callable[[str, Dict[str, Any]], Awaitable[Any]]


class LLMChains:
    def __init__(
        self,
        model: str = settings.llm_model,
        *,
        prefer: Prefer = "auto",
        client: Optional[AsyncOpenAI] = None,
        llm_base_url: str = settings.llm_base_url,
        api_key: Optional[str] = settings.llm_api_key,
        temperature: float = settings.llm_temperature,
        max_tokens: int = settings.max_token,
        request_timeout: float = 60.0,
        tool_timeout: float = 45.0,
        tool_retries: int = 1,
    ):
        self.model = model
        self.prefer = prefer
        self.temperature = float(temperature or 0.0)
        self.max_tokens = int(max_tokens) or 256
        self.request_timeout = float(request_timeout)
        self.tool_timeout = float(tool_timeout)
        self.tool_retries = int(tool_retries)
        self.client = client or AsyncOpenAI(base_url=llm_base_url, api_key=api_key)

    # ====================================================
    # Helper low-level untuk panggil API OpenAI SDK
    # ====================================================
    async def chat_completions(self, **kwargs) -> Any:
        return await asyncio.wait_for(
            self.client.chat.completions.create(**kwargs), timeout=self.request_timeout
        )

    async def responses_create(self, **kwargs) -> Any:
        return await asyncio.wait_for(
            self.client.responses.create(**kwargs), timeout=self.request_timeout
        )

    # =====================================================
    # Generate text langsung (tanpa parsing)
    # =====================================================
    async def chat_completions_text(
        self,
        messages: List[Dict[str, Any]],
        *,
        json_schema: Optional[Dict[str, Any]] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        args: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if max_tokens or self.max_tokens:
            args["max_tokens"] = max_tokens or self.max_tokens
        if json_schema:
            args["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": (json_schema.get("$id") or "schema"),
                    "schema": json_schema,
                    "strict": True,
                },
            }
        resp = await self.chat_completions(**args)
        return extract_assistant_text_chat(resp)

    async def responses_text(
        self,
        input: List[Dict[str, Any]] | str,
        *,
        max_output_tokens: Optional[int] = None,
    ) -> str:
        args: Dict[str, Any] = {
            "model": self.model,
            "input": input,
            "temperature": self.temperature,
        }
        if max_output_tokens or self.max_tokens:
            args["max_output_tokens"] = max_output_tokens or self.max_tokens
        resp = await self.responses_create(**args)
        return extract_output_text_responses(resp)

    # =====================================================
    # Structure Output helpers (Pydantic)
    # =====================================================
    async def chat_completions_parse(
        self,
        messages: List[Dict[str, Any]],
        *,
        pydantic_model: Type[BaseModel],
        max_tokens: Optional[int] = None,
        strict: bool = True,
    ) -> BaseModel:
        """
        1) Coba native: client.chat.completions.parse(..., response_format=YourModel)
        2) Jika gagal (method tidak ada / SDK tidak mendukung / error lain): fallback ke .create + schema JSON, lalu parse manual.
        Tidak ada normalisasi hasil.
        """
        # --- Native-first ---
        try:
            resp = await asyncio.wait_for(
                self.client.chat.completions.parse(
                    model=self.model,
                    messages=messages,  # type: ignore
                    response_format=pydantic_model,  # <- Pydantic langsung
                    temperature=self.temperature,
                    max_tokens=max_tokens or self.max_tokens,
                ),
                timeout=self.request_timeout,
            )
            parsed = getattr(resp, "output_parsed", None)
            if parsed is not None:
                return parsed
        except (AttributeError, TypeError) as e:
            logger.debug(
                "chat.completions.parse tidak tersedia/kompatibel, fallback. err=%s", e
            )
        except (
            BadRequestError,
            RateLimitError,
            AuthenticationError,
            APIConnectionError,
            InternalServerError,
            APITimeoutError,
        ) as e:
            # Beberapa error model tetap bisa dipulihkan via fallback (schema JSON)
            logger.info(
                "chat.parse error (%s), coba fallback create+schema.", type(e).__name__
            )

        # --- Fallback: create + JSON schema dari Pydantic, lalu parse manual ---
        resp = await self.chat_completions(
            model=self.model,
            messages=messages,
            response_format=json_schema_from_pydantic(pydantic_model, strict=strict),
            temperature=self.temperature,
            max_tokens=max_tokens or self.max_tokens,
        )
        text = extract_assistant_text_chat(resp)
        return pydantic_parse(pydantic_model, text)

    async def responses_parse(
        self,
        input: List[Dict[str, Any]] | str,
        *,
        pydantic_model: Type[BaseModel],
        max_output_tokens: Optional[int] = None,
        strict: bool = True,
    ) -> BaseModel:
        """
        1) Coba native: client.responses.parse(..., response_format=YourModel)
        2) Fallback: client.responses.create(..., response_format=json_schema) → ambil text → parse manual Pydantic.
        """
        # --- Native-first ---
        try:
            resp = await asyncio.wait_for(
                self.client.responses.parse(
                    model=self.model,
                    input=ensure_responses_input(input)
                    if isinstance(input, list)
                    else input,  # type: ignore
                    text_format=pydantic_model,  # <- Pydantic langsung
                    temperature=self.temperature,
                    max_output_tokens=max_output_tokens or self.max_tokens,
                ),
                timeout=self.request_timeout,
            )
            parsed = getattr(resp, "output_parsed", None)
            if parsed is not None:
                return parsed
        except (AttributeError, TypeError) as e:
            logger.debug(
                "responses.parse tidak tersedia/kompatibel, fallback. err=%s", e
            )
        except (
            BadRequestError,
            RateLimitError,
            AuthenticationError,
            APIConnectionError,
            InternalServerError,
            APITimeoutError,
        ) as e:
            logger.info(
                "responses.parse error (%s), coba fallback create+schema.",
                type(e).__name__,
            )

        # --- Fallback: create + JSON schema dari Pydantic, lalu parse manual ---
        resp = await self.responses_create(
            model=self.model,
            input=ensure_responses_input(input) if isinstance(input, list) else input,
            response_format=json_schema_from_pydantic(pydantic_model, strict=strict),
            temperature=self.temperature,
            max_output_tokens=max_output_tokens or self.max_tokens,
        )
        text = extract_output_text_responses(resp)
        return pydantic_parse(pydantic_model, text)

    # =====================================================
    # Function Call helpers (untuk Tools)
    # =====================================================
    async def chat_function_call(
        self,
        messages: List[Dict[str, Any]],
        *,
        tools: List[Dict[str, Any]],
        tool_choice: Literal["auto", "none"] | Dict[str, Any] = "auto",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Any]:
        """
        Satu langkah function-calling via Chat Completions.
        - Tidak ada normalisasi hasil.
        - tool_calls diekstraksi dari response dan dikembalikan apa adanya.
        """
        resp = await self.chat_completions(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=self.temperature if temperature is None else float(temperature),
            max_tokens=max_tokens or self.max_tokens,
        )
        tool_calls = extract_tool_calls_chat(resp)
        return tool_calls, resp

    async def responses_function_call(
        self,
        input_messages: List[Dict[str, Any]] | str,
        *,
        tools: List[Dict[str, Any]],
        tool_choice: Literal["auto", "none"] | Dict[str, Any] = "auto",
        temperature: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Any]:
        """
        Satu langkah function-calling via Responses API.
        - Tidak ada normalisasi hasil.
        - tool_calls diekstraksi dari response dan dikembalikan apa adanya.
        """
        resp = await self.responses_create(
            model=self.model,
            input=ensure_responses_input(input_messages)
            if isinstance(input_messages, list)
            else input_messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=self.temperature if temperature is None else float(temperature),
            max_output_tokens=max_output_tokens or self.max_tokens,
        )
        tool_calls = extract_tool_calls_responses(resp)
        return tool_calls, resp

