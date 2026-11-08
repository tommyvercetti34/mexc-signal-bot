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

import numpy as np
import pandas as pd
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

# --- Risk ---
LEVERAGE = _env("LEVERAGE", 15, int)
MARGIN_MODE = _env("MARGIN_MODE", "Isolated")
ATR_SL_MULT = _env("ATR_SL_MULT", 1.5, float)             # SL mesafesi = ATR * bu katsayi
RR_RATIO = _env("RR_RATIO", 1.5, float)                   # TP = risk * RR (min 1.5 onerilir)
MIN_SL_PCT = _env("MIN_SL_PCT", 0.25, float)              # cok dar stoplari (gurultu) ele
# 15x isolated'da tasfiye ~%6.6 civarinda; SL bunun cok altinda olmali:
MAX_SL_PCT = _env("MAX_SL_PCT", 4.0, float)

# --- Tekrar sinyal engeli ---
SIGNAL_COOLDOWN_MIN = _env("SIGNAL_COOLDOWN_MIN", 60, int)  # ayni coin icin en az bu kadar dk bekle

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
def send_telegram(text: str) -> bool:
    if DRY_RUN:
        log.info("[DRY_RUN] Telegram mesaji:\n%s", text)
        return True
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID tanimli degil! Mesaj:\n%s", text)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 200:
                return True
            if r.status_code == 429:  # flood limit
                wait = r.json().get("parameters", {}).get("retry_after", 5)
                log.warning("Telegram rate limit, %s sn bekleniyor", wait)
                time.sleep(wait + 1)
                continue
            log.error("Telegram hata %s: %s", r.status_code, r.text[:300])
            if 400 <= r.status_code < 500:
                return False  # token/chat_id hatali -> tekrar denemenin anlami yok
        except requests.RequestException as e:
            log.warning("Telegram baglanti hatasi (%d/%d): %s", attempt, MAX_RETRIES, e)
        time.sleep(2 * attempt)
    return False


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


def get_top_symbols(ex):
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
    return [s for s, _ in ranked[:TOP_N_SYMBOLS]]


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


def add_indicators(df):
    df = df.copy()
    df["atr"] = calc_atr(df, ATR_PERIOD)
    df["adx"] = calc_adx(df, ADX_PERIOD)
    df["k"], df["d"] = calc_stoch_rsi(df["close"], RSI_PERIOD, STOCH_PERIOD, STOCH_K_SMOOTH, STOCH_D_SMOOTH)
    df["vwap"] = calc_vwap(df, VWAP_ANCHOR_TZ)
    return df


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

    return {
        "direction": direction, "entry": entry, "sl": sl, "tp": tp,
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
    rr_txt = f"{RR_RATIO:g}R"
    return (
        "🎯 YENİ KRİPTO SİNYALİ\n"
        f"📌 Coin: {coin}\n"
        f"📊 Yön: {sig['direction']}\n"
        f"⚙️ İşlem Türü: {MARGIN_MODE}\n"
        f"🕒 Zaman Dilimi: {TF_LABEL}\n"
        f"🟢 Giriş: {fmt_price(ex, symbol, sig['entry'])}\n"
        f"🔴 Stop: {fmt_price(ex, symbol, sig['sl'])} (%{sig['sl_pct']:.2f} zarar)\n"
        f"🎯 Hedef: {fmt_price(ex, symbol, sig['tp'])}\n"
        f"⚖️ Risk/Reward: {rr_txt}\n"
        f"⚙️ Örnek Kaldıraç: {LEVERAGE}x\n"
        f"📅 {now_tr} TSİ"
    )


def in_cooldown(symbol):
    t = _last_signal.get(symbol)
    return t is not None and datetime.now(timezone.utc) - t < timedelta(minutes=SIGNAL_COOLDOWN_MIN)


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
            df = fetch_closed_ohlcv(ex, sym)
            if df is None:
                continue
            sig = evaluate(add_indicators(df))
            if not sig:
                continue
            msg = build_message(ex, sym, sig)
            log.info("SINYAL %s %s | ADX=%.1f K=%.1f D=%.1f", sym, sig["direction"], sig["adx"], sig["k"], sig["d"])
            if send_telegram(msg):
                _last_signal[sym] = datetime.now(timezone.utc)
                STATE["signals_sent"] += 1
                found += 1
        except Exception as e:  # tek coindeki hata tum taramayi durdurmasin
            log.exception("%s islenirken hata: %s", sym, e)

    log.info("Tarama bitti: %d sinyal, %.1f sn", found, time.time() - t0)


def seconds_until_next_candle():
    now = datetime.now(timezone.utc)
    minutes = now.hour * 60 + now.minute
    next_boundary = (minutes // TF_MINUTES + 1) * TF_MINUTES
    target = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=next_boundary, seconds=SCAN_DELAY_SEC)
    return max(1.0, (target - now).total_seconds())


def main():
    log.info("Bot basliyor | TF=%s TOP_N=%d ADX>%.0f ATRx%.2f RR=%.2f Lev=%dx DRY_RUN=%s",
             TIMEFRAME, TOP_N_SYMBOLS, ADX_THRESHOLD, ATR_SL_MULT, RR_RATIO, LEVERAGE, DRY_RUN)
    start_health_server()

    ex = None
    consecutive_failures = 0
    markets_loaded_at = 0.0

    if SEND_STARTUP_MESSAGE:
        send_telegram(f"🤖 Sinyal botu başlatıldı ({TF_LABEL}, ilk {TOP_N_SYMBOLS} coin, MEXC Futures).")

    first = True
    while True:
        try:
            if not (first and RUN_SCAN_ON_START):
                wait = seconds_until_next_candle()
                log.info("Sonraki tarama %.0f sn sonra", wait)
                time.sleep(wait)
            first = False

            if ex is None:
                ex = create_exchange()
            # Yeni listelemeler icin piyasalari 6 saatte bir yenile
            if time.time() - markets_loaded_at > 6 * 3600:
                if with_retry(ex.load_markets, True, desc="load_markets") is None:
                    raise RuntimeError("load_markets basarisiz")
                markets_loaded_at = time.time()

            scan(ex)
            STATE["last_scan_at"] = datetime.now(timezone.utc).isoformat()
            STATE["last_scan_ok"] = True
            consecutive_failures = 0

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
            time.sleep(min(60 * consecutive_failures, 300))


if __name__ == "__main__":
    main()
