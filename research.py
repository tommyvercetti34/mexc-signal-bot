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
RS_MAKER = _env("BT_MAKER_FEE", 0.0, float)
RS_TAKER = _env("BT_TAKER_FEE", 0.0002, float)
WARMUP = 220                                           # EMA200 isinmasi


# ============================================================================
# VERI
# ============================================================================
def fetch_15m(bot, ex, symbol, days):
    tf_ms = 15 * 60_000
    since = int((datetime.now(timezone.utc) - timedelta(days=days + 4)).timestamp() * 1000)
    now_ms = int(time.time() * 1000)
    rows = []
    while since < now_ms - tf_ms:
        batch = bot.with_retry(ex.fetch_ohlcv, symbol, "15m", since=since, limit=1000, desc=f"rs {symbol}")
        if not batch:
            break
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
    df = df[df["ts"] + pd.Timedelta(minutes=15) <= pd.Timestamp.now(tz="UTC")]
    return df.reset_index(drop=True) if len(df) > 2000 else None


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


def _shared(family):
    """Canli botla AYNI sinyal fonksiyonu (bot.strategy_signal_arrays)."""
    def fn(bot, d, p):
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
    parts.append(f"stop {p['sl']:g} ATR")
    parts.append(f"TP {p['tp'][0]:g}/{p['tp'][1]:g} ATR")
    parts.append(f"başa baş {p['be']:g} ATR'de" if p.get("be") else "başa baş yok")
    return ", ".join(parts)


# ============================================================================
# SIMULASYON
# ============================================================================
def simulate(bot, symbol, d, sigs, p, max_bars):
    """Her sinyal: tek giris, stop = sl*ATR, Hedef1'de %50 kapat + stop girise, Hedef2'de kalan.
    Canli botun islem motoru (bot.process_bar) kullanilir."""
    H, L, C, A = (d[c].to_numpy() for c in ("high", "low", "close", "atr"))
    ct = d["close_time"].to_numpy()
    out = []
    for i, direction, score in sigs:
        atr, e = float(A[i]), float(C[i])
        if not (atr > 0 and e > 0):
            continue
        sign = 1 if direction == "LONG" else -1
        sl = e - sign * p["sl"] * atr
        sl_dist = abs(e - sl) / e
        if sl_dist < 0.002 or sl_dist > 0.08:
            continue
        plan = {"direction": direction, "sign": sign, "entries": [e], "weights": [1.0], "sl": sl, "atr": atr}
        s = bot.new_state(plan, tp1_atr=p["tp"][0], tp2_atr=p["tp"][1], tp1_frac=0.5, move_be=True, taker=RS_TAKER,
                          be_atr=p.get("be", 0.0))
        j = i
        for j in range(i + 1, min(len(d), i + 1 + max_bars)):
            bot.process_bar(s, H[j], L[j], maker=RS_MAKER, taker=RS_TAKER)
            if s["closed"]:
                break
        if not s["closed"]:
            if j - i >= max_bars:
                bot.close_market(s, float(C[j]), taker=RS_TAKER)
            else:
                continue
        out.append({"symbol": symbol, "direction": direction, "score": score, "outcome": s["outcome"],
                    "entry_time": pd.Timestamp(ct[i]), "exit_time": pd.Timestamp(ct[j]),
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
        raise RuntimeError(f"BTC verisi alinamadi ({btc}) - borsa gecmis veriyi vermedi veya erisim kisitli")

    b4 = prep(bot, resample(raw[btc], 240), 240)
    b4["btc_up"] = (b4["close"] > b4["ema50"]).astype(int)
    b4["btc_dn"] = (b4["close"] < b4["ema50"]).astype(int)

    data = {}
    for sym, df in raw.items():
        if sym == btc and btc not in symbols:
            continue
        d15 = prep(bot, df, 15)
        d60 = prep(bot, resample(df, 60), 60)
        d15 = align(align(d15, b4, ["btc_up", "btc_dn"], ""), d60, ["close", "ema50"], "h1_")
        d60 = align(d60, b4, ["btc_up", "btc_dn"], "")
        d240 = align(prep(bot, resample(df, 240), 240), b4, ["btc_up", "btc_dn"], "")
        data[sym] = {15: d15, 60: d60, 240: d240}
    return data


EXCHANGE_NAMES = {"mexc": "MEXC", "binance": "Binance"}


def make_exchange(bot):
    """Arastirma verisinin cekilecegi borsa. binance = Binance USDT-M vadeli (binanceusdm)."""
    if RS_EXCHANGE == "binance":
        return bot.ccxt.binanceusdm({"enableRateLimit": True, "timeout": 20000})
    return bot.create_exchange()


def run(bot, ex=None, progress=None):
    ex = ex or make_exchange(bot)
    data = load_data(bot, ex, progress)
    t_all = [d[15]["close_time"] for d in data.values()]
    t0 = min(t.iloc[WARMUP] for t in t_all)
    t1 = max(t.iloc[-1] for t in t_all)
    split = t0 + (t1 - t0) * RS_TRAIN_FRAC
    end = t1 + pd.Timedelta(minutes=1)

    results = []
    for fam in FAMILIES:
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
        params[w["family"]["sid"]] = p
    return ["Key: STRATEGIES",
            f"Value: {','.join(w['family']['sid'] for w in ws)}",
            "Key: STRATEGY_PARAMS",
            f"Value: {_json.dumps(params, separators=(',', ':'))}"]


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def format_report(bot, results, n_coins, t0, split, t1):
    tz = bot.DISPLAY_TZ
    f = lambda t: t.tz_convert(tz).strftime("%d.%m")
    lines = [
        f"🔬 STRATEJİ ARAŞTIRMASI · {EXCHANGE_NAMES.get(RS_EXCHANGE, RS_EXCHANGE)}",
        f"🗓 {RS_DAYS} gün · {n_coins} coin · eğitim {f(t0)}–{f(split)} · doğrulama {f(split)}–{f(t1)}",
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


def render_test_curves(bot, results):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    colors = ["#2962FF", "#43A047", "#FF9800", "#9E9E9E", "#E53935", "#8E24AA", "#00897B", "#6D4C41", "#F06292"]
    fig, ax = plt.subplots(figsize=(10, 5), dpi=110)
    fig.patch.set_facecolor("#FFFBE6")
    ax.set_facecolor("#FFFBE6")
    for r, c in zip(results, colors):
        cur = r["test"]["curve"]
        if len(cur) < 2:
            continue
        x = [pd.Timestamp(t).tz_convert(bot.DISPLAY_TZ) for t, _ in cur]
        ax.step(x, [e for _, e in cur], where="post", color=c, linewidth=1.8,
                label=f"{'✓ ' if r['passed'] else ''}{r['family']['name']} (${r['test']['final']:,.0f})")
    ax.axhline(RS_BALANCE, color="#555", linewidth=0.8, linestyle=":")
    ax.grid(True, color="#E8E2C8")
    ax.set_title(f"Doğrulama dönemi (görülmemiş veri) · ${RS_BALANCE:,.0f} · risk %{RS_RISK_PCT:g}/işlem",
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
    try:
        bot.send_telegram(f"🔬 Strateji araştırması başladı ({EXCHANGE_NAMES.get(RS_EXCHANGE, RS_EXCHANGE)}): {RS_DAYS} gün, {RS_TOP_N} coin, {len(FAMILIES)} strateji ailesi. "
                          f"Veri indirme ve testler 10-20 dakika sürebilir...")
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
