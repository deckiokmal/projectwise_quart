# projectwise/services/workflow/reflection_actor.py
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional, Literal

from pydantic import BaseModel, Field, field_validator
from quart import current_app

from projectwise.services.llm_chain.llm_chains import LLMChains, Prefer

from projectwise.config import ServiceConfigs
from projectwise.utils.logger import get_logger
from projectwise.services.mcp.adapter import MCPToolAdapter


logger = get_logger(__name__)
settings = ServiceConfigs()


# ===============================================
# Pydantic Models — Plan, Execution, Critique
# ===============================================
class ToolSpec(BaseModel):
    name: str = Field(..., description="Nama MCP tool")
    description: Optional[str] = None
    input_schema: Optional[Dict[str, Any]] = Field(
        default=None, description="JSONSchema argumen tool (jika tersedia)"
    )


class PlanStep(BaseModel):
    id: str = Field(..., description="ID unik langkah")
    goal: str = Field(..., description="Tujuan spesifik langkah ini")
    tool: Optional[str] = Field(None, description="Nama MCP tool yang akan dipakai")
    args: Optional[Dict[str, Any]] = Field(
        default=None, description="Argumen untuk tool (harus sesuai input_schema)"
    )
    when: Optional[str] = Field(
        default=None, description="Kondisi/urutan eksekusi (opsional)"
    )
    success_criteria: Optional[str] = Field(
        default=None, description="Kriteria keberhasilan langkah"
    )
    expected_outputs: Optional[List[str]] = Field(
        default=None, description="Daftar output yang diharapkan"
    )

    @field_validator("id")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v:
            return str(uuid.uuid4())
        return v


class TaskPlan(BaseModel):
    overall_objective: str = Field(..., description="Sasaran akhir yang ingin dicapai")
    steps: List[PlanStep] = Field(default_factory=list)
    notes: Optional[str] = Field(
        default=None, description="Catatan strategi/constraint tambahan"
    )

    @field_validator("steps")
    @classmethod
    def _min_one(cls, v: List[PlanStep]) -> List[PlanStep]:
        if not v:
            raise ValueError("Plan harus memiliki minimal satu langkah")
        return v


class StepResult(BaseModel):
    step_id: str
    tool: Optional[str]
    args: Optional[Dict[str, Any]]
    ok: bool
    output: Any = None
    error: Optional[str] = None
    time_ms: int = 0


class ExecutionTrace(BaseModel):
    results: List[StepResult]
    summary: Optional[str] = None


class Critique(BaseModel):
    verdict: Literal["accept", "revise"] = Field(
        ..., description="Apakah hasil eksekusi sudah memadai?"
    )
    reasoning: str = Field(..., description="Alasan/verifikasi atas keputusan")
    issues: List[str] = Field(default_factory=list)
    recommendations: Optional[List[str]] = None
    next_action: Literal["finalize", "re_run", "ask_clarification"] = "finalize"
    revised_plan: Optional[TaskPlan] = None


# ===============================================
# Prompt — Planner, Actor (guidance), Critic
# ===============================================
PROMPT_PLANNER = (
    """
Anda adalah Planner untuk workflow plan→actor→critic. Tugas Anda:
1) Bentuk rencana eksekusi langkah-demi-langkah untuk mencapai objective.
2) Gunakan MCP tools yang tersedia (lihat daftar) hanya jika relevan & perlu.
3) Pastikan argumen tool sesuai JSONSchema input; isi nilai default bila aman.
4) Hasilkan rencana valid dalam format Pydantic `TaskPlan` (JSON), tanpa komentar.
5) Pastikan minimal 1 langkah dan tiap langkah memiliki tujuan yang jelas.

Prinsip:
- Konservatif terhadap efek samping. Gunakan tool yang bersifat read-only bila ragu.
- Jelaskan kriteria sukses tiap langkah agar mudah dievaluasi.
- Rencana ringkas namun bisa dieksekusi apa adanya oleh Actor.
"""
).strip()

PROMPT_ACTOR_GUIDE = (
    """
Anda adalah Actor. Ikuti rencana dari Planner secara deterministik.
Untuk tiap langkah:
- Jika tool tersedia: panggil tool dengan argumen yang disediakan.
- Validasi tipe argumen sederhana sebelum memanggil tool.
- Catat keluaran atau error tanpa mengubah rencana.
Output yang Anda kembalikan hanyalah ringkasan eksekusi (bukan keputusan).
"""
).strip()

PROMPT_CRITIC = (
    """
Anda adalah Critic. Evaluasi apakah eksekusi memenuhi objective & kriteria sukses.
- Jika hasil kurang memadai: jelaskan kekurangan, sarankan perbaikan, dan (opsional)
  berikan `revised_plan` yang ringkas untuk iterasi berikutnya.
- Jika sudah cukup: berikan alasan yang kuat.
Balas dalam format Pydantic `Critique` (JSON), tanpa komentar.
"""
).strip()


# ===============================================
# ReflectionActor — Orkestrator end-to-end
# ===============================================
class ReflectionActor:
    """Workflow plan → actor → critic berbasis LLMChains dan MCPToolAdapter.

    - Semua interaksi LLM melalui LLMChains (native-first + fallback kuat).
    - MCPToolAdapter dipakai untuk discovery & eksekusi tool.
    - Cocok dipanggil dari handler atau router (mis. routes/chat.py).
    """

    def __init__(
        self,
        *,
        llm: Optional[LLMChains] = None,
        mcp: Optional[MCPToolAdapter] = None,
        prefer: Prefer = "auto",
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        request_timeout: float = 90.0,
    ) -> None:
        self.llm = llm or LLMChains(
            model=model or settings.llm_model,
            prefer=prefer,
            temperature=temperature
            if temperature is not None
            else settings.llm_temperature,
            request_timeout=request_timeout,
        )
        self.mcp = mcp or MCPToolAdapter(
            current_app
        )  # diasumsikan adapter memegang koneksi
        self.prefer = prefer

    # ===============================================
    # Util internal — MCP tools & panggilan
    # ===============================================
    async def list_tools(self) -> List[ToolSpec]:
        """Ambil daftar tools dari MCP Adapter beserta input_schema-nya."""
        tools: List[ToolSpec] = []
        try:
            available = await self.mcp.get_tools()  # harus mengembalikan list dict

            # check prefer
            if (self.prefer or self.llm.prefer) == "responses":
                logger.info("List tools via responses format")
                for t in available:
                    tools.append(
                        ToolSpec(
                            name=t.get("name"),  # type: ignore
                            description=t.get("description"),
                            input_schema=(
                                t.get("inputSchema") or t.get("input_schema")
                            ),
                        )
                    )
            else:
                logger.info("List tools via chat completions format")
                for t in available:
                    fn = t.get("function", {})
                    tools.append(
                        ToolSpec(
                            name=fn.get("name"),
                            description=fn.get("description"),
                            input_schema=fn.get("parameters"),
                        )
                    )
        except Exception:
            logger.exception("Gagal mengambil daftar MCP tools")
        return tools

    async def _call_tool(self, name: str, args: Dict[str, Any]) -> Any:
        """Pembungkus pemanggilan tool MCP dengan logging & error handling."""
        t0 = time.time()
        try:
            logger.info(
                "call_tool %s args=%s", name, json.dumps(args, ensure_ascii=False)
            )
            res = await self.mcp.call_tool(name, args)
            dt = int((time.time() - t0) * 1000)
            return True, res, None, dt
        except Exception as e:
            logger.exception("Call MCP tool gagal: %s", name)
            dt = int((time.time() - t0) * 1000)
            return False, None, str(e), dt

    # ===============================================
    # Main methods — plan
    # ===============================================
    async def plan(self, objective: str) -> TaskPlan:
        """Bangun rencana TaskPlan memakai LLMChains dengan konteks tools MCP."""
        tools = await self.list_tools()
        tools_json = [t.model_dump() for t in tools]
        # logger.info("Plan: Available tools: %s", json.dumps(tools_json, ensure_ascii=False))

        messages = [
            {"role": "system", "content": PROMPT_PLANNER},
            {
                "role": "user",
                "content": (
                    "Objective:\n" + objective + "\n\n"
                    "TOOLS (JSON):\n" + json.dumps(tools_json, ensure_ascii=False)
                ),
            },
        ]

        # Preferensi engine diatur oleh self.llm.prefer, fallback otomatis di LLMChains
        try:
            if (self.prefer or self.llm.prefer) == "responses":
                plan = await self.llm.responses_parse(
                    input=messages, pydantic_model=TaskPlan
                )
            elif (self.prefer or self.llm.prefer) == "chat":
                plan = await self.llm.chat_completions_parse(
                    messages=messages, pydantic_model=TaskPlan
                )
            else:
                # AUTO: gunakan responses terlebih dahulu, lalu fallback chat
                try:
                    plan = await self.llm.responses_parse(
                        input=messages, pydantic_model=TaskPlan
                    )
                except Exception:
                    plan = await self.llm.chat_completions_parse(
                        messages=messages, pydantic_model=TaskPlan
                    )
        except Exception as e:
            logger.exception(
                "Gagal membuat rencana; fallback ekstra via chat schema: %s", e
            )
            plan = await self.llm.chat_completions_parse(
                messages=messages, pydantic_model=TaskPlan
            )

        return plan  # type: ignore

    # ===============================================
    # Main methods — actor
    # ===============================================
    async def actor(self, plan: TaskPlan) -> ExecutionTrace:
        """Jalankan tiap langkah pada plan, memanggil MCP tools sesuai definisi."""
        results: List[StepResult] = []

        # Berikan guidance singkat untuk dokumentasi/traceability
        logger.info("[actor] %s", PROMPT_ACTOR_GUIDE.replace("\n", " | "))

        for step in plan.steps:
            ok: bool
            out: Any
            err: Optional[str]
            dt: int

            if step.tool:
                ok, out, err, dt = await self._call_tool(step.tool, step.args or {})
            else:
                # Langkah tanpa tool → tidak ada eksekusi MCP, beri output deskriptif
                t0 = time.time()
                out = {
                    "note": "Langkah ini tidak memanggil tool. Lewati eksekusi MCP.",
                    "goal": step.goal,
                }
                err = None
                ok = True
                dt = int((time.time() - t0) * 1000)

            results.append(
                StepResult(
                    step_id=step.id,
                    tool=step.tool,
                    args=step.args,
                    ok=ok,
                    output=out,
                    error=err,
                    time_ms=dt,
                )
            )

        # Ringkas hasil untuk bahan Critic
        summary = {
            "ok": all(r.ok for r in results),
            "n_steps": len(results),
            "errors": [r.model_dump() for r in results if not r.ok],
        }
        return ExecutionTrace(
            results=results, summary=json.dumps(summary, ensure_ascii=False)
        )

    # ===============================================
    # Main methods — critic
    # ===============================================
    async def critic(
        self, objective: str, plan: TaskPlan, trace: ExecutionTrace
    ) -> Critique:
        """Evaluasi hasil eksekusi dan sarankan revisi bila perlu."""
        messages = [
            {"role": "system", "content": PROMPT_CRITIC},
            {
                "role": "user",
                "content": (
                    "Objective:\n" + objective + "\n\n"
                    "Plan (JSON):\n" + plan.model_dump_json() + "\n\n"
                    "Execution Trace (JSON):\n" + trace.model_dump_json()
                ),
            },
        ]

        try:
            if (self.prefer or self.llm.prefer) == "responses":
                crt = await self.llm.responses_parse(
                    input=messages, pydantic_model=Critique
                )
            elif (self.prefer or self.llm.prefer) == "chat":
                crt = await self.llm.chat_completions_parse(
                    messages=messages, pydantic_model=Critique, max_tokens=15000
                )
            else:
                # AUTO → coba responses dulu, fallback chat
                try:
                    crt = await self.llm.responses_parse(
                        input=messages, pydantic_model=Critique
                    )
                except Exception:
                    crt = await self.llm.chat_completions_parse(
                        messages=messages, pydantic_model=Critique
                    )
        except Exception:
            logger.exception("Gagal mengkritisi hasil; fallback ekstra via chat schema")
            crt = await self.llm.chat_completions_parse(
                messages=messages, pydantic_model=Critique
            )

        return crt  # type: ignore

    # ===============================================
    # Main methods — run full cycle
    # ===============================================
    async def run(
        self,
        objective: str,
        *,
        max_loops: int = 2,
    ) -> Dict[str, Any]:
        """Jalankan siklus lengkap: plan → actor → critic (dengan iterasi terbatas).

        max_loops: jumlah maksimum iterasi perbaikan (critic → revised_plan → actor ...)
        Return dict ringkas yang siap dipakai handler/route lain.
        """
        timeline: List[Dict[str, Any]] = []

        for i in range(max_loops):
            # PLAN
            t0 = time.time()
            plan = await self.plan(objective)
            t_plan = int((time.time() - t0) * 1000)
            timeline.append(
                {"phase": "plan", "time_ms": t_plan, "plan": plan.model_dump()}
            )

            # ACTOR
            t0 = time.time()
            trace = await self.actor(plan)
            t_actor = int((time.time() - t0) * 1000)
            timeline.append(
                {"phase": "actor", "time_ms": t_actor, "trace": trace.model_dump()}
            )

            # CRITIC
            t0 = time.time()
            critique = await self.critic(objective, plan, trace)
            t_critic = int((time.time() - t0) * 1000)
            timeline.append(
                {
                    "phase": "critic",
                    "time_ms": t_critic,
                    "critique": critique.model_dump(),
                }
            )

            logger.info(
                "[reflection] loop=%d verdict=%s next=%s",
                i + 1,
                critique.verdict,
                critique.next_action,
            )

            if critique.verdict == "accept" or critique.next_action == "finalize":
                break

            if critique.next_action == "re_run" and critique.revised_plan:
                # Gunakan revised_plan untuk iterasi berikutnya
                plan = critique.revised_plan
                # lanjut loop berikutnya (akan regenerate actor→critic)
            elif critique.next_action == "ask_clarification":
                # Berhenti dan minta klarifikasi pada sisi UI/handler
                break
            else:
                # Tidak ada revised_plan → keluar agar tidak loop tanpa kemajuan
                break

        return {
            "status": "success",
            "message": "Reflection workflow selesai",
            "timeline": timeline,
            "final": {
                "plan": plan.model_dump() if "plan" in locals() else None,
                "trace": trace.model_dump() if "trace" in locals() else None,
                "critique": critique.model_dump() if "critique" in locals() else None,
            },
        }
