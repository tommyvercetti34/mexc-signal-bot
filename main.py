"""
MEXC Futures - Kripto Sinyal & Tarama Botu
==========================================
Strateji (15m):
  1) Trend  : Fiyat VWAP (gunluk, UTC sifirlanan) ustunde -> sadece LONG, altinda -> sadece SHORT
  2) Guc    : ADX(14) > 25 (altindaysa piyasa yatay kabul edilir, sinyal yok)
  3) Tetik  : Stoch RSI (14,14,3,3)
               - K, D'yi 20 altinda yukari keserse  -> LONG
               - K, D'yi 80 ustunde asagi keserse   -> SHORT
  4) SuperTrend (10,3) sinyal yonuyle ayni olmali
  Giris    : 3 kademe limit emir (%20/%30/%50): sinyal fiyati + alttaki 2 destek (SHORT'ta direnc)
  Stop     : son kademenin dayandigi destegin altinda; her kademe icin 15x likidasyon kontrolu
  Kar al   : Hedef 1'de %50 kapat + stop girise, Hedef 2'de kalan (hedefler ortalama girise gore)

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
STRATEGY = _env("STRATEGY", "breakout").lower()          # breakout = trend kirilimi (arastirmayi gecen), reversal = eski donus sistemi
TIMEFRAME = _env("TIMEFRAME", "1h" if STRATEGY == "breakout" else "15m")
TOP_N_SYMBOLS = _env("TOP_N_SYMBOLS", 50, int)            # 24s hacme gore ilk N USDT perpetual
MIN_QUOTE_VOLUME_USDT = _env("MIN_QUOTE_VOLUME_USDT", 5_000_000, float)
EXCLUDE_SYMBOLS = [s.strip().upper() for s in _env("EXCLUDE_SYMBOLS", "").split(",") if s.strip()]  # orn: "USDC,PEPE"
OHLCV_LIMIT = _env("OHLCV_LIMIT", 500, int)               # 500 x 15m = ~5 gun (indikatorler + destek seviyeleri)
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

# --- Trend kirilimi (STRATEGY=breakout) - arastirmada dogrulamayi gecen ayarlar ---
BO_LOOKBACK = _env("BO_LOOKBACK", 20, int)                # kac mumluk tepe/dip kirilimi
BO_ADX_MIN = _env("BO_ADX_MIN", 20.0, float)              # en az trend gucu
BO_SL_ATR = _env("BO_SL_ATR", 2.5, float)                 # stop = giris -/+ bu kadar ATR
BO_TP1_ATR = _env("BO_TP1_ATR", 2.0, float)               # Hedef 1 (%50 kapat)
BO_TP2_ATR = _env("BO_TP2_ATR", 5.0, float)               # Hedef 2 (kalan)
BO_BE_ATR = _env("BO_BE_ATR", 1.0, float)                 # bu kadar ATR karda stop girise (0 = kapali)
BO_MAX_HOURS = _env("BO_MAX_HOURS", 72.0, float)          # en uzun islem suresi
RISK_PCT = _env("RISK_PCT", 1.0, float)                   # islem basina risk: stop olursa bakiyenin %'si
BTC_REGIME_EMA = _env("BTC_REGIME_EMA", 50, int)          # BTC 4 saatlik kapanis bu EMA'nin ustu = yukari rejim

# --- Kademeli alim (scale-in, STRATEGY=reversal) ---
# Giris 1 = sinyal mumunun kapanisi. Giris 2/3 = altindaki destek (SHORT'ta ustundeki direnc) seviyeleri.
# Stop = son kademenin dayandigi destegin altinda. Her sinyalde 15x likidasyon kontrolu yapilir.
ENTRY_LEVELS = _env("ENTRY_LEVELS", 3, int)               # 1 = tek giris
ENTRY_WEIGHTS = _env("ENTRY_WEIGHTS", "20,30,50")         # her kademe, islem butcesinin % kaci (toplam <=100)
LEVEL_MODE = _env("LEVEL_MODE", "support")                # support = pivot destek/direnc, atr = sabit ATR adimi
PIVOT_K = _env("PIVOT_K", 5, int)                         # pivot: solunda ve saginda K mumdan daha dusuk/yuksek
LEVEL_LOOKBACK = _env("LEVEL_LOOKBACK", 400, int)         # destek aranacak gecmis mum sayisi (400 x 15m ~ 4 gun)
ENTRY_STEP_ATR = _env("ENTRY_STEP_ATR", 1.0, float)       # destek bulunamazsa kademe araligi (ATR)
MIN_TIER_GAP_ATR = _env("MIN_TIER_GAP_ATR", 0.5, float)   # kademeler arasi en az mesafe (ATR)
MIN_TIER_GAP_PCT = _env("MIN_TIER_GAP_PCT", 0.3, float)   # ... ve en az % mesafe
MAX_LEVEL_DEPTH_ATR = _env("MAX_LEVEL_DEPTH_ATR", 4.0, float)  # en derin kademe girisin en fazla kac ATR otesinde
SL_BUFFER_ATR = _env("SL_BUFFER_ATR", 0.5, float)         # stop, son destegin bu kadar ATR altina
ATR_SL_MULT = _env("ATR_SL_MULT", 1.5, float)             # tek giriste stop mesafesi (ATR)
MMR_PCT = _env("MMR_PCT", 0.6, float)                     # bakim teminati orani (likidasyon hesabi icin)
LIQ_BUFFER_PCT = _env("LIQ_BUFFER_PCT", 0.5, float)       # kademe/stop likidasyondan en az bu kadar uzak olmali
MAX_SL_FROM_AVG_PCT = _env("MAX_SL_FROM_AVG_PCT", 2.5, float)  # hepsi dolarsa ortalamadan stopa en fazla %
MIN_SL_PCT = _env("MIN_SL_PCT", 0.4, float)               # ... ve en az % (cok dar stoplari ele)
MAX_SL_PCT = MAX_SL_FROM_AVG_PCT                          # (eski ayar adi ile uyum)

# --- Kademeli kar alma ---
TP1_ATR = _env("TP1_ATR", 1.5, float)                     # Hedef 1 = ortalama giris +/- bu kadar ATR
TP2_ATR = _env("TP2_ATR", 3.0, float)                     # Hedef 2 = ortalama giris +/- bu kadar ATR
TP1_CLOSE_PCT = _env("TP1_CLOSE_PCT", 50.0, float)        # Hedef 1'de pozisyonun % kaci kapansin
MOVE_SL_TO_BE = _env("MOVE_SL_TO_BE", True, bool)         # Hedef 1 sonrasi stop ortalama girise cekilsin
BE_TRIGGER_ATR = _env("BE_TRIGGER_ATR", 1.0, float)       # fiyat ortalamadan bu kadar ATR lehe gidince stop girise (0 = kapali)
BE_FEE_BUFFER_PCT = _env("BE_FEE_BUFFER_PCT", 0.05, float)  # basa bas stopu komisyonu karsilasin diye girisin bu kadar karli tarafina
RR_RATIO = TP2_ATR / ATR_SL_MULT                          # (eski ayar adi ile uyum, sadece bilgi)

# --- Pozisyon buyuklugu / eszamanli islem ---
ACCOUNT_BALANCE = _env("ACCOUNT_BALANCE", 2000.0, float)  # hesap bakiyesi ($) - mesajdaki tutarlar buna gore
MAX_OPEN_TRADES = _env("MAX_OPEN_TRADES", 2, int)         # ayni anda en fazla kac islem
SLOT_PCT = _env("SLOT_PCT", 0.0, float)                   # islem basina ayrilan bakiye %; 0 = 100 / MAX_OPEN_TRADES
COMPOUND = _env("COMPOUND", True, bool)                   # kapanan islemlerin K/Z'si bakiyeye eklensin mi
FEE_MAKER = _env("FEE_MAKER", 0.0, float)                 # limit emir komisyonu (MEXC standart %0)
FEE_TAKER = _env("FEE_TAKER", 0.0002, float)              # market emir komisyonu (MEXC standart %0.02)
NOTIFY_ENTRY_FILLS = _env("NOTIFY_ENTRY_FILLS", True, bool)  # 2./3. kademe dolunca mesaj at

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
MAX_CLOSED_HISTORY = _env("MAX_CLOSED_HISTORY", 2000, int)  # dashboard icin saklanan kapanmis islem sayisi
# Kalici kayit (Render her deploy'da dosyalari siler): GitHub Gist
GITHUB_TOKEN = _env("GITHUB_TOKEN", "")                   # sadece "gist" yetkili token
GIST_ID = _env("GIST_ID", "")
# Dashboard
DASHBOARD_KEY = _env("DASHBOARD_KEY", "")                 # doluysa /dashboard?key=... ile acilir

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
    """Kademe agirliklarini dogrular. (normalize agirliklar [toplam 1], adim_atr, butcenin kullanilan orani) doner."""
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
    alloc = min(1.0, total / 100.0) if total > 1.0 else 1.0   # "40,20,20" -> butcenin %80'i kullanilir
    if total > 100.0:
        logging.warning("ENTRY_WEIGHTS toplami %%%.0f > %%100, butcenin tamamina olceklendi", total)
    return [v / total for v in w], ENTRY_STEP_ATR, alloc


LADDER_WEIGHTS, LADDER_STEP_ATR, LADDER_ALLOC = _parse_ladder()
SLOT_FRACTION = (SLOT_PCT / 100.0) if SLOT_PCT > 0 else 1.0 / max(1, MAX_OPEN_TRADES)

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
    def _send(self, code, body: bytes, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        url = urlparse(self.path)
        key = parse_qs(url.query).get("key", [""])[0]
        try:
            if url.path in ("/dashboard", "/dashboard/", "/api/data"):
                if DASHBOARD_KEY and key != DASHBOARD_KEY:
                    return self._send(403, "Yetkisiz: ?key=... gerekli".encode(), "text/plain; charset=utf-8")
                if url.path == "/api/data":
                    return self._send(200, json.dumps(dashboard_data(), ensure_ascii=False).encode(),
                                      "application/json; charset=utf-8")
                return self._send(200, DASHBOARD_HTML.encode(), "text/html; charset=utf-8")
            body = (
                f"OK\nstarted_at={STATE['started_at'].isoformat()}\n"
                f"last_scan_at={STATE['last_scan_at']}\nlast_scan_ok={STATE['last_scan_ok']}\n"
                f"signals_sent={STATE['signals_sent']}\n"
            ).encode()
            self._send(200, body, "text/plain; charset=utf-8")
        except Exception as e:
            log.error("HTTP hata: %s", e)
            try:
                self._send(500, b"error", "text/plain")
            except Exception:
                pass

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
    if len(text) > 3900:                          # Telegram siniri 4096: paragraf sinirlarindan bol
        parts, cur = [], ""
        for para in text.split("\n\n"):
            if cur and len(cur) + len(para) + 2 > 3900:
                parts.append(cur)
                cur = ""
            cur = f"{cur}\n\n{para}" if cur else para
        parts.append(cur)
        first = None
        for part in parts:
            mid = send_telegram(part[:4000], reply_to)
            first = first or mid
        return first
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


def fetch_closed_ohlcv(ex, symbol, tf=None):
    tf = tf or TF_MINUTES
    code = {15: "15m", 60: "1h", 240: "4h"}.get(tf, TIMEFRAME)
    raw = with_retry(ex.fetch_ohlcv, symbol, code, limit=OHLCV_LIMIT, desc=f"ohlcv {symbol} {code}")
    if not raw or len(raw) < 100:
        return None
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    # Henuz kapanmamis son mumu at
    now = datetime.now(timezone.utc)
    df = df[df["ts"] + timedelta(minutes=tf) <= now].reset_index(drop=True)
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
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
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
    if sig.get("tp1") is not None and abs(sig["tp1"] - tp) > 1e-12:
        lines.append((sig["tp1"], "#43A047", "HEDEF 1", "--"))
        lines.append((tp, "#2E7D32", "HEDEF 2", "-"))
    else:
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
    if sig.get("family") == "breakout" and sig.get("level"):
        lb = sig.get("lookback", BO_LOOKBACK)
        x_lvl = max(0, n - 1 - lb)
        ax.hlines(sig["level"], x_lvl, n - 1, colors="#616161", linewidth=1.0, linestyles=":")
        ax.text(x_lvl, sig["level"], f" kırılım seviyesi ({lb} mum) ", fontsize=7, color="#616161",
                va="bottom", ha="left")
    if sig.get("family") == "sweep" and sig.get("level"):
        x_lvl = max(0, n - 15)
        ax.hlines(sig["level"], x_lvl, n - 1, colors="#616161", linewidth=1.0, linestyles=":")
        ax.text(x_lvl, sig["level"], " süpürülen uç (sweep) ", fontsize=7, color="#616161",
                va="bottom" if sig["direction"] == "SHORT" else "top", ha="left")
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
    ax.set_title(f"{base}USDT · MEXC Futures · {sig.get('tf_label', TF_LABEL)} · {dir_txt}   |   ADX {sig['adx']:.1f}"
                 + (f"   |   {sig['strategy_name']}" if sig.get("strategy_name") else ""),
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
def trigger_direction(prev, cur):
    """Tek mum icin giris tetigi: 'LONG' / 'SHORT' / None (ADX + Stoch RSI + VWAP + SuperTrend)."""
    needed = ("atr", "adx", "k", "d", "vwap")
    if any(pd.isna(cur[c]) for c in needed) or pd.isna(prev["k"]) or pd.isna(prev["d"]):
        return None
    if cur["adx"] < ADX_THRESHOLD:
        return None
    cross_up = prev["k"] <= prev["d"] and cur["k"] > cur["d"] and max(prev["k"], prev["d"]) < STOCH_OVERSOLD
    cross_dn = prev["k"] >= prev["d"] and cur["k"] < cur["d"] and min(prev["k"], prev["d"]) > STOCH_OVERBOUGHT
    direction = None
    if cross_up and cur["close"] > cur["vwap"]:
        direction = "LONG"
    elif cross_dn and cur["close"] < cur["vwap"]:
        direction = "SHORT"
    if direction and USE_SUPERTREND:
        if pd.isna(cur["st"]):
            return None
        if (direction == "LONG" and cur["st_dir"] != 1) or (direction == "SHORT" and cur["st_dir"] != -1):
            return None
    return direction


# ----------------------------------------------------------------------------
# KADEME PLANLAYICI (destek/direnc + 15x likidasyon kontrolu)
# ----------------------------------------------------------------------------
def liq_price(avg, sign, leverage=None):
    """Isolated USDT-M yaklasik likidasyon fiyati."""
    lev = leverage or LEVERAGE
    return avg * (1 - sign * (1.0 / lev - MMR_PCT / 100.0))


def _pivot_levels(high, low, i, sign):
    """i anina kadar ONAYLANMIS pivot dip (LONG) / tepe (SHORT) seviyeleri. Gelecege bakmaz."""
    k = PIVOT_K
    start = max(k, i - LEVEL_LOOKBACK)
    end = i - k                                   # pivotun sagindaki k mum da kapanmis olmali
    if end <= start:
        return []
    src = low if sign == 1 else high
    out = []
    for j in range(start, end + 1):
        win = src[j - k:j + k + 1]
        if (sign == 1 and src[j] <= win.min()) or (sign == -1 and src[j] >= win.max()):
            out.append(float(src[j]))
    return out


def plan_trade(high, low, close, atr_arr, i, direction, weights=None, mode=None, leverage=None):
    """Kademe fiyatlari, stop ve risk kontrolleri. Gecemezse None doner."""
    sign = 1 if direction == "LONG" else -1
    weights = weights or LADDER_WEIGHTS
    mode = mode or LEVEL_MODE
    lev = leverage or LEVERAGE
    e1, atr = float(close[i]), float(atr_arr[i])
    if not (atr > 0 and e1 > 0):
        return None
    n = len(weights)
    liq_dist = 1.0 / lev - MMR_PCT / 100.0
    if liq_dist <= 0:
        return None
    buf = LIQ_BUFFER_PCT / 100.0
    gap = max(MIN_TIER_GAP_ATR * atr, e1 * MIN_TIER_GAP_PCT / 100.0)
    max_depth = min(e1 * liq_dist * 0.95, MAX_LEVEL_DEPTH_ATR * atr)

    # 1) Kademe fiyatlari
    entries, supports, used_support = [e1], [], False
    if n == 1:
        sl = e1 - sign * ATR_SL_MULT * atr
    else:
        cands = []
        if mode == "support":
            cands = [p for p in _pivot_levels(high, low, i, sign)
                     if 0 < sign * (e1 - p) <= max_depth]
            cands = sorted(set(cands), key=lambda p: sign * (e1 - p))    # girise en yakindan uzaga
        prev, last_support = e1, None
        for _ in range(1, n):
            pick = next((p for p in cands if sign * (prev - p) >= gap), None)
            if pick is not None:
                tier = pick + sign * 0.1 * atr                            # destegin biraz onune limit
                if sign * (prev - tier) < gap:
                    tier = prev - sign * gap
                last_support, used_support = pick, True
                supports.append(pick)
            else:
                tier = prev - sign * ENTRY_STEP_ATR * atr
                if sign * (e1 - tier) > max_depth:
                    tier = e1 - sign * max_depth
                last_support = tier
                supports.append(None)
            entries.append(tier)
            prev = tier
        base = last_support if sign * (entries[-1] - last_support) >= 0 else entries[-1]
        sl = base - sign * SL_BUFFER_ATR * atr

    # 2) Kontroller (gecmezse kademeleri girise dogru sikistirmayi dene)
    def check(ent, stop):
        if any(sign * (ent[j - 1] - ent[j]) < gap * 0.999 for j in range(1, len(ent))):
            return None
        if sign * (ent[-1] - stop) <= 0:
            return None
        for j in range(1, len(ent)):              # her kademe dolmadan once likidasyon olmasin
            w_sum = sum(weights[:j])
            avg_j = sum(e * w for e, w in zip(ent[:j], weights[:j])) / w_sum
            lq = liq_price(avg_j, sign, lev)
            if sign * (ent[j] - lq) < lq * buf:
                return None
        avg = sum(e * w for e, w in zip(ent, weights))
        lq = liq_price(avg, sign, lev)
        if sign * (stop - lq) < lq * buf:         # stop likidasyondan once gelmeli
            return None
        sl_avg = sign * (avg - stop) / avg * 100
        if sl_avg > MAX_SL_FROM_AVG_PCT or sl_avg < MIN_SL_PCT:
            return None
        return avg, lq, sl_avg

    res, compressed = check(entries, sl), False
    for _ in range(8):
        if res or n == 1:
            break
        entries = [e1 + (e - e1) * 0.85 for e in entries]
        sl = e1 + (sl - e1) * 0.85
        compressed = True
        res = check(entries, sl)
    if not res:
        return None
    avg, lq, sl_avg = res
    return {
        "direction": direction, "sign": sign, "entry": e1, "entries": entries, "weights": list(weights),
        "supports": supports, "used_support": used_support, "compressed": compressed,
        "sl": sl, "atr": atr, "avg_full": avg, "liq_full": lq, "liq_e1": liq_price(e1, sign, lev),
        "sl_avg_pct": sl_avg, "sl_e1_pct": sign * (e1 - sl) / e1 * 100,
    }


# ----------------------------------------------------------------------------
# ISLEM MOTORU (canli takip ve backtest ayni fonksiyonu kullanir)
# ----------------------------------------------------------------------------
def new_state(plan, tp1_atr=None, tp2_atr=None, tp1_frac=None, move_be=None, taker=None, be_atr=None):
    taker = FEE_TAKER if taker is None else taker
    w = plan["weights"]
    return {
        "direction": plan["direction"], "sign": plan["sign"], "entries": plan["entries"], "weights": w,
        "filled": [True] + [False] * (len(w) - 1), "cancelled": False,
        "sl": plan["sl"], "sl_orig": plan["sl"], "atr": plan["atr"],
        "tp1_atr": TP1_ATR if tp1_atr is None else tp1_atr,
        "tp2_atr": TP2_ATR if tp2_atr is None else tp2_atr,
        "tp1_frac": (TP1_CLOSE_PCT / 100.0) if tp1_frac is None else tp1_frac,
        "move_be": MOVE_SL_TO_BE if move_be is None else move_be,
        "be_atr": BE_TRIGGER_ATR if be_atr is None else be_atr, "be_done": False,
        "tp1_done": False, "rem": 1.0, "ret": 0.0, "fee": w[0] * taker,
        "closed": False, "outcome": None, "exit": None,
    }


def st_avg(s):
    """Dolan kademelerin ortalamasi ve butcedeki toplam agirligi."""
    ws = sum(w for w, f in zip(s["weights"], s["filled"]) if f)
    avg = sum(e * w for e, w, f in zip(s["entries"], s["weights"], s["filled"]) if f) / ws
    return avg, ws


def st_targets(s):
    if s.get("tp_fixed") is not None:              # eski surum kayitlari
        return s["tp_fixed"], s["tp_fixed"]
    avg, _ = st_avg(s)
    return avg + s["sign"] * s["tp1_atr"] * s["atr"], avg + s["sign"] * s["tp2_atr"] * s["atr"]


def _st_close(s, frac, px, rate):
    """Acik pozisyonun frac kadarini px'ten kapatir; getiriyi butce notional'ina oranla biriktirir."""
    for e, w, f in zip(s["entries"], s["weights"], s["filled"]):
        if f:
            part = w * s["rem"] * frac
            s["ret"] += part * s["sign"] * (px - e) / e
            s["fee"] += part * rate
    s["rem"] *= (1 - frac)


def process_bar(s, high, low, maker=None, taker=None):
    """Bir mumu isler. Olaylar: ('FILL', i) ('TP1', px) ('TP2', px) ('SL', px) ('BE', px).
    Ayni mumda stop ve hedef varsa temkinli davranilir: once stop."""
    maker = FEE_MAKER if maker is None else maker
    taker = FEE_TAKER if taker is None else taker
    if s["closed"]:
        return []
    sg, ev = s["sign"], []
    touch = (lambda p: low <= p) if sg == 1 else (lambda p: high >= p)
    reach = (lambda p: high >= p) if sg == 1 else (lambda p: low <= p)

    # 1) kademe dolumlari: hedef mumunda yeni kademe sayilmaz; ama stop da goruluyorsa fiyat kademelerden
    #    gecmis demektir -> hepsi sayilir (zarari kucuk gostermemek icin)
    if not s["tp1_done"] and not s["cancelled"]:
        tp1, _ = st_targets(s)
        if touch(s["sl"]) or not reach(tp1):
            for j, e in enumerate(s["entries"]):
                if not s["filled"][j] and touch(e):
                    s["filled"][j] = True
                    s["fee"] += s["weights"][j] * maker
                    ev.append(("FILL", j))
    pre_targets = st_targets(s) if not ev else None   # dolum olduysa bu mumda hedefler...
    if ev:                                            # ...dolum ONCESI ortalamaya gore (temkinli)
        saved = list(s["filled"])
        for kind, j in ev:
            s["filled"][j] = False
        pre_targets = st_targets(s)
        s["filled"] = saved
    # 2) stop
    if touch(s["sl"]):
        _st_close(s, 1.0, s["sl"], taker)
        outcome = "TP1_BE" if s["tp1_done"] else ("BE" if s.get("be_done") else "SL")
        s.update(closed=True, exit=s["sl"], outcome=outcome)
        ev.append(("SL" if outcome == "SL" else "BE", s["sl"]))
        return ev
    # 2b) basa bas: fiyat ortalamadan be_atr kadar lehe gittiyse stop girise (yeni stop sonraki mumdan gecerli)
    if not s["tp1_done"] and not s.get("be_done") and s.get("be_atr", 0) > 0 and s["atr"] > 0:
        avg_now, _ = st_avg(s)
        if reach(avg_now + sg * s["be_atr"] * s["atr"]):
            s["sl"] = avg_now * (1 + sg * BE_FEE_BUFFER_PCT / 100.0)
            s["be_done"], s["cancelled"] = True, True          # dolmayan kademeler iptal
            ev.append(("BE_SET", s["sl"]))
    # 3) hedefler
    tp1, tp2 = pre_targets
    if not s["tp1_done"] and reach(tp1):
        frac = s["tp1_frac"] if tp1 != tp2 else 1.0
        _st_close(s, frac, tp1, maker)
        s["tp1_done"], s["cancelled"], s["tp1_px"] = True, True, tp1   # dolmayan kademeler iptal
        ev.append(("TP1", tp1))
        if s["rem"] <= 1e-9:
            s.update(closed=True, exit=tp1, outcome="TP")
            return ev
        if s["move_be"]:
            s["sl"] = st_avg(s)[0] * (1 + sg * BE_FEE_BUFFER_PCT / 100.0)
            s["be_done"] = True
    if s["tp1_done"] and not s["closed"] and reach(tp2):
        _st_close(s, 1.0, tp2, maker)
        s.update(closed=True, exit=tp2, outcome="TP")
        ev.append(("TP2", tp2))
    return ev


def close_market(s, px, outcome="EXPIRED", taker=None):
    taker = FEE_TAKER if taker is None else taker
    _st_close(s, 1.0, px, taker)
    s.update(closed=True, exit=px, outcome=outcome)


def st_result(s, fees=True):
    """(butce notional'ina gore net getiri, dolan agirlik, dolan kademe sayisi)."""
    net = s["ret"] - (s["fee"] if fees else 0.0)
    _, ws = st_avg(s)
    return net, ws, sum(s["filled"])


def evaluate_reversal(df):
    """Donus sistemi: son kapanmis mum icin sinyal + kademe plani; yoksa None."""
    if len(df) < 3:
        return None
    prev, cur = df.iloc[-2], df.iloc[-1]
    direction = trigger_direction(prev, cur)
    if not direction:
        return None
    i = len(df) - 1
    plan = plan_trade(df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(),
                      df["atr"].to_numpy(), i, direction)
    if not plan:
        log.debug("Sinyal var ama kademe/likidasyon kontrolunu gecemedi")
        return None
    s = new_state(plan)
    tp1_e1, tp2_e1 = st_targets(s)
    s_full = dict(s, filled=[True] * len(plan["entries"]))
    tp1_full, tp2_full = st_targets(s_full)
    plan.update(
        candle_ts=cur["ts"], adx=float(cur["adx"]), k=float(cur["k"]), d=float(cur["d"]), vwap=float(cur["vwap"]),
        tp1=tp1_e1, tp=tp2_e1, tp1_full=tp1_full, tp2_full=tp2_full, sl_pct=plan["sl_avg_pct"],
    )
    return plan


# ----------------------------------------------------------------------------
# STRATEJI KAYDI - sinyal fonksiyonlari research.py ile ORTAK (test edilen = calisan)
# ----------------------------------------------------------------------------
def reversal_base_arrays(d):
    """Donus tetigi (Stoch RSI kesisimi + VWAP + ADX + SuperTrend), vektorel. (long_, short, adx)."""
    k, dd, adx = d["k"].to_numpy(), d["d"].to_numpy(), d["adx"].to_numpy()
    pk, pd_ = np.r_[np.nan, k[:-1]], np.r_[np.nan, dd[:-1]]
    c, vwap = d["close"].to_numpy(), d["vwap"].to_numpy()
    st, st_dir = d["st"].to_numpy(), d["st_dir"].to_numpy()
    with np.errstate(invalid="ignore"):
        cross_up = (pk <= pd_) & (k > dd) & (np.maximum(pk, pd_) < STOCH_OVERSOLD)
        cross_dn = (pk >= pd_) & (k < dd) & (np.minimum(pk, pd_) > STOCH_OVERBOUGHT)
        strong = adx >= ADX_THRESHOLD
        long_ = cross_up & (c > vwap) & strong
        short = cross_dn & ~long_ & (c < vwap) & strong
        if USE_SUPERTREND:
            long_ &= (st_dir == 1) & ~np.isnan(st)
            short &= (st_dir == -1) & ~np.isnan(st)
    return long_, short, adx


def breakout_signal_arrays(d, lookback, adx_min, btc_up, btc_dn):
    """Kirilim: kapanis onceki N mumun tepesinin ustunde + EMA200 ustu + ADX>=min + BTC 4s yukari (SHORT tersi)."""
    hh = d["high"].rolling(lookback).max().shift(1).to_numpy()
    ll = d["low"].rolling(lookback).min().shift(1).to_numpy()
    c, e200, adx = d["close"].to_numpy(), d["ema200"].to_numpy(), d["adx"].to_numpy()
    up, dn = np.asarray(btc_up), np.asarray(btc_dn)
    with np.errstate(invalid="ignore"):
        long_ = (c > hh) & (c > e200) & (adx >= adx_min) & (up == 1)
        short = (c < ll) & (c < e200) & (adx >= adx_min) & (dn == 1) & ~long_
    return np.nan_to_num(long_, nan=0).astype(bool), np.nan_to_num(short, nan=0).astype(bool)


def pullback_signal_arrays(d, rsi_th, btc_up, btc_dn):
    """Trend ici geri cekilme: EMA50>EMA200 trendinde son 2 mumda EMA20'ye degip ustunde kapanis + RSI esigi
    yukari kesiyor + BTC 4s yukari (SHORT tersi)."""
    c, lo, hi = d["close"].to_numpy(), d["low"].to_numpy(), d["high"].to_numpy()
    e20 = d["close"].ewm(span=20, adjust=False).mean().to_numpy()
    e50 = d["close"].ewm(span=50, adjust=False).mean().to_numpy()
    e200 = d["ema200"].to_numpy()
    rsi = calc_rsi(d["close"], 14).to_numpy()
    prsi = np.r_[np.nan, rsi[:-1]]
    up, dn = np.asarray(btc_up), np.asarray(btc_dn)
    touch_lo = np.minimum(lo, np.r_[np.nan, lo[:-1]]) <= e20
    touch_hi = np.maximum(hi, np.r_[np.nan, hi[:-1]]) >= e20
    with np.errstate(invalid="ignore"):
        long_ = (e50 > e200) & (c > e50) & touch_lo & (c > e20) & (prsi < rsi_th) & (rsi >= rsi_th) & (up == 1)
        short = (e50 < e200) & (c < e50) & touch_hi & (c < e20) & (prsi > 100 - rsi_th) & (rsi <= 100 - rsi_th) \
            & (dn == 1) & ~long_
    return np.nan_to_num(long_, nan=0).astype(bool), np.nan_to_num(short, nan=0).astype(bool)


KILL_ZONES_UTC = ((7, 10), (12, 16))   # Londra 07-10, New York 12-16 (UTC, mum ACILIS saati)


def _ts_ms(d):
    """ts sutunu -> int64 milisaniye (datetime ya da sayisal olabilir)."""
    s = d["ts"]
    if pd.api.types.is_numeric_dtype(s):
        return s.to_numpy().astype("int64")
    return pd.to_datetime(s, utc=True).dt.as_unit("ms").astype("int64").to_numpy()


def htf_prev_levels(d, htf_min):
    """Her alt-zaman mumu icin bir ONCEKI TAMAMLANMIS ust zaman mumunun tepe/dibi.
    Ust zaman mumu ayni veriden kurulur (15dk -> 1s, 1s -> 4s); eksik/yarim kova kullanilmaz."""
    ts = _ts_ms(d)
    n = len(ts)
    hi_out, lo_out = np.full(n, np.nan), np.full(n, np.nan)
    if n < 3:
        return hi_out, lo_out
    ltf_ms = int(np.median(np.diff(ts)))
    per = max(1, int(htf_min * 60_000 // max(ltf_ms, 1)))
    b = ts // (htf_min * 60_000)
    g = pd.DataFrame({"b": b, "h": d["high"].to_numpy(), "l": d["low"].to_numpy()}).groupby("b").agg(
        h=("h", "max"), l=("l", "min"), c=("h", "size"))
    g = g[g["c"] == per]                          # sadece tam (kapanmis, eksiksiz) kovalar
    hmap, lmap = g["h"].to_dict(), g["l"].to_dict()
    for i in range(n):
        pb = b[i] - 1
        if pb in hmap:
            hi_out[i], lo_out[i] = hmap[pb], lmap[pb]
    return hi_out, lo_out


def sweep_signal_arrays(d, params):
    """HTF Sweep + CISD (tamamen nedensel durum makinesi).
    Ayi: mum, onceki tamamlanmis HTF mumunun TEPESINI asar (sweep). Uc noktayi olusturan yesil mum serisinin
    ilk mumunun ACILISI = CISD seviyesi. Sonraki `win` mum icinde bir mum bu seviyenin ALTINDA, govdesi >= body*ATR
    kirmizi kapanirsa SHORT. Boga tam tersi. Stop = sweep uc noktasi +/- 0.1 ATR. Donus: (long, short, stop)."""
    htf = int(params.get("htf", 60))
    win = int(params.get("win", 12))
    body_min = float(params.get("body", 0.5))
    kz = int(params.get("kz", 0))
    O, H, L, C, A = (d[c].to_numpy().astype(float) for c in ("open", "high", "low", "close", "atr"))
    ts = _ts_ms(d)
    n = len(C)
    ph, pl = htf_prev_levels(d, htf)
    long_, short = np.zeros(n, bool), np.zeros(n, bool)
    stop = np.full(n, np.nan)
    bear = None   # (uc, uc_index, cisd_seviyesi)
    bull = None

    def run_open(i, want_up):
        k = i if ((C[i] >= O[i]) if want_up else (C[i] <= O[i])) else i - 1
        first = None
        while k >= 0 and i - k < 10 and ((C[k] >= O[k]) if want_up else (C[k] <= O[k])):
            first = k
            k -= 1
        return O[first] if first is not None else O[i]

    for i in range(n):
        # --- once mevcut kurulumda CISD tetigi (sweep mumundan SONRAKI mumlar) ---
        sig_s = sig_l = False
        if bear is not None and i - bear[1] > win:
            bear = None                           # sure doldu
        if bull is not None and i - bull[1] > win:
            bull = None
        if bear is not None and H[i] <= bear[0] and C[i] < bear[2] and (O[i] - C[i]) >= body_min * A[i] > 0:
            sig_s = True
        if bull is not None and L[i] >= bull[0] and C[i] > bull[2] and (C[i] - O[i]) >= body_min * A[i] > 0:
            sig_l = True
        # --- yeni / uzayan sweep; uc asildi ama gecerli sweep degilse kurulum iptal ---
        if not np.isnan(ph[i]) and H[i] > ph[i] and (bear is None or H[i] > bear[0]):
            bear = (H[i], i, run_open(i, True))
        elif bear is not None and H[i] > bear[0]:
            bear = None
        if not np.isnan(pl[i]) and L[i] < pl[i] and (bull is None or L[i] < bull[0]):
            bull = (L[i], i, run_open(i, False))
        elif bull is not None and L[i] < bull[0]:
            bull = None
        if sig_s and sig_l:
            sig_s = sig_l = False
        if kz and (sig_s or sig_l):
            hr = (ts[i] // 3_600_000) % 24
            if not any(a <= hr < b for a, b in KILL_ZONES_UTC):
                sig_s = sig_l = False
        if sig_s:
            short[i], stop[i] = True, bear[0] + 0.1 * A[i]
            bear = None
        if sig_l:
            long_[i], stop[i] = True, bull[0] - 0.1 * A[i]
            bull = None
    return long_, short, stop


def sweep_levels(e, sign, atr, sw_sl, p):
    """Yapisal stop + R bazli hedefler -> motorun ATR cinsinden hedefleri. Uygun degilse None.
    Donus: (sl, tp1_atr, tp2_atr)."""
    if not (atr > 0 and e > 0) or sw_sl is None or not np.isfinite(sw_sl):
        return None
    risk = (e - sw_sl) * sign
    if not (0.25 * atr <= risk <= 3.0 * atr):
        return None
    r1, r2 = (float(x) for x in p.get("tp_r", (1.0, 2.0)))
    return float(sw_sl), r1 * risk / atr, r2 * risk / atr


# ============================================================================
# TOPLU TEST: TradingView fikirleri (yapisal stop + R hedefleri)
# Hepsi sadece KAPANMIS mumlarla, gelecek bilgisi olmadan calisir. Donus: (long, short, stop)
# ============================================================================
def _swing_stop(L, H, A, i, sign, lb=12, lo=0.75, hi=3.0, C=None):
    """Son lb mumun dibi/tepesi; risk [lo, hi] ATR araligina sikistirilir (BOSWaves mantigi)."""
    e = C[i]
    a = max(0, i - lb + 1)
    raw = (e - L[a:i + 1].min()) if sign == 1 else (H[a:i + 1].max() - e)
    risk = min(max(raw, lo * A[i]), hi * A[i])
    return e - sign * risk


def ha_pullback_arrays(d, params):
    """Heikin Ashi geri cekilme (forexobroker 'C' versiyonu): yon HA'dan, seviyeler GERCEK mumlardan.
    LONG: HA kapanis > HA-kapanis EMA50, onceki HA mumu kirmizi, bu HA mumu yesil. Stop = sinyal mumunun gercek dibi."""
    O, H, L, C = (d[c].to_numpy().astype(float) for c in ("open", "high", "low", "close"))
    n = len(C)
    hc = (O + H + L + C) / 4.0
    ho = np.empty(n)
    ho[0] = (O[0] + C[0]) / 2.0
    for i in range(1, n):
        ho[i] = (ho[i - 1] + hc[i - 1]) / 2.0
    ema = pd.Series(hc).ewm(span=int(params.get("ema", 50)), adjust=False).mean().to_numpy()
    green, red = hc > ho, hc < ho
    pg, pr = np.r_[False, green[:-1]], np.r_[False, red[:-1]]
    l = (hc > ema) & pr & green
    s = (hc < ema) & pg & red
    stop = np.where(l, L, np.where(s, H, np.nan))
    return l, s, stop


def harmony_arrays(d, params):
    """Reversal Pressure (Harmony Algo) yaklasik: fiyat onceki 63 kapanisin medyanindan >= 1.4 olcek uzaklasir
    (olcek = max(IQR/1.349, ATR)), medyan+ATR dondurulur; yeni uc nokta kaniti sifirlar; ters yonde CUSUM birikimi
    >= 1.2 dondurulmus ATR ve kapanis onceki mumun tepesinin (dibinin) otesinde -> sinyal. Hazirlik 24 mumda biter."""
    win, ext, rec, exp_ = 63, float(params.get("ext", 1.4)), float(params.get("rec", 1.2)), 24
    O, H, L, C, A = (d[c].to_numpy().astype(float) for c in ("open", "high", "low", "close", "atr"))
    n = len(C)
    cs = pd.Series(C).shift(1)
    med = cs.rolling(win).median().to_numpy()
    iqr = (cs.rolling(win).quantile(0.75) - cs.rolling(win).quantile(0.25)).to_numpy() / 1.349
    patr = np.r_[np.nan, A[:-1]]
    scale = np.fmax(iqr, patr)
    l, s, stop = np.zeros(n, bool), np.zeros(n, bool), np.full(n, np.nan)
    st = {1: None, -1: None}      # yon -> dict(ext, start, atr, S)
    drift = 0.1
    for i in range(1, n):
        for sg in (1, -1):        # sg=1: asagi uzama -> LONG aranir
            z = st[sg]
            if z is not None and i - z["start"] > exp_:
                z = st[sg] = None
            if z is None:
                if np.isfinite(scale[i]) and scale[i] > 0 and sg * (med[i] - C[i]) >= ext * scale[i]:
                    st[sg] = {"ext": L[i] if sg == 1 else H[i], "start": i, "atr": patr[i], "S": 0.0}
                continue
            if (sg == 1 and L[i] < z["ext"]) or (sg == -1 and H[i] > z["ext"]):
                z["ext"] = L[i] if sg == 1 else H[i]          # yeni ters uc: kanit sifirlanir
                z["S"] = 0.0
                continue
            z["S"] = max(0.0, z["S"] + sg * (C[i] - C[i - 1]) / z["atr"] - drift)
            if z["S"] >= rec and ((sg == 1 and C[i] > H[i - 1]) or (sg == -1 and C[i] < L[i - 1])):
                if sg == 1:
                    l[i] = True
                else:
                    s[i] = True
                stop[i] = z["ext"] - sg * 0.1 * A[i]
                st[sg] = None
    both = l & s
    l, s = l & ~both, s & ~both
    return l, s, np.where(l | s, stop, np.nan)


def impulse_arrays(d, params):
    """Momentum Impulse Follow Through (Pineify) yaklasik: kapanis-kapanis hareketi onceki 60 mumun medyan/MAD
    olcegine gore >= 3 sapma ve >= 2 olcek -> itki. Sonra 'kabul' sartlari (yas>=3, uzama>=0.25 itki, kalis>=%60,
    verim>=0.25, geri verme<=0.5, ters gidis<=0.35 itki) ARDISIK 2 kapanista saglanirsa itki yonunde sinyal.
    Fiyat baslangicin 0.10 itki otesine donerse iptal; 40 mumda sure dolar. Stop = itkiden beri en uc fiyat."""
    O, H, L, C, A = (d[c].to_numpy().astype(float) for c in ("open", "high", "low", "close", "atr"))
    n = len(C)
    r = pd.Series(C).diff()
    rp = r.shift(1)
    med = rp.rolling(60).median()
    mad = (rp - med).abs().rolling(60).median()       # yaklasik MAD (kayan medyan etrafinda)
    rng_floor = (pd.Series(H - L).shift(1).rolling(60).median() * 0.1)
    scale = np.fmax((1.4826 * mad).to_numpy(), rng_floor.to_numpy())
    med, r = med.to_numpy(), r.to_numpy()
    l, s, stop = np.zeros(n, bool), np.zeros(n, bool), np.full(n, np.nan)
    ev = None
    for i in range(1, n):
        if np.isfinite(scale[i]) and scale[i] > 0 and np.isfinite(r[i]):
            if abs(r[i] - med[i]) / scale[i] >= 3 and abs(r[i]) >= 2 * scale[i] and np.sign(r[i]) == np.sign(r[i] - med[i]):
                sg = 1 if r[i] > 0 else -1
                ev = {"sg": sg, "o": C[i - 1], "e": C[i], "imp": abs(r[i]), "t": i, "ok": 0,
                      "mfe": 0.0, "adv": 0.0, "travel": 0.0, "beyond": 0, "ext": L[i] if sg == 1 else H[i]}
                continue
        if ev is None:
            continue
        sg, imp = ev["sg"], ev["imp"]
        age = i - ev["t"]
        if age > 40:
            ev = None
            continue
        ev["ext"] = min(ev["ext"], L[i]) if sg == 1 else max(ev["ext"], H[i])
        disp = sg * (C[i] - ev["e"])
        ev["mfe"] = max(ev["mfe"], sg * ((H[i] if sg == 1 else L[i]) - ev["e"]))
        ev["adv"] = max(ev["adv"], sg * (ev["e"] - (L[i] if sg == 1 else H[i])))
        ev["travel"] += abs(C[i] - C[i - 1])
        ev["beyond"] += int(disp > 0)
        if sg * (ev["o"] - C[i]) >= 0.10 * imp:       # baslangicin otesine dondu -> iptal
            ev = None
            continue
        eff = disp / ev["travel"] if ev["travel"] > 0 else 0.0
        give = (ev["mfe"] - disp) / ev["mfe"] if ev["mfe"] > 0 else 1.0
        good = (age >= 3 and disp >= 0.25 * imp and ev["beyond"] / age >= 0.60 and eff >= 0.25
                and give <= 0.50 and ev["adv"] <= 0.35 * imp)
        ev["ok"] = ev["ok"] + 1 if good else 0
        if ev["ok"] >= 2:
            if sg == 1:
                l[i] = True
            else:
                s[i] = True
            stop[i] = ev["ext"] - sg * 0.1 * A[i]
            ev = None
    return l, s, stop


def _cci(d, n=20):
    tp = (d["high"] + d["low"] + d["close"]) / 3.0
    sma = tp.rolling(n).mean()
    mad = tp.rolling(n).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return ((tp - sma) / (0.015 * mad)).to_numpy()


def divergence_arrays(d, params):
    """CCI/RSI uyumsuzlugu (Deutz1): pivot 5 sol / 5 sag. Pivot ancak 5 mum SONRA kesinlesir -> sinyal o anda.
    Boga: daha dusuk pivot dip + CCI (ve 'both' modunda RSI) daha yuksek. Stop = pivot dibi - 0.1 ATR."""
    k = int(params.get("piv", 5))
    need_rsi = params.get("mode", "cci") == "both"
    H, L, C, A = (d[c].to_numpy().astype(float) for c in ("high", "low", "close", "atr"))
    n = len(C)
    cci = _cci(d, 20)
    rsi = calc_rsi(d["close"].astype(float), 14).to_numpy()
    l, s, stop = np.zeros(n, bool), np.zeros(n, bool), np.full(n, np.nan)
    last_lo = last_hi = None
    for i in range(2 * k, n):
        j = i - k
        lw, rw = slice(j - k, j), slice(j + 1, i + 1)
        if L[j] < L[lw].min() and L[j] <= L[rw].min():
            if last_lo is not None and L[j] < L[last_lo] and cci[j] > cci[last_lo] and (not need_rsi or rsi[j] > rsi[last_lo]):
                l[i], stop[i] = True, L[j] - 0.1 * A[i]
            last_lo = j
        if H[j] > H[lw].max() and H[j] >= H[rw].max():
            if last_hi is not None and H[j] > H[last_hi] and cci[j] < cci[last_hi] and (not need_rsi or rsi[j] < rsi[last_hi]):
                if l[i]:
                    l[i], stop[i] = False, np.nan
                else:
                    s[i], stop[i] = True, H[j] + 0.1 * A[i]
            last_hi = j
    return l, s, stop


def _alma(x, n=34, offset=0.85, sigma=6.0):
    m, sd = offset * (n - 1), n / sigma
    w = np.exp(-((np.arange(n) - m) ** 2) / (2 * sd * sd))
    w /= w.sum()
    return pd.Series(x).rolling(n).apply(lambda v: float(np.dot(v, w)), raw=True).to_numpy()


def ribbon_arrays(d, params):
    """Trend Target Ribbon (BOSWaves): ALMA(34); egim = ALMA degisimi / (5 x ATR). Boga: egim > 0.08 ve kapanis
    ALMA + 0.65 x stdev(20) ustunde. Durum degisince sinyal. Stop = son 12 mum dibi, risk 0.75-3 ATR'ye sikistirilir."""
    H, L, C, A = (d[c].to_numpy().astype(float) for c in ("high", "low", "close", "atr"))
    n = len(C)
    al = _alma(C, 34)
    lb, th, conf = 5, float(params.get("slope", 0.08)), 0.65
    slope = (al - np.r_[np.full(lb, np.nan), al[:-lb]]) / (lb * A)
    sd = pd.Series(C).rolling(20).std().to_numpy()
    with np.errstate(invalid="ignore"):
        bull = (slope > th) & (C > al + conf * sd)
        bear = (slope < -th) & (C < al - conf * sd)
    state = pd.Series(np.where(bull, 1.0, np.where(bear, -1.0, np.nan))).ffill().fillna(0).to_numpy()
    prev = np.r_[0.0, state[:-1]]
    l, s = (state == 1) & (prev != 1), (state == -1) & (prev != -1)
    stop = np.full(n, np.nan)
    for i in np.where(l | s)[0]:
        if A[i] > 0:
            stop[i] = _swing_stop(L, H, A, i, 1 if l[i] else -1, C=C)
    return l, s, stop


def td_arrays(d, params):
    """TD Sequential (Dual Phase Reversal / DGT): kurulum = art arda 9 kapanis 4 mum oncesinin altinda (alis) /
    ustunde (satis); yeni sayim sadece yon degisiminde baslar. mode '9q' = 9 tamamlaninca + mukemmellik (8. veya 9.
    mumun dibi 6. ve 7.'nin diplerinden dusuk); mode '13' = kurulum sonrasi kapanis <= 2 mum onceki dip sayimi 13'e
    ulasinca. Stop = son 9 mumun dibi, risk 0.75-3 ATR'ye sikistirilir."""
    mode = str(params.get("mode", "9q"))
    H, L, C, A = (d[c].to_numpy().astype(float) for c in ("high", "low", "close", "atr"))
    n = len(C)
    l, s, stop = np.zeros(n, bool), np.zeros(n, bool), np.full(n, np.nan)
    bc = sc = 0                  # kurulum sayaclari
    cd = {1: None, -1: None}     # geri sayim: yon -> sayac
    for i in range(5, n):
        dn, up = C[i] < C[i - 4], C[i] > C[i - 4]
        bc = (bc + 1 if (bc > 0 or (C[i - 1] >= C[i - 5])) else 0) if dn else 0
        sc = (sc + 1 if (sc > 0 or (C[i - 1] <= C[i - 5])) else 0) if up else 0
        sig = 0
        if bc == 9:
            if mode == "9q" and min(L[i], L[i - 1]) <= min(L[i - 2], L[i - 3]):
                sig = 1
            cd[1], cd[-1] = 0, None
        if sc == 9:
            if mode == "9q" and max(H[i], H[i - 1]) >= max(H[i - 2], H[i - 3]):
                sig = -1
            cd[-1], cd[1] = 0, None
        if mode == "13":
            if cd[1] is not None and bc != 9 and C[i] <= L[i - 2]:
                cd[1] += 1
                if cd[1] >= 13:
                    sig, cd[1] = 1, None
            if cd[-1] is not None and sc != 9 and C[i] >= H[i - 2]:
                cd[-1] += 1
                if cd[-1] >= 13:
                    sig, cd[-1] = -1, None
        if bc == 9:
            bc = 0                 # kurulum tamam; yeni kurulum yeniden yon degisimi ister
        if sc == 9:
            sc = 0
        if sig and A[i] > 0:
            (l if sig == 1 else s)[i] = True
            stop[i] = _swing_stop(L, H, A, i, sig, lb=9, C=C)
    return l, s, stop


def candle_profile_arrays(d, params):
    """Rolling Candle Profile (Zeiierman) 'fiyat baskisi' modu yaklasik: son 60 mum, yeniye artan agirlik.
    Mum basina alim baskisi = (kapanis - dip), satim = (tepe - kapanis). Baskinlik = (A-S)/(A+S) >= 0.25;
    60 mumluk aralik 12 banda bolunur, sinyal yonunde baskin >= 7 bant; fiyat yukselen EMA21 ustunde; ilk kez saglandiginda
    sinyal, ayni yonde en az 6 mum ara. Stop = son 12 mum dibi, risk 0.75-3 ATR (yazar stop vermiyor)."""
    win, nb, dom_th, min_b, gap = 60, 12, 0.25, int(params.get("bands", 7)), 6
    O, H, L, C, A = (d[c].to_numpy().astype(float) for c in ("open", "high", "low", "close", "atr"))
    n = len(C)
    bp, sp = C - L, H - C
    ema = pd.Series(C).ewm(span=21, adjust=False).mean().to_numpy()
    w = np.arange(1, win + 1, dtype=float)
    condl, conds = np.zeros(n, bool), np.zeros(n, bool)
    for i in range(win, n):
        a = i - win + 1
        hh, ll = H[a:i + 1], L[a:i + 1]
        b, s_ = bp[a:i + 1] * w, sp[a:i + 1] * w
        tot = b.sum() + s_.sum()
        if tot <= 0:
            continue
        dom = (b.sum() - s_.sum()) / tot
        top, bot = hh.max(), ll.min()
        if top <= bot:
            continue
        edges = np.linspace(bot, top, nb + 1)
        rngc = np.maximum(hh - ll, 1e-12)
        ov = np.clip(np.minimum(hh[:, None], edges[None, 1:]) - np.maximum(ll[:, None], edges[None, :-1]), 0, None) / rngc[:, None]
        bb, ss = (ov * b[:, None]).sum(0), (ov * s_[:, None]).sum(0)
        nbull, nbear = int((bb > ss * 1.1).sum()), int((ss > bb * 1.1).sum())
        condl[i] = dom >= dom_th and nbull >= min_b and C[i] > ema[i] and ema[i] > ema[i - 1]
        conds[i] = dom <= -dom_th and nbear >= min_b and C[i] < ema[i] and ema[i] < ema[i - 1]
    l = condl & ~np.r_[False, condl[:-1]]
    s = conds & ~np.r_[False, conds[:-1]]
    for arr in (l, s):                          # ayni yonde en az 'gap' mum ara
        last = -10**9
        for i in np.where(arr)[0]:
            if i - last < gap:
                arr[i] = False
            else:
                last = i
    stop = np.full(n, np.nan)
    for i in np.where(l | s)[0]:
        if A[i] > 0:
            stop[i] = _swing_stop(L, H, A, i, 1 if l[i] else -1, C=C)
    return l, s, stop


STRUCT_FAMILIES = {"sweep": None, "ha_pb": ha_pullback_arrays, "harmony": harmony_arrays, "impulse": impulse_arrays,
                   "diverg": divergence_arrays, "ribbon": ribbon_arrays, "tdseq": td_arrays,
                   "cprofile": candle_profile_arrays}
STRUCT_REASONS = {
    "ha_pb": "📈 Heikin Ashi trend içi geri çekilme bitti (HA EMA50 {side}, ilk {col} HA mumu)",
    "harmony": "📈 Aşırı uzamadan sonra kapanışla teyitli toparlanma (Reversal Pressure)",
    "impulse": "📈 Güçlü itki hareketi ardından devam teyidi (Momentum Impulse)",
    "diverg": "📈 Fiyat–CCI/RSI uyumsuzluğu (pivot 5 mum sonra kesinleşti)",
    "ribbon": "📈 ALMA trend şeridi yön değiştirdi (Trend Target Ribbon)",
    "tdseq": "📈 TD Sequential tükenme sayımı tamamlandı",
    "cprofile": "📈 Mum profili baskınlığı + yükselen EMA21 (Candle Profile)",
}


def strategy_signal_arrays(family, d, params, btc_up, btc_dn, h1_close=None, h1_ema50=None):
    """Tum aileler icin tek giris noktasi. (long_, short, skor)."""
    adx = d["adx"].to_numpy()
    up, dn = np.asarray(btc_up), np.asarray(btc_dn)
    if family == "breakout":
        l, s = breakout_signal_arrays(d, int(params["lookback"]), BO_ADX_MIN, up, dn)
    elif family == "pullback":
        l, s = pullback_signal_arrays(d, float(params["rsi"]), up, dn)
    elif family in ("reversal_btc", "reversal_f", "reversal_raw"):
        l, s, _ = reversal_base_arrays(d)
        with np.errstate(invalid="ignore"):
            if family in ("reversal_btc", "reversal_f"):
                l = l & (up == 1)
                s = s & (dn == 1)
            if family == "reversal_f":
                hc, he = np.asarray(h1_close), np.asarray(h1_ema50)
                l = l & (hc > he)
                s = s & (hc < he)
        l, s = np.nan_to_num(l, nan=0).astype(bool), np.nan_to_num(s, nan=0).astype(bool) & ~np.nan_to_num(l, nan=0).astype(bool)
    elif family == "supertrend":
        sd = d["st_dir"].to_numpy().astype(float)
        psd = np.r_[np.nan, sd[:-1]]
        c, e200 = d["close"].to_numpy(), d["ema200"].to_numpy()
        with np.errstate(invalid="ignore"):
            l = (sd == 1) & (psd == -1)           # SuperTrend asagidan yukari dondu
            s = (sd == -1) & (psd == 1)           # yukaridan asagi dondu
            if int(params.get("trend", 1)):       # trend filtresi: EMA200 + BTC 4s rejimi
                l = l & (c > e200) & (up == 1)
                s = s & (c < e200) & (dn == 1)
        l, s = np.nan_to_num(l, nan=0).astype(bool), np.nan_to_num(s, nan=0).astype(bool)
    elif family in STRUCT_FAMILIES:
        fn = sweep_signal_arrays if family == "sweep" else STRUCT_FAMILIES[family]
        l, s, stop = fn(d, params)
        d["struct_sl"] = stop                     # yapisal stop (evaluate_strategy / research.simulate okur)
    else:
        raise ValueError(f"bilinmeyen strateji ailesi: {family}")
    if family in ("breakout", "pullback") and int(params.get("st", 0)):
        sd = d["st_dir"].to_numpy()               # SuperTrend onay filtresi: yon SuperTrend ile ayni olmali
        l, s = l & (sd == 1), s & (sd == -1)
    return l, s, adx


TF_LABELS = {15: "15 Dakika", 60: "1 Saat", 240: "4 Saat"}
TF_CODES = {15: "15m", 60: "1h", 240: "4h"}

STRAT_DEFS = {
    "breakout_1h": {"family": "breakout", "tf": 60, "name": "Trend kırılımı", "max_hours": BO_MAX_HOURS,
                    "params": {"lookback": BO_LOOKBACK, "sl": BO_SL_ATR, "tp": [BO_TP1_ATR, BO_TP2_ATR], "be": BO_BE_ATR}},
    "breakout_4h": {"family": "breakout", "tf": 240, "name": "Trend kırılımı", "max_hours": 240,
                    "params": {"lookback": 20, "sl": 3.0, "tp": [3.0, 9.0], "be": 1.5}},
    "pullback_1h": {"family": "pullback", "tf": 60, "name": "Trend içi geri çekilme", "max_hours": 72,
                    "params": {"rsi": 50, "sl": 2.5, "tp": [1.5, 3.0], "be": 0.0}},
    "pullback_4h": {"family": "pullback", "tf": 240, "name": "Trend içi geri çekilme", "max_hours": 240,
                    "params": {"rsi": 50, "sl": 3.0, "tp": [3.0, 6.0], "be": 1.5}},
    "reversal_15m": {"family": "reversal_f", "tf": 15, "name": "Dönüş + BTC/1s filtre", "max_hours": 48,
                     "params": {"sl": 2.5, "tp": [2.0, 4.0], "be": 0.0}},
    "reversal_1h": {"family": "reversal_btc", "tf": 60, "name": "Dönüş + BTC filtre", "max_hours": 96,
                    "params": {"sl": 2.5, "tp": [2.0, 4.0], "be": 0.0}},
    "supertrend_1h": {"family": "supertrend", "tf": 60, "name": "SuperTrend dönüşü", "max_hours": 96,
                      "params": {"trend": 1, "sl": 2.5, "tp": [2.0, 5.0], "be": 1.0}},
    "supertrend_4h": {"family": "supertrend", "tf": 240, "name": "SuperTrend dönüşü", "max_hours": 240,
                      "params": {"trend": 1, "sl": 3.0, "tp": [3.0, 6.0], "be": 1.0}},
    "sweep_15m": {"family": "sweep", "tf": 15, "name": "HTF Sweep + CISD (1s)", "max_hours": 24,
                  "params": {"htf": 60, "win": 12, "body": 0.5, "kz": 0, "tp_r": [1.0, 2.0], "be": 0.0}},
    "sweep_1h": {"family": "sweep", "tf": 60, "name": "HTF Sweep + CISD (4s)", "max_hours": 72,
                 "params": {"htf": 240, "win": 12, "body": 0.5, "kz": 0, "tp_r": [1.0, 2.0], "be": 0.0}},
    "ha_pb_15m": {"family": "ha_pb", "tf": 15, "name": "Heikin Ashi geri çekilme", "max_hours": 24,
                    "params": {'ema': 50, 'tp_r': [1.5, 1.5], 'be': 0.0}},
    "ha_pb_1h": {"family": "ha_pb", "tf": 60, "name": "Heikin Ashi geri çekilme", "max_hours": 72,
                    "params": {'ema': 50, 'tp_r': [1.5, 1.5], 'be': 0.0}},
    "harmony_15m": {"family": "harmony", "tf": 15, "name": "Reversal Pressure", "max_hours": 24,
                    "params": {'ext': 1.4, 'rec': 1.2, 'tp_r': [1.0, 2.0], 'be': 0.0}},
    "harmony_1h": {"family": "harmony", "tf": 60, "name": "Reversal Pressure", "max_hours": 72,
                    "params": {'ext': 1.4, 'rec': 1.2, 'tp_r': [1.0, 2.0], 'be': 0.0}},
    "impulse_15m": {"family": "impulse", "tf": 15, "name": "Momentum itki devamı", "max_hours": 24,
                    "params": {'tp_r': [1.0, 2.0], 'be': 0.0}},
    "impulse_1h": {"family": "impulse", "tf": 60, "name": "Momentum itki devamı", "max_hours": 72,
                    "params": {'tp_r': [1.0, 2.0], 'be': 0.0}},
    "diverg_15m": {"family": "diverg", "tf": 15, "name": "CCI/RSI uyumsuzluğu", "max_hours": 24,
                    "params": {'piv': 5, 'mode': 'cci', 'tp_r': [1.0, 2.0], 'be': 0.0}},
    "diverg_1h": {"family": "diverg", "tf": 60, "name": "CCI/RSI uyumsuzluğu", "max_hours": 72,
                    "params": {'piv': 5, 'mode': 'cci', 'tp_r': [1.0, 2.0], 'be': 0.0}},
    "ribbon_15m": {"family": "ribbon", "tf": 15, "name": "ALMA trend şeridi", "max_hours": 24,
                    "params": {'slope': 0.08, 'tp_r': [1.0, 2.0], 'be': 0.0}},
    "ribbon_1h": {"family": "ribbon", "tf": 60, "name": "ALMA trend şeridi", "max_hours": 72,
                    "params": {'slope': 0.08, 'tp_r': [1.0, 2.0], 'be': 0.0}},
    "tdseq_15m": {"family": "tdseq", "tf": 15, "name": "TD Sequential", "max_hours": 24,
                    "params": {'mode': '9q', 'tp_r': [1.0, 2.0], 'be': 0.0}},
    "tdseq_1h": {"family": "tdseq", "tf": 60, "name": "TD Sequential", "max_hours": 72,
                    "params": {'mode': '9q', 'tp_r': [1.0, 2.0], 'be': 0.0}},
    "cprofile_15m": {"family": "cprofile", "tf": 15, "name": "Mum profili", "max_hours": 24,
                    "params": {'bands': 7, 'tp_r': [1.0, 2.0], 'be': 0.0}},
    "cprofile_1h": {"family": "cprofile", "tf": 60, "name": "Mum profili", "max_hours": 72,
                    "params": {'bands': 7, 'tp_r': [1.0, 2.0], 'be': 0.0}},
    "scalein_15m": {"family": "scalein", "tf": 15, "name": "Kademeli dönüş (eski sistem)", "max_hours": MAX_TRADE_HOURS,
                    "params": {}},
}


def _load_strategies():
    raw = _env("STRATEGIES", "")
    if not raw:
        raw = "scalein_15m" if STRATEGY == "reversal" else ("breakout_4h" if TIMEFRAME == "4h" else "breakout_1h")
    ids = [x.strip().lower() for x in raw.split(",") if x.strip()]
    bad = [x for x in ids if x not in STRAT_DEFS]
    if bad:
        logging.error("Bilinmeyen strateji(ler) yok sayildi: %s (gecerli: %s)", bad, ", ".join(STRAT_DEFS))
    ids = [x for x in ids if x in STRAT_DEFS] or ["breakout_1h"]
    try:
        over = json.loads(_env("STRATEGY_PARAMS", "") or "{}")
    except ValueError as e:
        logging.error("STRATEGY_PARAMS JSON hatali, yok sayildi: %s", e)
        over = {}
    out = {}
    for sid in ids:
        spec = {**STRAT_DEFS[sid], "id": sid, "params": dict(STRAT_DEFS[sid]["params"])}
        if isinstance(over.get(sid), dict):
            o = dict(over[sid])
            if "max_hours" in o:
                spec["max_hours"] = float(o.pop("max_hours"))
            spec["params"].update(o)
        spec["tf_label"] = TF_LABELS.get(spec["tf"], f"{spec['tf']}dk")
        out[sid] = spec
    return out


ACTIVE_STRATEGIES = _load_strategies()


def strategy_label(sid):
    sp = ACTIVE_STRATEGIES.get(sid) or {**STRAT_DEFS.get(sid, {"name": sid, "tf": 0}), "id": sid}
    return f"{sp['name']} ({TF_LABELS.get(sp['tf'], '?')})"


def safe_leverage(sl_dist):
    """Stop, likidasyondan once gelecek sekilde en yuksek kaldirac (en fazla LEVERAGE)."""
    room = sl_dist * (1 + LIQ_BUFFER_PCT / 100.0 + 0.25) + MMR_PCT / 100.0
    return max(1, min(LEVERAGE, int(1.0 / room)))


def btc_perp_symbol(ex):
    """BTC/USDT SUREKLI (perpetual) kontrati. Vadeli (ceyreklik) kontratlari secmez - Binance'te onlar da var."""
    mk = ex.markets or {}
    if "BTC/USDT:USDT" in mk:
        return "BTC/USDT:USDT"
    for sym, m in mk.items():
        if m.get("base") == "BTC" and m.get("quote") == "USDT" and m.get("swap") and m.get("linear"):
            return sym
    return "BTC/USDT:USDT"


_btc_cache = {"t": 0.0, "val": None}


def btc_regime(ex):
    """BTC/USDT 4 saatlik SON KAPANMIS mumun kapanisi EMA'nin ustunde mi? (up, dn) - 10 dk onbellek."""
    if time.time() - _btc_cache["t"] < 600 and _btc_cache["val"] is not None:
        return _btc_cache["val"]
    sym = btc_perp_symbol(ex)
    raw = with_retry(ex.fetch_ohlcv, sym, "4h", limit=300, desc="btc 4h")
    if not raw:
        return None
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df[df["ts"] + 4 * 3600_000 <= int(time.time() * 1000)]
    if len(df) < BTC_REGIME_EMA + 5:
        return None
    close = df["close"].astype(float)
    ema = close.ewm(span=BTC_REGIME_EMA, adjust=False).mean()
    val = (int(close.iloc[-1] > ema.iloc[-1]), int(close.iloc[-1] < ema.iloc[-1]))
    _btc_cache.update(t=time.time(), val=val)
    return val


def evaluate_strategy(sid, df, btc, h1=None, spec=None):
    """Son kapanmis mum icin strateji sinyali + tek girisli plan; yoksa None.
    btc = (up, dn) son kapanmis 4s BTC rejimi; h1 = (close, ema50) son kapanmis 1s (reversal_15m icin)."""
    spec = spec or ACTIVE_STRATEGIES.get(sid) or {**STRAT_DEFS[sid], "id": sid,
                                                  "tf_label": TF_LABELS.get(STRAT_DEFS[sid]["tf"], "")}
    fam, p = spec["family"], spec["params"]
    if fam == "scalein":
        return evaluate_reversal(df)
    if btc is None or len(df) < 220:
        return None
    n = len(df)
    up, dn = np.zeros(n), np.zeros(n)
    up[-1], dn[-1] = btc
    hc = he = None
    if fam == "reversal_f":
        if not h1:
            return None
        hc, he = np.full(n, np.nan), np.full(n, np.nan)
        hc[-1], he[-1] = h1
    long_, short, adx = strategy_signal_arrays(fam, df, p, up, dn, hc, he)
    if not (long_[-1] or short[-1]):
        return None
    cur = df.iloc[-1]
    direction = "LONG" if long_[-1] else "SHORT"
    sign = 1 if direction == "LONG" else -1
    e, atr = float(cur["close"]), float(cur["atr"])
    if not (atr > 0 and e > 0):
        return None
    if fam in STRUCT_FAMILIES:
        lv = sweep_levels(e, sign, atr, float(df["struct_sl"].iloc[-1]), p)
        if lv is None:
            return None
        sl, tp1_atr, tp2_atr = lv
    else:
        sl = e - sign * float(p["sl"]) * atr
        tp1_atr, tp2_atr = float(p["tp"][0]), float(p["tp"][1])
    sl_dist = abs(e - sl) / e
    if sl_dist < 0.002 or sl_dist > 0.08:
        return None
    lev = safe_leverage(sl_dist)
    be_atr = float(p.get("be", 0.0))
    plan = {"direction": direction, "sign": sign, "entry": e, "entries": [e], "weights": [1.0], "supports": [],
            "used_support": False, "compressed": False, "sl": sl, "atr": atr, "avg_full": e,
            "liq_full": liq_price(e, sign, lev), "sl_avg_pct": sl_dist * 100, "sl_e1_pct": sl_dist * 100,
            "sl_dist": sl_dist, "leverage": lev, "strategy": sid, "family": fam, "tf": spec["tf"],
            "tf_label": spec["tf_label"], "strategy_name": spec["name"], "max_hours": spec["max_hours"],
            "tp1_atr": tp1_atr, "tp2_atr": tp2_atr, "be_atr": be_atr, "st_confirm": bool(int(p.get("st", 0)))}
    if fam in STRUCT_FAMILIES:
        plan["tp_r"] = [float(x) for x in p.get("tp_r", (1, 2))]
    if fam == "sweep":
        plan["level"] = float(sl + sign * 0.1 * atr)   # suepurulen uc nokta
        plan["htf"] = int(p.get("htf", 60))
    if fam == "breakout":
        lb = int(p["lookback"])
        plan["level"] = float(df["high"].iloc[-lb - 1:-1].max() if sign == 1 else df["low"].iloc[-lb - 1:-1].min())
        plan["lookback"] = lb
    s = new_state(plan, tp1_atr=tp1_atr, tp2_atr=tp2_atr, tp1_frac=0.5, move_be=True, be_atr=be_atr)
    tp1, tp2 = st_targets(s)
    plan.update(candle_ts=cur["ts"], adx=float(cur["adx"]), k=float(cur["k"]), d=float(cur["d"]),
                vwap=float(cur["vwap"]), tp1=tp1, tp=tp2, tp1_full=tp1, tp2_full=tp2, sl_pct=sl_dist * 100)
    return plan


def evaluate_breakout(df, btc):
    """(geriye uyum) 1 saatlik trend kirilimi."""
    return evaluate_strategy("breakout_1h", df, btc, spec={**STRAT_DEFS["breakout_1h"], "id": "breakout_1h",
                                                          "tf_label": "1 Saat"})


def evaluate(df, btc=None, h1=None):
    """(geriye uyum) ilk aktif strateji."""
    sid = next(iter(ACTIVE_STRATEGIES))
    return evaluate_strategy(sid, df, btc, h1)


def size_position(sig):
    """Pozisyon buyuklugu. Tek girisli stratejiler: risk bazli (stop olursa bakiyenin RISK_PCT'si);
    kademeli eski sistem: bakiye x slot x kademe agirliklari."""
    bal = current_balance()
    slot = bal * SLOT_FRACTION
    if sig.get("family", "scalein") != "scalein" and "sl_dist" in sig:
        lev = sig["leverage"]
        notional = bal * RISK_PCT / 100.0 / sig["sl_dist"]
        margin = notional / lev
        used = sum(t.get("margin") or 0 for t in TRADES["open"])
        cap = min(slot, max(0.0, bal - used))
        if margin > cap:
            margin = cap
            notional = margin * lev
        sig.update(balance=bal, slot=slot, margins=[margin], notional=notional,
                   risk_usd=notional * sig["sl_dist"])
    else:
        _, total, margins = plan_margin()
        sig.update(balance=bal, slot=slot, margins=margins, notional=total * LEVERAGE, leverage=LEVERAGE,
                   strategy=sig.get("strategy", "scalein_15m"), family="scalein")
    return sig


def build_message_single(ex, symbol, sig):
    base = ex.markets.get(symbol, {}).get("base", symbol.split("/")[0])
    p = lambda v: fmt_price(ex, symbol, v)
    lev = sig["leverage"]
    margin = sig["margins"][0]
    fam = sig["family"]
    if fam == "breakout":
        kind = "yukarı" if sig["direction"] == "LONG" else "aşağı"
        reason = (f"📈 Son {sig['lookback']} mumun {'tepesi' if sig['sign'] == 1 else 'dibi'} ({p(sig['level'])}) "
                  f"{kind} kırıldı · ADX {sig['adx']:.0f}")
        if sig.get("st_confirm"):
            reason += " · SuperTrend onaylı"
    elif fam == "pullback":
        reason = f"📈 Trend içinde EMA20'ye geri çekilme ve RSI dönüşü · ADX {sig['adx']:.0f}"
        if sig.get("st_confirm"):
            reason += " · SuperTrend onaylı"
    elif fam == "sweep":
        htf = TF_LABELS.get(int(sig.get("htf", 60)), "üst zaman")
        side = "tepesi" if sig["direction"] == "SHORT" else "dibi"
        reason = (f"📈 Önceki {htf} mumunun {side} süpürüldü (uç {p(sig['level'])}) ve CISD ile yön değişti"
                  f" · TP {sig['tp_r'][0]:g}R / {sig['tp_r'][1]:g}R")
    elif fam in STRUCT_REASONS:
        lg = sig["direction"] == "LONG"
        reason = STRUCT_REASONS[fam].format(side="üstünde" if lg else "altında", col="yeşil" if lg else "kırmızı")
        reason += f" · TP {sig['tp_r'][0]:g}R / {sig['tp_r'][1]:g}R"
    elif fam == "supertrend":
        reason = (f"📈 SuperTrend ({ST_PERIOD},{ST_MULT:g}) {'yukarı' if sig['direction'] == 'LONG' else 'aşağı'} döndü"
                  f" · ADX {sig['adx']:.0f}")
    else:
        zone = "aşırı satım" if sig["direction"] == "LONG" else "aşırı alım"
        reason = f"📈 Stoch RSI {zone} bölgesinden dönüş · ADX {sig['adx']:.0f}"
    msg = (
        f"🎯 YENİ KRİPTO SİNYALİ · {sig['strategy_name']}\n"
        f"📌 Coin: {base}/USDT\n"
        f"📊 Yön: {sig['direction']}\n"
        f"⚙️ İşlem Türü: {MARGIN_MODE} · {lev}x\n"
        f"🕒 Zaman Dilimi: {sig['tf_label']}\n"
        f"{reason}\n"
        f"🟢 Giriş: {p(sig['entry'])} (piyasa)\n"
        f"🔴 Stop: {p(sig['sl'])} (%{sig['sl_dist'] * 100:.2f} · {lev}x ile ~%{sig['sl_dist'] * 100 * lev:.0f})\n"
        f"☠️ Tahmini likidasyon: {p(sig['liq_full'])}\n"
        f"🎯 Hedef 1 (%50 kapat): {p(sig['tp1'])}\n"
        f"🎯 Hedef 2 (kalan): {p(sig['tp'])}\n"
    )
    if sig.get("be_atr", 0) > 0:
        msg += f"🛡 Fiyat {sig['be_atr']:g} ATR lehe gidince stop girişe çekilir\n"
    rr = abs(sig["tp"] - sig["entry"]) / abs(sig["entry"] - sig["sl"])
    msg += f"⚖️ Risk/Reward (Hedef 2): {rr:.1f}R\n"
    msg += (f"💵 Teminat: {_usd(margin)} → pozisyon {_usd(sig['notional'])} · "
            f"stop olursa -{_usd(sig['risk_usd'])} (bakiyenin %{sig['risk_usd'] / sig['balance'] * 100:.1f} kadarı)\n")
    msg += f"⚙️ Kaldıraç: {lev}x\n📅 {datetime.now(DISPLAY_TZ).strftime('%d.%m.%Y %H:%M')} TSİ"
    return msg


build_message_breakout = build_message_single


def fmt_price(ex, symbol, price):
    try:
        return ex.price_to_precision(symbol, price)
    except Exception:
        return f"{price:.8f}".rstrip("0").rstrip(".") if price < 1 else f"{price:.4f}"


def current_balance():
    """Hesap bakiyesi: ACCOUNT_BALANCE + (COMPOUND ise) takip edilen kapanmis islemlerin $ K/Z'si."""
    if not COMPOUND:
        return ACCOUNT_BALANCE
    try:
        return ACCOUNT_BALANCE + sum(t.get("usd", 0.0) for t in TRADES["closed"]
                                     if t.get("closed_ms", 0) >= TRADES.get("balance_since_ms", 0))
    except NameError:
        return ACCOUNT_BALANCE


def plan_margin():
    """Bu islem icin toplam teminat (tum kademeler) ve kademe bazli tutarlar."""
    slot = current_balance() * SLOT_FRACTION
    total = slot * LADDER_ALLOC
    return slot, total, [total * w for w in LADDER_WEIGHTS]


def _usd(v):
    return f"${v:,.0f}" if abs(v) >= 100 else f"${v:,.2f}"


def worst_case_loss(sig):
    """Tum kademeler dolup stop olursa butce notional'ina gore kayip orani (pozitif sayi)."""
    sg = sig["sign"]
    return sum(w * sg * (e - sig["sl"]) / e for e, w in zip(sig["entries"], sig["weights"]))


def build_message(ex, symbol, sig):
    base = ex.markets.get(symbol, {}).get("base", symbol.split("/")[0])
    coin = f"{base}/USDT"
    now_tr = datetime.now(DISPLAY_TZ).strftime("%d.%m.%Y %H:%M")
    p = lambda v: fmt_price(ex, symbol, v)
    entries = sig["entries"]
    margins = sig.get("margins")
    n = len(entries)
    msg = (
        "🎯 YENİ KRİPTO SİNYALİ\n"
        f"📌 Coin: {coin}\n"
        f"📊 Yön: {sig['direction']}\n"
        f"⚙️ İşlem Türü: {MARGIN_MODE} · {LEVERAGE}x\n"
        f"🕒 Zaman Dilimi: {TF_LABEL}\n"
    )
    if n == 1:
        msg += f"🟢 Giriş: {p(entries[0])}" + (f" ({_usd(margins[0])})" if margins else "") + "\n"
    else:
        msg += "━━ Kademeli giriş (limit emir) ━━\n"
        for i, (e, w) in enumerate(zip(entries, sig["weights"])):
            amt = f"{_usd(margins[i])} · %{w * 100:.0f}" if margins else f"%{w * 100:.0f}"
            src = "sinyal fiyatı" if i == 0 else ("destek" if sig["direction"] == "LONG" else "direnç") \
                if sig["supports"][i - 1] is not None else "ATR"
            msg += f"🟢 Giriş {i + 1} ({amt}): {p(e)} · {src}\n"
        msg += f"📍 Ortalama (hepsi dolarsa): {p(sig['avg_full'])}\n"
    lev_loss = sig["sl_avg_pct"] * LEVERAGE
    msg += (
        f"🔴 Stop: {p(sig['sl'])} (ort.'ya göre %{sig['sl_avg_pct']:.2f} · {LEVERAGE}x ile ~%{lev_loss:.0f})\n"
        f"☠️ Tahmini likidasyon: {p(sig['liq_full'])}" + (" (hepsi dolarsa)" if n > 1 else "") + "\n"
        "━━ Kademeli kâr al ━━\n"
        f"🎯 Hedef 1 (%{TP1_CLOSE_PCT:.0f} kapat): {p(sig['tp1'])}"
        + (" → stop girişe çekilir\n" if MOVE_SL_TO_BE else "\n")
        + f"🎯 Hedef 2 (kalan): {p(sig['tp'])}\n"
    )
    if n > 1:
        msg += f"ℹ️ Hedefler kademe doldukça güncellenir (hepsi dolarsa {p(sig['tp1_full'])} / {p(sig['tp2_full'])})\n"
    rr = abs(sig["tp2_full"] - sig["avg_full"]) / abs(sig["avg_full"] - sig["sl"])
    msg += f"⚖️ Risk/Reward (hepsi dolarsa, Hedef 2): {rr:.1f}R\n"
    if margins:
        total_m = sum(margins)
        loss = total_m * LEVERAGE * worst_case_loss(sig)
        msg += (f"💵 Bütçe: {_usd(total_m)} (bakiye {_usd(sig['balance'])}) · "
                f"maks. zarar: -{_usd(loss)} (bakiyenin %{loss / sig['balance'] * 100:.1f} kadarı)\n")
    msg += f"⚙️ Örnek Kaldıraç: {LEVERAGE}x\n📅 {now_tr} TSİ"
    return msg


def in_cooldown(symbol):
    t = _last_signal.get(symbol)
    return t is not None and datetime.now(timezone.utc) - t < timedelta(minutes=SIGNAL_COOLDOWN_MIN)


# ============================================================================
# ISLEM TAKIBI (TP / STOP BILDIRIMLERI)
# ============================================================================
TRADES = {"open": [], "closed": [], "stats": {"tp": 0, "sl": 0, "expired": 0, "pnl_pct": 0.0},
          "started_ms": int(time.time() * 1000)}
_trade_lock = threading.Lock()
GIST_FILE = "trades.json"


def _gist_enabled():
    return bool(GITHUB_TOKEN and GIST_ID)


def _gist_headers():
    return {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}


def _apply_loaded(data, source):
    TRADES["open"] = data.get("open", [])
    TRADES["closed"] = data.get("closed", [])
    TRADES["stats"].update(data.get("stats", {}))
    TRADES["started_ms"] = data.get("started_ms", TRADES["started_ms"])
    log.info("%s: %d acik, %d kapanmis islem yuklendi", source, len(TRADES["open"]), len(TRADES["closed"]))


def load_trades():
    if _gist_enabled():
        try:
            r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=_gist_headers(), timeout=20)
            r.raise_for_status()
            f = r.json().get("files", {}).get(GIST_FILE)
            if f:
                content = f.get("content", "")
                if f.get("truncated") and f.get("raw_url"):
                    content = requests.get(f["raw_url"], headers=_gist_headers(), timeout=30).text
                if content.strip():
                    _apply_loaded(json.loads(content), "Gist")
                    return
        except Exception as e:
            log.error("Gist okunamadi, yerel dosya denenecek: %s", e)
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                _apply_loaded(json.load(f), "Yerel dosya")
    except Exception as e:
        log.error("Islem dosyasi okunamadi (%s): %s", STATE_FILE, e)


def _push_gist(payload):
    try:
        r = requests.patch(f"https://api.github.com/gists/{GIST_ID}", headers=_gist_headers(),
                           json={"files": {GIST_FILE: {"content": payload}}}, timeout=30)
        if r.status_code >= 300:
            log.error("Gist yazilamadi %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.error("Gist yazilamadi: %s", e)


def save_trades(remote=False):
    """Yerel dosyaya her zaman, Gist'e sadece onemli olaylarda (acilis/kademe/kapanis) yazar."""
    TRADES["closed"] = TRADES["closed"][-MAX_CLOSED_HISTORY:]
    payload = json.dumps(TRADES, ensure_ascii=False)
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.error("Islem dosyasi yazilamadi: %s", e)
    if remote and _gist_enabled():
        threading.Thread(target=_push_gist, args=(payload,), daemon=True).start()


def dashboard_data():
    """Dashboard'un /api/data ucu: acik ve kapanmis islemler + bot durumu."""
    with _trade_lock:
        open_rows = []
        for t in TRADES["open"]:
            st_ = _ensure_state(t)
            avg, w = st_avg(st_)
            tp1, tp2 = st_targets(st_)
            last = t.get("last_price")
            sign = st_["sign"]
            open_rows.append({
                "coin": t["coin"], "direction": t["direction"], "avg_entry": avg, "entry1": st_["entries"][0],
                "sl": st_["sl"], "tp": tp2, "tp1": tp1, "tp1_done": st_["tp1_done"],
                "filled": sum(st_["filled"]), "levels": len(st_["entries"]), "filled_weight": w,
                "last_price": last, "opened_ms": t["opened_ms"], "margin": t.get("margin"),
                "leverage": t.get("leverage", LEVERAGE), "strategy": t.get("strategy", "reversal"),
                "pnl_pct": (sign * (last - avg) / avg * 100) if last else None,
            })
        return {
            "now_ms": int(time.time() * 1000),
            "started_ms": TRADES.get("started_ms"),
            "bot": {
                "last_scan_at": STATE["last_scan_at"], "last_scan_ok": STATE["last_scan_ok"],
                "timeframe": TF_LABEL, "leverage": LEVERAGE, "margin_mode": MARGIN_MODE,
                "rr": f"TP {TP1_ATR:g}/{TP2_ATR:g} ATR", "levels": len(LADDER_WEIGHTS), "supertrend": USE_SUPERTREND,
                "weights": [round(x * 100) for x in LADDER_WEIGHTS], "level_mode": LEVEL_MODE,
                "strategy": "multi" if len(ACTIVE_STRATEGIES) > 1 else next(iter(ACTIVE_STRATEGIES)), "risk_pct": RISK_PCT,
                "strategies": {sid: strategy_label(sid) for sid in ACTIVE_STRATEGIES},
                "strategy_names": {sid: strategy_label(sid) for sid in STRAT_DEFS},
                "strategy_label": " + ".join(strategy_label(sid) for sid in ACTIVE_STRATEGIES) + f" · risk %{RISK_PCT:g}/işlem",
                "persistent": _gist_enabled(), "dry_run": DRY_RUN,
                "account_balance": ACCOUNT_BALANCE, "balance": current_balance(),
                "margin_pct": SLOT_FRACTION * LADDER_ALLOC * 100, "max_open": MAX_OPEN_TRADES,
            },
            "open": open_rows,
            "closed": list(TRADES["closed"]),
        }


def has_open_trade(symbol):
    return any(t["symbol"] == symbol for t in TRADES["open"])


def register_trade(ex, symbol, sig, message_id):
    opened = sig["candle_ts"] + timedelta(minutes=TF_MINUTES)   # sinyal mumunun kapanis ani = giris ani
    base = ex.markets.get(symbol, {}).get("base", symbol.split("/")[0]) if ex.markets else symbol
    trade = {
        "symbol": symbol, "coin": f"{base}/USDT", "direction": sig["direction"],
        "entry": sig["entry"], "sl": sig["sl"], "tp": sig["tp"], "liq_full": sig.get("liq_full"),
        "state": (new_state(sig, tp1_atr=sig["tp1_atr"], tp2_atr=sig["tp2_atr"], tp1_frac=0.5, move_be=True,
                            be_atr=sig["be_atr"])
                  if sig.get("family", "scalein") != "scalein" else new_state(sig)),
        "strategy": sig.get("strategy", "scalein_15m"), "family": sig.get("family", "scalein"),
        "tf": sig.get("tf", 15), "leverage": sig.get("leverage", LEVERAGE),
        "max_hours": sig.get("max_hours", MAX_TRADE_HOURS),
        "opened_ms": int(opened.timestamp() * 1000),
        "last_checked_ms": int(opened.timestamp() * 1000),
        "message_id": message_id,
        "margin": sum(sig["margins"]) if sig.get("margins") else None,   # toplam butce teminati ($)
    }
    with _trade_lock:
        TRADES["open"].append(trade)
        save_trades(remote=True)


def _ensure_state(t):
    """Onceki surumlerden kalan acik islemleri yeni motora cevirir (tek hedef, tam kapanis)."""
    if "state" in t:
        return t["state"]
    entries = t.get("entries") or [t["entry"]]
    weights = t.get("weights") or [1.0 / len(entries)] * len(entries)
    sign = 1 if t["direction"] == "LONG" else -1
    s = new_state({"direction": t["direction"], "sign": sign, "entries": entries, "weights": weights,
                   "sl": t["sl"], "atr": 0.0}, tp1_frac=1.0, move_be=False)
    s["filled"] = t.get("filled") or [True] + [False] * (len(entries) - 1)
    s["tp_fixed"] = t["tp"]
    t["state"] = s
    return s


def _fmt_duration(ms):
    mins = int(ms / 60000)
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    parts = ([f"{d}g"] if d else []) + ([f"{h}s"] if h else []) + [f"{m}dk"]
    return " ".join(parts)


def _signed(v, nd):
    return f"{'+' if v >= 0 else '-'}%{abs(v):.{nd}f}"


def _money_signed(v):
    return f"{'+' if v >= 0 else '-'}{_usd(abs(v))}"


def trade_usd(t, net):
    if not t.get("margin"):
        return None
    return max(t["margin"] * t.get("leverage", LEVERAGE) * net, -t["margin"])


def _fill_message(ex, t, idx):
    s = t["state"]
    avg, ws = st_avg(s)
    tp1, tp2 = st_targets(s)
    nf, n = sum(s["filled"]), len(s["entries"])
    p = lambda v: fmt_price(ex, t["symbol"], v)
    amt = f" ({_usd(t['margin'] * s['weights'][idx])})" if t.get("margin") else ""
    return (
        f"➕ {idx + 1}. KADEME ALINDI{amt}\n"
        f"📌 Coin: {t['coin']} | {t['direction']}\n"
        f"🟢 Giriş {idx + 1}: {p(s['entries'][idx])}\n"
        f"📍 Yeni ortalama: {p(avg)} ({nf}/{n} kademe · bütçenin %{ws * 100:.0f} kadarı)\n"
        f"🔴 Stop: {p(s['sl'])} (ort.'ya göre %{abs(avg - s['sl']) / avg * 100:.2f})\n"
        f"☠️ Tahmini likidasyon: {p(liq_price(avg, s['sign']))}\n"
        f"🎯 Yeni hedefler: {p(tp1)} (%{s['tp1_frac'] * 100:.0f} kapat) / {p(tp2)}\n"
        f"✏️ TP emirlerini yeni hedeflere taşı\n"
        f"📅 {datetime.now(DISPLAY_TZ).strftime('%d.%m.%Y %H:%M')} TSİ"
    )


def _be_message(ex, t, sl):
    s = t["state"]
    avg, _ = st_avg(s)
    tp1, tp2 = st_targets(s)
    n, nf = len(s["entries"]), sum(s["filled"])
    p = lambda v: fmt_price(ex, t["symbol"], v)
    msg = (
        f"🛡 STOP GİRİŞE ÇEKİLDİ · işlem artık risksiz\n"
        f"📌 Coin: {t['coin']} | {t['direction']}\n"
        f"📍 Ortalama giriş: {p(avg)} · fiyat {s['be_atr']:g} ATR lehe gitti\n"
        f"🔴 Yeni stop: {p(sl)} (komisyonu karşılayacak kadar kârlı tarafta)\n"
    )
    if nf < n:
        msg += f"🚫 Dolmayan {n - nf} kademe emrini iptal et\n"
    msg += f"🎯 Hedefler aynı: {p(tp1)} / {p(tp2)}\n📅 {datetime.now(DISPLAY_TZ).strftime('%d.%m.%Y %H:%M')} TSİ"
    return msg


def _tp1_message(ex, t, px):
    s = t["state"]
    avg, _ = st_avg(s)
    _, tp2 = st_targets(s)
    net, _, _ = st_result(s)
    usd = trade_usd(t, net)
    p = lambda v: fmt_price(ex, t["symbol"], v)
    n, nf = len(s["entries"]), sum(s["filled"])
    msg = (
        f"🎯 HEDEF 1 ALINDI · pozisyonun %{s['tp1_frac'] * 100:.0f} kadarı kapandı\n"
        f"📌 Coin: {t['coin']} | {t['direction']}\n"
        f"📍 Ortalama giriş: {p(avg)} · Hedef 1: {p(px)}\n"
    )
    if usd is not None:
        msg += f"💵 Realize kâr: {_money_signed(usd)}\n"
    if s["move_be"]:
        msg += f"🛡 Stop girişe çekildi: {p(s['sl'])} (kalan pozisyon artık risksiz)\n"
    if nf < n:
        msg += f"🚫 Dolmayan {n - nf} kademe emrini iptal et\n"
    msg += f"🎯 Kalan için Hedef 2: {p(tp2)}\n📅 {datetime.now(DISPLAY_TZ).strftime('%d.%m.%Y %H:%M')} TSİ"
    return msg


def _close_message(ex, t, now_ms):
    s = t["state"]
    avg, ws = st_avg(s)
    net, ws, nf = st_result(s)
    n = len(s["entries"])
    pnl = net / ws * 100                          # dolan pozisyona gore % (fiyat bazli)
    L = t.get("leverage", LEVERAGE)
    lev = pnl * L
    usd = trade_usd(t, net)
    st = TRADES["stats"]
    total = st["tp"] + st["sl"]
    winrate = (st["tp"] / total * 100) if total else 0
    p = lambda v: fmt_price(ex, t["symbol"], v)
    head = {
        "TP": "✅ HEDEF 2 GERÇEKLEŞTİ · işlem tamamlandı",
        "TP1_BE": "✅ HEDEF 1 + GİRİŞTE KAPANDI (kârda)",
        "SL": "❌ STOP OLDU",
        "BE": "⚪ GİRİŞTE KAPANDI (başa baş, zarar yok)",
        "EXPIRED": "⌛ SÜRE DOLDU (piyasadan kapatıldı)",
    }.get(s["outcome"], s["outcome"])
    msg = (
        f"{head}\n"
        f"📌 Coin: {t['coin']}\n"
        f"📊 Yön: {t['direction']}\n"
        + (f"🟢 Giriş: {p(avg)}\n" if n == 1 else f"🟢 Ortalama giriş: {p(avg)} ({nf}/{n} kademe)\n")
        + f"🏁 Son çıkış: {p(s['exit'])}\n"
        f"{'💰' if net >= 0 else '📉'} Sonuç: {_signed(pnl, 2)} ({L}x ile ~{_signed(lev, 1)})\n"
    )
    if usd is not None:
        msg += f"💵 {_money_signed(usd)} · Bakiye: {_usd(current_balance())}\n"
    if s["outcome"] in ("SL", "EXPIRED", "BE") and nf < n and not s["cancelled"]:
        msg += f"🚫 Dolmayan {n - nf} kademe emrini iptal et\n"
    msg += (
        f"⏱️ Süre: {_fmt_duration(now_ms - t['opened_ms'])}\n"
        f"📈 Toplam: {st['tp']} kârlı / {st['sl']} stop (Başarı %{winrate:.0f})\n"
        f"📅 {datetime.now(DISPLAY_TZ).strftime('%d.%m.%Y %H:%M')} TSİ"
    )
    return msg


def check_trade(ex, t):
    """1 dakikalik mumlari motora verir. Olay listesi doner (kapanis dahil)."""
    s = _ensure_state(t)
    raw = with_retry(ex.fetch_ohlcv, t["symbol"], "1m", since=t["last_checked_ms"], limit=500,
                     desc=f"takip {t['symbol']}")
    events = []
    if raw:
        for ts, o, h, l, c, v in raw:
            if ts + 60_000 <= t["opened_ms"]:
                continue                       # giristen onceki mum
            if ts + 60_000 > int(time.time() * 1000):
                break                          # henuz kapanmamis mum: sonraki kontrolde
            for kind, val in process_bar(s, h, l):
                # mesaji olay anindaki duruma gore hazirla (ayni kontrolde islem kapansa bile dogru kalsin)
                text = None
                if kind == "FILL" and NOTIFY_ENTRY_FILLS:
                    text = _fill_message(ex, t, val)
                elif kind == "TP1" and not s["closed"]:
                    text = _tp1_message(ex, t, val)
                elif kind == "BE_SET" and not s["tp1_done"]:
                    text = _be_message(ex, t, val)
                events.append((kind, val, text))
            t["last_checked_ms"] = int(ts) + 60_000
            if s["closed"]:
                break
        t["last_price"] = float(raw[-1][4])
    now_ms = int(time.time() * 1000)
    max_h = t.get("max_hours", MAX_TRADE_HOURS)
    if not s["closed"] and max_h > 0 and now_ms - t["opened_ms"] > max_h * 3600_000 \
            and t.get("last_price"):
        close_market(s, t["last_price"])
        events.append(("EXPIRED", t["last_price"], None))
    return events


def monitor_trades(ex):
    if not TRADES["open"]:
        return
    now_ms = int(time.time() * 1000)
    changed = remote = False
    for t in list(TRADES["open"]):
        try:
            events = check_trade(ex, t)
            changed = True
            s = t["state"]
            for kind, val, text in events:
                if kind == "FILL":
                    remote = True
                    log.info("KADEME DOLDU %s %d/%d", t["symbol"], val + 1, len(s["entries"]))
                elif kind in ("TP1", "BE_SET"):
                    remote = True
                    log.info("%s %s", "HEDEF 1" if kind == "TP1" else "BASA BAS STOP", t["symbol"])
                if text:
                    send_telegram(text, reply_to=t.get("message_id"))
            if not s["closed"]:
                continue
            net, ws, nf = st_result(s)
            usd = trade_usd(t, net)
            avg, _ = st_avg(s)
            with _trade_lock:
                stt = TRADES["stats"]
                key = {"TP": "tp", "TP1_BE": "tp", "SL": "sl", "BE": "be", "EXPIRED": "expired"}.get(s["outcome"], "expired")
                stt[key] = stt.get(key, 0) + 1
                stt["pnl_pct"] = round(stt.get("pnl_pct", 0.0) + net / ws * 100, 4)
                TRADES["open"].remove(t)
                TRADES["closed"].append({
                    "symbol": t["symbol"], "coin": t["coin"], "direction": t["direction"],
                    "outcome": s["outcome"], "avg_entry": avg, "exit": s["exit"], "sl": s["sl_orig"],
                    "tp": t.get("tp"), "entry1": s["entries"][0], "filled": nf, "levels": len(s["entries"]),
                    "filled_weight": ws, "tp1_hit": s["tp1_done"],
                    "pnl_pct": round(net / ws * 100, 4), "pos_ret_pct": round(net * 100, 4), "net": True,
                    "leverage": t.get("leverage", LEVERAGE), "strategy": t.get("strategy", "scalein_15m"),
                    "family": t.get("family", "scalein"),
                    "opened_ms": t["opened_ms"], "closed_ms": now_ms,
                    "margin": t.get("margin"), "usd": round(usd, 4) if usd is not None else None,
                })
            remote = True
            log.info("ISLEM KAPANDI %s %s -> %s (%.2f%%)", t["symbol"], t["direction"], s["outcome"], net / ws * 100)
            send_telegram(_close_message(ex, t, now_ms), reply_to=t.get("message_id"))
        except Exception as e:
            log.exception("Islem takibinde hata (%s): %s", t.get("symbol"), e)
    if changed:
        with _trade_lock:
            save_trades(remote=remote)


# ============================================================================
# TARAMA DONGUSU
# ============================================================================
def due_timeframes(t=None):
    """Bu tarama aninda kapanmis olan zaman dilimleri (UTC hizali)."""
    t = t or datetime.now(timezone.utc)
    m = t.hour * 60 + t.minute
    m -= m % 15                                   # 15dk sinirina yuvarla
    return sorted({sp["tf"] for sp in ACTIVE_STRATEGIES.values() if m % sp["tf"] == 0})


def scan(ex, due=None):
    """due: taranacak zaman dilimleri (dk). None = aktif stratejilerin hepsi."""
    t0 = time.time()
    due = set(due) if due is not None else {sp["tf"] for sp in ACTIVE_STRATEGIES.values()}
    strategies = [sp for sp in ACTIVE_STRATEGIES.values() if sp["tf"] in due]
    if not strategies:
        return
    free = (MAX_OPEN_TRADES - len(TRADES["open"])) if (TRACK_TRADES and MAX_OPEN_TRADES > 0) else 10**6
    if free <= 0:
        log.info("Tarama atlandi: %d/%d islem slotu dolu", len(TRADES["open"]), MAX_OPEN_TRADES)
        return
    symbols = get_top_symbols(ex)
    if not symbols:
        raise RuntimeError("Sembol listesi alinamadi (borsa erisimi?)")
    btc = None
    if any(sp["family"] != "scalein" for sp in strategies):
        btc = btc_regime(ex)
        if btc is None:
            raise RuntimeError("BTC 4s rejimi alinamadi")
    log.info("Tarama: %s | %d sembol | %s bos slot%s", ", ".join(sp["id"] for sp in strategies), len(symbols),
             free if free < 10**6 else "sinirsiz",
             f" | BTC rejim: {'YUKARI' if btc[0] else 'ASAGI' if btc[1] else 'NOTR'}" if btc else "")

    # 1) Tum coinleri ve bu anda kapanan zaman dilimlerinin stratejilerini tara
    candidates = []
    for sym in symbols:
        if in_cooldown(sym) or (TRACK_TRADES and ONE_TRADE_PER_SYMBOL and has_open_trade(sym)):
            continue
        cache = {}

        def frame(tf):
            if tf not in cache:
                raw = fetch_closed_ohlcv(ex, sym, tf)
                cache[tf] = add_indicators(raw) if raw is not None else None
            return cache[tf]

        for sp in strategies:
            try:
                dfi = frame(sp["tf"])
                if dfi is None:
                    continue
                h1 = None
                if sp["family"] == "reversal_f":
                    d1 = frame(60)
                    if d1 is None:
                        continue
                    h1 = (float(d1["close"].iloc[-1]), float(d1["close"].ewm(span=50, adjust=False).mean().iloc[-1]))
                sig = evaluate_strategy(sp["id"], dfi, btc, h1, spec=sp)
                if sig:
                    sig.setdefault("strategy", sp["id"])
                    sig.setdefault("tf", sp["tf"])
                    sig.setdefault("tf_label", sp["tf_label"])
                    sig.setdefault("strategy_name", sp["name"])
                    candidates.append((sym, sig, dfi))
                    log.info("ADAY %s %s %s | ADX=%.1f", sp["id"], sym, sig["direction"], sig["adx"])
                    break                         # bu coin icin tek aday yeter
            except Exception as e:                # tek coindeki hata tum taramayi durdurmasin
                log.exception("%s / %s islenirken hata: %s", sym, sp["id"], e)

    # 2) Bos slot kadarini gonder: once buyuk zaman dilimi, sonra trend gucu (ADX)
    candidates.sort(key=lambda c: (c[1].get("tf", 0), c[1]["adx"]), reverse=True)
    found = 0
    for sym, sig, dfi in candidates:
        if found >= free:
            log.info("Slot dolu, atlanan sinyal: %s %s %s", sig["strategy"], sym, sig["direction"])
            continue
        try:
            size_position(sig)
            if sum(sig["margins"]) <= 1:
                log.info("Teminat yetersiz, sinyal atlandi: %s", sym)
                continue
            msg = build_message(ex, sym, sig) if sig.get("family") == "scalein" else build_message_single(ex, sym, sig)
            log.info("SINYAL %s %s %s | ADX=%.1f teminat=$%.0f pozisyon=$%.0f", sig["strategy"], sym, sig["direction"],
                     sig["adx"], sum(sig["margins"]), sig.get("notional", 0))
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
        except Exception as e:
            log.exception("%s gonderilirken hata: %s", sym, e)

    log.info("Tarama bitti: %d aday, %d sinyal, %.1f sn | acik islem: %d/%d", len(candidates), found,
             time.time() - t0, len(TRADES["open"]), MAX_OPEN_TRADES)


def scan_step_minutes():
    return min(sp["tf"] for sp in ACTIVE_STRATEGIES.values())


def next_scan_time():
    step = scan_step_minutes()
    now = datetime.now(timezone.utc)
    minutes = now.hour * 60 + now.minute
    next_boundary = (minutes // step + 1) * step
    return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=next_boundary, seconds=SCAN_DELAY_SEC)


def main():
    for sid, sp in ACTIVE_STRATEGIES.items():
        log.info("Strateji aktif: %s (%s) params=%s", sid, sp["tf_label"], sp["params"])
    log.info("Risk %%%g/islem | en fazla %d islem | BTC rejim EMA%d (4s)", RISK_PCT, MAX_OPEN_TRADES, BTC_REGIME_EMA)
    log.info("Bot basliyor | TF=%s TOP_N=%d ADX>%.0f Lev=%dx Kademe=%s (%s) TP=%g/%g ATR Slot=%d ST=%s DRY_RUN=%s",
             TIMEFRAME, TOP_N_SYMBOLS, ADX_THRESHOLD, LEVERAGE,
             "/".join(f"{w * 100:.0f}" for w in LADDER_WEIGHTS), LEVEL_MODE, TP1_ATR, TP2_ATR,
             MAX_OPEN_TRADES, USE_SUPERTREND, DRY_RUN)
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

    if _env("RUN_RESEARCH", False, bool):
        try:
            import research
            threading.Thread(target=research.run_and_report, args=(sys.modules[__name__],), daemon=True).start()
            log.info("Strateji arastirmasi arka planda baslatildi")
        except Exception as e:
            log.error("Arastirma baslatilamadi (research.py repoda mi?): %s", e)

    if SEND_STARTUP_MESSAGE:
        extra =f", {len(TRADES['open'])} açık işlem takipte" if TRACK_TRADES and TRADES["open"] else ""
        strat = " + ".join(strategy_label(sid) for sid in ACTIVE_STRATEGIES)
        send_telegram(f"🤖 Sinyal botu başlatıldı · {strat} · MEXC Futures{extra}.")

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
                if STATE["last_scan_at"] is None:     # acilis: sadece son 20 dk icinde kapanan zaman dilimleri
                    nm = datetime.now(timezone.utc)
                    mm = nm.hour * 60 + nm.minute
                    due = [sp["tf"] for sp in ACTIVE_STRATEGIES.values() if mm % sp["tf"] <= 20]
                else:
                    due = due_timeframes(next_scan - timedelta(seconds=SCAN_DELAY_SEC))
                scan(ex, due)
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



# ============================================================================
# DASHBOARD SAYFASI  (/dashboard)
# ============================================================================
DASHBOARD_HTML = r'''<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sinyal Botu · Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root{
    --bg:#f6f5f1; --surface:#ffffff; --ink:#1d1d1b; --ink2:#5b5a55; --muted:#8c8a83; --line:#e7e4dc;
    --accent:#2f5bd3; --good:#1f8a4c; --good-bg:#e6f4ec; --bad:#c43c3c; --bad-bg:#fbeaea; --warn:#9a6a00; --warn-bg:#fbf1dc;
    --chip:#efede7;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#141413; --surface:#1e1e1c; --ink:#ecebe6; --ink2:#b3b1aa; --muted:#85837c; --line:#2f2e2b;
      --accent:#7a9bff; --good:#4cc283; --good-bg:#183526; --bad:#ff7a7a; --bad-bg:#3a1d1d; --warn:#e6b450; --warn-bg:#3a2e14;
      --chip:#2a2a27;
    }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif}
  .wrap{max-width:1180px;margin:0 auto;padding:20px 16px 48px}
  header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin-bottom:16px}
  h1{font-size:20px;margin:0;font-weight:650;letter-spacing:-.01em}
  .sub{color:var(--ink2);font-size:13px;margin-top:2px}
  .pill{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;font-size:12px;background:var(--chip);color:var(--ink2)}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
  .dot.ok{background:var(--good)} .dot.err{background:var(--bad)}
  .controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin-bottom:16px}
  .controls label{font-size:12px;color:var(--ink2);display:flex;align-items:center;gap:6px}
  .controls input[type=number]{width:90px;padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font:inherit}
  .seg{display:inline-flex;background:var(--chip);border-radius:9px;padding:2px}
  .seg button{border:0;background:transparent;color:var(--ink2);padding:5px 10px;border-radius:7px;font:inherit;font-size:12px;cursor:pointer}
  .seg button.on{background:var(--surface);color:var(--ink);box-shadow:0 1px 2px rgba(0,0,0,.08)}
  .spacer{flex:1}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:10px;margin-bottom:16px}
  .tile{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
  .tile .lab{font-size:12px;color:var(--ink2)}
  .tile .val{font-size:24px;font-weight:650;margin-top:4px;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
  .tile .note{font-size:12px;color:var(--muted);margin-top:2px}
  .grid2{display:grid;grid-template-columns:2fr 1fr;gap:12px;margin-bottom:16px}
  @media (max-width:860px){.grid2{grid-template-columns:1fr}}
  .card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px}
  .card h2{font-size:14px;margin:0 0 10px;font-weight:620}
  .card h2 small{font-weight:400;color:var(--muted);margin-left:6px}
  .chartbox{position:relative;height:260px}
  table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
  th,td{text-align:left;padding:8px 8px;border-bottom:1px solid var(--line);white-space:nowrap;font-size:13px}
  th{font-size:12px;color:var(--ink2);font-weight:550}
  td.r,th.r{text-align:right}
  .tablewrap{overflow-x:auto}
  .badge{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:550}
  .b-tp{background:var(--good-bg);color:var(--good)} .b-sl{background:var(--bad-bg);color:var(--bad)} .b-exp{background:var(--warn-bg);color:var(--warn)}
  .b-long{background:var(--good-bg);color:var(--good)} .b-short{background:var(--bad-bg);color:var(--bad)}
  .pos{color:var(--good)} .neg{color:var(--bad)}
  .empty{color:var(--muted);text-align:center;padding:24px 8px}
  .foot{color:var(--muted);font-size:12px;margin-top:18px;line-height:1.6}
  .warnbar{background:var(--warn-bg);color:var(--warn);border-radius:10px;padding:8px 12px;font-size:13px;margin-bottom:14px;display:none}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>MEXC Sinyal Botu</h1>
      <div class="sub" id="cfg">—</div>
    </div>
    <span class="pill"><span class="dot" id="statusDot"></span><span id="statusTxt">Bağlanıyor…</span></span>
  </header>

  <div class="warnbar" id="persistWarn">⚠️ Kalıcı kayıt (GitHub Gist) ayarlı değil: Render'da yeni deploy yapıldığında işlem geçmişi sıfırlanır.</div>

  <div class="controls">
    <div class="seg" id="period">
      <button data-d="1">Bugün</button><button data-d="7">7 gün</button><button data-d="30">30 gün</button><button data-d="0" class="on">Tümü</button>
    </div>
    <span class="spacer"></span>
    <label>Bakiye $ <input type="number" id="bal" value="2000" min="1" step="100"></label>
    <label>Teminat/işlem % <input type="number" id="mpct" value="10" min="0.5" max="100" step="0.5"></label>
    <label><input type="checkbox" id="fees" checked> Komisyon (~%0,06)</label>
  </div>

  <div class="kpis" id="kpis"></div>

  <div class="grid2">
    <div class="card">
      <h2>Sermaye eğrisi <small id="eqNote"></small></h2>
      <div class="chartbox"><canvas id="eqChart" aria-label="Sermaye eğrisi"></canvas></div>
    </div>
    <div class="card">
      <h2>Coin bazında net K/Z <small>($)</small></h2>
      <div class="chartbox"><canvas id="coinChart" aria-label="Coin bazında kâr zarar"></canvas></div>
    </div>
  </div>

  <div class="card" id="stratCard" style="margin-bottom:16px;display:none">
    <h2>Strateji bazında sonuçlar</h2>
    <div class="tablewrap"><table id="stratTbl"></table></div>
  </div>

  <div class="card" style="margin-bottom:16px">
    <h2>Açık işlemler <small id="openCount"></small></h2>
    <div class="tablewrap"><table id="openTbl"></table></div>
  </div>

  <div class="card">
    <h2 style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">Kapanan işlemler <small id="closedCount"></small>
      <span class="spacer"></span>
      <span class="seg" id="ofilter"><button data-o="ALL" class="on">Tümü</button><button data-o="TP">Kârlı</button><button data-o="SL">Stop</button><button data-o="EXPIRED">Süre doldu</button></span>
    </h2>
    <div class="tablewrap"><table id="closedTbl"></table></div>
  </div>

  <div class="foot" id="foot">
    $ hesapları: her işleme <b>bakiye × teminat %</b> kadar sabit teminat ve botun kaldıracı varsayılır; kademeli girişlerde dolmayan kademeler hesaba katılmaz.
    Funding ve kayma dahil değildir. Sayfa her 60 saniyede yenilenir. Yatırım tavsiyesi değildir.
  </div>
</div>

<script>
(() => {
  const TZ = "Europe/Istanbul";
  const KEY = new URLSearchParams(location.search).get("key") || "";
  const FEE_RT = 0.0006;
  const S = { data: null, days: 0, outcome: "ALL", showAll: false };
  let eqChart = null, coinChart = null;

  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const fmtTime = ms => new Intl.DateTimeFormat("tr-TR",{timeZone:TZ,day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}).format(new Date(ms));
  const money = (v, sign=true) => (sign ? (v>=0?"+":"−") : (v<0?"−":"")) + "$" + Math.abs(v).toLocaleString("tr-TR",{maximumFractionDigits:2, minimumFractionDigits: Math.abs(v)<100?2:0});
  const pct = (v, d=2) => (v>=0?"+":"−") + "%" + Math.abs(v).toLocaleString("tr-TR",{minimumFractionDigits:d,maximumFractionDigits:d});
  const price = v => v==null ? "—" : (v>=100 ? v.toLocaleString("tr-TR",{maximumFractionDigits:2}) : v>=1 ? v.toLocaleString("tr-TR",{maximumFractionDigits:4}) : v.toPrecision(5));
  const cls = v => v>0 ? "pos" : v<0 ? "neg" : "";
  const dur = ms => { const m=Math.floor(ms/60000), d=Math.floor(m/1440), h=Math.floor(m%1440/60), mm=m%60; return (d?d+"g ":"")+(h?h+"s ":"")+mm+"dk"; };
  const outBadge = o => o==="TP" ? '<span class="badge b-tp">✓ Hedef 2</span>' : o==="TP1_BE" ? '<span class="badge b-tp">✓ Hedef 1 + giriş</span>' : o==="SL" ? '<span class="badge b-sl">✕ Stop</span>' : o==="BE" ? '<span class="badge b-exp">⚪ Başa baş</span>' : '<span class="badge b-exp">⌛ Süre doldu</span>';
  const isWin = o => o==="TP" || o==="TP1_BE";
  const dirBadge = d => d==="LONG" ? '<span class="badge b-long">▲ LONG</span>' : '<span class="badge b-short">▼ SHORT</span>';

  function settings(){
    const bal = Math.max(1, +$("bal").value || 2000);
    const m = Math.min(100, Math.max(0.1, +$("mpct").value || 10));
    return { bal, margin: bal*m/100, fees: $("fees").checked };
  }

  function usd(t, st){
    // risk bazli islemler: botun gercek $ sonucunu girilen bakiyeye oranla
    if (t.usd != null && t.strategy && t.family !== "scalein" && t.strategy !== "reversal") return t.usd * st.bal / (S.data?.bot.account_balance || st.bal);
    const lev = t.leverage || (S.data?.bot.leverage ?? 15);
    const notional = st.margin * lev;
    let v = notional * (t.pos_ret_pct ?? t.pnl_pct*(t.filled_weight??1)) / 100;
    if (st.fees && !t.net) v -= notional * (t.filled_weight ?? 1) * FEE_RT;   // yeni kayitlarda komisyon zaten dusulu
    return Math.max(v, -st.margin);
  }

  function filtered(){
    const all = S.data?.closed ?? [];
    if (!S.days) return all;
    const from = S.days===1
      ? new Date(new Intl.DateTimeFormat("en-CA",{timeZone:TZ}).format(new Date())+"T00:00:00+03:00").getTime()
      : Date.now() - S.days*86400000;
    return all.filter(t => t.closed_ms >= from);
  }

  function render(){
    if (!S.data) return;
    const d = S.data, st = settings();
    const b = d.bot;
    $("cfg").textContent = b.strategy !== "scalein_15m"
      ? `${b.strategy_label} · en fazla ${b.max_open} işlem${b.dry_run?" · DRY_RUN":""}`
      : `${b.timeframe} · ${b.leverage}x ${b.margin_mode} · ${b.levels} kademe ${(b.weights||[]).join("/")} · ${b.rr} · en fazla ${b.max_open} işlem · SuperTrend ${b.supertrend?"açık":"kapalı"}${b.dry_run?" · DRY_RUN":""}`;
    $("persistWarn").style.display = b.persistent ? "none" : "block";
    const ok = b.last_scan_ok !== false;
    $("statusDot").className = "dot " + (ok ? "ok" : "err");
    $("statusTxt").textContent = b.last_scan_at ? `${ok?"Çalışıyor":"Hata"} · son tarama ${fmtTime(Date.parse(b.last_scan_at))}` : "İlk tarama bekleniyor";

    const closed = filtered().slice().sort((a,b)=>a.closed_ms-b.closed_ms);
    const rows = closed.map(t => ({...t, usd: usd(t, st)}));
    const tp = rows.filter(t=>isWin(t.outcome)).length, sl = rows.filter(t=>t.outcome==="SL").length, ex = rows.filter(t=>t.outcome==="EXPIRED").length, be = rows.filter(t=>t.outcome==="BE").length;
    const net = rows.reduce((s,t)=>s+t.usd,0);
    const gw = rows.filter(t=>t.usd>0).reduce((s,t)=>s+t.usd,0), gl = -rows.filter(t=>t.usd<=0).reduce((s,t)=>s+t.usd,0);
    const pf = gl>0 ? gw/gl : (gw>0 ? Infinity : 0);
    let eq = st.bal, peak = st.bal, mdd = 0; const curve = [];
    if (rows.length) curve.push({x: rows[0].opened_ms, y: st.bal});
    for (const t of rows){ eq += t.usd; peak = Math.max(peak, eq); mdd = Math.max(mdd, (peak-eq)/peak*100); curve.push({x:t.closed_ms, y:eq, t}); }
    const avgDur = rows.length ? rows.reduce((s,t)=>s+(t.closed_ms-t.opened_ms),0)/rows.length : 0;
    const wr = (tp+sl) ? tp/(tp+sl)*100 : 0;

    const tiles = [
      ["Net kâr / zarar", `<span class="${cls(net)}">${money(net)}</span>`, `Bakiye ${money(st.bal+net,false)} · ${pct(net/st.bal*100,1)}`],
      ["Kazanma oranı", rows.length ? `%${wr.toFixed(0)}` : "—", `${tp} kârlı · ${sl} stop · ${be} başa baş · ${ex} süre doldu`],
      ["İşlem sayısı", `${rows.length}`, `${d.open.length} açık işlem`],
      ["Profit factor", rows.length ? (pf===Infinity?"∞":pf.toFixed(2)) : "—", "Toplam kâr ÷ toplam zarar"],
      ["Maks. düşüş", rows.length ? `%${mdd.toFixed(1)}` : "—", "Zirveden en büyük geri çekilme"],
      ["Ort. işlem süresi", rows.length ? dur(avgDur) : "—", `İşlem başı teminat ${money(st.margin,false)}`],
    ];
    $("kpis").innerHTML = tiles.map(([l,v,n])=>`<div class="tile"><div class="lab">${l}</div><div class="val">${v}</div><div class="note">${n}</div></div>`).join("");

    drawEquity(curve, st);
    drawCoins(rows);
    renderOpen(d.open, st);
    renderStrategies(rows);
    renderClosed(rows);
  }

  function drawEquity(curve, st){
    $("eqNote").textContent = curve.length ? `(başlangıç ${money(st.bal,false)})` : "";
    if (!window.Chart) return;
    const line = css("--accent"), grid = css("--line"), ink2 = css("--ink2");
    const cfg = {
      type:"line",
      data:{datasets:[{data:curve, borderColor:line, backgroundColor:line, borderWidth:2, pointRadius:0, pointHoverRadius:5, stepped:"after", fill:false}]},
      options:{
        responsive:true, maintainAspectRatio:false, animation:false,
        interaction:{mode:"index", intersect:false},
        parsing:false,
        scales:{
          x:{type:"linear", grid:{display:false}, ticks:{color:ink2, maxTicksLimit:6, callback:v=>fmtTime(v)}},
          y:{grid:{color:grid}, ticks:{color:ink2, callback:v=>"$"+Number(v).toLocaleString("tr-TR")}}
        },
        plugins:{legend:{display:false}, tooltip:{callbacks:{
          title:i=>fmtTime(i[0].raw.x),
          label:i=>{const t=i.raw.t; return t ? [`Bakiye: ${money(i.raw.y,false)}`, `${t.coin} ${t.direction} · ${t.outcome==="TP"?"TP":t.outcome==="SL"?"Stop":"Süre doldu"} · ${money(t.usd)}`] : `Başlangıç: ${money(i.raw.y,false)}`;}
        }}}
      }
    };
    if (eqChart) eqChart.destroy();
    eqChart = new Chart($("eqChart"), cfg);
  }

  function drawCoins(rows){
    if (!window.Chart) return;
    const by = {};
    rows.forEach(t => { (by[t.coin] ??= {v:0,n:0}); by[t.coin].v += t.usd; by[t.coin].n++; });
    let items = Object.entries(by).sort((a,b)=>b[1].v-a[1].v);
    if (items.length > 12) items = [...items.slice(0,6), ...items.slice(-6)];
    const good = css("--good"), bad = css("--bad"), grid = css("--line"), ink2 = css("--ink2");
    const cfg = {
      type:"bar",
      data:{labels:items.map(([c])=>c.replace("/USDT","")), datasets:[{data:items.map(([,o])=>o.v), backgroundColor:items.map(([,o])=>o.v>=0?good:bad), borderRadius:4, barPercentage:.7}]},
      options:{
        indexAxis:"y", responsive:true, maintainAspectRatio:false, animation:false,
        scales:{x:{grid:{color:grid}, ticks:{color:ink2, callback:v=>"$"+v}}, y:{grid:{display:false}, ticks:{color:ink2}}},
        plugins:{legend:{display:false}, tooltip:{callbacks:{label:i=>`${money(i.raw)} · ${items[i.dataIndex][1].n} işlem`}}}
      }
    };
    if (coinChart) coinChart.destroy();
    coinChart = new Chart($("coinChart"), cfg);
  }

  function renderStrategies(rows){
    const names = S.data.bot.strategy_names || {};
    const by = {};
    rows.forEach(t => { const k = t.strategy || "scalein_15m"; (by[k] ??= []).push(t); });
    const keys = Object.keys(by);
    const card = $("stratCard");
    if (!keys.length){ card.style.display = "none"; return; }
    card.style.display = "block";
    $("stratTbl").innerHTML = `<thead><tr><th>Strateji</th><th class="r">İşlem</th><th class="r">Kazanma</th><th class="r">Profit factor</th><th class="r">Net $</th></tr></thead><tbody>` +
      keys.map(k => {
        const r = by[k], w = r.filter(t=>t.usd>0).reduce((a,t)=>a+t.usd,0), l = -r.filter(t=>t.usd<=0).reduce((a,t)=>a+t.usd,0);
        const tp = r.filter(t=>isWin(t.outcome)).length, sl = r.filter(t=>t.outcome==="SL").length, net = w - l;
        const pf = l>0 ? (w/l).toFixed(2) : (w>0 ? "∞" : "—");
        return `<tr><td><b>${esc(names[k] || k)}</b></td><td class="r">${r.length}</td><td class="r">${(tp+sl)?"%"+(tp/(tp+sl)*100).toFixed(0):"—"}</td><td class="r">${pf}</td><td class="r ${cls(net)}"><b>${money(net)}</b></td></tr>`;
      }).join("") + "</tbody>";
  }

  function renderOpen(open, st){
    $("openCount").textContent = `(${open.length}${S.data.bot.max_open ? " / " + S.data.bot.max_open + " slot" : ""})`;
    if (!open.length){ $("openTbl").innerHTML = '<tr><td class="empty">Şu an açık işlem yok</td></tr>'; return; }
    const lev = S.data.bot.leverage;
    $("openTbl").innerHTML = `<thead><tr><th>Açılış</th><th>Strateji</th><th>Coin</th><th>Yön</th><th class="r">Ort. giriş</th><th class="r">Son fiyat</th><th class="r">Stop</th><th class="r">Hedef 1</th><th class="r">Hedef 2</th><th>Kademe</th><th class="r">Anlık K/Z</th><th class="r">Kaldıraçlı</th><th class="r">Süre</th></tr></thead><tbody>` +
      open.slice().sort((a,b)=>b.opened_ms-a.opened_ms).map(t => {
        const p = t.pnl_pct;
        return `<tr><td>${fmtTime(t.opened_ms)}</td><td>${esc((S.data.bot.strategy_names||{})[t.strategy] || "—")}</td><td><b>${esc(t.coin)}</b></td><td>${dirBadge(t.direction)}</td>
          <td class="r">${price(t.avg_entry)}</td><td class="r">${price(t.last_price)}</td><td class="r">${price(t.sl)}</td><td class="r">${t.tp1_done ? "✓ alındı" : price(t.tp1)}</td><td class="r">${price(t.tp)}</td>
          <td>${t.filled}/${t.levels}</td><td class="r ${cls(p)}">${p==null?"—":pct(p)}</td><td class="r ${cls(p)}">${p==null?"—":pct(p*(t.leverage||lev),1)}</td>
          <td class="r">${dur(S.data.now_ms - t.opened_ms)}</td></tr>`;
      }).join("") + "</tbody>";
  }

  function renderClosed(rows){
    const list = rows.filter(t => S.outcome==="ALL" || t.outcome===S.outcome || (S.outcome==="TP" && t.outcome==="TP1_BE")).sort((a,b)=>b.closed_ms-a.closed_ms);
    $("closedCount").textContent = `(${list.length})`;
    if (!list.length){ $("closedTbl").innerHTML = '<tr><td class="empty">Bu filtrede kapanan işlem yok</td></tr>'; return; }
    const lev = S.data.bot.leverage;
    $("closedTbl").innerHTML = `<thead><tr><th>Kapanış</th><th>Coin</th><th>Yön</th><th>Sonuç</th><th class="r">Ort. giriş</th><th class="r">Çıkış</th><th>Kademe</th><th class="r">K/Z</th><th class="r">${lev}x</th><th class="r">$</th><th class="r">Süre</th></tr></thead><tbody>` +
      list.slice(0, S.showAll ? 1000 : 50).map(t => `<tr><td>${fmtTime(t.closed_ms)}</td><td><b>${esc(t.coin)}</b></td><td>${dirBadge(t.direction)}</td><td>${outBadge(t.outcome)}</td>
        <td class="r">${price(t.avg_entry)}</td><td class="r">${price(t.exit)}</td><td>${t.filled}/${t.levels}</td>
        <td class="r ${cls(t.pnl_pct)}">${pct(t.pnl_pct)}</td><td class="r ${cls(t.pnl_pct)}">${pct(t.pnl_pct*(t.leverage||lev),1)}</td>
        <td class="r ${cls(t.usd)}"><b>${money(t.usd)}</b></td><td class="r">${dur(t.closed_ms-t.opened_ms)}</td></tr>`).join("") + "</tbody>" +
      (list.length > 50 ? `<tfoot><tr><td colspan="11" style="text-align:center;border:0;padding-top:12px"><button id="moreBtn" style="border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:8px;padding:6px 14px;font:inherit;cursor:pointer">${S.showAll ? "Daha az göster" : "Tümünü göster (" + list.length + ")"}</button></td></tr></tfoot>` : "");
    const mb = $("moreBtn"); if (mb) mb.onclick = () => { S.showAll = !S.showAll; render(); };
  }

  async function load(){
    try{
      const r = await fetch("/api/data" + (KEY ? "?key="+encodeURIComponent(KEY) : ""), {cache:"no-store"});
      if (!r.ok) throw new Error(r.status);
      S.data = await r.json();
      if (!S.inited){
        S.inited = true;
        let saved = null; try{ saved = localStorage.getItem("dash_settings"); }catch(e){}
        const b = S.data.bot;
        if (!saved && b.account_balance){ $("bal").value = b.account_balance; $("mpct").value = (+b.margin_pct).toFixed(1); }
      }
      render();
    }catch(e){
      $("statusDot").className = "dot err";
      $("statusTxt").textContent = "Bota ulaşılamadı (" + e.message + ")";
    }
  }

  // ayarlari tarayicida hatirla
  try{
    const saved = JSON.parse(localStorage.getItem("dash_settings") || "{}");
    if (saved.bal) $("bal").value = saved.bal;
    if (saved.mpct) $("mpct").value = saved.mpct;
    if (saved.fees === false) $("fees").checked = false;
  }catch(e){}
  const persist = () => { try{ localStorage.setItem("dash_settings", JSON.stringify({bal:$("bal").value, mpct:$("mpct").value, fees:$("fees").checked})); }catch(e){} };
  ["bal","mpct","fees"].forEach(id => $(id).addEventListener("input", () => { persist(); render(); }));

  document.querySelectorAll("#period button").forEach(b => b.onclick = () => {
    document.querySelectorAll("#period button").forEach(x=>x.classList.remove("on")); b.classList.add("on"); S.days = +b.dataset.d; render();
  });
  document.querySelectorAll("#ofilter button").forEach(b => b.onclick = () => {
    document.querySelectorAll("#ofilter button").forEach(x=>x.classList.remove("on")); b.classList.add("on"); S.outcome = b.dataset.o; render();
  });

  load();
  setInterval(load, 60000);
})();
</script>
</body>
</html>
'''


if __name__ == "__main__":
    main()
