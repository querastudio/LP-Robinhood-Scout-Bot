# Robinhood Scout Bot

Status: secrets sudah dikonfigurasi (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GMGN_API_KEY`, `ALCHEMY_API_KEY`).

Telegram bot yang memindai token/pool di Robinhood Chain (Arbitrum Orbit L2)
lewat GMGN API + data pool on-chain (DexPaprika/Alchemy/DexScreener), dan
mengirim notifikasi ke Telegram untuk token yang lolos filter.

## Setup

1. `pip install -r requirements.txt`
2. Set secrets di GitHub repo (Settings → Secrets and variables → Actions):
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — wajib
   - `GMGN_API_KEY` — wajib
   - `ALCHEMY_API_KEY` — opsional
   - `KRYSTAL_API_KEY` — opsional, belum jelas apakah endpoint `/pool/list` benar-benar butuh auth (swagger doc tidak mencantumkan security scheme untuk endpoint ini)
   - `KRYSTAL_CHAIN_ID` — opsional, default **4663** (chain ID Robinhood Chain, dikonfirmasi via dashboard Alchemy: network enum `robinhood-mainnet`, native token ETH). Cuma perlu di-override kalau ternyata salah.
3. Jalankan lokal: `python main.py`
4. Preview format notifikasi tanpa scan sungguhan: `python send_test_alert.py`

## GitHub Actions

Workflow `.github/workflows/robinhood_scout.yml` berjalan tiap 5 menit via
`schedule:` dan juga mendukung `workflow_dispatch` (dengan input
`send_test_alert` untuk preview). **Catatan penting**: `schedule:` cron
GitHub Actions sering di-drop untuk interval sesering 5 menit — setelah bot
tervalidasi, setup cron eksternal (mis. cron-job.org) yang memanggil
`workflow_dispatch` API sebagai pemicu utama yang reliable.

## Update: bug base URL GMGN sudah diperbaiki

Run scan pertama gagal total — `api.gmgn.ai` (domain yang saya tebak awal)
**tidak resolve DNS sama sekali**. Setelah baca source code `gmgn-cli`
(npm package resmi GMGN, `GMGNAI/gmgn-skills`) langsung, ketemu domain
yang benar dan detail request yang sebelumnya salah tebak:
- Base URL: **`https://openapi.gmgn.ai`** (bukan `api.gmgn.ai`)
- Auth: header **`X-APIKEY: <key>`** + query `timestamp` (unix seconds) +
  `client_id` (random UUID) — **bukan** `Authorization: Bearer`
- `hot_searches`: body `{"params": [{"label": "hot-search", "chain": ...,
  "interval": "24h", "limit": ...}]}` — array per-chain config, bukan
  `{"chain": ...}` polos
- `token_signal`: body `{"chain": ..., "groups": [{"signal_type": [7]}]}`
  — signal_type array di dalam `groups`, bukan integer polos
- `rank` butuh query `interval` (mis. `"1h"`) — sebelumnya tidak dikirim
- GMGN API **reject traffic via IPv6** dengan 401/403 — client sekarang
  paksa pakai IPv4

Yang masih **belum** terverifikasi: nama field di **response** JSON (baru
tahu bentuk *request*-nya dari source code, belum sempat lihat response
asli). `DEBUG_API_RAW=true` tetap aktif untuk validasi ini di run
berikutnya.

## Validasi skema API (WAJIB sebelum production)

Skema request/response GMGN API (`apis/gmgn.py`), DexPaprika
(`apis/chain_data.py`), dan **Krystal** (`apis/krystal.py`) di kode ini
**belum diverifikasi lewat call langsung**. Untuk Krystal, endpoint
(`GET /all/v1/pool/list`, host `api.krystal.app`) dan parameternya
(`token`, `chainId`, `limit`) sudah dikonfirmasi dari `doc.json` (OpenAPI
spec resmi) — tapi skema *response*-nya di dokumen itu salah mapping
(nunjuk ke tipe generic `SearchOutput`, bukan struktur pool), jadi
`normalize_krystal_pool()` di `apis/krystal.py` masih tebakan berdasarkan
struktur pool terdekat yang ada di dokumen (`multichain.LpPool`). Jalankan
`workflow_dispatch` sekali dan cek Actions log — `DEBUG_API_RAW=true`
(default) mencetak raw JSON response dari tiap endpoint GMGN dan Krystal.
Sesuaikan field mapping di `normalize_*` functions kalau ternyata berbeda
dari asumsi, terutama:
- nama field `token0`/`token1`/`tokenAmounts` untuk pasangan token
- unit fee tier (basis points vs persen vs fraksi), atau field fee tier
  yang benar kalau ternyata bukan `fee`/`feeTier`/`fees[0]`
- nama field TVL/volume/fees 24h
- apakah endpoint ini butuh auth header sama sekali (swagger tidak
  mencantumkan security scheme untuk endpoint ini)

Semua filter yang datanya tidak tersedia dari API di-skip secara graceful
(tampil "N/A" di notifikasi), bot tidak akan crash atau menolak semua token
karena satu field hilang.

## Threshold / filter

Semua threshold dikonfigurasi lewat environment variable, default di
`config.py`. Lihat tabel filter lengkap di prompt asli — ringkasnya:
market cap, holders, top-10 holder %, liquidity, volume 1h, price change 1h,
total fees, hot search visiting count. ATH break (`signal_type == 7`)
default sebagai bonus/highlight, bukan hard filter (`REQUIRE_ATH_BREAK=true`
untuk mengubahnya jadi wajib).

**Filter pool (hard, wajib lolos):**
- Pool harus dipasangkan dengan salah satu quote asset di `ALLOWED_QUOTE_SYMBOLS`
  (default `ETH,WETH,USDG`) — token ditolak kalau semua pool-nya dipasangkan
  dengan token lain.
- Fee tier pool minimal `MIN_BASE_FEE_PCT` (default 2%) — token ditolak kalau
  fee tier diketahui dan di bawah ambang ini. Kalau data fee tier-nya sendiri
  tidak tersedia dari API, filter ini di-skip (bukan reject), konsisten dengan
  aturan graceful-N/A di seluruh bot ini.
