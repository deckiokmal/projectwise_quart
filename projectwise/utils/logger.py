"""
projectwise/utils/logger.py → logger produksi dengan 3 mode:

file (default): tulis ke logs/YYYY-MM/<module>.log, rotasi harian, retensi (default 90 hari), month-aware, thread-safe, tanpa stream=None.

stdout: hanya ke console (serahkan rotasi/agregasi ke Docker/systemd/Loki, paling aman untuk multi-proses).

socket: kirim log ke listener terpisah via TCP (tanpa duplikasi & kontensi file antar proses).

utils/log_listener.py → listener TCP ringan yang menerima log dari banyak proses dan menulis per-module (tetap logs/YYYY-MM/<module>.log), rotasi harian + retensi, month-aware.
Jalankan: `python -m projectwise.utils.log_listener`
Semua konfigurasi cukup di .env di root proyek.
"""

# projectwise/utils/logger.py
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

from logging.handlers import TimedRotatingFileHandler, SocketHandler

# Quart bisa tak tersedia saat import util ini
try:
    from quart import current_app  # type: ignore
except Exception:  # pragma: no cover
    current_app = None  # type: ignore

# Pydantic v2
from pydantic_settings import BaseSettings, SettingsConfigDict


# ==========
# ENV loader
# ==========
def _detect_project_root() -> Path:
    """
    Cari akar proyek:
    - ENV PROJECT_ROOT
    - folder yang punya pyproject.toml atau .git
    - fallback: 3 level di atas file ini
    """
    env_root = os.getenv("PROJECT_ROOT")
    if env_root:
        return Path(env_root).resolve()

    here = Path(__file__).resolve()
    for p in [here] + list(here.parents):
        if (p / "pyproject.toml").exists() or (p / ".git").exists():
            return p
    return here.parents[2] if len(here.parents) >= 3 else here.parent


def _load_env_from_project_root_once() -> None:
    """
    Muat .env dari akar proyek → os.environ (jika ada).
    Tidak menimpa variabel yang sudah ada.
    """
    # sentinel sederhana agar hanya sekali
    if getattr(_load_env_from_project_root_once, "_done", False):
        return

    root = _detect_project_root()
    env_path = root / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                key = k.strip()
                val = v.strip().strip("'").strip('"')
                os.environ.setdefault(key, val)
        except Exception:
            pass
    _load_env_from_project_root_once._done = True  # type: ignore[attr-defined]


# ==========
# Settings
# ==========
class ProjectwiseLogSettings(BaseSettings):
    """
    Konfigurasi via ENV (prefix LOG_) / .env / overlay dari Quart app.config

      - LOG_MODE=file|stdout|socket
      - LOG_LEVEL=INFO|DEBUG|WARNING|ERROR
      - LOG_FORMAT="%(asctime)s %(levelname)s %(name)s: %(message)s"
      - LOG_DATEFMT="%Y-%m-%d %H:%M:%S"
      - LOG_RETENTION=90
      - LOG_ROOT_DIR="/path/proyek" (opsional; default autodetect)
      - LOG_CONSOLE=true|false
      - LOG_CONSOLE_LEVEL=INFO|DEBUG|... (opsional; default ikut LOG_LEVEL)
      - LOG_MONTH_FORMAT="%Y-%m"
      - LOG_USE_UTC=false|true
      - LOG_SOCKET_HOST=127.0.0.1
      - LOG_SOCKET_PORT=9020
    """

    model_config = SettingsConfigDict(
        env_prefix="LOG_",
        extra="ignore",
        env_file=".env",  # fallback; kita juga muat manual dari project root
        env_file_encoding="utf-8",
    )

    mode: str = "file"  # file | stdout | socket
    level: str = "INFO"
    format: str = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    datefmt: str = "%Y-%m-%d %H:%M:%S"
    retention: int = 90
    root_dir: Optional[Path] = None
    console: bool = True
    console_level: Optional[str] = None
    month_format: str = "%Y-%m"
    use_utc: bool = False
    socket_host: str = "127.0.0.1"
    socket_port: int = 9020


def _base_settings() -> ProjectwiseLogSettings:
    _load_env_from_project_root_once()
    return ProjectwiseLogSettings()


def _overlay_with_quart(s: ProjectwiseLogSettings) -> ProjectwiseLogSettings:
    """
    Overlay dari Quart app.config (jika ada context).
    Prioritas: app.config > ENV/.env.
    """
    if not current_app:
        return s
    try:
        cfg = current_app.config  # type: ignore[attr-defined]
    except Exception:
        return s

    # buat salinan (sifatnya ringan)
    merged = ProjectwiseLogSettings.model_construct(**s.model_dump())  # type: ignore
    # overlay nilai jika ada di app.config
    for key in (
        "LOG_MODE",
        "LOG_LEVEL",
        "LOG_FORMAT",
        "LOG_DATEFMT",
        "LOG_RETENTION",
        "LOG_ROOT_DIR",
        "LOG_CONSOLE",
        "LOG_CONSOLE_LEVEL",
        "LOG_MONTH_FORMAT",
        "LOG_USE_UTC",
        "LOG_SOCKET_HOST",
        "LOG_SOCKET_PORT",
    ):
        if key in cfg:
            val = cfg[key]
            attr = key[4:].lower()  # LOG_LEVEL -> level
            setattr(merged, attr, val)
    return merged


# ==========
# Utilities
# ==========
def _to_level(level: str) -> int:
    return getattr(logging, str(level).upper(), logging.INFO)


def _monthly_dir_factory_for(s: ProjectwiseLogSettings) -> Callable[[datetime], Path]:
    """
    Kembalikan fungsi factory direktori bulanan sesuai settings s.
    """
    base = (s.root_dir or _detect_project_root()) / "logs"

    def _factory(dt: datetime) -> Path:
        p = base / dt.strftime(s.month_format)
        p.mkdir(parents=True, exist_ok=True)
        return p

    return _factory


# =======================================================
# Month-aware TimedRotatingFileHandler (tanpa stream=None)
# =======================================================
class MonthAwareTimedRotatingFileHandler(TimedRotatingFileHandler):
    """
    - Rotasi harian dari stdlib (midnight).
    - Setelah rotasi, baseFilename dipindah ke direktori bulan terkini.
    - Tidak menyetel stream=None (aman untuk type-checker & performa).
    """

    def __init__(
        self,
        base_name: str,  # e.g. "routes.log"
        month_dir_factory: Callable[[datetime], Path],
        when: str = "midnight",
        backupCount: int = 90,
        encoding: str = "utf-8",
        utc: bool = False,
        delay: bool = False,  # langsung buka stream
    ):
        self._base_name = base_name
        self._month_dir_factory = month_dir_factory

        now_dt = datetime.utcnow() if utc else datetime.now()
        init_dir = month_dir_factory(now_dt)
        filename = str(init_dir / base_name)

        super().__init__(
            filename=filename,
            when=when,
            backupCount=backupCount,
            encoding=encoding,
            utc=utc,
            delay=delay,
        )

    def doRollover(self):
        if self.stream:
            try:
                self.stream.flush()
            except Exception:
                pass
            try:
                self.stream.close()
            except Exception:
                pass

        currentTime = int(time.time())
        t = self.rolloverAt - self.interval
        if self.utc:
            timeTuple = time.gmtime(t)
        else:
            timeTuple = time.localtime(t)
            dstNow = time.localtime(currentTime)[-1]
            dstThen = timeTuple[-1]
            if dstNow != dstThen:
                addend = 3600
                timeTuple = time.localtime(t + addend)

        # file hasil rotasi (di direktori lama)
        dfn = self.rotation_filename(
            self.baseFilename + "." + time.strftime(self.suffix, timeTuple)
        )
        try:
            if os.path.exists(dfn):
                os.remove(dfn)
        except Exception:
            pass

        try:
            if os.path.exists(self.baseFilename):
                os.rename(self.baseFilename, dfn)
        except FileExistsError:
            pass
        except Exception:
            pass

        # retensi
        if self.backupCount > 0:
            for s in self.getFilesToDelete():
                try:
                    os.remove(s)
                except Exception:
                    pass

        # pindahkan base ke direktori bulan terkini
        now_dt = datetime.utcnow() if self.utc else datetime.now()
        new_dir = self._month_dir_factory(now_dt)
        try:
            new_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self.baseFilename = str(Path(new_dir) / self._base_name)

        if not self.delay:
            self.stream = self._open()

        newRolloverAt = self.computeRollover(currentTime)
        while newRolloverAt <= currentTime:
            newRolloverAt = newRolloverAt + self.interval
        self.rolloverAt = newRolloverAt


# ===================================
# get_logger(name): lazy & race-safe
# ===================================
_init_lock = threading.Lock()
_inited_loggers: set[str] = set()


def get_logger(name: str) -> logging.Logger:
    """
    Logger 3-mode produksi (file/stdout/socket) dengan perilaku:
      - Per-module logfile: logs/YYYY-MM/<last-segment>.log (mode file)
      - Rotasi harian + retensi (default 90)
      - Month-aware (tak ada artefak file base)
      - Idempotent & thread-safe (hindari duplikasi handler)
      - Overlay config dari Quart app.config jika ada
    """
    # 1) Ambil settings (ENV/.env) lalu overlay Quart (bila ada context)
    s_env = _base_settings()
    s = _overlay_with_quart(s_env)

    mode = (s.mode or "file").lower().strip()

    logger = logging.getLogger(name)
    logger.setLevel(_to_level(s.level))
    logger.propagate = False

    # Fast path
    if name in _inited_loggers and logger.handlers:
        return logger

    # Slow path (race-safe)
    with _init_lock:
        if name in _inited_loggers and logger.handlers:
            return logger

        formatter = logging.Formatter(fmt=s.format, datefmt=s.datefmt)

        if mode == "stdout":
            ch = logging.StreamHandler(sys.stdout)
            ch.setLevel(_to_level(s.console_level or s.level))
            ch.setFormatter(formatter)
            logger.addHandler(ch)

        elif mode == "socket":
            sh = SocketHandler(s.socket_host, int(s.socket_port))
            sh.setLevel(_to_level(s.level))
            try:
                sh.closeOnError = True  # type: ignore[attr-defined]
            except Exception:
                pass
            logger.addHandler(sh)

            if s.console:
                ch = logging.StreamHandler(sys.stdout)
                ch.setLevel(_to_level(s.console_level or s.level))
                ch.setFormatter(formatter)
                logger.addHandler(ch)

        else:  # mode == "file" (default, single-process disarankan)
            # nama file berdasarkan segmen terakhir dari logger name
            last_segment = (name.rsplit(".", 1)[-1] or "app").replace(":", "_")
            base_name = f"{last_segment}.log"

            month_dir_factory = _monthly_dir_factory_for(s)
            fh = MonthAwareTimedRotatingFileHandler(
                base_name=base_name,
                month_dir_factory=month_dir_factory,
                when="midnight",
                backupCount=int(s.retention),
                encoding="utf-8",
                utc=bool(s.use_utc),
                delay=False,
            )
            fh.setLevel(_to_level(s.level))
            fh.setFormatter(formatter)
            logger.addHandler(fh)

            if s.console:
                ch = logging.StreamHandler(sys.stdout)
                ch.setLevel(_to_level(s.console_level or s.level))
                ch.setFormatter(formatter)
                logger.addHandler(ch)

        _inited_loggers.add(name)

    return logger
