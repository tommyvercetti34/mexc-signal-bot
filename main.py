"""
MEXC Futures - Kripto Sinyal & Tarama Botu
==========================================
Strateji (15m):
  1) Trend  : Fiyat VWAP (gunluk, UTC sifirlanan) ustunde -> sadece LONG, altinda -> sadece SHORT
  2) Guc    : ADX(14) > 25 (altindaysa piyasa yatay kabul edilir, sinyal yok)
  3) Tetik  : Stoch RSI (14,14,3,3)
               - K, D'yi 20 altinda yukari keserse  -> LONG
               - K, D'yi 80 ustunde asagi keserse   -> SHORT
  Risk     : SL = Giris -/+ ATR(14) * ATR_SL_MULT,  TP = Giris +/- Risk * RR_RATIO

Sadece KAPANMIS mumlar uzerinde hesap yapilir (repaint yok).
Bu bot emir ACMAZ; sadece Telegram'a sinyal gonderir.

Tum ayarlar asagidaki CONFIG bolumunde; her biri ortam degiskeni (env var) ile
ezilebilir, boylece Render/Railway panelinden kodu degistirmeden ayarlanabilir.
"""

import os
import sys
import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

import io
import json

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")  # sunucuda ekransiz cizim
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import requests
import ccxt


# ============================================================================
# CONFIG
# ============================================================================
def _env(name, default, cast=str):
    val = os.getenv(name)
    if val is None or val == "":
        return default
    if cast is bool:
        return val.strip().lower() in ("1", "true", "yes", "on", "evet")
    try:
        return cast(val)
    except ValueError:
        logging.warning("Gecersiz %s=%r, varsayilan kullaniliyor: %r", name, val, default)
        return default


# --- Telegram ---
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID", "")          # kanal icin: -100xxxxxxxxxx veya @kanaladi

# --- Tarama ---
TIMEFRAME = _env("TIMEFRAME", "15m")
TOP_N_SYMBOLS = _env("TOP_N_SYMBOLS", 50, int)            # 24s hacme gore ilk N USDT perpetual
MIN_QUOTE_VOLUME_USDT = _env("MIN_QUOTE_VOLUME_USDT", 5_000_000, float)
EXCLUDE_SYMBOLS = [s.strip().upper() for s in _env("EXCLUDE_SYMBOLS", "").split(",") if s.strip()]  # orn: "USDC,PEPE"
OHLCV_LIMIT = _env("OHLCV_LIMIT", 300, int)               # 300 x 15m = ~3 gun (indikator isinmasi + VWAP icin)
SCAN_DELAY_SEC = _env("SCAN_DELAY_SEC", 15, int)          # mum kapanisindan kac sn sonra taransin
RUN_SCAN_ON_START = _env("RUN_SCAN_ON_START", True, bool)

# --- Indikatorler ---
ADX_PERIOD = _env("ADX_PERIOD", 14, int)
ADX_THRESHOLD = _env("ADX_THRESHOLD", 25.0, float)
RSI_PERIOD = _env("RSI_PERIOD", 14, int)
STOCH_PERIOD = _env("STOCH_PERIOD", 14, int)
STOCH_K_SMOOTH = _env("STOCH_K_SMOOTH", 3, int)
STOCH_D_SMOOTH = _env("STOCH_D_SMOOTH", 3, int)
STOCH_OVERSOLD = _env("STOCH_OVERSOLD", 20.0, float)
STOCH_OVERBOUGHT = _env("STOCH_OVERBOUGHT", 80.0, float)
ATR_PERIOD = _env("ATR_PERIOD", 14, int)
VWAP_ANCHOR_TZ = _env("VWAP_ANCHOR_TZ", "UTC")            # VWAP her gun bu saat dilimine gore 00:00'da sifirlanir
USE_SUPERTREND = _env("USE_SUPERTREND", True, bool)       # LONG icin SuperTrend yukari, SHORT icin asagi olmali
ST_PERIOD = _env("ST_PERIOD", 10, int)
ST_MULT = _env("ST_MULT", 3.0, float)

# --- Risk ---
LEVERAGE = _env("LEVERAGE", 15, int)
MARGIN_MODE = _env("MARGIN_MODE", "Isolated")
ATR_SL_MULT = _env("ATR_SL_MULT", 1.5, float)             # SL mesafesi = ATR * bu katsayi
RR_RATIO = _env("RR_RATIO", 1.5, float)                   # TP = risk * RR (min 1.5 onerilir)
MIN_SL_PCT = _env("MIN_SL_PCT", 0.25, float)              # cok dar stoplari (gurultu) ele
# Kademeli giris: Giris1 = sinyal kapanisi, GirisN = Giris1 -/+ (N-1) * ENTRY_STEP_ATR * ATR
# Stop ve Hedef Giris1'e gore SABIT kalir; kademeler stop ile giris arasina yerlesir.
ENTRY_LEVELS = _env("ENTRY_LEVELS", 3, int)               # 1 = tek giris (kademesiz)
ENTRY_STEP_ATR = _env("ENTRY_STEP_ATR", 0.5, float)       # kademeler arasi mesafe (ATR cinsinden)
ENTRY_WEIGHTS = _env("ENTRY_WEIGHTS", "33,33,34")         # her kademenin pozisyondaki % payi
NOTIFY_ENTRY_FILLS = _env("NOTIFY_ENTRY_FILLS", True, bool)  # 2./3. kademe dolunca mesaj at
# 15x isolated'da tasfiye ~%6.6 civarinda; SL bunun cok altinda olmali:
MAX_SL_PCT = _env("MAX_SL_PCT", 4.0, float)

# --- Grafik ---
SEND_CHART = _env("SEND_CHART", True, bool)               # sinyalle birlikte grafik gorseli gonder
CHART_BARS = _env("CHART_BARS", 80, int)                  # grafikte gosterilecek mum sayisi
CHART_EMA_FAST = _env("CHART_EMA_FAST", 50, int)
CHART_EMA_SLOW = _env("CHART_EMA_SLOW", 200, int)

# --- Tekrar sinyal engeli ---
SIGNAL_COOLDOWN_MIN = _env("SIGNAL_COOLDOWN_MIN", 60, int)  # ayni coin icin en az bu kadar dk bekle

# --- Islem takibi (TP / Stop bildirimleri) ---
TRACK_TRADES = _env("TRACK_TRADES", True, bool)           # acik sinyalleri izle, TP/SL olunca mesaj at
MONITOR_INTERVAL_SEC = _env("MONITOR_INTERVAL_SEC", 60, int)  # kac sn'de bir kontrol edilsin
MAX_TRADE_HOURS = _env("MAX_TRADE_HOURS", 48, float)      # bu sureden sonra TP/SL olmazsa "suresi doldu" kapat
ONE_TRADE_PER_SYMBOL = _env("ONE_TRADE_PER_SYMBOL", True, bool)  # acik islem varken ayni coine yeni sinyal verme
STATE_FILE = _env("STATE_FILE", "trades.json")

# --- Sistem ---
DISPLAY_TZ = ZoneInfo(_env("DISPLAY_TZ", "Europe/Istanbul"))
DRY_RUN = _env("DRY_RUN", False, bool)                    # True: Telegram'a gondermez, sadece loglar
SEND_STARTUP_MESSAGE = _env("SEND_STARTUP_MESSAGE", True, bool)
HTTP_PORT = _env("PORT", 0, int)                          # Render/Railway PORT verir -> saglik sunucusu acilir
LOG_LEVEL = _env("LOG_LEVEL", "INFO")
MAX_RETRIES = _env("MAX_RETRIES", 4, int)

# ============================================================================

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("mexc-bot")

def _parse_ladder():
    """Kademe agirliklarini ve adimini dogrular. (agirliklar[0-1], adim_atr) dondurur."""
    n = max(1, ENTRY_LEVELS)
    try:
        w = [float(x) for x in ENTRY_WEIGHTS.split(",") if x.strip()][:n]
    except ValueError:
        w = []
    if len(w) != n or any(v <= 0 for v in w):
        w = [1.0] * n
        if n > 1:
            logging.warning("ENTRY_WEIGHTS gecersiz/eksik, esit agirlik kullaniliyor")
    total = sum(w)
    w = [v / total for v in w]
    step = ENTRY_STEP_ATR
    if n > 1 and step * (n - 1) >= ATR_SL_MULT * 0.9:       # son kademe stopa cok yakin/otesinde olmasin
        step = ATR_SL_MULT * 0.9 / (n - 1)
        logging.warning("ENTRY_STEP_ATR stopa cok yakin, %.2f ATR'ye dusuruldu", step)
    return w, step


LADDER_WEIGHTS, LADDER_STEP_ATR = _parse_ladder()

TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}[TIMEFRAME]
TF_LABEL = {15: "15 Dakika", 5: "5 Dakika", 30: "30 Dakika", 60: "1 Saat", 240: "4 Saat"}.get(TF_MINUTES, TIMEFRAME)

STATE = {
    "started_at": datetime.now(timezone.utc),
    "last_scan_at": None,
    "last_scan_ok": None,
    "signals_sent": 0,
}
_last_signal = {}   # symbol -> datetime (UTC) of last signal


# ============================================================================
# SAGLIK SUNUCUSU (Render free web service icin; disaridan ping ile uyanik tutulur)
# ============================================================================
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (
            f"OK\nstarted_at={STATE['started_at'].isoformat()}\n"
            f"last_scan_at={STATE['last_scan_at']}\nlast_scan_ok={STATE['last_scan_ok']}\n"
            f"signals_sent={STATE['signals_sent']}\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):  # erisim loglarini sustur
        pass


def start_health_server():
    if not HTTP_PORT:
        log.info("PORT tanimli degil, saglik sunucusu baslatilmadi (worker modunda calisiliyor).")
        return

    def _serve():
        while True:
            try:
                HTTPServer(("0.0.0.0", HTTP_PORT), _HealthHandler).serve_forever()
            except Exception as e:
                log.error("Saglik sunucusu hatasi: %s - 5 sn sonra yeniden baslatiliyor", e)
                time.sleep(5)

    threading.Thread(target=_serve, daemon=True).start()
    log.info("Saglik sunucusu 0.0.0.0:%s uzerinde calisiyor", HTTP_PORT)


# ============================================================================
# TELEGRAM
# ============================================================================
def _tg_post(method, *, data=None, json_payload=None, files=None, timeout=20):
    """Telegram API cagrisi. Basarili olursa gonderilen mesajin message_id'sini, olmazsa None dondurur."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(url, data=data, json=json_payload, files=files, timeout=timeout)
            if r.status_code == 200:
                return r.json().get("result", {}).get("message_id", -1)
            if r.status_code == 429:  # flood limit
                wait = r.json().get("parameters", {}).get("retry_after", 5)
                log.warning("Telegram rate limit, %s sn bekleniyor", wait)
                time.sleep(wait + 1)
                continue
            log.error("Telegram %s hata %s: %s", method, r.status_code, r.text[:300])
            if 400 <= r.status_code < 500:
                return None  # token/chat_id hatali -> tekrar denemenin anlami yok
        except requests.RequestException as e:
            log.warning("Telegram baglanti hatasi (%d/%d): %s", attempt, MAX_RETRIES, e)
        time.sleep(2 * attempt)
    return None


def send_telegram(text: str, reply_to: int = None):
    """Metin mesaji gonderir. reply_to verilirse o mesaja yanit olarak gider. message_id veya None doner."""
    if DRY_RUN:
        log.info("[DRY_RUN] Telegram mesaji (reply_to=%s):\n%s", reply_to, text)
        return -1
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID tanimli degil! Mesaj:\n%s", text)
        return None
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
    if reply_to and reply_to > 0:
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    return _tg_post("sendMessage", json_payload=payload, timeout=15)


def send_telegram_photo(png: bytes, caption: str):
    """Grafigi altyazili fotograf olarak gonderir. message_id veya None doner (None -> cagiran metne duser)."""
    if DRY_RUN:
        path = f"/tmp/chart_{int(time.time())}.png"
        with open(path, "wb") as f:
            f.write(png)
        log.info("[DRY_RUN] Grafik kaydedildi: %s\n%s", path, caption)
        return -1
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return None
    return _tg_post(
        "sendPhoto",
        data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024]},
        files={"photo": ("chart.png", png, "image/png")},
        timeout=30,
    )


# ============================================================================
# BORSA
# ============================================================================
def create_exchange():
    ex = ccxt.mexc({
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {"defaultType": "swap"},
    })
    return ex


def with_retry(fn, *args, desc="istek", **kwargs):
    """CCXT cagrilarini ag kopmalarina karsi ustel bekleme ile tekrarlar."""
    delay = 2
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except ccxt.RateLimitExceeded as e:
            log.warning("Rate limit (%s) %d/%d: %s", desc, attempt, MAX_RETRIES, e)
            time.sleep(delay * 3)
        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout) as e:
            log.warning("Ag hatasi (%s) %d/%d: %s", desc, attempt, MAX_RETRIES, e)
            time.sleep(delay)
        except ccxt.BadSymbol as e:
            log.debug("Gecersiz sembol (%s): %s", desc, e)
            return None
        except ccxt.ExchangeError as e:
            log.warning("Borsa hatasi (%s) %d/%d: %s", desc, attempt, MAX_RETRIES, e)
            time.sleep(delay)
        delay = min(delay * 2, 30)
    log.error("%s %d denemede basarisiz oldu", desc, MAX_RETRIES)
    return None


def get_top_symbols(ex, top_n=None):
    """USDT-margined perpetual (swap) piyasalarini 24s USDT hacmine gore siralar."""
    markets = ex.markets or with_retry(ex.load_markets, desc="load_markets") or {}
    candidates = {
        s: m for s, m in markets.items()
        if m.get("swap") and m.get("linear") and m.get("quote") == "USDT"
        and m.get("settle") == "USDT" and m.get("active", True)
        and m.get("base", "").upper() not in EXCLUDE_SYMBOLS
    }
    tickers = with_retry(ex.fetch_tickers, desc="fetch_tickers") or {}

    ranked = []
    for sym, t in tickers.items():
        if sym not in candidates:
            continue
        qv = t.get("quoteVolume")
        if not qv:
            bv, last = t.get("baseVolume"), t.get("last")
            qv = (bv or 0) * (last or 0)
        if qv and qv >= MIN_QUOTE_VOLUME_USDT:
            ranked.append((sym, float(qv)))

    ranked.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in ranked[:(top_n or TOP_N_SYMBOLS)]]


def fetch_closed_ohlcv(ex, symbol):
    raw = with_retry(ex.fetch_ohlcv, symbol, TIMEFRAME, limit=OHLCV_LIMIT, desc=f"ohlcv {symbol}")
    if not raw or len(raw) < 100:
        return None
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    # Henuz kapanmamis son mumu at
    now = datetime.now(timezone.utc)
    df = df[df["ts"] + timedelta(minutes=TF_MINUTES) <= now].reset_index(drop=True)
    return df if len(df) >= 100 else None


# ============================================================================
# INDIKATORLER (saf pandas/numpy - TradingView ile ayni Wilder yontemi)
# ============================================================================
def rma(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False).mean()


def calc_atr(df, n):
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return rma(tr, n)


def calc_adx(df, n):
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    atr = calc_atr(df, n)
    plus_di = 100 * rma(plus_dm, n) / atr.replace(0, np.nan)
    minus_di = 100 * rma(minus_dm, n) / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return rma(dx.fillna(0), n)


def calc_rsi(close, n):
    delta = close.diff()
    gain = rma(delta.clip(lower=0), n)
    loss = rma(-delta.clip(upper=0), n)
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi.fillna(100).where(loss != 0, 100)


def calc_stoch_rsi(close, rsi_n, stoch_n, k_n, d_n):
    rsi = calc_rsi(close, rsi_n)
    lo = rsi.rolling(stoch_n).min()
    hi = rsi.rolling(stoch_n).max()
    stoch = 100 * (rsi - lo) / (hi - lo).replace(0, np.nan)
    k = stoch.rolling(k_n).mean()
    d = k.rolling(d_n).mean()
    return k, d


def calc_vwap(df, tz="UTC"):
    """Gunluk anchor'li VWAP (her gun 00:00'da sifirlanir)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    session = df["ts"].dt.tz_convert(tz).dt.date
    pv = (tp * df["volume"]).groupby(session).cumsum()
    vv = df["volume"].groupby(session).cumsum()
    return pv / vv.replace(0, np.nan)


def calc_supertrend(df, n, mult):
    """TradingView SuperTrend. (cizgi, yon) dondurur; yon: 1 = yukari (yesil), -1 = asagi (kirmizi)."""
    atr = calc_atr(df, n).to_numpy()
    hl2 = ((df["high"] + df["low"]) / 2).to_numpy()
    close = df["close"].to_numpy()
    up_basic, dn_basic = hl2 - mult * atr, hl2 + mult * atr
    size = len(df)
    lower, upper = np.full(size, np.nan), np.full(size, np.nan)
    direction = np.ones(size, dtype=int)
    st = np.full(size, np.nan)
    for i in range(size):
        if np.isnan(atr[i]):
            continue
        if i == 0 or np.isnan(lower[i - 1]):
            lower[i], upper[i] = up_basic[i], dn_basic[i]
            direction[i] = 1
        else:
            lower[i] = up_basic[i] if (up_basic[i] > lower[i - 1] or close[i - 1] < lower[i - 1]) else lower[i - 1]
            upper[i] = dn_basic[i] if (dn_basic[i] < upper[i - 1] or close[i - 1] > upper[i - 1]) else upper[i - 1]
            if direction[i - 1] == -1 and close[i] > upper[i - 1]:
                direction[i] = 1
            elif direction[i - 1] == 1 and close[i] < lower[i - 1]:
                direction[i] = -1
            else:
                direction[i] = direction[i - 1]
        st[i] = lower[i] if direction[i] == 1 else upper[i]
    return pd.Series(st, index=df.index), pd.Series(direction, index=df.index)


def add_indicators(df):
    df = df.copy()
    df["atr"] = calc_atr(df, ATR_PERIOD)
    df["adx"] = calc_adx(df, ADX_PERIOD)
    df["k"], df["d"] = calc_stoch_rsi(df["close"], RSI_PERIOD, STOCH_PERIOD, STOCH_K_SMOOTH, STOCH_D_SMOOTH)
    df["vwap"] = calc_vwap(df, VWAP_ANCHOR_TZ)
    df["st"], df["st_dir"] = calc_supertrend(df, ST_PERIOD, ST_MULT)
    df["ema_fast"] = df["close"].ewm(span=CHART_EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=CHART_EMA_SLOW, adjust=False).mean()
    return df


# ============================================================================
# GRAFIK
# ============================================================================
C_UP, C_DN = "#2962FF", "#E53935"
C_BG, C_GRID = "#FFFBE6", "#E8E2C8"


def render_chart(ex, symbol, df, sig):
    """Mum grafigi + EMA/VWAP + Giris/Stop/Hedef bolgeleri + hacim + Stoch RSI. PNG bytes dondurur."""
    d = df.tail(CHART_BARS).reset_index(drop=True)
    n = len(d)
    pad = max(10, n // 7)                                   # sag tarafta etiketler icin bosluk
    x = np.arange(n)

    fig, (ax, axv, axs) = plt.subplots(
        3, 1, figsize=(10, 7.2), dpi=110, sharex=True,
        gridspec_kw={"height_ratios": [5, 1.1, 1.4], "hspace": 0.04},
    )
    fig.patch.set_facecolor(C_BG)
    for a in (ax, axv, axs):
        a.set_facecolor(C_BG)
        a.grid(True, color=C_GRID, linewidth=0.7)
        for s in a.spines.values():
            s.set_color(C_GRID)
        a.tick_params(colors="#555", labelsize=8)
        a.yaxis.tick_right()

    # --- mumlar ---
    up = d["close"] >= d["open"]
    colors = np.where(up, C_UP, C_DN)
    ax.vlines(x, d["low"], d["high"], colors=colors, linewidth=0.9)
    body_lo = np.minimum(d["open"], d["close"])
    body_h = (d["open"] - d["close"]).abs()
    min_h = (d["high"].max() - d["low"].min()) * 0.0015
    for i in range(n):
        ax.add_patch(Rectangle((x[i] - 0.33, body_lo[i]), 0.66, max(body_h[i], min_h),
                               facecolor=colors[i], edgecolor=colors[i], linewidth=0.5))

    ax.plot(x, d["ema_fast"], color="#FF9800", linewidth=1.3, label=f"EMA{CHART_EMA_FAST}")
    ax.plot(x, d["ema_slow"], color="#9C27B0", linewidth=1.3, label=f"EMA{CHART_EMA_SLOW}")
    sess = d["ts"].dt.tz_convert(VWAP_ANCHOR_TZ).dt.date
    vwap_plot = d["vwap"].where(sess == sess.shift(), np.nan)   # gun gecisinde cizgiyi kes
    ax.plot(x, vwap_plot, color="#00897B", linewidth=1.3, linestyle="--", label="VWAP")
    if "st" in d:
        st_up = d["st"].where(d["st_dir"] == 1)
        st_dn = d["st"].where(d["st_dir"] == -1)
        ax.plot(x, st_up, color="#43A047", linewidth=1.6, label=f"SuperTrend ({ST_PERIOD},{ST_MULT:g})")
        ax.plot(x, st_dn, color=C_DN, linewidth=1.6)

    # --- giris / stop / hedef bolgeleri ---
    x0, x1 = n - 1, n - 1 + pad
    e, sl, tp = sig["entry"], sig["sl"], sig["tp"]
    entries = sig.get("entries", [e])
    ax.add_patch(Rectangle((x0, min(e, sl)), pad, abs(e - sl), facecolor=C_DN, alpha=0.15, linewidth=0))
    ax.add_patch(Rectangle((x0, min(e, tp)), pad, abs(e - tp), facecolor="#43A047", alpha=0.15, linewidth=0))

    lines = [(sl, C_DN, "STOP", "-")]
    if len(entries) == 1:
        lines.append((e, C_UP, "GİRİŞ", "-"))
    else:
        for i, ep in enumerate(entries):
            lines.append((ep, C_UP, f"GİRİŞ {i + 1}", "-" if i == 0 else "--"))
    lines.append((tp, "#43A047", "HEDEF", "-"))

    lo = min(d["low"].min(), sl, tp)
    hi = max(d["high"].max(), sl, tp)
    m = (hi - lo) * 0.06
    # etiket cakismasini onle: dikeyde en az %4.5 aralik birak
    gap = (hi - lo + 2 * m) * 0.045
    order = sorted(range(len(lines)), key=lambda i: lines[i][0])
    label_y = {}
    prev_y = -np.inf
    for i in order:
        y = max(lines[i][0], prev_y + gap)
        label_y[i] = y
        prev_y = y
    overflow = prev_y - (hi + m)
    if overflow > 0:
        label_y = {i: y - overflow for i, y in label_y.items()}

    for i, (price, color, label, ls) in enumerate(lines):
        ax.hlines(price, 0, x1, colors=color, linewidth=1.1, alpha=0.9, linestyles=ls)
        ax.text(x1 + 0.3, label_y[i], f" {label} {fmt_price(ex, symbol, price)} ", color="white", fontsize=7.5,
                va="center", ha="left", fontweight="bold",
                bbox=dict(boxstyle="square,pad=0.25", facecolor=color, edgecolor="none"))
    # sinyal mumu isareti
    ax.annotate("▲" if sig["direction"] == "LONG" else "▼",
                (x0, d["low"].iloc[-1] if sig["direction"] == "LONG" else d["high"].iloc[-1]),
                xytext=(0, -14 if sig["direction"] == "LONG" else 6), textcoords="offset points",
                ha="center", color=C_UP if sig["direction"] == "LONG" else C_DN, fontsize=11)

    ax.set_ylim(lo - m, hi + m)
    ax.set_xlim(-1, x1 + pad * 0.9)
    ax.legend(loc="upper left", fontsize=8, frameon=False, ncol=4)

    base = ex.markets.get(symbol, {}).get("base", symbol.split("/")[0]) if ex.markets else symbol
    dir_txt = "LONG" if sig["direction"] == "LONG" else "SHORT"
    ax.set_title(f"{base}USDT · MEXC Futures · {TF_LABEL} · {dir_txt}   |   ADX {sig['adx']:.1f}",
                 loc="left", fontsize=11, fontweight="bold", color="#333")

    # --- hacim ---
    axv.bar(x, d["volume"], color=colors, width=0.66, alpha=0.8)
    axv.set_yticks([])
    axv.text(0.005, 0.85, "Hacim", transform=axv.transAxes, fontsize=7.5, color="#555")

    # --- Stoch RSI ---
    axs.plot(x, d["k"], color=C_UP, linewidth=1.1, label="K")
    axs.plot(x, d["d"], color="#FF9800", linewidth=1.1, label="D")
    axs.axhspan(STOCH_OVERBOUGHT, 100, color=C_DN, alpha=0.08)
    axs.axhspan(0, STOCH_OVERSOLD, color="#43A047", alpha=0.08)
    axs.axhline(STOCH_OVERBOUGHT, color=C_DN, linewidth=0.7, linestyle=":")
    axs.axhline(STOCH_OVERSOLD, color="#43A047", linewidth=0.7, linestyle=":")
    axs.set_ylim(-2, 102)
    axs.set_yticks([STOCH_OVERSOLD, STOCH_OVERBOUGHT])
    axs.text(0.005, 0.8, f"Stoch RSI ({RSI_PERIOD},{STOCH_PERIOD},{STOCH_K_SMOOTH},{STOCH_D_SMOOTH})",
             transform=axs.transAxes, fontsize=7.5, color="#555")

    # --- zaman ekseni (TSI) ---
    times = d["ts"].dt.tz_convert(DISPLAY_TZ)
    step = max(1, n // 8)
    ticks = list(range(0, n, step))
    axs.set_xticks(ticks)
    axs.set_xticklabels([times.iloc[i].strftime("%d.%m %H:%M") for i in ticks], fontsize=7.5)

    fig.subplots_adjust(left=0.02, right=0.90, top=0.94, bottom=0.06)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


# ============================================================================
# SINYAL MANTIGI
# ============================================================================
def evaluate(df):
    """Son kapanmis mum icin sinyal dondurur: dict veya None."""
    if len(df) < 3:
        return None
    prev, cur = df.iloc[-2], df.iloc[-1]
    needed = ("atr", "adx", "k", "d", "vwap")
    if any(pd.isna(cur[c]) for c in needed) or pd.isna(prev["k"]) or pd.isna(prev["d"]):
        return None

    # 1) Guc filtresi
    if cur["adx"] < ADX_THRESHOLD:
        return None

    # 2) Tetikleyici: Stoch RSI kesisimi (kesisim bolge icinde basliyor olmali)
    cross_up = prev["k"] <= prev["d"] and cur["k"] > cur["d"] and max(prev["k"], prev["d"]) < STOCH_OVERSOLD
    cross_dn = prev["k"] >= prev["d"] and cur["k"] < cur["d"] and min(prev["k"], prev["d"]) > STOCH_OVERBOUGHT

    # 3) Trend filtresi: VWAP
    direction = None
    if cross_up and cur["close"] > cur["vwap"]:
        direction = "LONG"
    elif cross_dn and cur["close"] < cur["vwap"]:
        direction = "SHORT"
    if not direction:
        return None

    # 4) SuperTrend filtresi: yon SuperTrend ile ayni olmali
    if USE_SUPERTREND:
        if pd.isna(cur["st"]):
            return None
        if (direction == "LONG" and cur["st_dir"] != 1) or (direction == "SHORT" and cur["st_dir"] != -1):
            return None

    entry = float(cur["close"])
    risk = float(cur["atr"]) * ATR_SL_MULT
    if direction == "LONG":
        sl, tp = entry - risk, entry + risk * RR_RATIO
    else:
        sl, tp = entry + risk, entry - risk * RR_RATIO

    sl_pct = abs(entry - sl) / entry * 100
    if sl <= 0 or tp <= 0 or sl_pct < MIN_SL_PCT or sl_pct > MAX_SL_PCT:
        log.debug("SL %%%.2f limit disi, sinyal elendi", sl_pct)
        return None

    # Kademeli girisler
    sign = 1 if direction == "LONG" else -1
    step = float(cur["atr"]) * LADDER_STEP_ATR
    entries = [entry - sign * i * step for i in range(len(LADDER_WEIGHTS))]
    avg_full = sum(e * w for e, w in zip(entries, LADDER_WEIGHTS))
    rr_full = abs(tp - avg_full) / abs(avg_full - sl)
    avg_sl_pct = abs(avg_full - sl) / avg_full * 100

    return {
        "direction": direction, "entry": entry, "sl": sl, "tp": tp,
        "entries": entries, "weights": LADDER_WEIGHTS, "avg_full": avg_full,
        "rr_full": rr_full, "avg_sl_pct": avg_sl_pct,
        "sl_pct": sl_pct, "candle_ts": cur["ts"],
        "adx": float(cur["adx"]), "k": float(cur["k"]), "d": float(cur["d"]), "vwap": float(cur["vwap"]),
    }


def fmt_price(ex, symbol, price):
    try:
        return ex.price_to_precision(symbol, price)
    except Exception:
        return f"{price:.8f}".rstrip("0").rstrip(".") if price < 1 else f"{price:.4f}"


def build_message(ex, symbol, sig):
    base = ex.markets.get(symbol, {}).get("base", symbol.split("/")[0])
    coin = f"{base}/USDT"
    now_tr = datetime.now(DISPLAY_TZ).strftime("%d.%m.%Y %H:%M")
    p = lambda v: fmt_price(ex, symbol, v)
    entries = sig.get("entries", [sig["entry"]])
    head = (
        "🎯 YENİ KRİPTO SİNYALİ\n"
        f"📌 Coin: {coin}\n"
        f"📊 Yön: {sig['direction']}\n"
        f"⚙️ İşlem Türü: {MARGIN_MODE}\n"
        f"🕒 Zaman Dilimi: {TF_LABEL}\n"
    )
    tail = (
        f"⚙️ Örnek Kaldıraç: {LEVERAGE}x\n"
        f"📅 {now_tr} TSİ"
    )
    if len(entries) == 1:
        body = (
            f"🟢 Giriş: {p(sig['entry'])}\n"
            f"🔴 Stop: {p(sig['sl'])} (%{sig['sl_pct']:.2f} zarar)\n"
            f"🎯 Hedef: {p(sig['tp'])}\n"
            f"⚖️ Risk/Reward: {RR_RATIO:g}R\n"
        )
    else:
        lines = "".join(
            f"🟢 Giriş {i + 1} (%{w * 100:.0f}): {p(e)}\n" for i, (e, w) in enumerate(zip(entries, sig["weights"]))
        )
        body = (
            lines
            + f"📍 Ortalama (hepsi dolarsa): {p(sig['avg_full'])}\n"
            f"🔴 Stop: {p(sig['sl'])} (ort.'ya göre %{sig['avg_sl_pct']:.2f} zarar)\n"
            f"🎯 Hedef: {p(sig['tp'])}\n"
            f"⚖️ Risk/Reward: {RR_RATIO:g}R (1 kademe) → {sig['rr_full']:.1f}R ({len(entries)} kademe)\n"
        )
    return head + body + tail


def in_cooldown(symbol):
    t = _last_signal.get(symbol)
    return t is not None and datetime.now(timezone.utc) - t < timedelta(minutes=SIGNAL_COOLDOWN_MIN)


# ============================================================================
# ISLEM TAKIBI (TP / STOP BILDIRIMLERI)
# ============================================================================
TRADES = {"open": [], "stats": {"tp": 0, "sl": 0, "expired": 0, "pnl_pct": 0.0}}
_trade_lock = threading.Lock()


def load_trades():
    global TRADES
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            TRADES["open"] = data.get("open", [])
            TRADES["stats"].update(data.get("stats", {}))
            log.info("%d acik islem dosyadan yuklendi", len(TRADES["open"]))
    except Exception as e:
        log.error("Islem dosyasi okunamadi (%s): %s", STATE_FILE, e)


def save_trades():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(TRADES, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.error("Islem dosyasi yazilamadi: %s", e)


def has_open_trade(symbol):
    return any(t["symbol"] == symbol for t in TRADES["open"])


def register_trade(ex, symbol, sig, message_id):
    opened = sig["candle_ts"] + timedelta(minutes=TF_MINUTES)   # sinyal mumunun kapanis ani = giris ani
    base = ex.markets.get(symbol, {}).get("base", symbol.split("/")[0]) if ex.markets else symbol
    entries = sig.get("entries", [sig["entry"]])
    trade = {
        "symbol": symbol, "coin": f"{base}/USDT", "direction": sig["direction"],
        "entry": sig["entry"], "sl": sig["sl"], "tp": sig["tp"],
        "entries": entries, "weights": sig.get("weights", [1.0]),
        "filled": [True] + [False] * (len(entries) - 1),       # 1. kademe sinyal aninda alinmis sayilir
        "opened_ms": int(opened.timestamp() * 1000),
        "last_checked_ms": int(opened.timestamp() * 1000),
        "message_id": message_id,
    }
    with _trade_lock:
        TRADES["open"].append(trade)
        save_trades()


def _ladder(t):
    """Eski kayitlarla uyum: kademe alanlari yoksa tek giris varsay."""
    entries = t.get("entries") or [t["entry"]]
    weights = t.get("weights") or [1.0] * len(entries)
    filled = t.get("filled") or [True] + [False] * (len(entries) - 1)
    return entries, weights, filled


def _avg_filled(t):
    entries, weights, filled = _ladder(t)
    w = sum(wi for wi, f in zip(weights, filled) if f)
    avg = sum(e * wi for e, wi, f in zip(entries, weights, filled) if f) / w
    return avg, w, sum(filled), len(entries)


def _pnl(t, exit_price):
    """Dolan kisma gore % fiyat getirisi (ortalamaya gore) ve pozisyonun dolan orani."""
    avg, w, nf, n = _avg_filled(t)
    sign = 1 if t["direction"] == "LONG" else -1
    return sign * (exit_price - avg) / avg * 100, avg, w, nf, n


def _fmt_duration(ms):
    mins = int(ms / 60000)
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    parts = ([f"{d}g"] if d else []) + ([f"{h}s"] if h else []) + [f"{m}dk"]
    return " ".join(parts)


def _signed(v, nd):
    return f"{'+' if v >= 0 else '-'}%{abs(v):.{nd}f}"


def _close_message(ex, t, outcome, exit_price, now_ms):
    pnl, avg, w, nf, n = _pnl(t, exit_price)
    lev = pnl * LEVERAGE
    st = TRADES["stats"]
    total = st["tp"] + st["sl"]
    winrate = (st["tp"] / total * 100) if total else 0
    now_tr = datetime.now(DISPLAY_TZ).strftime("%d.%m.%Y %H:%M")
    p = lambda v: fmt_price(ex, t["symbol"], v)

    if outcome == "TP":
        head, line = "✅ HEDEF GERÇEKLEŞTİ", f"🎯 Hedef: {p(t['tp'])}"
        res = f"💰 Kâr: {_signed(pnl, 2)} ({LEVERAGE}x ile ~{_signed(lev, 1)})"
    elif outcome == "SL":
        head, line = "❌ STOP OLDU", f"🔴 Stop: {p(t['sl'])}"
        res = f"📉 Zarar: {_signed(pnl, 2)} ({LEVERAGE}x ile ~{_signed(lev, 1)})"
    else:
        head, line = "⌛ SÜRE DOLDU (TP/Stop gelmedi)", f"💲 Son fiyat: {p(exit_price)}"
        res = f"📊 Anlık sonuç: {_signed(pnl, 2)} ({LEVERAGE}x ile ~{_signed(lev, 1)})"

    entry_line = f"🟢 Giriş: {p(avg)}" if n == 1 else \
        f"🟢 Ortalama giriş: {p(avg)} ({nf}/{n} kademe · pozisyonun %{w * 100:.0f} kadarı)"
    msg = (
        f"{head}\n"
        f"📌 Coin: {t['coin']}\n"
        f"📊 Yön: {t['direction']}\n"
        f"{entry_line}\n"
        f"{line}\n"
        f"{res}\n"
    )
    if n > 1 and nf < n:
        msg += f"🚫 Dolmayan {n - nf} kademe emrini iptal et\n"
    msg += (
        f"⏱️ Süre: {_fmt_duration(now_ms - t['opened_ms'])}\n"
        f"📈 Toplam: {st['tp']} TP / {st['sl']} Stop (Başarı %{winrate:.0f})\n"
        f"📅 {now_tr} TSİ"
    )
    return msg


def _fill_message(ex, t, idx):
    avg, w, nf, n = _avg_filled(t)
    p = lambda v: fmt_price(ex, t["symbol"], v)
    sl_pct = abs(avg - t["sl"]) / avg * 100
    return (
        f"➕ {idx + 1}. KADEME ALINDI\n"
        f"📌 Coin: {t['coin']} | {t['direction']}\n"
        f"🟢 Giriş {idx + 1}: {p(t['entries'][idx])}\n"
        f"📍 Yeni ortalama: {p(avg)} ({nf}/{n} kademe)\n"
        f"🔴 Stop: {p(t['sl'])} (ort.'ya göre %{sl_pct:.2f})\n"
        f"🎯 Hedef: {p(t['tp'])}\n"
        f"📅 {datetime.now(DISPLAY_TZ).strftime('%d.%m.%Y %H:%M')} TSİ"
    )


def check_trade(ex, t):
    """1 dakikalik mumlarla kademe dolumlarini ve TP/SL'yi kontrol eder.
    (sonuc, fiyat, yeni_dolan_kademeler) dondurur; sonuc: 'TP' | 'SL' | 'EXPIRED' | None."""
    raw = with_retry(ex.fetch_ohlcv, t["symbol"], "1m", since=t["last_checked_ms"], limit=500,
                     desc=f"takip {t['symbol']}")
    now_ms = int(time.time() * 1000)
    entries, weights, filled = _ladder(t)
    t["entries"], t["weights"], t["filled"] = entries, weights, filled
    new_fills = []
    last_close = None
    if raw:
        long_ = t["direction"] == "LONG"
        for ts, o, h, l, c, v in raw:
            if ts + 60_000 <= t["opened_ms"]:
                continue                       # giristen onceki mum
            # 1) kademe dolumlari (kademeler stopun onunde oldugu icin stoptan once dokunulmus olur)
            for i, ep in enumerate(entries):
                if not filled[i] and ((l <= ep) if long_ else (h >= ep)):
                    filled[i] = True
                    new_fills.append(i)
            # 2) stop / hedef (ayni mumda ikisi de varsa temkinli: Stop say)
            if (l <= t["sl"]) if long_ else (h >= t["sl"]):
                return "SL", t["sl"], new_fills
            if (h >= t["tp"]) if long_ else (l <= t["tp"]):
                return "TP", t["tp"], new_fills
        t["last_checked_ms"] = int(raw[-1][0])   # son (acik olabilir) mumu sonraki kontrolde tekrar incele
        last_close = float(raw[-1][4])

    if MAX_TRADE_HOURS > 0 and now_ms - t["opened_ms"] > MAX_TRADE_HOURS * 3600_000 and last_close:
        return "EXPIRED", last_close, new_fills
    return None, None, new_fills


def monitor_trades(ex):
    if not TRADES["open"]:
        return
    now_ms = int(time.time() * 1000)
    changed = False
    for t in list(TRADES["open"]):
        try:
            outcome, price, new_fills = check_trade(ex, t)
            changed = True                     # last_checked_ms / filled guncellendi
            if new_fills and NOTIFY_ENTRY_FILLS and outcome is None:
                for i in new_fills:
                    send_telegram(_fill_message(ex, t, i), reply_to=t.get("message_id"))
                    log.info("KADEME DOLDU %s %d/%d", t["symbol"], i + 1, len(t["entries"]))
            if not outcome:
                continue
            pnl, *_ = _pnl(t, price)
            with _trade_lock:
                st = TRADES["stats"]
                st[{"TP": "tp", "SL": "sl", "EXPIRED": "expired"}[outcome]] += 1
                st["pnl_pct"] = round(st.get("pnl_pct", 0.0) + pnl, 4)
                TRADES["open"].remove(t)
            log.info("ISLEM KAPANDI %s %s -> %s (%.2f%%)", t["symbol"], t["direction"], outcome, pnl)
            send_telegram(_close_message(ex, t, outcome, price, now_ms), reply_to=t.get("message_id"))
        except Exception as e:
            log.exception("Islem takibinde hata (%s): %s", t.get("symbol"), e)
    if changed:
        with _trade_lock:
            save_trades()


# ============================================================================
# TARAMA DONGUSU
# ============================================================================
def scan(ex):
    t0 = time.time()
    symbols = get_top_symbols(ex)
    if not symbols:
        raise RuntimeError("Sembol listesi alinamadi (borsa erisimi?)")
    log.info("Tarama basladi: %d sembol", len(symbols))

    found = 0
    for sym in symbols:
        try:
            if in_cooldown(sym):
                continue
            if TRACK_TRADES and ONE_TRADE_PER_SYMBOL and has_open_trade(sym):
                continue
            df = fetch_closed_ohlcv(ex, sym)
            if df is None:
                continue
            dfi = add_indicators(df)
            sig = evaluate(dfi)
            if not sig:
                continue
            msg = build_message(ex, sym, sig)
            log.info("SINYAL %s %s | ADX=%.1f K=%.1f D=%.1f ST=%s", sym, sig["direction"], sig["adx"],
                     sig["k"], sig["d"], "UP" if dfi["st_dir"].iloc[-1] == 1 else "DOWN")

            msg_id = None
            if SEND_CHART:
                try:
                    msg_id = send_telegram_photo(render_chart(ex, sym, dfi, sig), msg)
                except Exception as e:
                    log.exception("Grafik olusturulamadi (%s), sadece metin gonderilecek: %s", sym, e)
            if not msg_id:
                msg_id = send_telegram(msg)
            if msg_id:
                _last_signal[sym] = datetime.now(timezone.utc)
                STATE["signals_sent"] += 1
                found += 1
                if TRACK_TRADES:
                    register_trade(ex, sym, sig, msg_id)
        except Exception as e:  # tek coindeki hata tum taramayi durdurmasin
            log.exception("%s islenirken hata: %s", sym, e)

    log.info("Tarama bitti: %d sinyal, %.1f sn | acik islem: %d", found, time.time() - t0, len(TRADES["open"]))


def next_scan_time():
    now = datetime.now(timezone.utc)
    minutes = now.hour * 60 + now.minute
    next_boundary = (minutes // TF_MINUTES + 1) * TF_MINUTES
    return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=next_boundary, seconds=SCAN_DELAY_SEC)


def main():
    log.info("Bot basliyor | TF=%s TOP_N=%d ADX>%.0f ATRx%.2f RR=%.2f Lev=%dx ST=%s Takip=%s DRY_RUN=%s",
             TIMEFRAME, TOP_N_SYMBOLS, ADX_THRESHOLD, ATR_SL_MULT, RR_RATIO, LEVERAGE,
             USE_SUPERTREND, TRACK_TRADES, DRY_RUN)
    start_health_server()
    if TRACK_TRADES:
        load_trades()

    ex = None
    consecutive_failures = 0
    markets_loaded_at = 0.0
    last_monitor = 0.0

    if _env("RUN_BACKTEST", False, bool):
        try:
            import backtest
            threading.Thread(target=backtest.run_and_report, args=(sys.modules[__name__],), daemon=True).start()
            log.info("Backtest arka planda baslatildi")
        except Exception as e:
            log.error("Backtest baslatilamadi (backtest.py repoda mi?): %s", e)

    if SEND_STARTUP_MESSAGE:
        extra =f", {len(TRADES['open'])} açık işlem takipte" if TRACK_TRADES and TRADES["open"] else ""
        send_telegram(f"🤖 Sinyal botu başlatıldı ({TF_LABEL}, MEXC Futures{extra}).")

    next_scan = datetime.now(timezone.utc) if RUN_SCAN_ON_START else next_scan_time()
    log.info("Ilk tarama: %s", next_scan.astimezone(DISPLAY_TZ).strftime("%H:%M:%S"))

    while True:
        try:
            if ex is None:
                ex = create_exchange()
            # Yeni listelemeler icin piyasalari 6 saatte bir yenile
            if time.time() - markets_loaded_at > 6 * 3600:
                if with_retry(ex.load_markets, True, desc="load_markets") is None:
                    raise RuntimeError("load_markets basarisiz")
                markets_loaded_at = time.time()

            # 1) Acik islemleri izle
            if TRACK_TRADES and time.time() - last_monitor >= MONITOR_INTERVAL_SEC:
                monitor_trades(ex)
                last_monitor = time.time()

            # 2) Mum kapanisinda tara
            if datetime.now(timezone.utc) >= next_scan:
                scan(ex)
                STATE["last_scan_at"] = datetime.now(timezone.utc).isoformat()
                STATE["last_scan_ok"] = True
                next_scan = next_scan_time()
                log.info("Sonraki tarama: %s TSI", next_scan.astimezone(DISPLAY_TZ).strftime("%H:%M:%S"))
            consecutive_failures = 0

            # 3) Bir sonraki ise kadar uyu
            until_scan = (next_scan - datetime.now(timezone.utc)).total_seconds()
            until_monitor = (last_monitor + MONITOR_INTERVAL_SEC - time.time()) if TRACK_TRADES else until_scan
            time.sleep(max(1.0, min(until_scan, until_monitor)))

        except KeyboardInterrupt:
            log.info("Durduruldu.")
            break
        except Exception as e:
            consecutive_failures += 1
            STATE["last_scan_ok"] = False
            log.exception("Ana dongu hatasi (%d. ardisik): %s", consecutive_failures, e)
            if consecutive_failures >= 3:
                log.warning("Borsa baglantisi sifirlaniyor")
                ex, markets_loaded_at = None, 0.0
            if consecutive_failures == 5:
                send_telegram("⚠️ Bot 5 kez üst üste tarama yapamadı. Logları kontrol et (MEXC erişimi / ağ).")
            if datetime.now(timezone.utc) >= next_scan:
                next_scan = next_scan_time()      # basarisiz taramayi sonsuza dek tekrarlama
            time.sleep(min(60 * consecutive_failures, 300))


if __name__ == "__main__":
    main()
