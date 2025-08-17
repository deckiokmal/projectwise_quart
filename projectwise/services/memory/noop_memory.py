# ADD: projectwise/services/memory/noop_memory.py
from __future__ import annotations
from typing import Any, Dict, List, Optional

class NoOpAsyncMemory:
    """Fallback memory saat vector store belum siap (degraded mode)."""
    # ADD: penanda internal
    degraded = True

    async def add(
        self,
        *,
        messages: List[Dict[str, str]],
        user_id: str,
        agent_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        infer: bool = False,
    ) -> Dict[str, Any]:
        # ADD: simulasi 'sukses' tanpa persist
        return {"ok": False, "degraded": True, "saved": 0}

    async def search(self, *, query: str, user_id: str, limit: int = 5) -> Dict[str, Any]:
        # ADD: selalu kosong di degraded mode
        return {"results": []}
