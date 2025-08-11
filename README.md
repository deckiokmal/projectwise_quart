# **Quart full async**

---

## **Struktur Folder Proyek (Best Practice untuk Quart + Async)**

```
myapp/
│
├── main.py                     # Entry point aplikasi (async, jalankan Hypercorn)
│
├── myapp/                      # Paket utama aplikasi
│   ├── __init__.py              # create_app() async
│   ├── config.py                # Konfigurasi environment
│   ├── extensions.py            # Registrasi MCP client & resource async lainnya
│   ├── routes/                  # Semua route / blueprint
│   │   ├── __init__.py
│   │   ├── ui.py                 # UI routes (HTML/JS/CSS)
│   │   ├── api_chat.py           # API chat routes
│   │   └── api_control.py        # API kontrol MCP server
│   │
│   ├── services/                 # Layanan & integrasi eksternal
│   │   ├── __init__.py
│   │   ├── mcp_client.py         # Wrapper MCPClient async
│   │   └── db.py                  # (Opsional) DB async
│   │
│   ├── templates/                # File HTML (Jinja2 untuk Quart)
│   │   └── index.html
│   │
│   ├── static/                   # File statis (CSS, JS, gambar)
│   │   ├── css/
│   │   ├── js/
│   │   └── images/
│   │
│   └── utils/                    # Helper functions
│       ├── __init__.py
│       └── logger.py
│
├── tests/                        # Unit tests & integration tests
│   └── test_chat.py
│
├── requirements.txt              # Dependensi Python
└── README.md                     # Dokumentasi proyek
```

---

## **Penjelasan Struktur**

* **`main.py`**
  Entry point. Menjalankan `asyncio.run()` untuk `serve()` pakai Hypercorn ASGI server.

* **`myapp/__init__.py`**
  Fungsi `async def create_app()` yang menginisialisasi app Quart, load config, register blueprint, dan init resource async (MCP client).

* **`myapp/extensions.py`**
  Menyimpan koneksi global (misalnya MCPClient, DB pool) supaya bisa diakses di seluruh route tanpa inisialisasi ulang.

* **`myapp/routes/`**
  Dipisah antara UI (HTML render) dan API (JSON).

  * `ui.py`: untuk halaman web.
  * `api_chat.py`: endpoint chat via MCP client.
  * `api_control.py`: endpoint untuk kontrol MCP server.

* **`myapp/services/`**
  Semua integrasi eksternal, seperti MCPClient, API lain, atau database async.

* **`myapp/templates/` & `myapp/static/`**
  Untuk UI frontend (HTML + CSS/JS). Quart support Jinja2, jadi langsung bisa render.

* **`tests/`**
  Untuk unit/integration test, biar code tetap stabil.

---
