"""
Strateji Arastirmasi - walk-forward (egitim / dogrulama)
=======================================================
Ayni veride 4 strateji ailesini yaristirir. Her ailenin ayari ILK donemde (egitim) secilir,
SON donemde (dogrulama, strateji bu veriyi hic gormedi) sinanir. Sadece dogrulamayi da gecen
strateji canliya alinmaya adaydir.

Aileler:
  1) Trend kirilimi (1 saat): N mumluk tepe/dip kirilimi + EMA200 trend + BTC 4s rejimi
  2) Trend ici geri cekilme (1 saat): EMA50>EMA200 trendinde EMA20'ye geri cekilme + RSI donusu + BTC rejimi
  3) Donus sinyali + filtre (15 dk): mevcut Stoch RSI tetigi + BTC 4s rejimi + 1 saatlik trend
  4) Mevcut sistem (15 dk): referans
  5) Trend kirilimi (4 saat)   6) Trend ici geri cekilme (4 saat)
  7) SuperTrend donusu (1 saat) 8) SuperTrend donusu (4 saat)  9) Donus sinyali + BTC filtre (1 saat)

Pozisyon buyuklugu: her islemde stop olursa bakiyenin RS_RISK_PCT'si (%1) kaybedilir;
kaldirac en fazla LEVERAGE (15x), ayni anda en fazla MAX_OPEN_TRADES islem.
Komisyon: limit %0, market %0.02 (BT_MAKER_FEE / BT_TAKER_FEE). Funding ve kayma dahil degil.

Calistirma: Render'da RUN_RESEARCH=true  ya da bilgisayarda  python research.py
"""

import gc
import io
import os
import time
import logging
import itertools
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

log = logging.getLogger("research")


def _env(name, default, cast=str):
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return cast(v)
    except ValueError:
        return default


RS_DAYS = _env("RS_DAYS", 90, int)
RS_TOP_N = _env("RS_TOP_N", 30, int)
RS_BALANCE = _env("RS_BALANCE", 2000.0, float)
RS_RISK_PCT = _env("RS_RISK_PCT", 1.0, float)          # islem basina risk (bakiyenin %'si)
RS_MAX_OPEN = _env("RS_MAX_OPEN", 0, int)              # 0 = botun MAX_OPEN_TRADES
RS_TRAIN_FRAC = _env("RS_TRAIN_FRAC", 0.67, float)     # egitim donemi orani
RS_MIN_TRADES = _env("RS_MIN_TRADES", 25, int)         # egitimde en az islem (az islemle secim yapma)
RS_MIN_TEST_TRADES = _env("RS_MIN_TEST_TRADES", 30, int)  # dogrulamada en az islem (t-testi icin alt sinir)
RS_MIN_T = _env("RS_MIN_T", 2.5, float)                # dogrulamada islem basi getirinin t-istatistigi (sans elemesi;
                                                       # 9 strateji birlikte test edildigi icin coklu test duzeltmeli)
RS_EXCHANGE = _env("RS_EXCHANGE", "mexc").lower()       # mexc | binance (veri ve coin listesi bu borsadan)
RS_MODE = _env("RS_MODE", "standard").lower()          # standard | long4h (sadece 4 saatlik stratejiler, uzun gecmis)
RS_LONG_DAYS = _env("RS_LONG_DAYS", 730, int)           # long4h modunda kac gunluk 4s veri
RS_SYMBOLS = _env("RS_SYMBOLS", "BTC,ETH,SOL,XRP,DOGE,ADA,LINK,AVAX,LTC,BCH,DOT,TRX,NEAR,ATOM,UNI,ETC,FIL,AAVE,XLM,BNB")
# long4h modunda sabit coin listesi (bugunku hacme gore secim yok -> hayatta kalma yanliligi azalir)
RS_MAKER = _env("BT_MAKER_FEE", 0.0, float)
RS_TAKER = _env("BT_TAKER_FEE", 0.0002, float)
WARMUP = 220                                           # EMA200 isinmasi


# ============================================================================
# VERI
# ============================================================================
def fetch_15m(bot, ex, symbol, days, tf_min=15, min_bars=2000):
    tf_ms = tf_min * 60_000
    code = {1: "1m", 3: "3m", 5: "5m", 15: "15m", 60: "1h", 240: "4h"}[tf_min]
    since = int((datetime.now(timezone.utc) - timedelta(days=days + 4)).timestamp() * 1000)
    now_ms = int(time.time() * 1000)
    rows = []
    while since < now_ms - tf_ms:
        batch = bot.with_retry(ex.fetch_ohlcv, symbol, code, since=since, limit=1000, desc=f"rs {symbol} {code}")
        if not batch:
            if rows:
                break
            since += 1000 * tf_ms                 # borsa bu kadar eskisini vermiyor: ilk mevcut veriye kadar ilerle
            continue
        rows.extend(batch)
        nxt = batch[-1][0] + tf_ms
        if nxt <= since or len(batch) < 2:
            break
        since = nxt
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df = df[df["ts"] + pd.Timedelta(minutes=tf_min) <= pd.Timestamp.now(tz="UTC")]
    return df.reset_index(drop=True) if len(df) > min_bars else None


def resample(df, minutes):
    """15m -> daha buyuk zaman dilimi; sadece TAMAMLANMIS mumlar."""
    rule = f"{minutes}min"
    g = df.set_index("ts").resample(rule, label="left", closed="left")
    out = g.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    cnt = g["close"].count()
    out = out[cnt == minutes // 15].dropna().reset_index()
    return out


def prep(bot, df, tf_min):
    d = bot.add_indicators(df)
    c = d["close"]
    d["ema20"] = c.ewm(span=20, adjust=False).mean()
    d["ema50"] = c.ewm(span=50, adjust=False).mean()
    d["ema200"] = c.ewm(span=200, adjust=False).mean()
    d["rsi"] = bot.calc_rsi(c, 14)
    d["close_time"] = d["ts"] + pd.Timedelta(minutes=tf_min)
    return d


def align(d, other, cols, prefix):
    """other'in (daha buyuk TF) her d mumunun KAPANISINDA bilinen son kapanmis degerlerini ekler."""
    right = other[["close_time"] + cols].rename(columns={c: prefix + c for c in cols})
    right = right.rename(columns={"close_time": "_ct"}).sort_values("_ct")
    out = pd.merge_asof(d.sort_values("close_time"), right, left_on="close_time", right_on="_ct",
                        direction="backward")
    return out.drop(columns=["_ct"]).reset_index(drop=True)


# ============================================================================
# SINYAL AILELERI  ->  [(i, yon, skor)]
# ============================================================================
def _dir_list(long_, short, score, start=WARMUP):
    long_ = np.nan_to_num(long_, nan=0).astype(bool)
    short = np.nan_to_num(short, nan=0).astype(bool) & ~long_
    idx = np.where(long_ | short)[0]
    return [(int(i), "LONG" if long_[i] else "SHORT", float(score[i])) for i in idx if i >= start]


_SIG_CACHE = {}


def _shared(family):
    """Canli botla AYNI sinyal fonksiyonu (bot.strategy_signal_arrays)."""
    def fn(bot, d, p):
        if family in bot.STRUCT_FAMILIES:         # sinyal hedeften bagimsiz: tp_r degisince yeniden hesaplama
            key = (family, id(d), len(d), float(d["close"].iloc[0]), float(d["close"].iloc[-1]), tuple(sorted((k, str(v)) for k, v in p.items() if k not in ("tp_r", "be"))))
            if key not in _SIG_CACHE:
                long_, short, score = bot.strategy_signal_arrays(family, d, p, d["btc_up"].to_numpy(),
                                                                 d["btc_dn"].to_numpy())
                _SIG_CACHE[key] = (long_, short, score, d["struct_sl"].to_numpy().copy())
            long_, short, score, stop = _SIG_CACHE[key]
            d["struct_sl"] = stop
            return _dir_list(long_, short, score)
        h1c = d["h1_close"].to_numpy() if "h1_close" in d else None
        h1e = d["h1_ema50"].to_numpy() if "h1_ema50" in d else None
        long_, short, score = bot.strategy_signal_arrays(family, d, p, d["btc_up"].to_numpy(), d["btc_dn"].to_numpy(),
                                                         h1c, h1e)
        return _dir_list(long_, short, score)
    return fn


sig_breakout = _shared("breakout")
sig_pullback = _shared("pullback")
sig_reversal_filtered = _shared("reversal_f")
sig_reversal_btc = _shared("reversal_btc")
sig_supertrend = _shared("supertrend")
sig_sweep = _shared("sweep")
SIG_FN = {f: _shared(f) for f in ("ha_pb", "harmony", "impulse", "diverg", "ribbon", "tdseq", "cprofile")}              # HTF Sweep + CISD (yapisal stop d["struct_sl"] sutununa yazilir)


def sig_reversal_raw(bot, d, p):
    long_, short, score = bot.strategy_signal_arrays("reversal_raw", d, p, np.ones(len(d)), np.ones(len(d)))
    return _dir_list(long_, short, score)


FAMILIES = [
    {"sid": "breakout_1h", "key": "breakout", "name": "Trend kırılımı (1 saat)", "tf": 60, "fn": sig_breakout, "max_hours": 72,
     "grid": {"lookback": [20, 40], "sl": [1.5, 2.5], "tp": [(1.5, 3.0), (2.0, 5.0)], "be": [0.0, 1.0], "st": [0, 1]}},
    {"sid": "pullback_1h", "key": "pullback", "name": "Trend içi geri çekilme (1 saat)", "tf": 60, "fn": sig_pullback, "max_hours": 72,
     "grid": {"rsi": [40, 50], "sl": [1.5, 2.5], "tp": [(1.5, 3.0), (2.0, 4.0)], "be": [0.0, 1.0], "st": [0, 1]}},
    {"sid": "reversal_15m", "key": "rev_f", "name": "Dönüş sinyali + BTC/1s filtre (15 dk)", "tf": 15, "fn": sig_reversal_filtered,
     "max_hours": 48, "grid": {"sl": [1.5, 2.5], "tp": [(1.0, 2.0), (1.5, 3.0), (2.0, 4.0)], "be": [0.0, 1.0]}},
    {"sid": None, "key": "rev", "name": "Mevcut sistem (15 dk, referans)", "tf": 15, "fn": sig_reversal_raw, "max_hours": 48,
     "grid": {"sl": [1.5], "tp": [(1.5, 3.0)], "be": [0.0]}},
    # --- buyuk zaman dilimleri: komisyon riske gore cok daha kucuk kalir ---
    {"sid": "breakout_4h", "key": "breakout4h", "name": "Trend kırılımı (4 saat)", "tf": 240, "fn": sig_breakout, "max_hours": 240,
     "grid": {"lookback": [20, 55], "sl": [2.0, 3.0], "tp": [(2.0, 6.0), (3.0, 9.0)], "be": [0.0, 1.5], "st": [0, 1]}},
    {"sid": "pullback_4h", "key": "pullback4h", "name": "Trend içi geri çekilme (4 saat)", "tf": 240, "fn": sig_pullback, "max_hours": 240,
     "grid": {"rsi": [40, 50], "sl": [2.0, 3.0], "tp": [(2.0, 4.0), (3.0, 6.0)], "be": [0.0, 1.5], "st": [0, 1]}},
    {"sid": "supertrend_1h", "key": "st1h", "name": "SuperTrend dönüşü (1 saat)", "tf": 60, "fn": sig_supertrend,
     "max_hours": 96, "grid": {"trend": [0, 1], "sl": [2.0, 3.0], "tp": [(2.0, 4.0), (3.0, 6.0)], "be": [0.0, 1.0]}},
    {"sid": "supertrend_4h", "key": "st4h", "name": "SuperTrend dönüşü (4 saat)", "tf": 240, "fn": sig_supertrend,
     "max_hours": 240, "grid": {"trend": [0, 1], "sl": [2.0, 3.0], "tp": [(2.0, 4.0), (3.0, 6.0)], "be": [0.0, 1.0]}},
    {"sid": "reversal_1h", "key": "rev1h", "name": "Dönüş sinyali + BTC filtre (1 saat)", "tf": 60, "fn": sig_reversal_btc, "max_hours": 96,
     "grid": {"sl": [1.5, 2.5], "tp": [(1.5, 3.0), (2.0, 4.0)], "be": [0.0, 1.0]}},
    # --- HTF Sweep + CISD (TradingView fikri): yapisal stop, R bazli hedefler ---
    {"sid": "sweep_15m", "key": "sweep15", "name": "HTF Sweep + CISD (15 dk / 1 saat)", "tf": 15, "fn": sig_sweep, "max_hours": 24,
     "grid": {"htf": [60], "tp_r": [(1.0, 2.0), (1.0, 3.0), (1.5, 3.0)], "kz": [0, 1], "be": [0.0]}},
    {"sid": "sweep_1h", "key": "sweep1h", "name": "HTF Sweep + CISD (1 saat / 4 saat)", "tf": 60, "fn": sig_sweep, "max_hours": 72,
     "grid": {"htf": [240], "tp_r": [(1.0, 2.0), (1.0, 3.0), (1.5, 3.0)], "kz": [0, 1], "be": [0.0]}},
    # --- TOPLU TEST: TradingView fikirleri (yapisal stop + R hedefleri), 15 dk ve 1 saat ---
    {"sid": "ha_pb_15m", "key": "ha_pb15m", "name": "Heikin Ashi geri çekilme (15 dk)", "tf": 15, "fn": SIG_FN["ha_pb"],
     "max_hours": 24, "grid": {"ema": [50], "tp_r": [(1.5, 1.5), (1.0, 2.0)], "be": [0.0]}},
    {"sid": "ha_pb_1h", "key": "ha_pb1h", "name": "Heikin Ashi geri çekilme (1 saat)", "tf": 60, "fn": SIG_FN["ha_pb"],
     "max_hours": 72, "grid": {"ema": [50], "tp_r": [(1.5, 1.5), (1.0, 2.0)], "be": [0.0]}},
    {"sid": "harmony_15m", "key": "harmony15m", "name": "Reversal Pressure (Harmony) (15 dk)", "tf": 15, "fn": SIG_FN["harmony"],
     "max_hours": 24, "grid": {"ext": [1.4, 2.0], "rec": [1.2], "tp_r": [(1.0, 2.0), (1.5, 3.0)], "be": [0.0]}},
    {"sid": "harmony_1h", "key": "harmony1h", "name": "Reversal Pressure (Harmony) (1 saat)", "tf": 60, "fn": SIG_FN["harmony"],
     "max_hours": 72, "grid": {"ext": [1.4, 2.0], "rec": [1.2], "tp_r": [(1.0, 2.0), (1.5, 3.0)], "be": [0.0]}},
    {"sid": "impulse_15m", "key": "impulse15m", "name": "Momentum itki devamı (Pineify) (15 dk)", "tf": 15, "fn": SIG_FN["impulse"],
     "max_hours": 24, "grid": {"tp_r": [(1.0, 2.0), (1.5, 3.0)], "be": [0.0]}},
    {"sid": "impulse_1h", "key": "impulse1h", "name": "Momentum itki devamı (Pineify) (1 saat)", "tf": 60, "fn": SIG_FN["impulse"],
     "max_hours": 72, "grid": {"tp_r": [(1.0, 2.0), (1.5, 3.0)], "be": [0.0]}},
    {"sid": "diverg_15m", "key": "diverg15m", "name": "CCI/RSI uyumsuzluğu (15 dk)", "tf": 15, "fn": SIG_FN["diverg"],
     "max_hours": 24, "grid": {"piv": [5], "mode": ["cci", "both"], "tp_r": [(1.0, 2.0), (1.5, 3.0)], "be": [0.0]}},
    {"sid": "diverg_1h", "key": "diverg1h", "name": "CCI/RSI uyumsuzluğu (1 saat)", "tf": 60, "fn": SIG_FN["diverg"],
     "max_hours": 72, "grid": {"piv": [5], "mode": ["cci", "both"], "tp_r": [(1.0, 2.0), (1.5, 3.0)], "be": [0.0]}},
    {"sid": "ribbon_15m", "key": "ribbon15m", "name": "ALMA trend şeridi (BOSWaves) (15 dk)", "tf": 15, "fn": SIG_FN["ribbon"],
     "max_hours": 24, "grid": {"slope": [0.08], "tp_r": [(1.0, 2.0), (2.0, 4.0)], "be": [0.0]}},
    {"sid": "ribbon_1h", "key": "ribbon1h", "name": "ALMA trend şeridi (BOSWaves) (1 saat)", "tf": 60, "fn": SIG_FN["ribbon"],
     "max_hours": 72, "grid": {"slope": [0.08], "tp_r": [(1.0, 2.0), (2.0, 4.0)], "be": [0.0]}},
    {"sid": "tdseq_15m", "key": "tdseq15m", "name": "TD Sequential (DGT) (15 dk)", "tf": 15, "fn": SIG_FN["tdseq"],
     "max_hours": 24, "grid": {"mode": ["9q", "13"], "tp_r": [(1.0, 2.0), (1.5, 3.0)], "be": [0.0]}},
    {"sid": "tdseq_1h", "key": "tdseq1h", "name": "TD Sequential (DGT) (1 saat)", "tf": 60, "fn": SIG_FN["tdseq"],
     "max_hours": 72, "grid": {"mode": ["9q", "13"], "tp_r": [(1.0, 2.0), (1.5, 3.0)], "be": [0.0]}},
    {"sid": "cprofile_15m", "key": "cprofile15m", "name": "Mum profili (Zeiierman) (15 dk)", "tf": 15, "fn": SIG_FN["cprofile"],
     "max_hours": 24, "grid": {"bands": [7], "tp_r": [(1.0, 2.0), (1.5, 3.0)], "be": [0.0]}},
    {"sid": "cprofile_1h", "key": "cprofile1h", "name": "Mum profili (Zeiierman) (1 saat)", "tf": 60, "fn": SIG_FN["cprofile"],
     "max_hours": 72, "grid": {"bands": [7], "tp_r": [(1.0, 2.0), (1.5, 3.0)], "be": [0.0]}},
]


def grid_params(fam):
    keys = list(fam["grid"])
    for combo in itertools.product(*[fam["grid"][k] for k in keys]):
        yield dict(zip(keys, combo))


def fmt_params(fam, p):
    parts = []
    if "lookback" in p:
        parts.append(f"{p['lookback']} mum kırılım")
    if "rsi" in p:
        parts.append(f"RSI {p['rsi']}")
    if "trend" in p:
        parts.append("EMA200+BTC filtreli" if p["trend"] else "filtresiz")
    if "st" in p:
        parts.append("SuperTrend onaylı" if p["st"] else "SuperTrend'siz")
    if "mode" in p:
        parts.append({"cci": "sadece CCI", "both": "CCI+RSI", "9q": "9 sayımı (mükemmel)", "13": "13 geri sayımı"}.get(p["mode"], p["mode"]))
    if "ext" in p:
        parts.append(f"uzama {p['ext']:g} ölçek")
    if "tp_r" in p and "htf" not in p:
        parts.append("yapısal stop")
        parts.append(f"TP {p['tp_r'][0]:g}R/{p['tp_r'][1]:g}R")
    elif "tp_r" in p:
        parts.append("sadece Londra/NY seansı" if p.get("kz") else "tüm saatler")
        parts.append("stop sweep ucunda")
        parts.append(f"TP {p['tp_r'][0]:g}R/{p['tp_r'][1]:g}R")
    else:
        parts.append(f"stop {p['sl']:g} ATR")
        parts.append(f"TP {p['tp'][0]:g}/{p['tp'][1]:g} ATR")
    parts.append(f"başa baş {p['be']:g} ATR'de" if p.get("be") else "başa baş yok")
    return ", ".join(parts)


# ============================================================================
# SIMULASYON
# ============================================================================
FILL_STATS = {}      # limit giris: sid -> [sinyal, dolan]


def simulate(bot, symbol, d, sigs, p, max_bars):
    """Her sinyal: tek giris, stop = sl*ATR, Hedef1'de %50 kapat + stop girise, Hedef2'de kalan.
    Canli botun islem motoru (bot.process_bar) kullanilir."""
    H, L, C, A = (d[c].to_numpy() for c in ("high", "low", "close", "atr"))
    ct = d["close_time"].to_numpy()
    SW = d["struct_sl"].to_numpy() if "struct_sl" in d else None
    out = []
    for i, direction, score in sigs:
        atr, e = float(A[i]), float(C[i])
        if not (atr > 0 and e > 0):
            continue
        sign = 1 if direction == "LONG" else -1
        if "tp_r" in p:                           # sweep: yapisal stop + R hedefleri (canli botla ayni fonksiyon)
            lv = bot.sweep_levels(e, sign, atr, float(SW[i]), p)
            if lv is None:
                continue
            sl, tp1_atr, tp2_atr = lv
        else:
            sl = e - sign * p["sl"] * atr
            tp1_atr, tp2_atr = p["tp"][0], p["tp"][1]
        entry_fee, i0, fill_bar = RS_TAKER, i, None
        if p.get("entry") == "limit":             # LIMIT (maker) giris: sinyal kapanisindan off*ATR geride, N mum gecerli
            st_ = FILL_STATS.setdefault(p.get("_sid", "?"), [0, 0])
            st_[0] += 1
            lim = e - sign * float(p.get("lim_atr", 0.0)) * atr
            tp1_lvl = e + sign * tp1_atr * atr
            for jj in range(i + 1, min(len(d), i + 1 + int(p.get("lim_bars", 2)))):
                if (sign == 1 and L[jj] < lim) or (sign == -1 and H[jj] > lim):   # fiyat limitin OTESINE gecti -> doldu
                    fill_bar = jj
                    break
                if (sign == 1 and H[jj] >= tp1_lvl) or (sign == -1 and L[jj] <= tp1_lvl):
                    break                         # dolmadan hedefe kosti -> islem kacti
            if fill_bar is None:
                continue
            st_[1] += 1
            tp2_lvl = e + sign * tp2_atr * atr    # stop ve hedefler sinyaldeki MUTLAK seviyelerde kalir
            tp1_atr, tp2_atr = sign * (tp1_lvl - lim) / atr, sign * (tp2_lvl - lim) / atr
            e, entry_fee, i0 = lim, RS_MAKER, fill_bar
        sl_dist = abs(e - sl) / e
        if sl_dist < 0.002 or sl_dist > 0.08 or tp1_atr <= 0:
            continue
        plan = {"direction": direction, "sign": sign, "entries": [e], "weights": [1.0], "sl": sl, "atr": atr}
        s = bot.new_state(plan, tp1_atr=tp1_atr, tp2_atr=tp2_atr, tp1_frac=0.5, move_be=True, taker=entry_fee,
                          be_atr=p.get("be", 0.0))
        if fill_bar is not None:                  # dolum mumu: sadece stop kontrolu (hedef sayilmaz, temkinli)
            bot.process_bar(s, (e if sign == 1 else H[fill_bar]), (L[fill_bar] if sign == 1 else e),
                            maker=RS_MAKER, taker=RS_TAKER)
        j = i0
        for j in range(i0 + 1, min(len(d), i0 + 1 + max_bars)):
            if s["closed"]:
                j -= 1
                break
            bot.process_bar(s, H[j], L[j], maker=RS_MAKER, taker=RS_TAKER)
            if s["closed"]:
                break
        if not s["closed"]:
            if j - i0 >= max_bars:
                bot.close_market(s, float(C[j]), taker=RS_TAKER)
            else:
                continue
        out.append({"symbol": symbol, "direction": direction, "score": score, "outcome": s["outcome"],
                    "entry_time": pd.Timestamp(ct[i0]), "exit_time": pd.Timestamp(ct[max(j, i0)]),
                    "ret": s["ret"], "fee": s["fee"], "sl_dist": sl_dist})
    return out


def portfolio(bot, trades, start, end):
    """Risk bazli boyutlama: notional = bakiye x risk% / stop mesafesi; kaldirac <= LEVERAGE."""
    max_open = RS_MAX_OPEN or bot.MAX_OPEN_TRADES or 10**6
    tr = sorted([t for t in trades if start <= t["entry_time"] < end], key=lambda t: (t["entry_time"], -t["score"]))
    equity, open_pos, done, levs = RS_BALANCE, [], [], []
    curve = [(start, equity)]

    def settle(until):
        nonlocal equity
        for q in sorted([q for q in open_pos if q["exit_time"] <= until], key=lambda q: q["exit_time"]):
            equity += q["pnl"]
            curve.append((q["exit_time"], equity))
            open_pos.remove(q)

    for t in tr:
        settle(t["entry_time"])
        if len(open_pos) >= max_open or any(q["symbol"] == t["symbol"] for q in open_pos):
            continue
        notional = equity * RS_RISK_PCT / 100 / t["sl_dist"]
        lev_i = bot.safe_leverage(t["sl_dist"])           # canli botla ayni: stop likidasyondan once gelsin
        margin = notional / lev_i
        used = sum(q["margin"] for q in open_pos)
        if used + margin > equity:                # teminat yetmezse kucult
            margin = max(0.0, equity - used)
            notional = margin * lev_i
        if notional <= 1:
            continue
        pnl = max(notional * (t["ret"] - t["fee"]), -margin)
        risk_usd = notional * t["sl_dist"]
        open_pos.append({"exit_time": t["exit_time"], "pnl": pnl, "margin": margin, "symbol": t["symbol"]})
        levs.append(notional / equity)
        done.append(dict(t, pnl=pnl, r=pnl / risk_usd if risk_usd > 0 else 0.0))
    settle(pd.Timestamp.max.tz_localize("UTC"))
    n = len(done)
    wins = [t["pnl"] for t in done if t["pnl"] > 0]
    losses = [-t["pnl"] for t in done if t["pnl"] <= 0]
    eq = np.array([e for _, e in curve])
    peak = np.maximum.accumulate(eq)
    rs_ = np.array([t["r"] for t in done])
    t_stat = float(rs_.mean() / (rs_.std(ddof=1) / np.sqrt(n))) if n > 2 and rs_.std(ddof=1) > 0 else 0.0
    return {
        "t": t_stat, "avg_r": float(rs_.mean()) if n else 0.0,
        "n": n, "final": equity, "ret": (equity / RS_BALANCE - 1) * 100,
        "winrate": len(wins) / n * 100 if n else 0.0,
        "pf": (sum(wins) / sum(losses)) if losses and sum(losses) > 0 else (float("inf") if wins else 0.0),
        "max_dd": float(((peak - eq) / peak).max() * 100) if len(eq) else 0.0,
        "avg_win": float(np.mean(wins)) if wins else 0.0, "avg_loss": float(np.mean(losses)) if losses else 0.0,
        "exposure": float(np.mean(levs)) if levs else 0.0, "curve": curve,
        "long": sum(t["pnl"] for t in done if t["direction"] == "LONG"),
        "short": sum(t["pnl"] for t in done if t["direction"] == "SHORT"),
    }


# ============================================================================
# CALISTIR
# ============================================================================
def load_data(bot, ex, progress=None):
    if not ex.markets:
        if bot.with_retry(ex.load_markets, desc="load_markets") is None:
            raise RuntimeError(f"{EXCHANGE_NAMES.get(RS_EXCHANGE, RS_EXCHANGE)} piyasa listesi alinamadi (bolge/erisim kisitlamasi olabilir)")
    symbols = bot.get_top_symbols(ex, top_n=RS_TOP_N)
    btc = bot.btc_perp_symbol(ex)
    if btc not in ex.markets:
        raise RuntimeError("BTC/USDT surekli vadeli kontrati bulunamadi (rejim filtresi icin gerekli)")
    raw = {}
    for idx, sym in enumerate(dict.fromkeys([btc] + symbols), 1):
        try:
            df = fetch_15m(bot, ex, sym, RS_DAYS)
            if df is not None:
                raw[sym] = df
        except Exception as e:
            log.warning("Arastirma veri hatasi %s: %s", sym, e)
        if progress and idx % 10 == 0:
            progress(f"{idx} coin indirildi")
    if btc not in raw:
        detail = ""
        try:                                      # asil borsa hatasini gor (bolge kisitlamasi vb.)
            since = int((datetime.now(timezone.utc) - timedelta(days=RS_DAYS + 4)).timestamp() * 1000)
            test = ex.fetch_ohlcv(btc, "15m", since=since, limit=1000)
            detail = f"borsa {len(test or [])} mum dondurdu (yetersiz gecmis)"
        except Exception as e:
            detail = f"{type(e).__name__}: {str(e)[:220]}"
        raise RuntimeError(f"BTC verisi alinamadi ({btc}) - {detail}")

    b4 = prep(bot, resample(raw[btc], 240), 240)
    b4["btc_up"] = (b4["close"] > b4["ema50"]).astype(int)
    b4["btc_dn"] = (b4["close"] < b4["ema50"]).astype(int)

    data = {}
    for sym in list(raw):
        df = raw.pop(sym)                         # ham veriyi isledikce bellekten birak
        if sym == btc and btc not in symbols:
            continue
        d15 = prep(bot, df, 15)
        d60 = prep(bot, resample(df, 60), 60)
        d15 = align(align(d15, b4, ["btc_up", "btc_dn"], ""), d60, ["close", "ema50"], "h1_")
        d60 = align(d60, b4, ["btc_up", "btc_dn"], "")
        d240 = align(prep(bot, resample(df, 240), 240), b4, ["btc_up", "btc_dn"], "")
        data[sym] = {15: _slim(d15), 60: _slim(d60), 240: _slim(d240)}
        del df, d15, d60, d240
        gc.collect()
    return data


KEEP_COLS = ["ts", "open", "high", "low", "close", "volume", "atr", "adx", "k", "d", "vwap", "st", "st_dir",
             "ema20", "ema50", "ema200", "rsi", "close_time", "btc_up", "btc_dn", "h1_close", "h1_ema50"]


def _slim(d):
    """Sadece stratejilerin kullandigi sutunlari tut (bellek: Render ucretsiz plan 512 MB)."""
    return d[[c for c in KEEP_COLS if c in d.columns]].copy()


EXCHANGE_NAMES = {"mexc": "MEXC", "binance": "Binance"}


def make_exchange(bot):
    """Arastirma verisinin cekilecegi borsa. binance = Binance USDT-M vadeli (binanceusdm)."""
    if RS_EXCHANGE == "binance":
        return bot.ccxt.binanceusdm({"enableRateLimit": True, "timeout": 20000})
    return bot.create_exchange()


def load_data_long4h(bot, ex, progress=None):
    """Sabit coin listesi icin dogrudan 4 saatlik mumlar (uzun gecmis)."""
    if not ex.markets:
        if bot.with_retry(ex.load_markets, desc="load_markets") is None:
            raise RuntimeError("piyasa listesi alinamadi")
    btc = bot.btc_perp_symbol(ex)
    wanted = [s.strip().upper() for s in RS_SYMBOLS.split(",") if s.strip()]
    syms = []
    for base in wanted:
        cand = f"{base}/USDT:USDT"
        if cand in ex.markets and base not in bot.EXCLUDE_SYMBOLS:
            syms.append(cand)
    if btc not in syms:
        syms.insert(0, btc)
    raw = {}
    for idx, sym in enumerate(syms, 1):
        try:
            df = fetch_15m(bot, ex, sym, RS_LONG_DAYS, tf_min=240, min_bars=WARMUP + 200)
            if df is not None:
                raw[sym] = df
        except Exception as e:
            log.warning("Arastirma veri hatasi %s: %s", sym, e)
        if progress and idx % 5 == 0:
            progress(f"{idx}/{len(syms)} coin")
    if btc not in raw:
        raise RuntimeError(f"BTC 4s verisi alinamadi ({btc})")
    b4 = prep(bot, raw[btc], 240)
    b4["btc_up"] = (b4["close"] > b4["ema50"]).astype(int)
    b4["btc_dn"] = (b4["close"] < b4["ema50"]).astype(int)
    data = {}
    for sym in list(raw):
        df = raw.pop(sym)
        d240 = align(prep(bot, df, 240), b4, ["btc_up", "btc_dn"], "")
        data[sym] = {240: _slim(d240)}
    skipped = [s.split("/")[0] for s in syms if s not in data]
    if skipped:
        log.info("Yeterli gecmisi olmayan coinler atlandi: %s", ", ".join(skipped))
    return data


def active_families():
    fams = [f for f in FAMILIES if f["tf"] == 240] if RS_MODE == "long4h" else FAMILIES
    want = [x.strip().lower() for x in os.getenv("RS_FAMILIES", "").split(",") if x.strip()]
    if want:                                      # orn. RS_FAMILIES=sweep_15m,sweep_1h -> sadece bunlar
        fams = [f for f in fams if (f.get("sid") or "") in want or f["key"] in want] or fams
    return fams


def run(bot, ex=None, progress=None):
    ex = ex or make_exchange(bot)
    data = load_data_long4h(bot, ex, progress) if RS_MODE == "long4h" else load_data(bot, ex, progress)
    base_tf = 240 if RS_MODE == "long4h" else 15
    t_all = [d[base_tf]["close_time"] for d in data.values()]
    t0 = min(t.iloc[WARMUP] for t in t_all)
    t1 = max(t.iloc[-1] for t in t_all)
    split = t0 + (t1 - t0) * RS_TRAIN_FRAC
    end = t1 + pd.Timedelta(minutes=1)

    results = []
    for fam in active_families():
        max_bars = int(fam["max_hours"] * 60 / fam["tf"])
        best = None
        tried = []
        for p in grid_params(fam):
            trades = []
            for sym, dd in data.items():
                d = dd[fam["tf"]]
                trades += simulate(bot, sym, d, fam["fn"](bot, d, p), p, max_bars)
            train = portfolio(bot, trades, t0, split)
            score = train["pf"] if train["n"] >= RS_MIN_TRADES else -1
            tried.append((p, train))
            if best is None or score > best[0]:
                best = (score, p, trades, train)
        _SIG_CACHE.clear()                        # bellek: onbellek sadece aile icinde
        _, p, trades, train = best
        test = portfolio(bot, trades, split, end)
        passed = (train["n"] >= RS_MIN_TRADES and train["pf"] >= 1.1 and test["n"] >= RS_MIN_TEST_TRADES
                  and test["pf"] >= 1.1 and test["max_dd"] <= 20 and test["t"] >= RS_MIN_T)
        results.append({"family": fam, "params": p, "train": train, "test": test, "passed": passed,
                        "grid_n": len(tried)})
        log.info("Arastirma %s: egitim PF %.2f (%d) | dogrulama PF %.2f (%d)", fam["key"], train["pf"],
                 train["n"], test["pf"], test["n"])
    return results, len(data), t0, split, t1


def config_lines(winners):
    """Gecen stratejiler icin kopyala-yapistir ayar satirlari."""
    import json as _json
    ws = [w for w in winners if w["family"].get("sid")]
    if not ws:
        return []
    params = {}
    for w in ws:
        p = {k: v for k, v in w["params"].items()}
        if "tp" in p:
            p["tp"] = list(p["tp"])
        if "tp_r" in p:
            p["tp_r"] = list(p["tp_r"])
        params[w["family"]["sid"]] = p
    return ["Key: STRATEGIES",
            f"Value: {','.join(w['family']['sid'] for w in ws)}",
            "Key: STRATEGY_PARAMS",
            f"Value: {_json.dumps(params, separators=(',', ':'))}"]


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def format_report(bot, results, n_coins, t0, split, t1):
    tz = bot.DISPLAY_TZ
    f = lambda t: t.tz_convert(tz).strftime("%d.%m.%y" if RS_MODE == "long4h" else "%d.%m")
    lines = [
        f"🔬 STRATEJİ ARAŞTIRMASI · {EXCHANGE_NAMES.get(RS_EXCHANGE, RS_EXCHANGE)}",
        (f"🗓 Uzun vade (4 saat) · {RS_LONG_DAYS} gün · {n_coins} sabit coin · eğitim {f(t0)}–{f(split)} · doğrulama {f(split)}–{f(t1)}"
         if RS_MODE == "long4h" else
         f"🗓 {RS_DAYS} gün · {n_coins} coin · eğitim {f(t0)}–{f(split)} · doğrulama {f(split)}–{f(t1)}"),
        f"💼 ${RS_BALANCE:,.0f} · işlem başı risk %{RS_RISK_PCT:g} · en fazla {RS_MAX_OPEN or bot.MAX_OPEN_TRADES} işlem · kaldıraç ≤{bot.LEVERAGE}x",
        f"🧾 Komisyon: limit %{RS_MAKER * 100:g} · market %{RS_TAKER * 100:g}",
        "Ayar eğitimde seçildi, doğrulama verisini strateji hiç görmedi.",
        "",
    ]
    for k, r in enumerate(results, 1):
        tr, te = r["train"], r["test"]
        mark = "✅ GEÇTİ" if r["passed"] else "❌ geçmedi"
        lines += [
            f"{k}) {r['family']['name']} — {mark}",
            f"   Ayar: {fmt_params(r['family'], r['params'])} ({r['grid_n']} ayar denendi)",
            f"   Eğitim: PF {_pf(tr['pf'])} · {tr['n']} işlem · %{tr['ret']:+.1f}",
            f"   Doğrulama: PF {_pf(te['pf'])} · {te['n']} işlem · %{te['winrate']:.0f} kazanma · %{te['ret']:+.1f} · maks. düşüş %{te['max_dd']:.1f}",
            f"   Anlamlılık: işlem başı {te['avg_r']:+.2f}R · t = {te['t']:.2f} (şart ≥ {RS_MIN_T:g})",
            f"   Doğrulamada LONG ${te['long']:+,.0f} · SHORT ${te['short']:+,.0f} · ort. pozisyon bakiyenin {te['exposure']:.1f} katı",
            "",
        ]
    winners = [r for r in results if r["passed"]]
    if winners:
        names = ", ".join(w["family"]["name"] for w in winners)
        lines.append(f"🏁 Geçen strateji(ler): {names}")
        cfg = config_lines(winners)
        if cfg:
            lines += ["", "⚙️ Botta açmak için Render → Environment'a ekle:"] + cfg
        lines.append("Önce kâğıt üzerinde (gerçek para olmadan) birkaç hafta takip et.")
    else:
        lines.append("🏁 Hiçbir strateji doğrulamayı geçemedi. Bu dönemde bu yaklaşımlarla güvenilir bir avantaj yok;")
        lines.append("gerçek parayla işlem önerilmez.")
    lines += ["", f"Geçti şartı: eğitim ve doğrulamada PF ≥ 1.1, doğrulamada ≥{RS_MIN_TEST_TRADES} işlem, maks. düşüş ≤ %20"
              f" ve t ≥ {RS_MIN_T:g} (tüm stratejiler birlikte düşünüldüğünde şansla geçme ihtimali ~%5).",
              "⚠️ Geçmiş performans geleceği garanti etmez. Funding ve kayma dahil değil."]
    return "\n".join(lines)


def render_test_curves(bot, results, title=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    colors = ["#2962FF", "#43A047", "#FF9800", "#E53935", "#8E24AA", "#00897B"]
    fig, ax = plt.subplots(figsize=(10, 5), dpi=110)
    fig.patch.set_facecolor("#FFFBE6")
    ax.set_facecolor("#FFFBE6")
    ranked = sorted([r for r in results if len(r["test"]["curve"]) >= 2], key=lambda r: -r["test"]["final"])
    for r in ranked[len(colors):]:            # cok strateji varsa: ilk 6 renkli, kalanlar ince gri
        cur = r["test"]["curve"]
        x = [pd.Timestamp(t).tz_convert(bot.DISPLAY_TZ) for t, _ in cur]
        ax.step(x, [e for _, e in cur], where="post", color="#BDBDBD", linewidth=0.8)
    for r, c in zip(ranked[:len(colors)], colors):
        cur = r["test"]["curve"]
        x = [pd.Timestamp(t).tz_convert(bot.DISPLAY_TZ) for t, _ in cur]
        ax.step(x, [e for _, e in cur], where="post", color=c, linewidth=1.8,
                label=f"{'✓ ' if r['passed'] else ''}{r['family']['name']} (${r['test']['final']:,.0f})")
    if len(ranked) > len(colors):
        ax.plot([], [], color="#BDBDBD", linewidth=0.8, label=f"diğer {len(ranked) - len(colors)} strateji")
    ax.axhline(RS_BALANCE, color="#555", linewidth=0.8, linestyle=":")
    ax.grid(True, color="#E8E2C8")
    ax.set_title(title or f"Doğrulama dönemi (görülmemiş veri) · ${RS_BALANCE:,.0f} · risk %{RS_RISK_PCT:g}/işlem",
                 loc="left", fontweight="bold", color="#333")
    ax.set_ylabel("Bakiye ($)")
    ax.legend(fontsize=8, frameon=False, loc="upper left")
    for s in ax.spines.values():
        s.set_color("#E8E2C8")
    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


def run_and_report(bot):
    if RS_MODE == "combo":
        return run_combo_and_report(bot)
    if RS_MODE == "funding":
        return run_funding_and_report(bot)
    if RS_MODE == "flip":
        return run_flip_and_report(bot)
    try:
        what = (f"uzun vade 4 saat, {RS_LONG_DAYS} gün, sabit coin listesi, {len(active_families())} strateji"
                if RS_MODE == "long4h" else f"{RS_DAYS} gün, {RS_TOP_N} coin, {len(FAMILIES)} strateji ailesi")
        bot.send_telegram(f"🔬 Strateji araştırması başladı ({EXCHANGE_NAMES.get(RS_EXCHANGE, RS_EXCHANGE)}): {what}. "
                          f"Veri indirme ve testler 30-60 dakika sürebilir...")
        t_start = time.time()
        results, n, t0, split, t1 = run(bot)
        report = format_report(bot, results, n, t0, split, t1)
        log.info("Arastirma bitti (%.0f sn)\n%s", time.time() - t_start, report)
        bot.send_telegram(report)
        bot.send_telegram_photo(render_test_curves(bot, results), "📈 Doğrulama dönemi sermaye eğrileri")
    except Exception as e:
        log.exception("Arastirma hatasi: %s", e)
        bot.send_telegram(f"⚠️ Strateji araştırması çalıştırılamadı: {e}")


if __name__ == "__main__":
    os.environ.setdefault("SEND_STARTUP_MESSAGE", "false")
    import main as bot
    res, n, t0, split, t1 = run(bot, progress=lambda m: print("...", m))
    print(format_report(bot, res, n, t0, split, t1))
    with open("research_curves.png", "wb") as fh:
        fh.write(render_test_curves(bot, res))
    print("\nGrafik: research_curves.png")


# ============================================================================
# KOMBINASYON TESTI (RS_MODE=combo)
# Secilmis stratejiler, AYARLARINA DOKUNULMADAN, daha once hic kullanilmamis bir donemde
# (varsayilan: son 180 gunden ONCEKI 540 gun) tek tek ve birlikte test edilir. Optimizasyon yok.
# ============================================================================
RS_COMBO = _env("RS_COMBO", "breakout_1h,pullback_1h,breakout_4h")
RS_COMBO_END_DAYS_AGO = _env("RS_COMBO_END_DAYS_AGO", 180, int)   # test penceresi bu kadar gun once biter
RS_COMBO_DAYS = _env("RS_COMBO_DAYS", 540, int)                   # test penceresinin uzunlugu
# Son 180 gunluk arastirmada (Binance seviyesi komisyon) secilen ayarlar:
COMBO_DEFAULTS = {
    "breakout_1h": {"lookback": 20, "st": 1, "sl": 2.5, "tp": [2.0, 5.0], "be": 0.0},
    "pullback_1h": {"rsi": 50, "st": 0, "sl": 1.5, "tp": [1.5, 3.0], "be": 0.0},
    "breakout_4h": {"lookback": 20, "st": 0, "sl": 2.0, "tp": [3.0, 9.0], "be": 0.0},
    "cprofile_15m": {"bands": 7, "tp_r": [1.5, 3.0], "be": 0.0},
    "cprofile_ha_15m": {"bands": 7, "ha": 1, "tp_r": [1.5, 3.0], "be": 0.0},
    "cprofile_td_15m": {"bands": 7, "td": 1, "tp_r": [1.5, 3.0], "be": 0.0},
}


def combo_specs(bot):
    import json as _json
    try:
        over = _json.loads(os.getenv("STRATEGY_PARAMS", "") or "{}")
    except ValueError:
        over = {}
    specs = []
    for sid in [s.strip() for s in RS_COMBO.split(",") if s.strip()]:
        if sid not in bot.STRAT_DEFS or bot.STRAT_DEFS[sid]["family"] == "scalein":
            log.warning("Kombinasyonda bilinmeyen/desteklenmeyen strateji atlandi: %s", sid)
            continue
        base = bot.STRAT_DEFS[sid]
        p = {**base["params"], **COMBO_DEFAULTS.get(sid, {}), **(over.get(sid) or {})}
        specs.append({"sid": sid, "family": base["family"], "tf": base["tf"], "params": p,
                      "max_hours": float(p.pop("max_hours", base["max_hours"])) if "max_hours" in p else base["max_hours"],
                      "name": f"{base['name']} ({bot.TF_LABELS[base['tf']]})"})
    return specs


def load_data_combo(bot, ex, tfs, progress=None):
    """Sabit coin listesi icin 1s/4s mumlari dogrudan ceker (15dk gerekmez)."""
    if not ex.markets:
        if bot.with_retry(ex.load_markets, desc="load_markets") is None:
            raise RuntimeError("piyasa listesi alinamadi")
    btc = bot.btc_perp_symbol(ex)
    syms = [f"{b.strip().upper()}/USDT:USDT" for b in RS_SYMBOLS.split(",") if b.strip()]
    syms = [s for s in syms if s in ex.markets and s.split("/")[0] not in bot.EXCLUDE_SYMBOLS]
    if btc not in syms:
        syms.insert(0, btc)
    days = RS_COMBO_END_DAYS_AGO + RS_COMBO_DAYS + 45          # + isinma payi
    need = sorted(set(tfs) | {240})                            # BTC rejimi icin 4s her zaman
    raw = {}
    for idx, sym in enumerate(syms, 1):
        for tf in need:
            try:
                df = fetch_15m(bot, ex, sym, days, tf_min=tf, min_bars=WARMUP + 100)
                if df is not None:
                    raw.setdefault(sym, {})[tf] = df
            except Exception as e:
                log.warning("Kombinasyon veri hatasi %s %s: %s", sym, tf, e)
        if progress and idx % 5 == 0:
            progress(f"{idx}/{len(syms)} coin")
    if btc not in raw or 240 not in raw[btc]:
        raise RuntimeError(f"BTC 4s verisi alinamadi ({btc})")
    b4 = prep(bot, raw[btc][240], 240)
    b4["btc_up"] = (b4["close"] > b4["ema50"]).astype(int)
    b4["btc_dn"] = (b4["close"] < b4["ema50"]).astype(int)
    data = {}
    for sym in list(raw):
        frames = raw.pop(sym)
        out = {}
        for tf, df in frames.items():
            if tf in tfs:
                dd = _slim(align(prep(bot, df, tf), b4, ["btc_up", "btc_dn"], ""))
                cut = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=RS_COMBO_END_DAYS_AGO - 2)
                out[tf] = dd[dd["close_time"] <= cut].reset_index(drop=True)   # pencere sonrasi gereksiz (bellek)
        if all(tf in out for tf in tfs):
            data[sym] = out
        gc.collect()
    return data


def run_combo(bot, ex=None, progress=None):
    ex = ex or make_exchange(bot)
    specs = combo_specs(bot)
    if not specs:
        raise RuntimeError("RS_COMBO icinde gecerli strateji yok")
    tfs = sorted({s["tf"] for s in specs})
    data = load_data_combo(bot, ex, tfs, progress)
    now = pd.Timestamp.now(tz="UTC")
    end = now - pd.Timedelta(days=RS_COMBO_END_DAYS_AGO)
    start = end - pd.Timedelta(days=RS_COMBO_DAYS)
    if not data:
        raise RuntimeError(f"{'/'.join(str(t) + 'dk' for t in tfs)} verisi hicbir coin icin alinamadi "
                           f"(borsa bu kadar eski mum vermiyor olabilir; RS_COMBO_END_DAYS_AGO / RS_COMBO_DAYS kucultun)")
    first = min(fr[tf]["close_time"].iloc[min(WARMUP, len(fr[tf]) - 1)] for fr in data.values() for tf in tfs)
    if first > start:                             # veri daha gec basliyor: pencereyi gercek veriye gore daralt
        log.warning("Kombinasyon penceresi veriye gore daraltildi: %s -> %s", start, first)
        start = first
    if start >= end - pd.Timedelta(days=14):
        raise RuntimeError(f"Görülmemiş dönem için yeterli geçmiş veri yok (ilk kullanılabilir mum: "
                           f"{first.tz_convert(bot.DISPLAY_TZ):%d.%m.%y}). RS_COMBO_END_DAYS_AGO değerini küçültün.")
    all_trades, rows = [], []
    for sp in specs:
        fn = _shared(sp["family"])
        max_bars = int(sp["max_hours"] * 60 / sp["tf"])
        trades = []
        for sym, frames in data.items():
            d = frames[sp["tf"]]
            sigs = [s for s in fn(bot, d, sp["params"]) if start <= d["close_time"].iloc[s[0]] < end]
            for t in simulate(bot, sym, d, sigs, sp["params"], max_bars):
                t["strategy"] = sp["sid"]
                trades.append(t)
        _SIG_CACHE.clear()
        res = portfolio(bot, trades, start, end)
        rows.append((sp, res))
        all_trades += trades
    combo = portfolio(bot, all_trades, start, end)
    return rows, combo, len(data), start, end


def format_combo_report(bot, rows, combo, n_coins, start, end):
    tz = bot.DISPLAY_TZ
    f = lambda t: t.tz_convert(tz).strftime("%d.%m.%y")
    ok = combo["n"] >= RS_MIN_TEST_TRADES and combo["pf"] >= 1.1 and combo["t"] >= RS_MIN_T and combo["max_dd"] <= 20
    lines = [
        f"🧩 KOMBİNASYON TESTİ · {EXCHANGE_NAMES.get(RS_EXCHANGE, RS_EXCHANGE)}",
        f"🗓 {f(start)} → {f(end)} ({(end - start).days} gün) · {n_coins} sabit coin",
        ("Bu dönem önceki araştırmalarda HİÇ kullanılmadı; ayarlar değiştirilmeden test edildi." if RS_COMBO_END_DAYS_AGO >= 170
         else "⚠️ Bu dönem standart araştırmada KULLANILDI (görülmemiş değil); ayarlar sabit tutuldu."),
        f"💼 ${RS_BALANCE:,.0f} · risk %{RS_RISK_PCT:g}/işlem · en fazla {RS_MAX_OPEN or bot.MAX_OPEN_TRADES} işlem · "
        f"komisyon %{RS_MAKER * 100:g} / %{RS_TAKER * 100:g}",
        "",
    ]
    for sp, r in rows:
        lines += [f"• {sp['name']}: PF {_pf(r['pf'])} · {r['n']} işlem · %{r['ret']:+.1f} · maks. düşüş %{r['max_dd']:.1f} · t = {r['t']:.2f}"]
    lines += [
        "",
        f"🧩 BİRLİKTE: PF {_pf(combo['pf'])} · {combo['n']} işlem · %{combo['winrate']:.0f} kazanma · %{combo['ret']:+.1f} · "
        f"maks. düşüş %{combo['max_dd']:.1f}",
        f"   Anlamlılık: işlem başı {combo['avg_r']:+.2f}R · t = {combo['t']:.2f} (şart ≥ {RS_MIN_T:g})",
        "",
        ("✅ Kombinasyon görülmemiş dönemde de anlamlı şekilde kârlı. Canlı bota alıp önce kâğıt üzerinde takip edebiliriz."
         if ok else
         "❌ Kombinasyon görülmemiş dönemde güvenilir bir avantaj göstermedi; gerçek parayla işlem önerilmez."),
        "⚠️ Geçmiş performans geleceği garanti etmez. Funding ve kayma dahil değil.",
    ]
    return "\n".join(lines), ok


def render_combo_curves(bot, rows, combo):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    colors = ["#2962FF", "#43A047", "#E53935", "#8E24AA", "#FF9800"]
    fig, ax = plt.subplots(figsize=(10, 5), dpi=110)
    fig.patch.set_facecolor("#FFFBE6")
    ax.set_facecolor("#FFFBE6")
    series = [(sp["name"], r) for sp, r in rows] + [("BİRLİKTE", combo)]
    for (name, r), c in zip(series, colors[:len(rows)] + ["#111111"]):
        cur = r["curve"]
        if len(cur) < 2:
            continue
        x = [pd.Timestamp(t).tz_convert(bot.DISPLAY_TZ) for t, _ in cur]
        ax.step(x, [e for _, e in cur], where="post", color=c, linewidth=2.6 if name == "BİRLİKTE" else 1.4,
                label=f"{name} (${r['final']:,.0f})")
    ax.axhline(RS_BALANCE, color="#555", linewidth=0.8, linestyle=":")
    ax.grid(True, color="#E8E2C8")
    ax.set_title(f"Kombinasyon · görülmemiş dönem · ${RS_BALANCE:,.0f} · risk %{RS_RISK_PCT:g}/işlem",
                 loc="left", fontweight="bold", color="#333")
    ax.set_ylabel("Bakiye ($)")
    ax.legend(fontsize=8, frameon=False, loc="upper left")
    for s in ax.spines.values():
        s.set_color("#E8E2C8")
    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


def run_combo_and_report(bot):
    try:
        bot.send_telegram(f"🧩 Kombinasyon testi başladı ({EXCHANGE_NAMES.get(RS_EXCHANGE, RS_EXCHANGE)}): {RS_COMBO} · "
                          f"{RS_COMBO_DAYS} günlük görülmemiş dönem. 5-20 dakika sürebilir...")
        rows, combo, n, start, end = run_combo(bot)
        report, _ = format_combo_report(bot, rows, combo, n, start, end)
        log.info("Kombinasyon testi bitti\n%s", report)
        bot.send_telegram(report)
        bot.send_telegram_photo(render_combo_curves(bot, rows, combo), "📈 Kombinasyon · görülmemiş dönem")
    except Exception as e:
        log.exception("Kombinasyon testi hatasi: %s", e)
        bot.send_telegram(f"⚠️ Kombinasyon testi çalıştırılamadı: {e}")


# ============================================================================
# FONLAMA MODU (RS_MODE=funding): Binance'te hacimce ilk N coin, MEXC fiyat + fonlama verisi,
# SABIT kurallar (ayar taramasi yok), tum veri iki yariya bolunup AYRI raporlanir.
# ============================================================================
RS_FUND_DAYS = _env("RS_FUND_DAYS", 365, int)
RS_UNIVERSE = _env("RS_UNIVERSE", "binance").lower()          # binance | mexc | list (RS_SYMBOLS)
RS_UNIVERSE_N = _env("RS_UNIVERSE_N", 30, int)
FUND_STRATS = [x.strip() for x in _env("RS_FUND_STRATS", "cprofile_15m,cprofile_fund_15m,fundrev_1h").split(",") if x.strip()]
ENTRY_VARIANTS = {            # strateji@varyant -> giris turu
    "mkt": ({}, "market giriş"),
    "lim0": ({"entry": "limit", "lim_atr": 0.0, "lim_bars": 2}, "limit giriş: sinyal fiyatı, 2 mum"),
    "lim25": ({"entry": "limit", "lim_atr": 0.25, "lim_bars": 4}, "limit giriş: 0,25 ATR geride, 4 mum"),
    "lim50": ({"entry": "limit", "lim_atr": 0.5, "lim_bars": 4}, "limit giriş: 0,5 ATR geride, 4 mum"),
}
_STABLE = {"USDC", "FDUSD", "TUSD", "USDE", "DAI", "BUSD", "USDP", "USD1", "RLUSD", "PYUSD"}
FUND_KEEP = ["ts", "open", "high", "low", "close", "atr", "adx", "st_dir", "close_time",
             "fund", "fund_p90", "fund_p10", "fund_ts"]


def universe_symbols(bot, ex_data):
    """Coin listesi + kaynagi. binance: Binance USDT-M sureklilerinde 24s hacme gore ilk N (erisim yoksa RS_SYMBOLS)."""
    mk = ex_data.markets or {}

    def to_data_sym(base):
        b = base.upper()
        for cand in (b, b[4:] if b.startswith("1000") else None, b[7:] if b.startswith("1000000") else None,
                     b[2:] if b.startswith("1M") else None):
            if cand and f"{cand}/USDT:USDT" in mk:
                return f"{cand}/USDT:USDT"
        return None

    if RS_UNIVERSE in ("binance", "mexc"):
        try:
            if RS_UNIVERSE == "binance":
                bx = bot.ccxt.binanceusdm({"enableRateLimit": True, "timeout": 20000})
                bx.load_markets()
                tick = bx.fetch_tickers()
                bm = bx.markets
            else:
                tick, bm = ex_data.fetch_tickers(), mk
            ranked = []
            for sym, t in tick.items():
                m = bm.get(sym) or {}
                base = (m.get("base") or "").upper()
                if not (m.get("swap") and m.get("linear") and m.get("quote") == "USDT") or not base:
                    continue
                if base in _STABLE or base in bot.EXCLUDE_SYMBOLS or base.endswith("STOCK") or base in bot.STOCK_BASES or not m.get("active", True):
                    continue
                qv = t.get("quoteVolume") or 0
                ranked.append((float(qv), base))
            ranked.sort(reverse=True)
            out, miss = [], []
            for _, base in ranked:
                ds = to_data_sym(base)
                (out if ds else miss).append(ds or base)
                if len(out) >= RS_UNIVERSE_N:
                    break
            if len(out) >= 5:
                src = f"{EXCHANGE_NAMES.get(RS_UNIVERSE, RS_UNIVERSE)} hacim ilk {len(out)}"
                if miss:
                    src += f" (veride bulunmayan: {', '.join(miss[:6])})"
                return out, src
        except Exception as e:
            log.warning("Coin listesi %s'ten alinamadi: %s", RS_UNIVERSE, e)
            fail = f"{RS_UNIVERSE} erişilemedi ({type(e).__name__}) → RS_SYMBOLS listesi"
        else:
            fail = f"{RS_UNIVERSE} listesi yetersiz → RS_SYMBOLS listesi"
    else:
        fail = "RS_SYMBOLS listesi"
    syms = [to_data_sym(b.strip()) for b in RS_SYMBOLS.split(",") if b.strip()]
    return [s for s in syms if s][:RS_UNIVERSE_N], fail


def load_data_funding(bot, ex, progress=None):
    if not ex.markets:
        if bot.with_retry(ex.load_markets, desc="load_markets") is None:
            raise RuntimeError("piyasa listesi alinamadi")
    syms, src = universe_symbols(bot, ex)
    data, cover = {}, []
    since_ms = int((time.time() - (RS_FUND_DAYS + 45) * 86400) * 1000)
    for idx, sym in enumerate(syms, 1):
        try:
            df = fetch_15m(bot, ex, sym, RS_FUND_DAYS, tf_min=15, min_bars=WARMUP + 500)
            if df is None:
                continue
            f = bot.fetch_funding_history(ex, sym, since_ms=since_ms)
            if f is not None:
                cover.append((sym.split("/")[0], len(f), int(f["ts"].iloc[0])))
            d15 = bot.attach_funding(prep(bot, df, 15), f, 15)
            d60 = bot.attach_funding(prep(bot, resample(df, 60), 60), f, 60)
            data[sym] = {15: d15[[c for c in FUND_KEEP if c in d15]].copy(),
                         60: d60[[c for c in FUND_KEEP if c in d60]].copy()}
            del df, d15, d60, f
            gc.collect()
        except Exception as e:
            log.warning("Fonlama modu veri hatasi %s: %s", sym, e)
        if progress and idx % 5 == 0:
            progress(f"{idx}/{len(syms)} coin")
    return data, src, cover


def run_funding(bot, ex=None, progress=None):
    ex = ex or make_exchange(bot)
    data, src, cover = load_data_funding(bot, ex, progress)
    if not data:
        raise RuntimeError("Hiçbir coin için 15 dk veri alınamadı")
    t0 = min(fr[15]["close_time"].iloc[WARMUP] for fr in data.values())
    t1 = max(fr[15]["close_time"].iloc[-1] for fr in data.values())
    mid = t0 + (t1 - t0) / 2
    end = t1 + pd.Timedelta(minutes=1)
    rows = []
    FILL_STATS.clear()
    for full_sid in FUND_STRATS:
        sid, _, var = full_sid.partition("@")
        base = bot.STRAT_DEFS[sid]
        vparams, vlabel = ENTRY_VARIANTS.get(var or "mkt", ({}, var))
        p = dict(base["params"], **vparams, _sid=full_sid)
        max_bars = int(base["max_hours"] * 60 / base["tf"])
        trades = []
        for sym, fr in data.items():
            d = fr[base["tf"]]
            l, s, score = bot.strategy_signal_arrays(base["family"], d, p, np.ones(len(d)), np.ones(len(d)))
            trades += simulate(bot, sym, d, _dir_list(l, s, score), p, max_bars)
        h1, h2, full = portfolio(bot, trades, t0, mid), portfolio(bot, trades, mid, end), portfolio(bot, trades, t0, end)
        passed = (h1["n"] >= 30 and h2["n"] >= 30 and h1["pf"] >= 1.1 and h2["pf"] >= 1.1
                  and full["t"] >= RS_MIN_T and full["max_dd"] <= 20)
        fs = FILL_STATS.get(full_sid)
        rows.append({"sid": full_sid, "name": f"{base['name']} ({bot.TF_LABELS[base['tf']]})" + (f" · {vlabel}" if var else ""),
                     "h1": h1, "h2": h2, "full": full, "passed": passed,
                     "fill": (fs[1], fs[0]) if fs else None})
    return rows, src, cover, len(data), t0, mid, t1


def format_funding_report(bot, rows, src, cover, n, t0, mid, t1):
    tz = bot.DISPLAY_TZ
    f = lambda t: t.tz_convert(tz).strftime("%d.%m.%y")
    line = lambda r: (f"PF {_pf(r['pf'])} · {r['n']} işlem · %{r['ret']:+.1f} · maks. düşüş %{r['max_dd']:.1f} · "
                      f"t = {r['t']:.2f}")
    fc = (f"{len(cover)}/{n} coinde fonlama verisi, en eskisi "
          f"{pd.Timestamp(min(c[2] for c in cover), unit='ms', tz='UTC').tz_convert(tz):%d.%m.%y}" if cover
          else "⚠️ FONLAMA VERİSİ ALINAMADI (fonlama stratejileri anlamsız)")
    out = [f"💸 FONLAMA ARAŞTIRMASI · {EXCHANGE_NAMES.get(RS_EXCHANGE, RS_EXCHANGE)} verisi",
           f"🗓 {f(t0)} → {f(t1)} · {n} coin ({src})",
           f"📊 {fc}",
           f"💼 ${RS_BALANCE:,.0f} · risk %{RS_RISK_PCT:g}/işlem · en fazla {RS_MAX_OPEN or bot.MAX_OPEN_TRADES} işlem · "
           f"komisyon %{RS_MAKER * 100:g} / %{RS_TAKER * 100:g}",
           "Kurallar önceden sabitlendi, ayar taraması yok. Her yarı ayrı bakiyeyle ($2.000) başlar.", ""]
    for i, r in enumerate(rows, 1):
        out += [f"{i}) {r['name']} — {'✅ GEÇTİ' if r['passed'] else '❌ geçmedi'}",
                f"   1. yarı ({f(t0)}–{f(mid)}): {line(r['h1'])}",
                f"   2. yarı ({f(mid)}–{f(t1)}): {line(r['h2'])}",
                f"   Tümü: işlem başı {r['full']['avg_r']:+.2f}R · t = {r['full']['t']:.2f} · "
                f"LONG ${r['full']['long']:+,.0f} · SHORT ${r['full']['short']:+,.0f}"]
        if r.get("fill"):
            fl, sg = r["fill"]
            out.append(f"   Limit emir dolma oranı: %{fl / sg * 100 if sg else 0:.0f} ({fl}/{sg} sinyal; dolmayanlar işlem kaçtı)")
        out.append("")
    ids = [r["sid"] for r in rows]
    if "cprofile_15m" in ids and "cprofile_fund_15m" in ids:
        base, filt = rows[ids.index("cprofile_15m")], rows[ids.index("cprofile_fund_15m")]
        out.append(f"🔎 Fonlama filtresinin etkisi (tümü): {base['full']['n']} → {filt['full']['n']} işlem, "
                   f"işlem başı {base['full']['avg_r']:+.2f}R → {filt['full']['avg_r']:+.2f}R")
    out.append("")
    out.append(f"Geçti şartı: iki yarıda da ≥30 işlem ve PF ≥ 1.1, tümünde t ≥ {RS_MIN_T:g} ve maks. düşüş ≤ %20.")
    out.append("⚠️ Coin listesi BUGÜNÜN hacmine göre seçildi (geçmişte popüler olmayanlar da var olabilir). "
               "Funding ödemeleri ve kayma işlem sonucuna dahil değil.")
    return "\n".join(out)


def run_funding_and_report(bot):
    try:
        bot.send_telegram(f"💸 Fonlama araştırması başladı: {RS_UNIVERSE} hacim ilk {RS_UNIVERSE_N} coin, "
                          f"{RS_FUND_DAYS} güne kadar geçmiş, {len(FUND_STRATS)} sabit strateji. 10-30 dakika sürebilir...")
        t_start = time.time()
        rows, src, cover, n, t0, mid, t1 = run_funding(bot)
        report = format_funding_report(bot, rows, src, cover, n, t0, mid, t1)
        log.info("Fonlama arastirmasi bitti (%.0f sn)\n%s", time.time() - t_start, report)
        bot.send_telegram(report)
        res = [{"family": {"name": r["name"]}, "test": r["full"], "passed": r["passed"]} for r in rows]
        bot.send_telegram_photo(render_test_curves(bot, res, title=f"Fonlama araştırması · tüm dönem · ${RS_BALANCE:,.0f}"),
                                "📈 Fonlama araştırması · sermaye eğrileri (tüm dönem)")
    except Exception as e:
        log.exception("Fonlama arastirmasi hatasi: %s", e)
        bot.send_telegram(f"⚠️ Fonlama araştırması çalıştırılamadı: {e}")



# ============================================================================
# SUREKLI DON-CEVIR MODU (RS_MODE=flip): sabit teminat, tek pozisyon, 1 dk grafikte SuperTrend
# yon degistirince ters pozisyona gec (stop-and-reverse). Her zaman piyasada.
# ============================================================================
RS_FLIP_SYMBOLS = _env("RS_FLIP_SYMBOLS", "BTC,ETH,SOL")
RS_FLIP_DAYS = _env("RS_FLIP_DAYS", 30, int)
RS_FLIP_TF = _env("RS_FLIP_TF", 1, int)
RS_FLIP_MARGIN = _env("RS_FLIP_MARGIN", 500, float)
RS_FLIP_LEV = _env("RS_FLIP_LEV", 25, float)
RS_FLIP_SLIP = _env("RS_FLIP_SLIP", 0.0001, float)          # her market emirde kayma (1 baz puan)
FLIP_VARIANTS = [("ST(7,2)", 7, 2.0), ("ST(10,3)", 10, 3.0), ("ST(10,5)", 10, 5.0), ("ST(14,4)", 14, 4.0),
                 ("ST(20,8)", 20, 8.0), ("ST(30,10)", 30, 10.0)]
FLIP_FEES = [(x.split(":")[0], float(x.split(":")[1])) for x in
             _env("RS_FLIP_FEES", "Lighter 0/0:0,MEXC web %0.016:0.00016,Hyperliquid %0.045:0.00045").split(",")]
RS_FLIP_TP_ROE = _env("RS_FLIP_TP_ROE", 0.0, float)         # >0: teminatta bu kadar % kar olunca kapat (orn 50)
FLIP_SLIPS = [float(x) for x in _env("RS_FLIP_SLIPS", "").split(",") if x.strip()] or [None]   # orn "0,0.00002,0.00005"
RS_FLIP_RISK = _env("RS_FLIP_RISK", 10.0, float)             # 'risk' ve 'stop' modlarinda islem basi en fazla kayip ($)
FLIP_MODES = {
    "sabit": "Tüm teminatla pozisyon, SuperTrend dönünce (mum kapanışı) ters",
    "risk": "Stop SuperTrend çizgisinde ve çizgiyi TAKİP ediyor; stop olursa kayıp ≈ bakiyenin %{riskp}",
    "stop": "Tüm teminatla pozisyon + sabit stop (bakiyenin %{riskp})",
    "trail": "Tüm teminatla pozisyon, stop SuperTrend çizgisinde ve çizgiyi TAKİP ediyor",
}


ACTIVE_FLIP_MODES = [m.strip() for m in _env("RS_FLIP_MODES", "sabit,risk,stop").split(",") if m.strip() in FLIP_MODES] \
    or list(FLIP_MODES)
RS_FLIP_COMPOUND = _env("RS_FLIP_COMPOUND", "true").lower() in ("1", "true", "yes", "evet")


def flip_sim2(bot, d, line, dirs, taker, start, end, mode, slip=None):
    """Tek coin, tek pozisyon, her zaman SuperTrend yonunde. Bilesik: kazanc/kayip sonraki isleme eklenir.
    sabit: teminat x kaldirac pozisyon, cikis = SuperTrend donusu (mum KAPANISI) -> ters ac.
    risk : stop = SuperTrend cizgisi ve cizgi her mum TAKIP edilir (sadece lehe); pozisyon, stop olursa kayip
           = bakiyenin (RS_FLIP_RISK / RS_FLIP_MARGIN) kadari olacak buyuklukte. Stop olunca bir sonraki donusu bekler.
    stop : sabit pozisyon + sabit $ stop (bakiyeyle orantili).
    Likidasyon: fiyat girisin (1/kaldirac - bakim) kadar tersine giderse teminat gider."""
    SLIP = RS_FLIP_SLIP if slip is None else slip
    lev = RS_FLIP_LEV
    liq_frac = 1.0 / lev - bot.MMR_PCT / 100.0
    O, H, L, C = (d[c].to_numpy() for c in ("open", "high", "low", "close"))
    ct = d["close_time"]
    idx = np.where(((ct >= start) & (ct < end)).to_numpy())[0]
    eq = RS_FLIP_MARGIN
    pos, entry, notional, stop, margin = 0, 0.0, 0.0, np.nan, 0.0
    fees = 0.0
    n = liqs = 0
    rets, curve = [], [eq]

    def close_at(px, liq=False):
        nonlocal eq, fees, n, pos, liqs
        if liq:
            pnl = -margin
            liqs += 1
        else:
            exit_px = px * (1 - pos * SLIP)
            pnl = notional * pos * (exit_px / entry - 1)
            fees += notional * taker
            pnl -= notional * taker
        before = eq + notional * taker            # giris komisyonu zaten dusulmustu
        eq += pnl
        rets.append((eq - before) / before if before > 0 else -1.0)
        curve.append(eq)
        n += 1
        pos = 0

    prev = dirs[idx[0] - 1] if len(idx) and idx[0] > 0 else np.nan
    for i in idx:
        if eq <= 1:
            break                                 # hesap bitti
        if pos:
            worst = L[i] if pos == 1 else H[i]
            if pos * (worst / entry - 1) <= -liq_frac and not (np.isfinite(stop) and pos * (stop - worst) >= 0
                                                            and pos * (stop / entry - 1) > -liq_frac):
                close_at(worst, liq=True)
            elif np.isfinite(stop) and ((pos == 1 and L[i] <= stop) or (pos == -1 and H[i] >= stop)):
                # mum stopun OTESINDE acildiysa (bosluk / cizgi fiyatin ustune cikti) dolum acilis fiyatindan
                close_at(min(stop, O[i]) if pos == 1 else max(stop, O[i]))
            elif RS_FLIP_TP_ROE > 0:              # teminat uzerinden ROE hedefi (limit emir, kayma yok)
                tp = entry * (1 + pos * RS_FLIP_TP_ROE / 100 / lev)
                if (pos == 1 and H[i] >= tp) or (pos == -1 and L[i] <= tp):
                    px = max(tp, O[i]) if pos == 1 else min(tp, O[i])
                    exit_px = px
                    pnl = notional * pos * (exit_px / entry - 1)
                    eq += pnl
                    rets.append(pnl / (eq - pnl) if eq - pnl > 0 else 0.0)
                    curve.append(eq)
                    n += 1
                    pos = 0
        cur = dirs[i]
        flipped = np.isfinite(prev) and np.isfinite(cur) and cur != prev
        prev = cur
        if pos and mode in ("risk", "trail") and np.isfinite(line[i]):   # iz suren stop: cizgi lehe gittikce stop da gelir
            stop = max(stop, line[i]) if pos == 1 else min(stop, line[i])
        if not flipped:
            continue
        if pos:
            close_at(C[i])
        want = int(cur)
        e = C[i] * (1 + want * SLIP)
        base = eq if RS_FLIP_COMPOUND else RS_FLIP_MARGIN
        cap = min(base, eq) * lev
        if mode == "risk":
            dist = abs(e - line[i]) / e if np.isfinite(line[i]) else 0
            if dist <= 0.0002:
                continue
            risk_usd = RS_FLIP_RISK * base / RS_FLIP_MARGIN
            notional = min(risk_usd / dist, cap)
            stop = line[i]
        elif mode == "trail":                    # tum teminatla pozisyon, stop SuperTrend cizgisinde ve takip ediyor
            if not np.isfinite(line[i]):
                continue
            notional = cap
            stop = line[i]
        elif mode == "stop":
            notional = cap
            stop = e * (1 - want * (RS_FLIP_RISK * base / RS_FLIP_MARGIN) / notional)
        else:
            notional = cap
            stop = np.nan
        margin = notional / lev
        pos, entry = want, e
        fees += notional * taker
        eq -= notional * taker
    if pos and len(idx) and eq > 1:
        close_at(C[idx[-1]])
    c = np.array(curve)
    return {"n": n, "fees": fees, "net": eq - RS_FLIP_MARGIN, "liqs": liqs, "pnls": rets,
            "final": eq, "dd": float(((np.maximum.accumulate(c) - c) / np.maximum.accumulate(c)).max() * 100) if len(c) else 0.0}


def run_flip(bot, ex=None, progress=None):
    ex = ex or make_exchange(bot)
    if not ex.markets:
        bot.with_retry(ex.load_markets, desc="load_markets")
    frames = {}
    for b in [x.strip().upper() for x in RS_FLIP_SYMBOLS.split(",") if x.strip()]:
        sym = f"{b}/USDT:USDT"
        if sym not in (ex.markets or {}):
            continue
        df = fetch_15m(bot, ex, sym, RS_FLIP_DAYS, tf_min=RS_FLIP_TF, min_bars=500)
        if df is None:
            continue
        df = df.copy()
        df["close_time"] = pd.to_datetime(df["ts"], utc=True) + pd.Timedelta(minutes=RS_FLIP_TF) \
            if not pd.api.types.is_numeric_dtype(df["ts"]) else pd.to_datetime(df["ts"] + RS_FLIP_TF * 60000, unit="ms", utc=True)
        frames[b] = df
    if not frames:
        raise RuntimeError(f"{RS_FLIP_TF} dk veri alınamadı")
    t0 = max(fr["close_time"].iloc[min(200, len(fr) - 1)] for fr in frames.values())
    t1 = min(fr["close_time"].iloc[-1] for fr in frames.values())
    mid = t0 + (t1 - t0) / 2
    days = max((t1 - t0).total_seconds() / 86400, 1e-9)
    res = []
    for vname, per, mult in FLIP_VARIANTS:
        sts = {b: bot.calc_supertrend(fr, per, mult) for b, fr in frames.items()}
        lines = {b: np.asarray(v[0], float) for b, v in sts.items()}
        dirs = {b: np.asarray(v[1], float) for b, v in sts.items()}
        for (mode, fname, tk, sl) in [(m, f, t, s) for m in ACTIVE_FLIP_MODES for f, t in FLIP_FEES for s in FLIP_SLIPS]:
            if len(FLIP_SLIPS) > 1:
                fname = f"{fname} · kayma %{sl * 100:g}"
            row = {"v": vname, "f": fname, "m": mode, "coins": {}}
            for half, (a, z) in (("h1", (t0, mid)), ("h2", (mid, t1 + pd.Timedelta(minutes=1)))):
                agg = {"n": 0, "fees": 0.0, "net": 0.0, "liqs": 0, "pnls": []}
                for b, fr in frames.items():
                    s = flip_sim2(bot, fr, lines[b], dirs[b], tk, a, z, mode, slip=sl)
                    for k in ("n", "fees", "net", "liqs", "pnls"):
                        agg[k] += s[k]
                    agg["dd"] = max(agg.get("dd", 0.0), s["dd"])
                    row["coins"].setdefault(b, 0.0)
                    row["coins"][b] += s["net"]
                row[half] = agg
            allp = np.array(row["h1"]["pnls"] + row["h2"]["pnls"])
            row["t"] = float(allp.mean() / (allp.std(ddof=1) / np.sqrt(len(allp)))) if len(allp) > 2 and allp.std(ddof=1) > 0 else 0.0
            row["passed"] = row["h1"]["net"] > 0 and row["h2"]["net"] > 0 and row["t"] >= RS_MIN_T
            res.append(row)
    return res, list(frames), t0, mid, t1, days


def format_flip_report(bot, res, coins, t0, mid, t1, days):
    f = lambda t: t.tz_convert(bot.DISPLAY_TZ).strftime("%d.%m %H:%M")
    cap = RS_FLIP_MARGIN * RS_FLIP_LEV
    out = [f"🔁 SÜREKLİ GİR-ÇIK TESTİ · {RS_FLIP_TF} dk · SuperTrend dönünce ters pozisyon",
           f"🗓 {f(t0)} → {f(t1)} ({days:.0f} gün) · {', '.join(coins)} (her coin ayrı ${RS_FLIP_MARGIN:,.0f} teminat, "
           f"başlangıçta en fazla ${cap:,.0f} pozisyon)",
           (f"🎯 TP: teminatta %{RS_FLIP_TP_ROE:g} kâr (fiyatta %{RS_FLIP_TP_ROE / RS_FLIP_LEV:g}) olunca kapat · " if RS_FLIP_TP_ROE > 0 else "") +
           f"{RS_FLIP_LEV:g}x · {'BİLEŞİK (kâr sonraki işleme eklenir)' if RS_FLIP_COMPOUND else 'sabit teminat'} · "
           f"kayma %{RS_FLIP_SLIP * 100:g}/emir (aksi yazmadıkça) · funding hariç · her yarı ${RS_FLIP_MARGIN:,.0f} ile başlar · "
           f"tutarlar TÜM coinlerin toplam kâr/zararı", ""]
    for mode, desc in [(m, FLIP_MODES[m]) for m in ACTIVE_FLIP_MODES]:
        out.append(f"■ {desc.replace('{riskp}', f'{RS_FLIP_RISK / RS_FLIP_MARGIN * 100:g}')}")
        for fname in dict.fromkeys(r["f"] for r in res if r["m"] == mode):
            rows = [r for r in res if r["m"] == mode and r["f"] == fname]
            best = max(rows, key=lambda r: r["h1"]["net"])          # ayar 1. yarida secilir...
            out.append(f"  {fname}:")
            for r in rows:
                n = r["h1"]["n"] + r["h2"]["n"]
                mark = " ◀ 1. yarının en iyisi" if r is best else ""
                liq = r["h1"]["liqs"] + r["h2"]["liqs"]
                out.append(f"   {r['v']}: 1.yarı ${r['h1']['net']:+,.0f} · 2.yarı ${r['h2']['net']:+,.0f} · "
                           f"{n / max(len(coins), 1) / days:.0f} işlem/gün/coin · maks düşüş %{max(r['h1'].get('dd', 0), r['h2'].get('dd', 0)):.0f}"
                           f" · t {r['t']:.2f}{' · ⚠️' + str(liq) + ' likidasyon' if liq else ''}{mark}")
            ok = best["h2"]["net"] > 0 and best["t"] >= RS_MIN_T
            out.append(f"   → Seçilen {best['v']} görmediği 2. yarıda ${best['h2']['net']:+,.0f} "
                       f"{'✅ tuttu' if ok else '❌ tutmadı'}")
        out.append("")
    out.append(f"Ayar seçimi: her senaryoda en iyi SuperTrend ayarı SADECE 1. yarıya bakılarak seçildi, "
               f"2. yarı onun dürüst sınavı. ✅ için 2. yarıda artı ve t ≥ {RS_MIN_T:g} gerekir.")
    return "\n".join(out)


def run_flip_and_report(bot):
    try:
        bot.send_telegram(f"🔁 Sürekli gir-çık testi başladı: {RS_FLIP_TF} dk, {RS_FLIP_SYMBOLS}, {RS_FLIP_DAYS} gün. 5-15 dakika sürebilir...")
        res, coins, t0, mid, t1, days = run_flip(bot)
        rep = format_flip_report(bot, res, coins, t0, mid, t1, days)
        log.info("Flip testi bitti\n%s", rep)
        bot.send_telegram(rep)
    except Exception as e:
        log.exception("Flip testi hatasi: %s", e)
        bot.send_telegram(f"⚠️ Sürekli gir-çık testi çalıştırılamadı: {e}")
