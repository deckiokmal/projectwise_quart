# Dokumentasi Logger Produksi (3 Mode) — **ProjectWise / MCP**

Dokumen ini menjelaskan **fungsi**, **cara penggunaan**, dan **konfigurasi** *logger produksi 3 mode* (file / stdout / socket) untuk **aplikasi Quart** dan **MCP Server/Worker**. Termasuk panduan **log listener** dan **Docker Compose** untuk skenario multi-proses.

> **Default:** mode **file** (rotasi harian, retensi 90 hari, direktori per-bulan).
> Struktur berkas: `logs/YYYY-MM/<module>.log` (contoh: `logs/2025-08/extensions.log`).

---

## 1) Fitur Utama

* **3 Mode Output**

  * **file**: tulis ke `logs/YYYY-MM/<module>.log`, **rotasi harian**, **retensi** `LOG_RETENTION` hari.
  * **stdout**: kirim ke console (untuk Docker/systemd/journald/ELK).
  * **socket**: kirim ke **log listener TCP** terpusat (tanpa kontensi file di multi-proses).
* **Month-Aware**: saat ganti bulan, file base pindah otomatis ke direktori bulan baru **setelah rotasi** → **tidak ada artefak** file base di bulan lama.
* **Per-Module File**: `get_logger(__name__)` → nama berkas = segmen terakhir dari `__name__`.
* **Idempotent & Thread-Safe**: inisialisasi **lazy**, anti-duplikasi handler, race-safe.
* **Overlay Konfigurasi**: baca dari `.env` / ENV, dan **dapat dioverride** oleh `app.config["LOG_*"]` (untuk Quart).
* **Format Konsisten** (console & file):
  `%(asctime)s %(levelname)s %(name)s: %(message)s`

---

## 2) Variabel Konfigurasi (ENV / .env / app.config)

> Simpan di **akar proyek** pada file `.env` (atau set environment variables).
> Untuk Quart, Anda juga bisa override pada `app.config["LOG_*"]`.

```env
# Mode: file | stdout | socket
LOG_MODE=file

# Level & format
LOG_LEVEL=INFO
LOG_FORMAT=%(asctime)s %(levelname)s %(name)s: %(message)s
LOG_DATEFMT=%Y-%m-%d %H:%M:%S

# File mode
LOG_RETENTION=90
LOG_MONTH_FORMAT=%Y-%m
LOG_USE_UTC=false
# (opsional) paksa root dir penulisan log -> logs/ di bawah path ini
# LOG_ROOT_DIR=/abs/path/ke/proyek

# Console sink
LOG_CONSOLE=true
# LOG_CONSOLE_LEVEL=INFO

# Socket mode
LOG_SOCKET_HOST=127.0.0.1
LOG_SOCKET_PORT=9020
```

**Catatan penting:**

* Jika ada **model Pydantic lain** (mis. `ServiceConfigs`) yang juga membaca `.env`, set `model_config = SettingsConfigDict(..., extra="ignore")` agar **kunci `LOG_*`** yang tidak dikenali **diabaikan** (menghindari error `extra_forbidden`).
* Untuk Determinisme `.env`, panggil `load_dotenv(<path_ke_.env>)` secara eksplisit.

---

## 3) Cara Pakai di **Quart**

### 3.1. Impor dan gunakan di modul manapun

```python
from projectwise.utils.logger import get_logger
logger = get_logger(__name__)

logger.info("App start")
logger.error("Something wrong", exc_info=True)
```

### 3.2. Integrasi di app factory (opsional overlay di app.config)

```python
def create_app():
    app = Quart(__name__)
    # app.config.update(
    #     LOG_MODE="stdout",
    #     LOG_LEVEL="INFO",
    # )
    log = get_logger("quart.app")
    log.info("Quart created")
    return app
```

> Disarankan menamai logger inti web: **`"quart.app"`** agar file menjadi `quart.log`.

---

## 4) Cara Pakai di **MCP Server/Worker**

Gunakan pola yang sama:

```python
from mcp_server.utils.logger import get_logger
log = get_logger(__name__)
log.info("MCP worker up")
```

* **Single-process** → **LOG\_MODE=file** aman.
* **Multi-process** (scale worker) → gunakan **LOG\_MODE=stdout** atau **LOG\_MODE=socket** untuk menghindari kontensi file.

---

## 5) Mode **Socket** dengan **Log Listener**

### 5.1. Jalankan Log Listener (tanpa Docker)

```bash
python -m mcp_server.log_listener
# atau
python mcp_server/log_listener.py
```

* Listener menerima log via TCP dan menulis **per-module** ke `logs/YYYY-MM/*.log` dengan rotasi harian & retensi.
* Gunakan di **localhost** atau jaringan internal. Jika lintas host, amankan port (VPN / firewall / TLS terminator).

### 5.2. Konfigurasi Worker (Quart/MCP)

Pada `.env` worker:

```env
LOG_MODE=socket
LOG_SOCKET_HOST=127.0.0.1
LOG_SOCKET_PORT=9020
LOG_LEVEL=INFO
LOG_CONSOLE=false
```

---

## 6) **Docker Compose** (Listener + Worker)

> Contoh untuk menjalankan **listener** + **worker Quart/MCP** dalam satu Compose.
> Skalakan worker sesuai kebutuhan (multi-proses) tanpa kontensi file.

```yaml
version: "3.9"

services:
  log-listener:
    image: python:3.11-slim
    container_name: log-listener
    working_dir: /app
    volumes:
      - ./:/app:ro
      - ./logs:/app/logs
    environment:
      TZ: Asia/Jakarta
      PROJECT_ROOT: /app
      LOG_MODE: file
      LOG_LEVEL: INFO
      LOG_RETENTION: "90"
      LOG_FORMAT: "%(asctime)s %(levelname)s %(name)s: %(message)s"
      LOG_DATEFMT: "%Y-%m-%d %H:%M:%S"
      LOG_MONTH_FORMAT: "%Y-%m"
      LOG_USE_UTC: "false"
      LOG_SOCKET_HOST: 0.0.0.0
      LOG_SOCKET_PORT: "9020"
      LOG_CONSOLE: "true"
    command: ["python","-m","mcp_server.log_listener"]
    expose:
      - "9020"
    # ports:
    #   - "9020:9020"  # buka jika kirim log dari host lain
    healthcheck:
      test: ["CMD", "python", "-c", "import socket; s=socket.create_connection(('127.0.0.1',9020),1); s.close()"]
      interval: 5s
      timeout: 2s
      retries: 10
    restart: unless-stopped

  quart:
    image: python:3.11-slim
    working_dir: /app
    volumes:
      - ./:/app:ro
    environment:
      TZ: Asia/Jakarta
      PROJECT_ROOT: /app
      LOG_MODE: socket
      LOG_SOCKET_HOST: log-listener
      LOG_SOCKET_PORT: "9020"
      LOG_LEVEL: INFO
      LOG_CONSOLE: "false"
      LOG_FORMAT: "%(asctime)s %(levelname)s %(name)s: %(message)s"
      LOG_DATEFMT: "%Y-%m-%d %H:%M:%S"
    # command: ["python","-m","projectwise.app"]  # ganti sesuai entrypoint Anda
    command: >
      sh -c "pip install -r requirements.txt && python -m projectwise.app"
    depends_on:
      log-listener:
        condition: service_healthy
    restart: unless-stopped
```

**Perintah penting:**

```bash
# Jalankan
docker compose up -d

# Skala worker quart (mis. 8 proses)
docker compose up -d --scale quart=8
```

> Untuk MCP worker gunakan service berbeda (mis. `mcp-worker`) dengan env dan command serupa; prinsipnya sama.

---

## 7) Best Practices (Skala 100 → 10.000 koneksi)

* **Single-process async**: `LOG_MODE=file` cukup; I/O minim, rotasi & retensi aman.
* **Multi-process**:

  * **stdout** jika Anda pakai agregator (Docker logs, journald, Fluent Bit, Loki, ELK).
  * **socket** jika ingin **penulis file terpusat** (hindari kontensi file). Listener satu proses saja.
* **Keamanan Socket**: gunakan di jaringan internal; jika perlu lintas host, letakkan di belakang TLS/VPN.
* **Monitoring**: pantau ukuran direktori `logs/YYYY-MM/` dan jumlah berkas (rotasi harian + retensi akan menjaga kebersihan).
* **Zona Waktu**: default `LOG_USE_UTC=false` (waktu lokal). Jika cluster lintas zona, pertimbangkan `true`.

---

## 8) Troubleshooting

* **Pydantic `extra_forbidden` saat startup**
  Tambahkan `extra="ignore"` pada `SettingsConfigDict` model yang membaca `.env` selain logger (mis. `ServiceConfigs`), agar kunci `LOG_*` diabaikan.

* **`.env` tidak terbaca**
  Pastikan memanggil `load_dotenv(<path_ke_.env>)` atau set `PROJECT_ROOT` untuk autoload; atur **WorkingDirectory**/Mount di Docker dengan benar.

* **File log tidak di direktori yang diharapkan**
  Set `LOG_ROOT_DIR` (absolute path) jika struktur proyek non-standar. Pastikan proses punya izin tulis.

* **Duplikasi log baris**
  Pastikan `logger.propagate = False` (sudah diatur oleh modul) dan **hindari** menambahkan handler lain melalui `logging.basicConfig()`/`dictConfig()` tanpa maksud.

---

## 9) Ringkasan Cepat (Cheatsheet)

1. **Default**: letakkan `.env` → `LOG_MODE=file` (cukup untuk dev/single-process).
2. **Panggil** `get_logger(__name__)` di setiap modul yang butuh logging.
3. **Scale** multi-proses:

   * Pilih **stdout** (agregator) **atau** **socket** (log listener).
   * Jalankan **log listener** (Docker Compose disediakan).
   * Atur worker `LOG_MODE=socket` & arahkan ke `LOG_SOCKET_HOST:PORT`.
4. **Periksa**: file log di `logs/YYYY-MM/*.log` (rotasi tiap tengah malam, retensi `LOG_RETENTION`).
