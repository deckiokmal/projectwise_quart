# projectwise/services/workflow/handler_proposal_generation.py
from __future__ import annotations

import asyncio
import json
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple, Union

from quart import Quart
from openai import APIConnectionError

from projectwise.utils.logger import get_logger
from projectwise.utils.llm_io import json_loads_safe, short_str, shape_user, shape_system, shape_assistant_text, extract_assistant_and_tool_calls_from_responses

from .prompt_instruction import PROMPT_PROPOSAL_GUIDELINES

# ==== Ambil util & adapter dari reflexion_actor (acuan utama) ====
#   - MCPToolAdapter: memakai app.extensions (mcp, mcp_lock, mcp_status, tool_cache)
#   - normalize_mcp_tools: menyiapkan schema function-calling yang bersih
#   - validate_tool_args: opsional validasi argumen (jika Anda aktifkan)
try:
    from .reflexion_actor import (
        MCPToolAdapter,
        normalize_mcp_tools,
        validate_tool_args,  # boleh None kalau tak ada
    )
except Exception as e:
    raise RuntimeError(
        "Wajib tersedia reflexion_actor.py dengan MCPToolAdapter/normalize_mcp_tools."
    ) from e


logger = get_logger(__name__)


# ====== State Mesin ======
class _State(Enum):
    INITIAL = auto()
    RAW_READY = auto()
    PLACEHOLDERS_OBTAINED = auto()
    CONTEXT_SENT = auto()
    DOC_SAVED = auto()


async def run(
    client,
    project_name: str,
    user_query: Optional[str] = None,
    override_template: Optional[str] = None,
    max_turns: int = 12,
    llm_timeout_sec: float = 60.0,
    tool_timeout_sec: float = 90.0,
    *,
    app: Quart,                  # wajib: untuk MCPToolAdapter
    use_schema_validation: bool = True,
) -> str:
    """
    Workflow Proposal (Responses API + MCP tools via ReflexionActor)

    Urutan baku:
      1) project_context_for_proposal(project_name)
      2) get_template_placeholders()
      3) generate_proposal_docx(override_template?)   ← input context JSON dikirim dulu oleh LLM

    Catatan:
    - client adalah Object AsyncOpenAI.
    - Menggunakan MCPToolAdapter dari reflexion_actor dengan app.extensions.
    - Tools diambil dari tool_cache (via adapter) lalu dinormalisasi -> format Responses API.
    - Gunakan Responses API (bukan Chat Completions).
    """
    log = logger
    log.info(
        "[proposal] start | project=%r | override_template=%r | turns=%d | t_llm=%ss | t_tool=%ss",
        project_name, bool(override_template), max_turns, llm_timeout_sec, tool_timeout_sec
    )
    
    

    # Normalisasi nama proyek (hindari ekstensi)
    if project_name.lower().endswith((".md", ".txt")):
        project_name = project_name.rsplit(".", 1)[0]

    # ===== Adapter & tools =====
    mcp = MCPToolAdapter(app)  # patuh mcp_lock & status
    try:
        raw_tools = await asyncio.wait_for(mcp.get_tools(), timeout=llm_timeout_sec)
        tools_for_llm, registry = normalize_mcp_tools(raw_tools or [])
        log.info("[proposal] tools ready: raw=%d normalized=%d", len(raw_tools or []), len(tools_for_llm))
    except Exception:
        log.exception("[proposal] gagal menyiapkan tools (tool_cache/normalisasi).")
        return "Gagal menyiapkan tools untuk proposal."

    # ===== Setup pesan awal =====
    system_prompt = PROMPT_PROPOSAL_GUIDELINES()
    first_user = user_query or f"Buatkan proposal untuk proyek '{project_name}'. Ikuti prosedur."
    messages: List[Dict[str, Any]] = [
        shape_system(system_prompt),
        shape_user(first_user),
    ]

    state: _State = _State.INITIAL
    placeholders: List[str] = []
    doc_path: Optional[str] = None
    retries: Dict[str, int] = {}

    sem = asyncio.Semaphore(5)

    async def _call_tool(name: str, args: Dict[str, Any]) -> str:
        async with sem:
            # Validasi skema argumen (opsional; registry schema detail sudah dinormalisasi)
            try:
                if use_schema_validation and validate_tool_args:
                    schema = (registry.get(name) or {}).get("parameters") or {"type": "object"}
                    args = validate_tool_args(schema, args)
            except Exception:
                log.exception("[proposal] schema validation warning for tool=%s", name)

            log.info("[proposal] TOOL → %s | args=%s", name, short_str(args))
            try:
                out = await asyncio.wait_for(mcp.call_tool(name, args), timeout=tool_timeout_sec)
                if not isinstance(out, str):
                    out = json.dumps(out, ensure_ascii=False)
                log.info("[proposal] TOOL ← %s | out=%s", name, short_str(out))
                return out
            except asyncio.TimeoutError:
                log.exception("[proposal] TOOL TIMEOUT ← %s", name)
                return json.dumps({"status": "failure", "error": f"Timeout {tool_timeout_sec}s"}, ensure_ascii=False)
            except Exception as e:
                log.exception("[proposal] TOOL ERROR ← %s", name)
                return json.dumps({"status": "failure", "error": str(e)}, ensure_ascii=False)

    def _context_complete(ctx_json: str) -> bool:
        try:
            data = json.loads(ctx_json)
            ok = isinstance(data, dict) and all(k in data for k in placeholders)
            if not ok:
                missing = [k for k in placeholders if k not in (data or {})]
                log.warning("[proposal] context missing=%s", missing)
            else:
                log.info("[proposal] context complete (%d placeholders).", len(placeholders))
            return ok
        except Exception:
            log.exception("[proposal] gagal parse context JSON.")
            return False

    # ===== Main loop =====
    for turn in range(max_turns):
        log.info("[proposal] turn %d/%d | state=%s | messages=%d", turn + 1, max_turns, state.name, len(messages))

        # tool_choice deterministik
        tool_choice: Union[str, Dict[str, Any]] = "auto"
        if state is _State.INITIAL and retries.get("project_context_for_proposal", 0) == 0:
            tool_choice = {"type": "function", "function": {"name": "project_context_for_proposal"}}
        elif state is _State.RAW_READY and retries.get("get_template_placeholders", 0) == 0:
            tool_choice = {"type": "function", "function": {"name": "get_template_placeholders"}}
        elif state in {_State.PLACEHOLDERS_OBTAINED, _State.CONTEXT_SENT} and retries.get("generate_proposal_docx", 0) == 0:
            tool_choice = {"type": "function", "function": {"name": "generate_proposal_docx"}}
        log.debug("[proposal] tool_choice=%s", tool_choice)

        # ==== Panggil LLM via Responses API ====
        try:
            resp = await asyncio.wait_for(
                client.llm.responses.create(  # Responses API (bukan chat.completions)
                    model=client.model,
                    input=messages,         # banyak SDK menerima daftar pesan ala ChatML
                    tools=tools_for_llm,    # tools sudah dinormalisasi (function schemas)
                    tool_choice=tool_choice,
                ),
                timeout=llm_timeout_sec,
            )
        except asyncio.TimeoutError:
            log.exception("[proposal] LLM TIMEOUT (turn=%d).", turn + 1)
            return "Timeout saat meminta keputusan model."
        except APIConnectionError:
            logger.error("LLM APIConnectionError.")
            human = "LLM API Connection Error. Silakan coba lagi."
            raise RuntimeError(human)
        except Exception:
            log.exception("[proposal] LLM ERROR (turn=%d).", turn + 1)
            return "Gagal berkomunikasi dengan model."

        # Ekstrak assistant + tool_calls dari Responses API (tahan-banting)
        assistant_text, tool_calls = extract_assistant_and_tool_calls_from_responses(resp)

        # Tambahkan assistant (text/tool_calls) ke messages untuk sejarah
        if assistant_text:
            messages.append(shape_assistant_text(assistant_text))
            log.debug("[proposal] assistant_text_len=%d", len(assistant_text))

        if not tool_calls:
            # Tidak ada tool_calls
            log.info("[proposal] no tool_calls | state=%s", state.name)

            if state is _State.PLACEHOLDERS_OBTAINED:
                # Tahap user mengirim JSON context
                ctx_raw = assistant_text or ""
                if not _context_complete(ctx_raw):
                    retries["context"] = retries.get("context", 0) + 1
                    if retries["context"] <= 1:
                        missing = [ph for ph in placeholders if ph not in json_loads_safe(ctx_raw)]
                        messages.append(shape_user(
                            "Sebagian placeholder masih kosong: "
                            f"{missing}. Kirim ulang **hanya JSON** "
                            "dengan format {\"placeholder\": \"nilai\"}."
                        ))
                        log.warning("[proposal] meminta pelengkapan context; retry=%d", retries["context"])
                        continue
                    return "Placeholder belum lengkap setelah 2× percobaan."

                # context lengkap → reset pesan agar ringkas untuk generate
                messages = [
                    shape_system(system_prompt),
                    shape_user(ctx_raw)
                ]
                log.info("[proposal] context dikirim. state: %s → CONTEXT_SENT", state.name)
                state = _State.CONTEXT_SENT
                continue

            if state is _State.DOC_SAVED:
                return assistant_text or f"Proposal berhasil dibuat di {doc_path}"

            # tak ada aksi; lanjut loop
            continue

        # ===== Ada tool_calls → eksekusi berurutan sesuai urutan panggilan =====
        # (Urutan tool secara kontrak tetap)
        tool_results: List[Tuple[str, str, str]] = []  # (fname, content_str, call_id)
        for tc in tool_calls:
            fname = tc["function"]["name"]
            fargs = json_loads_safe(tc["function"]["arguments"]) or {}

            # Isi argumen default sesuai kontrak
            if fname == "project_context_for_proposal":
                fargs.setdefault("project_name", project_name)
            elif fname == "generate_proposal_docx" and override_template:
                fargs.setdefault("override_template", override_template)

            # Eksekusi tool (timeout & logging di wrapper)
            raw = await _call_tool(fname, fargs)
            content_str = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)

            # Tambahkan hasil tool ke messages dalam format Responses-compatible
            # Banyak SDK Responses bisa terima {"role":"tool","tool_call_id":..., "name":..., "content":...}
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "name": fname,
                "content": content_str,
            })
            tool_results.append((fname, content_str, tc["id"]))
            log.debug("[proposal] tool_result appended: %s | len=%d", fname, len(content_str))

        # ===== Evaluasi hasil tiap tool & transisi state =====
        state_before = state
        for fname, content_str, _ in tool_results:
            payload = json_loads_safe(content_str)

            # 1) project_context_for_proposal
            if fname == "project_context_for_proposal" and state is _State.INITIAL:
                if payload.get("status") != "success":
                    retries[fname] = retries.get(fname, 0) + 1
                    log.warning("[proposal] project_context_for_proposal gagal. retry=%d", retries[fname])
                    if retries[fname] <= 1:
                        state = _State.INITIAL
                        break
                    return payload.get("error", "Dokumen proyek tidak ditemukan.")
                text = payload.get("text", "")
                messages.append(shape_user(text))
                state = _State.RAW_READY

            # 2) get_template_placeholders
            elif fname == "get_template_placeholders" and state is _State.RAW_READY:
                placeholders.clear()
                if isinstance(payload, list):
                    placeholders.extend(payload)
                elif isinstance(payload.get("placeholders"), list):
                    placeholders.extend(payload["placeholders"])
                messages.append(shape_user(f"Daftar placeholder: {placeholders}"))
                log.info("[proposal] placeholders=%d", len(placeholders))
                state = _State.PLACEHOLDERS_OBTAINED

            # 3) generate_proposal_docx
            elif fname == "generate_proposal_docx":
                if payload.get("status") != "success":
                    retries[fname] = retries.get(fname, 0) + 1
                    log.warning("[proposal] generate_proposal_docx gagal. retry=%d", retries[fname])
                    if retries[fname] <= 1:
                        state = _State.CONTEXT_SENT  # minta model mencoba lagi
                        break
                    return payload.get("error", "Gagal membuat proposal.")
                doc_path = payload.get("path")
                log.info("[proposal] dokumen tersimpan: %s", doc_path)
                state = _State.DOC_SAVED

        if state != state_before:
            log.info("[proposal] STATE: %s → %s", state_before.name, state.name)

        # lanjut loop
        continue

    # ==== Batas iterasi tercapai ====
    log.error("[proposal] berhenti: mencapai batas iterasi. last_state=%s", state.name)
    return doc_path or "Workflow berhenti: mencapai batas maksimum iterasi."
