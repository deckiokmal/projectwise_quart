# schema_validator.py
from __future__ import annotations
from typing import Any, Dict, List, Tuple
from jsonschema import Draft202012Validator, FormatChecker
from functools import lru_cache
import json
import time
from dataclasses import dataclass
import contextvars


format_checker = FormatChecker()


# ==============================
# Validation tools args schema
# ==============================


def _coerce_basic_types(arg: Any, schema: Dict[str, Any]) -> Any:
    """
    Coerce ringan untuk kasus umum ketika LLM mengembalikan string:
    - number -> float/int
    - integer -> int
    - boolean -> bool ("true"/"false"/"1"/"0")
    - array/object dari string JSON
    Catatan: sengaja minimal. Hindari over-coercion yang bisa menyesatkan.
    """
    import json

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
                x = json.loads(arg)
                if isinstance(x, list):
                    return x
            except Exception:
                pass
    if t == "object":
        if isinstance(arg, str):
            try:
                x = json.loads(arg)
                if isinstance(x, dict):
                    return x
            except Exception:
                pass
    return arg


def _apply_shallow_coercion(
    args: Dict[str, Any], schema: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Coercion ringan hanya pada properti level-1 untuk menjaga keamanan.
    Jika butuh deep coercion, bisa direkursifkan sesuai kebutuhan.
    """
    if not isinstance(args, dict):
        return args
    props = schema.get("properties") or {}
    out = dict(args)
    for k, v in list(out.items()):
        sub_schema = props.get(k, {})
        out[k] = _coerce_basic_types(v, sub_schema)
    return out


def validate_tool_args(
    tool_name: str,
    args: Dict[str, Any],
    input_schema: Dict[str, Any],
    coerce: bool = True,
) -> Tuple[bool, List[Dict[str, Any]], Dict[str, Any]]:
    """
    Validasi args terhadap input_schema JSON Schema Draft 2020-12.
    Return:
      - ok: bool
      - errors: list of {path, message, schema_path}
      - possibly_coerced_args: args yang sudah di-coerce ringan (jika aktif)
    """
    schema = input_schema or {}
    # Pastikan tipe object default bila tidak ditentukan (beberapa server MCP demikian)
    if "type" not in schema:
        schema = {"type": "object", **schema}

    coerced = _apply_shallow_coercion(args, schema) if coerce else args

    validator = Draft202012Validator(schema, format_checker=format_checker)
    errors: List[Dict[str, Any]] = []
    for e in validator.iter_errors(coerced):
        # e.path: deque path ke field yang error
        # e.schema_path: path ke bagian schema
        errors.append(
            {
                "path": ".".join([str(x) for x in e.path]) or "<root>",
                "message": e.message,
                "schema_path": "/".join([str(x) for x in e.schema_path]),
                "validator": e.validator,
            }
        )

    return (len(errors) == 0, errors, coerced)


# ==============================
# schema_cache.py
# ==============================


@lru_cache(maxsize=512)
def get_validator(schema_fingerprint: str):
    schema = json.loads(schema_fingerprint)
    return Draft202012Validator(schema, format_checker=format_checker)


def make_fingerprint(schema: dict) -> str:
    # Normalisasi → hash → cache key
    payload = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return json.dumps(json.loads(payload), sort_keys=True, separators=(",", ":"))


# ==============================
# circuit breaker
# ==============================


@dataclass
class BreakerState:
    failures: int = 0
    opened_until: float = 0.0


class CircuitBreaker:
    def __init__(self, threshold=5, cooldown=15.0):
        self.threshold = threshold
        self.cooldown = cooldown
        self._state = {}

    def allow(self, key: str) -> bool:
        st = self._state.get(key)
        if not st:
            return True
        if time.time() < st.opened_until:
            return False
        return True

    def record(self, key: str, ok: bool):
        st = self._state.setdefault(key, BreakerState())
        if ok:
            st.failures = 0
            st.opened_until = 0.0
        else:
            st.failures += 1
            if st.failures >= self.threshold:
                st.opened_until = time.time() + self.cooldown


# ==============================
# timeout_budget.py
# ==============================


_budget = contextvars.ContextVar("timeout_budget", default=None)


class TimeoutBudget:
    def __init__(self, total_s: float):
        self.deadline = time.monotonic() + total_s

    def remaining(self) -> float:
        return max(0.01, self.deadline - time.monotonic())


def start_budget(total_s: float):
    _budget.set(TimeoutBudget(total_s)) # type: ignore


def remaining_or(default: float) -> float:
    b = _budget.get()
    return b.remaining() if b else default
