# LP Scout Robinhood

Bot Telegram yang **hanya memindai dan merekomendasikan** pool LP *single-sided di bawah harga*
(range konsentrasi -70%) di Robinhood Chain (EVM, chainId 4663). Tidak memegang private key dan
tidak membuat transaksi apa pun.

> **Ambang label SEMENTARA.** Semua ambang (Ideal/Bagus/Minimal) berasal dari backtest Meteora
> Solana dan **belum divalidasi di Robinhood**. Riwayat scan disimpan (`data/state.json`) untuk
> kalibrasi nanti.

## Stack

Python 3.11 + `httpx` (async). Tanpa framework bot: Telegram dipanggil langsung lewat Bot API.
Dua mode jalan:

| Mode | Perintah | Kegunaan |
|---|---|---|
| Sekali jalan | `python main.py` | GitHub Actions / cron (bawaan repo ini). Tiap tick: proses perintah Telegram, lalu scan otomatis bila sudah `SCAN_MINUTES` sejak scan terakhir. |
| Daemon | `python main.py --daemon` | Long-polling Telegram + scan otomatis di proses sendiri (VPS/Docker). |
| Uji manual | `python main.py --dry-scan` | Scan nyata, cetak laporan ke stdout (tanpa Telegram/state). |

## Alur data

```
GeckoTerminal /pools (sort volume 24j, 8 halaman)
  → saring: pair ada sisi ETH/WETH/stable, TVL ≥ $10K, bukan stabil-stabil/aset besar/DEX V2
  → fee tier (cache 24j): nama pool → RPC fee() / V4 Initialize
  → peringkat (volume24h × fee tier)/TVL → 20 kandidat
  → candle 15m GeckoTerminal (cache 5m) → metrik → label
  → keamanan token (RPC) hanya untuk yang lolos → label akhir
```

Semua HTTP/RPC: timeout 10 dtk, retry + backoff, satu antrean global GeckoTerminal
(≥2,1 dtk/permintaan ≈ 28/menit; retry 429 menunggu lebih lama). Cache: daftar pool 2 menit,
candle 5 menit, fee tier 24 jam (`data/cache.json`).

### Sumber data & status verifikasi

| Data | Sumber | Status |
|---|---|---|
| Daftar pool, TVL, volume, harga, umur | GeckoTerminal `/networks/robinhood/pools?sort=h24_volume_usd_desc` | ✅ terverifikasi live (sort valid: `h24_volume_usd_desc`, `h24_tx_count_desc`; 20 pool/halaman) |
| Candle 15m | `/pools/{addr}/ohlcv/minute?aggregate=15&limit=100&currency=token` | ✅ terverifikasi live; volume dalam **quote token**, dikali harga quote (USD) — cocok dengan volume 24j pool ±15% pada fixture |
| Fee tier | Nama pool GeckoTerminal ("… 0.3%") | ✅ ada untuk sebagian pool (V3-style & sebagian V4); **tidak ada** untuk pool V4 tertentu dan DEX "metric" |
| Fee tier (on-chain) | RPC `fee()` (V3-style), event `Initialize` PoolManager (V4) | ⚠️ kode + tes dengan mock; **belum diuji ke node nyata** (RPC tidak terjangkau dari sandbox pengembangan) |
| Keamanan token | RPC: bytecode, `owner()`, slot proxy, simulasi beli/jual `eth_simulateV1` | ⚠️ idem, hanya tes mock |
| Krystal | — | Tidak dipakai: MCP hanya untuk asisten AI; API HTTP-nya butuh key + kredit dan tidak diverifikasi untuk runtime |

Pool yang fee tier-nya **tidak diketahui** tidak dinilai (fee tidak bisa diperkirakan) — bukan ditebak.

## Metrik (`lpscout/metrics.py`, murni & dites)

Per hari, dari 96 candle 15 menit terakhir (slot tanpa transaksi diisi harga sebelumnya):

- `feeYield = fee24h / TVL` — **perkiraan**: `fee24h ≈ volume24h × fee tier × (1 − PROTOCOL_FEE_SHARE)`; tanpa insentif/emisi
- `sigmaDaily = stdev(log return 15m) × √96`, `lvr = σ²/8`, `lvrRange70 = lvr / (1 − √0,3) ≈ lvr × 2,2`
- `netEdge = feeYield − lvr` (juga versi range -70%), `feeLvr = feeYield / lvr`
- `activity` = % candle bertransaksi · `concentration` = candle terbesar ÷ volume 24j · `pace6h` = (vol 6j × 4) ÷ vol 24j
- Bad print (close menyimpang > 70% dari kedua tetangga) dibuang; < 16 candle → metrik `null` → ditolak ("data kurang")

## Label (`lpscout/labels.py`, ambang di `.env`)

| Label | Fee/TVL | Net edge | Aktivitas | Konsentrasi | Laju 6j | Ukuran |
|---|---|---|---|---|---|---|
| ⭐ ideal | ≥ 20% | ≥ 10% | ≥ 90% | < 10% | – | 100% |
| ✅✅ bagus | ≥ 10% | ≥ 5% | ≥ 90% | < 10% | ≥ 50% | 75% |
| ✅ minimal | ≥ 5% | ≥ 2% | ≥ 90% | < 10% | ≥ 50% | 50% |

Turun satu tingkat bila TVL < $25.000 atau harga token volatil turun > 50% dalam 24 jam.
Keamanan gagal (honeypot/pajak transfer/jual gagal) → **tidak pernah** direkomendasikan.
Fee dinamis/hook V4 ditandai jelas (tier statis tidak dipercaya).

## Keamanan token (`lpscout/security.py`) — batasan jujur

Tanpa router per-DEX yang terverifikasi, simulasi dilakukan tanpa router: `eth_simulateV1`
menjalankan berurutan `transfer` pool→probe ("beli") dan probe→pool ("jual") lalu membandingkan
saldo (revert = honeypot; selisih = pajak). Hanya jalan bila node mendukung `eth_simulateV1`;
bila tidak, hasilnya **"belum terverifikasi"** (tidak pernah ditebak). Tambahan: ada-tidaknya
kode, selector `mint/blacklist/pause/setTax` di bytecode, `owner()`, proxy EIP-1967. Owner aktif
+ bisa mint = risiko. Konsentrasi holder **belum diperiksa** (belum ada sumber data terverifikasi).

## Perintah Telegram (hanya `TELEGRAM_OWNER_ID`)

`/scan` pindai sekarang · `/top` hasil terakhir · `/pool <alamat>` analisis satu pool (40 hex V3,
64 hex V4) · `/set [kunci nilai]` lihat/ubah setelan (`scan_minutes`, `min_tvl`, `cooldown_hours`,
`alert_min_label`, `auto_scan`, `top_candidates`) · `/status` · `/help`. Menu perintah didaftarkan
otomatis. Tiap handler dibatasi 25 dtk; `/scan` berjalan di latar belakang dan membalas saat selesai.

**Alert otomatis** hanya untuk pool yang *baru lolos* atau *naik label*, cooldown 2 jam per pool
(`ALERT_COOLDOWN_HOURS`). Pesan maks 3.500 karakter, diakhiri catatan setup single-sided -70%.

## Setup

1. `pip install -r requirements.txt` (dev: `requirements-dev.txt`), salin `.env.example` → `.env`.
2. Isi minimal `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID`; untuk fee/keamanan on-chain isi `RPC_URL`
   (atau `ALCHEMY_API_KEY`) dan, untuk pool V4, `V4_POOL_MANAGER`.
3. GitHub Actions: tambah secrets `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID`, `RPC_URL`/`ALCHEMY_API_KEY`
   (variabel opsional `V4_POOL_MANAGER`, `SCAN_MINUTES`). Workflow jalan tiap 5 menit; `data/` ikut
   `actions/cache`. Pemicu andal: cron-job.org → `workflow_dispatch`.

## Tes & kualitas

```
pip install -r requirements-dev.txt
pytest          # metrik, label, keamanan, fee, cache/retry, parser fixture GeckoTerminal, alert/cooldown, bot
ruff check .    # lint
mypy lpscout main.py   # typecheck
```

Fixture payload asli GeckoTerminal ada di `tests/fixtures/` (daftar pool, detail pool, OHLCV).

## Belum bisa diverifikasi

- Fee tier on-chain (`fee()`, `Initialize` V4), alamat `PoolManager` Robinhood, dan `eth_simulateV1` di node Robinhood.
- Sumber fee nyata (bukan perkiraan volume × tier); fee dinamis/hook V4 per pool.
- Simulasi honeypot per DEX lewat router; pajak yang hanya aktif di jalur tertentu.
- Konsentrasi holder; kualitas TVL GeckoTerminal untuk pool V4 tertentu (ada nilai tak wajar).
