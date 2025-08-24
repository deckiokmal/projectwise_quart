# projectwise/services/workflow/actor_critic.py
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Literal

from pydantic import BaseModel, Field
from quart import current_app

from projectwise.services.llm_chain.llm_chains import LLMChains, Prefer
from projectwise.services.mcp.adapter import MCPToolAdapter
from projectwise.config import ServiceConfigs
from projectwise.utils.logger import get_logger

logger = get_logger(__name__)
settings = ServiceConfigs()


# ---------- Data Models ----------
class ToolSpec(BaseModel):
    name: str
    description: Optional[str] = None
    input_schema: Optional[Dict[str, Any]] = None


class ToolCall(BaseModel):
    name: Optional[str] = None
    args: Dict[str, Any] = Field(default_factory=dict)


class ActorDecision(BaseModel):
    mode: Literal["answer", "tool"]
    answer_draft: Optional[str] = None
    tool: Optional[ToolCall] = None


class StepResult(BaseModel):
    tool: Optional[str]
    args: Optional[Dict[str, Any]]
    ok: bool
    output: Any = None
    error: Optional[str] = None
    time_ms: int = 0


class ActorTrace(BaseModel):
    decision: ActorDecision
    tool_attempts: List[StepResult] = Field(default_factory=list)
    candidate_answer: str


class Critique(BaseModel):
    verdict: Literal["finalize", "revise"]
    reasoning: str
    suggestions: Optional[List[str]] = None
    new_tool: Optional[str] = None
    new_args: Optional[Dict[str, Any]] = None
    ask_clarification: bool = False
    revised_text_hint: Optional[str] = None


# ---------- Prompts ----------
PROMPT_ACTOR_DECIDE = """
Anda adalah ACTOR. Tugas:
1) Tentukan apakah cukup menjawab langsung ("answer") atau perlu memanggil satu MCP tool ("tool").
2) Jika "tool", pilih nama tool yang paling relevan dan konstruksikan argumen sesuai JSONSchema.
3) Kembalikan JSON valid sesuai Pydantic ActorDecision.

Kriteria:
- Hindari efek samping; pilih tool read-only saat ragu.
- Jangan memanggil lebih dari 1 tool pada satu siklus.

Objective:
{objective}

TOOLS (JSON):
{tools_json}
""".strip()

PROMPT_ARG_REPAIR = """
Anda adalah Asisten yang memperbaiki argumen tool berdasarkan error & schema.
Kembalikan JSON: {"args": <obj argumen valid>}

Nama tool: {tool_name}
Schema: {schema_json}
Argumen_sebelumnya: {args_json}
Error: {error_text}

Perbaiki tipe/nilai yang salah, isi default aman jika perlu.
""".strip()

PROMPT_SYNTHESIZE = """
Ubah data berikut menjadi jawaban akhir yang ringkas, akurat, dan actionable untuk user (bahasa Indonesia).
Fokuskan pada hasil yang relevan dengan objective, jangan tampilkan JSON mentah.

Objective:
{objective}

Data:
{data_json}
""".strip()

PROMPT_CRITIC = """
Anda adalah CRITIC. Evaluasi apakah jawaban ACTOR sudah layak dikirim ke user.
Balas JSON Pydantic Critique dengan kunci:
- verdict: "finalize" atau "revise"
- reasoning: alasan singkat
- suggestions: daftar saran (opsional)
- new_tool/new_args: jika perlu coba tool lain/argumen baru (opsional)
- ask_clarification: true jika butuh tanya user
- revised_text_hint: jika cukup revisi teks, berikan arahan singkat

Objective:
{objective}

Jawaban_kandidat:
{candidate_text}
""".strip()


# ---------- Actor–Critic Orchestrator ----------
class ActorCritic:
    def __init__(
        self,
        *,
        llm: Optional[LLMChains] = None,
        mcp: Optional[MCPToolAdapter] = None,
        prefer: Prefer = "auto",
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        request_timeout: float = 90.0,
        tool_retry: int = 1,  # total percobaan tambahan setelah gagal pertama
    ) -> None:
        self.llm = llm or LLMChains(
            model=model or settings.llm_model,
            prefer=prefer,
            temperature=temperature
            if temperature is not None
            else settings.llm_temperature,
            request_timeout=request_timeout,
        )
        self.mcp = mcp or MCPToolAdapter(current_app)
        self.prefer = prefer
        self.tool_retry = max(0, int(tool_retry))

    async def _list_tools(self) -> List[ToolSpec]:
        tools: List[ToolSpec] = []
        try:
            available = await self.mcp.get_tools()
            # dukung format responses/chat
            for t in available:
                if "function" in t:
                    fn = t["function"]
                    tools.append(
                        ToolSpec(
                            name=fn.get("name"),
                            description=fn.get("description"),
                            input_schema=fn.get("parameters"),
                        )
                    )
                else:
                    tools.append(
                        ToolSpec(
                            name=t.get("name"), # type: ignore
                            description=t.get("description"),
                            input_schema=t.get("inputSchema") or t.get("input_schema"),
                        )
                    )
        except Exception:
            logger.exception("Gagal list MCP tools")
        return tools

    async def _call_tool_once(self, name: str, args: Dict[str, Any]) -> StepResult:
        t0 = time.time()
        try:
            logger.info(
                "MCP call → %s args=%s", name, json.dumps(args, ensure_ascii=False)
            )
            out = await self.mcp.call_tool(name, args)
            dt = int((time.time() - t0) * 1000)
            return StepResult(tool=name, args=args, ok=True, output=out, time_ms=dt)
        except Exception as e:
            logger.exception("Gagal call tool: %s", name)
            dt = int((time.time() - t0) * 1000)
            return StepResult(tool=name, args=args, ok=False, error=str(e), time_ms=dt)

    async def _repair_args(
        self, tool: ToolSpec, args: Dict[str, Any], error_text: str
    ) -> Dict[str, Any]:
        messages = [
            {
                "role": "user",
                "content": PROMPT_ARG_REPAIR.format(
                    tool_name=tool.name,
                    schema_json=json.dumps(tool.input_schema or {}, ensure_ascii=False),
                    args_json=json.dumps(args or {}, ensure_ascii=False),
                    error_text=error_text,
                ),
            }
        ]

        # parse ke pydantic kecil agar JSONnya rapih
        class _ArgFix(BaseModel):
            args: Dict[str, Any]

        try:
            fixed = await self.llm.responses_parse(
                input=messages, pydantic_model=_ArgFix
            )
        except Exception:
            fixed = await self.llm.chat_completions_parse(
                messages=messages, pydantic_model=_ArgFix
            )
        return fixed.args # type: ignore

    async def actor(self, objective: str) -> ActorTrace:
        tools = await self._list_tools()
        tools_json = json.dumps([t.model_dump() for t in tools], ensure_ascii=False)

        # 1) Putuskan answer atau tool
        decide_msgs = [
            {
                "role": "user",
                "content": PROMPT_ACTOR_DECIDE.format(
                    objective=objective,
                    tools_json=tools_json,
                ),
            }
        ]
        try:
            decision: ActorDecision = await self.llm.responses_parse(
                input=decide_msgs, pydantic_model=ActorDecision
            ) # type: ignore
        except Exception:
            decision = await self.llm.chat_completions_parse(
                messages=decide_msgs, pydantic_model=ActorDecision
            ) # type: ignore

        tool_attempts: List[StepResult] = []
        candidate_answer: str = decision.answer_draft or ""

        # 2) Jika pilih tool → call + retry jika gagal
        if decision.mode == "tool" and decision.tool and decision.tool.name:
            chosen = next((t for t in tools if t.name == decision.tool.name), None)
            if not chosen:
                candidate_answer = (
                    f"Tool '{decision.tool.name}' tidak tersedia. Coba jawab langsung."
                )
            else:
                # pertama
                first = await self._call_tool_once(
                    chosen.name, decision.tool.args or {}
                )
                tool_attempts.append(first)

                res_ok = first.ok
                result_payload = first.output

                # retry adaptif dengan argumen diperbaiki
                remaining = self.tool_retry
                last_args = decision.tool.args or {}
                while (not res_ok) and remaining > 0:
                    remaining -= 1
                    try:
                        new_args = await self._repair_args(
                            chosen, last_args, first.error or ""
                        )
                    except Exception:
                        new_args = last_args  # fallback: coba ulang argumen lama
                    retry_res = await self._call_tool_once(chosen.name, new_args)
                    tool_attempts.append(retry_res)
                    res_ok = retry_res.ok
                    result_payload = retry_res.output
                    last_args = new_args

                # 3) Sintesis hasil tool → candidate_answer
                synth_msgs = [
                    {
                        "role": "user",
                        "content": PROMPT_SYNTHESIZE.format(
                            objective=objective,
                            data_json=json.dumps(
                                {
                                    "attempts": [r.model_dump() for r in tool_attempts],
                                    "final_output": result_payload,
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    }
                ]
                try:

                    class _Ans(BaseModel):
                        text: str

                    ans = await self.llm.responses_parse(
                        input=synth_msgs, pydantic_model=_Ans
                    )
                except Exception:
                    ans = await self.llm.chat_completions_parse(
                        messages=synth_msgs, pydantic_model=_Ans
                    )
                candidate_answer = ans.text

        # fallback jika kosong
        candidate_answer = (
            candidate_answer or "Tidak ada jawaban yang memadai untuk saat ini."
        )

        return ActorTrace(
            decision=decision,
            tool_attempts=tool_attempts,
            candidate_answer=candidate_answer,
        )

    async def critic(self, objective: str, candidate_text: str) -> Critique:
        msgs = [
            {
                "role": "user",
                "content": PROMPT_CRITIC.format(
                    objective=objective,
                    candidate_text=candidate_text,
                ),
            }
        ]
        try:
            crt: Critique = await self.llm.responses_parse(
                input=msgs, pydantic_model=Critique
            )
        except Exception:
            crt = await self.llm.chat_completions_parse(
                messages=msgs, pydantic_model=Critique
            )
        return crt

    async def run(self, objective: str, *, max_loops: int = 1) -> Dict[str, Any]:
        """
        Jalankan Actor → Critic. Jika critic 'revise', lakukan 1 iterasi ekstra:
        - Jika ada new_tool/new_args → Actor panggil tool tsb, lalu Critic menilai lagi.
        - Jika hanya revised_text_hint → Actor revisi jawaban (tanpa tool) lalu dinilai lagi.
        - Jika ask_clarification → kembalikan flag agar UI bertanya ke user.
        """
        timeline: List[Dict[str, Any]] = []

        # Loop 1 (utama)
        t0 = time.time()
        trace = await self.actor(objective)
        t_actor = int((time.time() - t0) * 1000)
        timeline.append(
            {"phase": "actor", "time_ms": t_actor, "trace": trace.model_dump()}
        )

        t0 = time.time()
        crt = await self.critic(objective, trace.candidate_answer)
        t_critic = int((time.time() - t0) * 1000)
        timeline.append(
            {"phase": "critic", "time_ms": t_critic, "critique": crt.model_dump()}
        )

        if crt.verdict == "finalize":
            return {
                "status": "success",
                "finalize": True,
                "final_text": trace.candidate_answer,
                "timeline": timeline,
            }

        # Satu iterasi ekstra (opsional)
        if max_loops > 1:
            # Jika minta tool baru
            if crt.new_tool:
                # paksa Actor mode tool dengan new_tool/new_args
                forced_decision = ActorDecision(
                    mode="tool",
                    tool=ToolCall(name=crt.new_tool, args=crt.new_args or {}),
                )
                # pakai jalur eksekusi ringkas
                tools = await self._list_tools()
                chosen = next((t for t in tools if t.name == crt.new_tool), None)
                tool_attempts: List[StepResult] = []
                if chosen:
                    first = await self._call_tool_once(
                        chosen.name, forced_decision.tool.args
                    )
                    tool_attempts.append(first)
                    if not first.ok and self.tool_retry > 0:
                        new_args = await self._repair_args(
                            chosen, forced_decision.tool.args, first.error or ""
                        )
                        tool_attempts.append(
                            await self._call_tool_once(chosen.name, new_args)
                        )
                    # sintetis ulang
                    synth_msgs = [
                        {
                            "role": "user",
                            "content": PROMPT_SYNTHESIZE.format(
                                objective=objective,
                                data_json=json.dumps(
                                    {
                                        "attempts": [
                                            r.model_dump() for r in tool_attempts
                                        ],
                                        "final_output": (
                                            tool_attempts[-1].output
                                            if tool_attempts
                                            else None
                                        ),
                                    },
                                    ensure_ascii=False,
                                ),
                            ),
                        }
                    ]

                    class _Ans(BaseModel):
                        text: str

                    try:
                        ans = await self.llm.responses_parse(
                            input=synth_msgs, pydantic_model=_Ans
                        )
                    except Exception:
                        ans = await self.llm.chat_completions_parse(
                            messages=synth_msgs, pydantic_model=_Ans
                        )
                    trace2 = ActorTrace(
                        decision=forced_decision,
                        tool_attempts=tool_attempts,
                        candidate_answer=ans.text,
                    )
                else:
                    trace2 = ActorTrace(
                        decision=forced_decision,
                        tool_attempts=[],
                        candidate_answer=trace.candidate_answer,
                    )

                crt2 = await self.critic(objective, trace2.candidate_answer)
                timeline.append({"phase": "actor", "trace": trace2.model_dump()})
                timeline.append({"phase": "critic", "critique": crt2.model_dump()})
                if crt2.verdict == "finalize":
                    return {
                        "status": "success",
                        "finalize": True,
                        "final_text": trace2.candidate_answer,
                        "timeline": timeline,
                    }

            # Jika hanya perlu revisi teks
            if crt.revised_text_hint:
                msgs = [
                    {
                        "role": "user",
                        "content": PROMPT_SYNTHESIZE.format(
                            objective=objective,
                            data_json=json.dumps(
                                {
                                    "hint": crt.revised_text_hint,
                                    "draft": trace.candidate_answer,
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    }
                ]

                class _Ans(BaseModel):
                    text: str

                try:
                    ans = await self.llm.responses_parse(
                        input=msgs, pydantic_model=_Ans
                    )
                except Exception:
                    ans = await self.llm.chat_completions_parse(
                        messages=msgs, pydantic_model=_Ans
                    )
                final_text = ans.text
                crt3 = await self.critic(objective, final_text)
                timeline.append(
                    {"phase": "actor", "note": "revise-text", "text": final_text}
                )
                timeline.append({"phase": "critic", "critique": crt3.model_dump()})
                if crt3.verdict == "finalize":
                    return {
                        "status": "success",
                        "finalize": True,
                        "final_text": final_text,
                        "timeline": timeline,
                    }

        # Jika belum finalize
        return {
            "status": "needs_action",
            "finalize": False,
            "ask_clarification": bool(getattr(crt, "ask_clarification", False)),
            "suggestions": crt.suggestions or [],
            "timeline": timeline,
            "candidate_answer": trace.candidate_answer,
        }
