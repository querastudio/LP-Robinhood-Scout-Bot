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
- **Filter pool — Gate A, pairing (hard, wajib lolos)**:
  - Pool harus ada dan terkonfirmasi (dari Krystal, DexPaprika, atau field
    `quote_address` GMGN) — token ditolak (`no_eligible_quote_pair`) kalau
    sama sekali tidak ada data pool yang bisa dikonfirmasi. Quote asset-nya
    sendiri **bebas** (tidak lagi dibatasi whitelist ETH/WETH/USDG/dst) —
    kualitas token ditentukan oleh Gate B di bawah plus filter lain (volume
    organik, spike volume 5 menit, liquidity, holders, dll).

- **Filter pool — Gate B, kualitas pool (hard, kriteria CEREBRO)**: begitu
  pairing terkonfirmasi, `screener.select_best_sibling` mengumpulkan SEMUA
  sibling pool token itu (fee tier berbeda-beda) dan menjalankan 2 layer:

  **Layer 1** — hard filter per-pool (`screener.filter_pool_layer1`), tiap
  pool harus lolos semua sekaligus:

  | Metrik | Minimum default | Env var |
  |---|---|---|
  | TVL pool | ≥ $10.000 | `MIN_POOL_TVL` |
  | Volume 24h / TVL | ≥ 2x | `MIN_VOL_TVL_RATIO` |
  | Fee 24h / TVL | ≥ 10%/hari | `MIN_FEE_TVL_PCT` |
  | Base fee tier | ≥ 2% | `MIN_BASE_FEE_PCT` (sudah ada sebelumnya) |

  Sama seperti filter lain di bot ini: field yang datanya tidak tersedia
  dari API manapun (mis. Krystal tidak pernah punya timestamp umur pool)
  **di-skip untuk pool itu**, bukan me-reject-nya — data hilang tidak
  diperlakukan sebagai data buruk.

  **Umur pool BUKAN hard filter** — `MIN_POOL_AGE_DAYS` (default 7 hari)
  cuma referensi "nice to have", bukan syarat wajib. Pool yang lebih muda
  dari `NEW_POOL_WARNING_DAYS` (default 1 hari) tetap bisa lolos dan
  dikirim notif, cuma ditandai tag "⚠️ Pool Baru" di badan notifikasi biar
  user tahu dan bisa menilai sendiri — bukan otomatis ditolak.

  **Layer 2** — seleksi antar sibling pool: kalau token punya ≥2 pool yang
  lolos Layer 1, bot memilih SATU pemenang (bukan mengirim notif untuk
  semua), lewat "jomplang check" — pool dengan volume jauh di bawah
  sibling ter-ramai token itu (rasio < `SIBLING_VOLUME_RATIO_THRESHOLD`,
  default 0.5) di-skip dari prioritas, lalu di antara sisanya dipilih base
  fee tertinggi (tie-break volume tertinggi); kalau semua kandidat
  jomplang, fallback ke volume tertinggi. Token ditolak (`pool_quality`)
  kalau tidak ada satu pun sibling pool yang lolos Layer 1 — berbeda dari
  `no_eligible_quote_pair` (itu berarti tidak ada data pool sama sekali;
  ini berarti data pool ADA tapi tidak cukup bagus).

  Hasil seleksi ditampilkan di notifikasi sebagai baris "Sibling Win".

- **Field display tambahan (bukan filter)**: `Volume (6h)` dari
  GeckoTerminal (biar kelihatan aktivitas 5 menit itu one-off blip atau
  memang sustained sampai 6 jam), dan `Trades 24h` — jumlah buy/sell/swap
  dari GMGN (`buys_24h`/`sells_24h`/`swaps_24h`) — banyak transaksi kecil
  dari banyak wallet beda rasanya dengan 2-3 swap besar doang.

- **Layer 3 — tag informational (tidak menggagalkan pool)**: ditampilkan di
  notifikasi kalau datanya ada, tidak pernah jadi alasan reject.
  - 🔥 **Momentum naik** — volume 1 jam dibanding rata-rata volume 24 jam
    (per jam); tampil kalau rasionya di atas `MOMENTUM_RATIO_THRESHOLD`
    (default 1.5x).
  - ⚠️ **Pool Baru** — tampil kalau umur pool di bawah
    `NEW_POOL_WARNING_DAYS` (default 1 hari). Bukan filter, cuma warning.
  - 💡 **Fee vs Drawdown** — rasio Fee/TVL dibanding price drawdown 24 jam.
    **Belum aktif**: tidak ada API yang terintegrasi di bot ini (GMGN,
    Krystal, DexPaprika, Alchemy) yang menyediakan `price_change_24h` —
    lihat "Phase 2 backlog" di bawah.

- **Gate terakhir sebelum kirim — spike volume 5 menit** (`main.py`, bukan
  bagian dari `screener.py`): token yang lolos Gate A+B masih harus lolos
  cek "volume deras" — volume 5 menit terakhir (dari GeckoTerminal) harus
  ≥ `MIN_VOL_5M` (default $10.000) **dan** ≥ `VOL_5M_SPIKE_MULTIPLIER`x
  (default 1.5x) rata-rata volume per-5-menit token itu sendiri.
  - **Bypass**: kalau pool-nya sudah kebukti "kencang" secara 24 jam lewat
    Vol/TVL ≥ `SPIKE_BYPASS_VOL_TVL_PCT` (default 500%, alias 5x) **dan**
    Fee/TVL ≥ `SPIKE_BYPASS_FEE_TVL_PCT` (default 10%/hari), cek spike
    5-menit ini di-skip total — pool kayak CEREBRO (Vol/TVL 6.96x, Fee/TVL
    13.91%/hari) sudah kebukti aktif secara harian, jadi tidak boleh
    ketinggalan notif cuma karena momen 5 menit spesifik yang dicek pas
    lagi sepi.
  - Default sebelumnya ($50.000 + 3x) ternyata kelewat ketat — bahkan pool
    sekelas CEREBRO (TVL ~$101.7K) nyaris tidak pernah punya volume $50K
    dalam SATU window 5 menit (itu setara muter setengah TVL-nya tiap 5
    menit).

## Phase 2 backlog

Item-item ini butuh sumber data baru yang belum ada integrasinya di bot ini
— sengaja tidak diblokir dari rilis Phase 1 (overhaul filter pool CEREBRO):

- **Fee/active-liquidity band** — data konsentrasi liquidity Uniswap V3
  per tick range, perlu subgraph atau API khusus.
- **Win rate historis LP di pool tsb** — perlu indexer/analytics pihak
  ketiga yang melacak PnL wallet per pool, tidak disediakan GMGN/Krystal/
  DexPaprika/Alchemy.
- **Status smart-money address** (masih hold atau sudah keluar) — perlu
  tracking wallet spesifik, di luar cakupan API token/pool yang ada.
- **`price_change_24h`** untuk tag Layer 3 "Fee vs Drawdown" — field ini
  tidak diekspos GMGN (hanya `price_change_percent1h` yang tersedia),
  Krystal, maupun DexPaprika secara langsung.

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
