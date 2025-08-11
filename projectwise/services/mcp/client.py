from __future__ import annotations
import asyncio

from contextlib import AsyncExitStack, suppress
from typing import Any, Dict, List, Optional

from anyio import ClosedResourceError
from dotenv import load_dotenv

from projectwise.utils.logger import get_logger
from projectwise.config import ServiceConfigs
from openai import AsyncOpenAI
from mcp import ClientSession, JSONRPCError
from mcp.client.streamable_http import streamablehttp_client
from jsonschema import validate, ValidationError

load_dotenv()
settings = ServiceConfigs()
logger = get_logger("MCPClient")


class MCPClient:
    def __init__(self, model: str = settings.llm_model):
        # LLM, settings
        self.llm = AsyncOpenAI()
        self.model = model
        self.settings = settings

        # Connection state
        self._exit_stack = AsyncExitStack()
        self.session: Optional[ClientSession] = None
        self._connected = False

        # Background tasks
        self._keep_alive: Optional[asyncio.Task] = None
        self._tools_updater: Optional[asyncio.Task] = None

        # Reconnect coordination
        self._reconnect_lock = asyncio.Lock()
        self._reconnect_task: Optional[asyncio.Task] = None

        # Tool cache
        self.tool_cache: List[Dict[str, Any]] = []

    async def __aenter__(self) -> MCPClient:
        """
        Inisialisasi koneksi MCP secara lengkap:

        1. Masuk ke AsyncExitStack untuk manage lifecycle.
        2. Buka transport HTTP (streamable) ke server MCP.
        3. Buat ClientSession dari stream yang terbuka.
        4. Daftarkan handler notifikasi tools/list_changed.
        5. Inject capability elicitation dan daftarkan callback-nya.
        6. Lakukan handshake (initialize) dengan server.
        7. Tandai state sebagai terkoneksi (_connected=True).
        8. Jalankan task background:
           - _keep_alive_loop untuk heartbeat
           - _periodic_tools_update untuk refresh cache tool.
        9. Muat initial tool cache agar LLM punya daftar fungsi.
        """
        await self._exit_stack.__aenter__()
        try:
            # 1) Open HTTP stream only once
            read_s, write_s, _ = await self._exit_stack.enter_async_context(
                streamablehttp_client(self.settings.mcp_server_url)
            )

            # 2) Create MCP session
            self.session = await self._exit_stack.enter_async_context(
                ClientSession(
                    read_stream=read_s,
                    write_stream=write_s,
                    message_handler=self._on_session_message,  # type: ignore
                    elicitation_callback=self._handle_elicitation,  # type: ignore
                )
            )

            # 5) handshake
            await self.session.initialize()
            self._connected = True
            logger.info("MCP session initialized with elicitation support")

            # 6) Start background tasks
            self._keep_alive = asyncio.create_task(self._keep_alive_loop())
            self._tools_updater = asyncio.create_task(self._periodic_tools_update())

            # 7) Initial tool cache
            await self._refresh_tool_cache()
            logger.info(
                f"Initial tools: {[f['function']['name'] for f in self.tool_cache]}"
            )
            return self

        except Exception as e:
            logger.error("Failed to enter MCPClient context: %s", e, exc_info=True)
            await self._exit_stack.aclose()
            self._connected = False
            raise

    async def __aexit__(self, *args) -> None:
        """
        Tutup koneksi MCP dan bersihkan state:

        1. Batalkan semua background task (heartbeat, tools_updater, reconnect) dengan aman.
        2. Tutup ExitStack (stream & session) tanpa mem‐bubble exception.
        3. Reset properti internal:
           - set _connected=False
           - clear session, tool_cache, dan reference task.
        """
        # 1) Cancel background tasks cleanly
        for task in (self._keep_alive, self._tools_updater, self._reconnect_task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        # 2) Tear down session & transport without bubbling errors
        try:
            await self._exit_stack.aclose()
        except Exception as e:
            logger.warning("Ignored exit_stack.aclose error in __aexit__: %s", e)

        # 3) Cleanup internal state
        self._keep_alive = None
        self._tools_updater = None
        self._reconnect_task = None
        self._connected = False
        self.session = None
        self.tool_cache.clear()
        logger.info("MCPClient disconnected")

    async def connect(self) -> None:
        """
        Buka atau buka ulang koneksi MCP:

        1. Cek apakah sudah terkoneksi; jika iya, langsung keluar.
        2. Inisialisasi ulang AsyncExitStack untuk lifecycle baru.
        3. Panggil __aenter__() untuk:
           - membuka stream HTTP,
           - membuat session MCP,
           - melakukan handshake,
           - memulai task background,
           - memuat cache tool awal.
        """
        if self._connected:
            logger.info("Already connected")
            return
        self._exit_stack = AsyncExitStack()
        await self.__aenter__()  # reuse the context-manager logic

    async def shutdown(self) -> None:
        """
        Tutup koneksi MCP dengan rapi:

        1. Batalkan semua background task (heartbeat, tools_updater, reconnect) jika masih aktif.
        2. Tutup ExitStack untuk menutup stream dan session, sembunyikan error jika ada.
        3. Reset state internal:
           - _connected = False
           - session = None
           - kosongkan tool_cache
        """
        await self.__aexit__()

    # ———————— HEARTBEAT —————————
    async def _keep_alive_loop(self) -> None:
        """
        Loop heartbeat untuk menjaga koneksi tetap hidup:

        1. Dalam loop tak berhingga, tunggu 30 detik.
        2. Jika session hilang (None), keluar dari loop.
        3. Panggil tool "heartbeat" untuk cek kesehatan koneksi.
        4. Tangani CancelledError untuk menghentikan loop dengan tenang.
        5. Tangani JSONRPCError dengan logging peringatan.
        6. Tangani error lain dengan logging dan memicu reconnect otomatis.
        """
        try:
            while True:
                await asyncio.sleep(120)
                if not self.session:
                    return
                await self.call_tool("heartbeat", {})
        except asyncio.CancelledError:
            logger.info("keep_alive loop cancelled")
        except JSONRPCError as rpc:  # type: ignore
            logger.warning("Heartbeat RPC error: %s", rpc)
        except Exception as e:
            logger.warning("keep_alive error: %s", e, exc_info=True)
            await self._ensure_reconnected()

    # —————— TOOL CACHE REFRESH ————————
    async def _periodic_tools_update(self) -> None:
        """
        Perbarui cache tool secara berkala:

        1. Jika session belum aktif, hentikan eksekusi awal.
        2. Dalam loop tak berhingga:
           a. Tunggu 60 detik.
           b. Jika masih terkoneksi dan session tersedia, panggil _refresh_tool_cache().
        3. Tangani CancelledError untuk menghentikan loop tanpa error.
        4. Tangani JSONRPCError dengan logging peringatan.
        5. Tangani exception lain dengan logging detail.
        """
        if not self.session:
            logger.debug("Skipping tool update: no session")
            return

        try:
            while True:
                await asyncio.sleep(60)
                if self._connected and self.session:
                    await self._refresh_tool_cache()
        except asyncio.CancelledError:
            logger.info("tools_updater cancelled")
        except JSONRPCError as rpc:  # type: ignore
            logger.warning("tools_updater RPC error: %s", rpc)
        except Exception as e:
            logger.warning("tools_updater error: %s", e, exc_info=True)

    async def _refresh_tool_cache(self) -> None:
        """
        Muat ulang daftar tool dari server MCP:

        1. Jika session tidak tersedia, lewati pembaruan.
        2. Panggil session.list_tools() untuk mengambil metadata tool.
        3. Bangun ulang self.tool_cache sesuai schema yang diharapkan oleh LLM.
        4. Catat jumlah tool yang berhasil dimuat ulang.
        5. Jika gagal, tangani exception dengan logging peringatan.
        """
        if not self.session:
            logger.debug("Skipping tool cache refresh: no session")
            return

        try:
            resp = await self.session.list_tools()  # type: ignore
            self.tool_cache = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.inputSchema,
                    },
                }
                for t in resp.tools
            ]
            logger.debug("Tool cache refreshed (%d tools)", len(self.tool_cache))
        except Exception as e:
            logger.warning("refresh_tool_cache failed: %s", e, exc_info=True)

    # ————— MESSAGE HANDLER for notifications —————
    def _on_session_message(self, msg: Any) -> None:
        """
        Menangani pesan notifikasi dari server MCP:

        1. Dijalankan setiap kali ada message JSON-RPC masuk.
        2. Jika method == "notifications/tools/list_changed",
           jadwalkan pembaruan cache tool dengan _refresh_tool_cache().
        3. Log informasi penerimaan notifikasi untuk keperluan tracing.
        """
        # Auto‐refresh on tools/list_changed notification
        if getattr(msg, "method", "") == "notifications/tools/list_changed":
            logger.info("Received tools/list_changed, refreshing cache")
            asyncio.create_task(self._refresh_tool_cache())

    # ————— ELICITATION CALLBACK —————
    async def _handle_elicitation(self, params: dict) -> dict:
        """
        Tangani permintaan elicitation dari server MCP:

        1. Ambil `message` dan `requestedSchema` dari params.
        2. Jika schema tidak valid (bukan dict atau tanpa "properties"), log error dan kirim action "cancel".
        3. Tampilkan prompt ke user, tunjukkan fields yang diperlukan.
        4. Loop input:
           a. Tanyakan pilihan: [Enter]=accept, d=Decline, c=Cancel.
           b. Jika 'd', kembalikan {"action":"decline"}.
           c. Jika 'c', kembalikan {"action":"cancel"}.
           d. Jika accept, input nilai untuk tiap properti sesuai jenis (string, number, boolean).
           e. Validasi `content` dengan JSON Schema; jika gagal, ulangi form.
        5. Setelah valid, kembalikan {"action":"accept", "content": content}.
        """
        message = params["message"]
        schema = params["requestedSchema"]

        if not isinstance(schema, dict) or "properties" not in schema:
            logger.error("Invalid elicitation schema: %s", schema)
            return {"action": "cancel"}

        print(f"\n[Elicitation] {message}")
        print("Fields required:", schema.get("required", []))

        while True:
            # prompt user: accept, decline, or cancel
            choice = input("[Enter]=accept, d=Decline, c=Cancel: ").strip().lower()
            if choice == "d":
                return {"action": "decline"}
            if choice == "c":
                return {"action": "cancel"}

            # collect values
            content: dict = {}
            for prop, spec in schema.get("properties", {}).items():
                title = spec.get("title", prop)
                desc = spec.get("description", "")
                prompt = f"{title}{': ' + desc if desc else ''}: "

                while True:
                    raw = input(prompt).strip()
                    # boolean
                    if spec["type"] == "boolean":
                        if raw.lower() in ("true", "false"):
                            content[prop] = raw.lower() == "true"
                            break
                    # number/integer
                    elif spec["type"] in ("number", "integer"):
                        try:
                            num = float(raw) if spec["type"] == "number" else int(raw)
                            content[prop] = num
                            break
                        except ValueError:
                            print("Invalid number, try again.")
                    # string/enum
                    else:
                        content[prop] = raw
                        break

            # validate
            try:
                validate(instance=content, schema=schema)
                return {"action": "accept", "content": content}
            except ValidationError as ve:
                print(f"✕ Validation failed: {ve.message}. Silakan coba lagi.\n")
                # loop ulang seluruh form

    # ————— RECONNECT LOGIC —————
    async def _ensure_reconnected(self) -> None:
        """
        Pastikan koneksi MCP tetap hidup, dengan logika reconnect jika perlu:

        1. Jika sudah terhubung (self._connected True), langsung return.
        2. Jika ada tugas reconnect yang sedang berjalan, tunggu sampai selesai:
           a. Jika setelah tunggu masih belum terhubung, lempar ConnectionError.
        3. Dengan lock (_reconnect_lock), jadwalkan satu tugas reconnect:
           a. Buat task yang menjalankan _do_reconnect().
        4. Await task reconnect selesai.
        5. Jika setelah reconnect masih gagal (self._connected False), lempar ConnectionError.
        """
        if self._connected:
            return
        # If reconnect in progress, await it
        if self._reconnect_task and not self._reconnect_task.done():
            await self._reconnect_task
            if not self._connected:
                raise ConnectionError("Re-connect failed")
            return
        # Schedule a single reconnect
        async with self._reconnect_lock:
            if self._connected:
                return
            self._reconnect_task = asyncio.create_task(self._do_reconnect())
        await self._reconnect_task
        if not self._connected:
            raise ConnectionError("Re-connect failed")

    async def _do_reconnect(self) -> None:
        """
        Melakukan proses reconnect koneksi MCP secara lengkap:

        1. Log inisiasi teardown: hentikan dan bersihkan state lama.
        2. Panggil __aexit__() untuk menutup session & transport.
        3. Reset atribut terkait (_keep_alive, _tools_updater) menjadi None.
        4. Buat AsyncExitStack baru sebagai stack transport & session.
        5. Panggil __aenter__() untuk membangun kembali koneksi dan session.
        6. Jika sukses, set self._connected = True dan log keberhasilan.
        7. Jika gagal di mana pun, log error, dan biarkan self._connected = False.
        """
        try:
            logger.info("Tearing down for reconnect")
            await self.__aexit__()
            # cleanup taks lama
            self._keep_alive = None
            self._tools_updater = None
            # stack & session baru
            self._exit_stack = AsyncExitStack()
            logger.info("Rebuilding connection")
            await self.__aenter__()
            logger.info("Reconnect successful")
        except Exception as e:
            logger.error("Reconnect failed: %s", e, exc_info=True)
            self._connected = False

    # ————— CALL TOOL —————
    async def call_tool(self, name: str, args: Dict[str, Any]) -> Any:
        """
        Memanggil tool pada MCP server dengan penanganan koneksi dan error:

        1. Jika belum terhubung atau session None, panggil _ensure_reconnected() untuk reconnect.
        2. Log info nama tool dan argumen.
        3. Panggil self.session.call_tool(name, args) dan kembalikan res.content.
        4. Jika ClosedResourceError (stream tertutup), tandai disconnected, reconnect, lalu retry sekali.
        5. Jika JSONRPCError, log error spesifik dan lempar kembali.
        6. Untuk error lain, log unexpected error, tandai disconnected, dan lempar exception.
        """
        # Ensure connection
        if not self._connected or self.session is None:
            await self._ensure_reconnected()

        try:
            logger.info('call_tool "%s" args=%s', name, args)
            res = await self.session.call_tool(name, args)  # type: ignore
            return res.content
        except ClosedResourceError:
            logger.warning("Stream closed, triggering reconnect")
            self._connected = False
            await self._ensure_reconnected()
            res = await self.session.call_tool(name, args)  # type: ignore
            return res.content
        except JSONRPCError as e:  # type: ignore
            logger.error("RPC error [%s]: %s", e.code, e.message)  # type: ignore
            raise
        except Exception as e:
            logger.error("Unexpected call_tool error: %s", e, exc_info=True)
            self._connected = False
            raise
