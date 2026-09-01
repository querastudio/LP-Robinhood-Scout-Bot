# Robinhood Scout Bot

Telegram bot yang memindai token/pool di Robinhood Chain (Arbitrum Orbit L2)
lewat GMGN API + Krystal Cloud API (data pool), dan mengirim notifikasi ke
Telegram untuk token yang lolos filter.

## Status

Semua sumber data utama sudah **tervalidasi lewat live run** (bukan tebakan):

| Sumber | Status | Dipakai untuk |
|---|---|---|
| GMGN OpenAPI | ✅ Validated | Market cap, holders, honeypot, ownership renounced, hot search, ATH signal |
| Krystal Cloud API | ✅ Validated | TVL, fee tier, fees/volume 24h, quote pair (ETH/USDG filter) |
| Alchemy RPC | ✅ Working | Fallback ownership-renounced check kalau GMGN tidak punya datanya |
| DexPaprika | ⚠️ Belum tervalidasi | Fallback sekunder kalau Krystal tidak ada data pool untuk token tsb |

## Setup

1. `pip install -r requirements.txt`
2. Set secrets di GitHub repo (Settings → Secrets and variables → Actions):
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — wajib
   - `GMGN_API_KEY` — wajib
   - `KRYSTAL_API_KEY` — wajib untuk filter pool aktif (daftar di https://cloud.krystal.app)
   - `ALCHEMY_API_KEY` — opsional (fallback ownership check)
   - `KRYSTAL_CHAIN_ID` — opsional, default **4663** (chain id Robinhood Chain, terverifikasi via Krystal `/v1/chains` dan dashboard Alchemy)
3. Jalankan lokal: `python main.py`
4. Preview format notifikasi tanpa scan sungguhan: `python send_test_alert.py`

## GitHub Actions

Workflow `.github/workflows/robinhood_scout.yml` berjalan tiap 5 menit via
`schedule:` dan juga mendukung `workflow_dispatch` (dengan input
`send_test_alert` untuk preview). **Catatan penting**: `schedule:` cron
GitHub Actions sering di-drop untuk interval sesering 5 menit — setelah bot
tervalidasi, setup cron eksternal (mis. cron-job.org) yang memanggil
`workflow_dispatch` API sebagai pemicu utama yang reliable.

Semua filter yang datanya tidak tersedia dari API di-skip secara graceful
(tampil "N/A" di notifikasi), bot tidak akan crash atau menolak semua token
karena satu field hilang.

## Pause / Resume (hemat API call)

Kirim command ini lewat chat Telegram yang sama dengan `TELEGRAM_CHAT_ID`:

- `/pause` (atau `/stop`) — matikan scan. Selama paused, tiap run cuma
  melakukan satu panggilan `getUpdates` ke Telegram (buat dengar
  `/resume`) — **tidak** memanggil GMGN, DexPaprika, Krystal, GeckoTerminal,
  atau Alchemy RPC sama sekali, jadi tidak boros quota/rate-limit.
- `/resume` (atau `/start`) — nyalakan lagi, scan jalan normal tiap 5 menit.
- `/status` — cek status saat ini (Running/Paused).

Status paused/resume disimpan di `bot_state.json`, ikut ter-cache antar run
lewat `actions/cache` (sama seperti `cooldown_cache.json`). Command diproses
di awal tiap run — bot cuma memproses command yang dikirim dari chat id yang
sama dengan `TELEGRAM_CHAT_ID`, command dari chat lain diabaikan.

## Threshold / filter

Semua threshold dikonfigurasi lewat environment variable, default di
`config.py`. Ringkasnya:
- Market cap, holders, top-10 holder %, liquidity, volume 1h, price change 1h,
  total fees, hot search visiting count.
- ATH break (`signal_type == 7`) default sebagai bonus/highlight, bukan hard
  filter (`REQUIRE_ATH_BREAK=true` untuk mengubahnya jadi wajib).
- **Filter pool (hard, wajib lolos)**:
  - Pool harus ada dan terkonfirmasi (dari Krystal, DexPaprika, atau field
    `quote_address` GMGN) — token ditolak kalau sama sekali tidak ada data
    pool yang bisa dikonfirmasi. Quote asset-nya sendiri **bebas** (tidak
    lagi dibatasi whitelist ETH/WETH/USDG/dst) — kualitas token
    ditentukan oleh filter lain (volume organik, spike volume 5 menit,
    liquidity, holders, dll), bukan oleh pasangan quote asset-nya.
  - Fee tier pool minimal `MIN_BASE_FEE_PCT` (default 2%) — token ditolak
    kalau fee tier diketahui dan di bawah ambang ini. Kalau data fee tier-nya
    sendiri tidak tersedia dari API, filter ini di-skip (bukan reject).

## Riwayat debugging (untuk referensi)

Proses validasi menemukan beberapa bug nyata yang sudah diperbaiki:

**GMGN** — domain awal (`api.gmgn.ai`) tidak resolve DNS sama sekali; domain
benar adalah `https://openapi.gmgn.ai`. Auth pakai header `X-APIKEY` + query
`timestamp`/`client_id` (bukan Bearer token). Response `hot_searches` nested
per-chain (`data[0].tokens[]`), response `rank` nested dobel
(`data.data.rank[]`) — bug ini bikin filter rank selalu dapat 0 data di
beberapa run pertama. Field `ath` di `token_signal` adalah market cap, bukan
harga.

**Krystal** — domain awal yang ditemukan lewat `doc.json` (`api.krystal.app`)
ternyata API internal aplikasi wallet Krystal, bukan "Krystal Cloud" (produk
publik yang benar, di `cloud-api.krystal.app`). Endpoint yang benar
`GET /v1/pools`, dan parameter `chainId` harus angka polos (`4663`), bukan
string `"ethereum@4663"` seperti contoh di dokumentasi mereka sendiri —
server API menolaknya dengan `400 Bad Request`. Response `token0`/`token1`
juga dibungkus `{token: {...}, balance: ...}`, bukan objek Token langsung.

Detail lengkap ada di riwayat commit `apis/gmgn.py` dan `apis/krystal.py`.
