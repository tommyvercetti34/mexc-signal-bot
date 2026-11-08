"""
Backtest - MEXC Futures Sinyal Botu
===================================
Canli botla (main.py) AYNI indikatorleri ve AYNI giris/stop/hedef kurallarini kullanir.

Iki sekilde calisir:
  1) Render'da: ortam degiskeni RUN_BACKTEST=true -> bot acilista testi arka planda calistirir,
     raporu ve sermaye egrisini Telegram'a gonderir. (Bittikten sonra RUN_BACKTEST'i kaldir.)
  2) Bilgisayarda:  python backtest.py   (rapor konsola, grafik ve islem listesi dosyaya yazilir)

Varsayimlar (hepsi ortam degiskeniyle degistirilebilir):
  - Baslangic bakiyesi BT_BALANCE (2000$), her islemde bakiyenin BT_MARGIN_PCT'si (%10) teminat, LEVERAGE (15x)
  - Kademeler limit emir (maker), 1. giris ve stop market (taker), hedef limit (maker)
  - 15 dakikalik mumlarla simulasyon; ayni mumda hem stop hem hedef gorulurse STOP sayilir (temkinli)
  - Funding ucretleri ve kayma (slippage) dahil degil
"""

import os
import sys
import io
import time
import logging
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

log = logging.getLogger("backtest")


def _env(name, default, cast=str):
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return cast(v)
    except ValueError:
        return default


BT_DAYS = _env("BT_DAYS", 30, int)
BT_TOP_N = _env("BT_TOP_N", 30, int)
BT_BALANCE = _env("BT_BALANCE", 2000.0, float)
BT_MARGIN_PCT = _env("BT_MARGIN_PCT", 10.0, float)        # her islemde bakiyenin %'si teminat
BT_MAKER_FEE = _env("BT_MAKER_FEE", 0.0001, float)        # %0.01
BT_TAKER_FEE = _env("BT_TAKER_FEE", 0.0004, float)        # %0.04 (temkinli)
WARMUP_BARS = 150


# ============================================================================
# VERI
# ============================================================================
def fetch_history(bot, ex, symbol, days):
    """15m mumlari sayfa sayfa ceker (indikator isinmasi icin +3 gun)."""
    tf_ms = bot.TF_MINUTES * 60_000
    since = int((datetime.now(timezone.utc) - timedelta(days=days + 3)).timestamp() * 1000)
    now_ms = int(time.time() * 1000)
    rows = []
    while since < now_ms - tf_ms:
        batch = bot.with_retry(ex.fetch_ohlcv, symbol, bot.TIMEFRAME, since=since, limit=1000,
                               desc=f"bt {symbol}")
        if not batch:
            break
        rows.extend(batch)
        nxt = batch[-1][0] + tf_ms
        if nxt <= since:
            break
        since = nxt
        if len(batch) < 2:
            break
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df = df[df["ts"] + timedelta(minutes=bot.TF_MINUTES) <= datetime.now(timezone.utc)]
    return df.reset_index(drop=True) if len(df) > WARMUP_BARS + 50 else None


# ============================================================================
# SINYALLER (bot.evaluate ile ayni mantik, vektorel)
# ============================================================================
def signals(bot, d, use_st):
    k, dd, adx = d["k"].to_numpy(), d["d"].to_numpy(), d["adx"].to_numpy()
    pk, pd_ = np.r_[np.nan, k[:-1]], np.r_[np.nan, dd[:-1]]
    close, vwap, atr = d["close"].to_numpy(), d["vwap"].to_numpy(), d["atr"].to_numpy()
    st, st_dir = d["st"].to_numpy(), d["st_dir"].to_numpy()

    valid = ~(np.isnan(atr) | np.isnan(adx) | np.isnan(k) | np.isnan(dd) | np.isnan(vwap) | np.isnan(pk) | np.isnan(pd_))
    strong = adx >= bot.ADX_THRESHOLD
    with np.errstate(invalid="ignore"):
        cross_up = (pk <= pd_) & (k > dd) & (np.maximum(pk, pd_) < bot.STOCH_OVERSOLD)
        cross_dn = (pk >= pd_) & (k < dd) & (np.minimum(pk, pd_) > bot.STOCH_OVERBOUGHT)
        long_ = cross_up & (close > vwap)
        short = cross_dn & ~long_ & (close < vwap)
    if use_st:
        long_ &= (st_dir == 1) & ~np.isnan(st)
        short &= (st_dir == -1) & ~np.isnan(st)

    risk = atr * bot.ATR_SL_MULT
    sl_pct = risk / close * 100
    ok = valid & strong & (sl_pct >= bot.MIN_SL_PCT) & (sl_pct <= bot.MAX_SL_PCT) & (close - risk > 0)
    out = []
    for i in np.where(ok & (long_ | short))[0]:
        if i < WARMUP_BARS:
            continue
        out.append((i, "LONG" if long_[i] else "SHORT", float(close[i]), float(atr[i])))
    return out


# ============================================================================
# ISLEM SIMULASYONU
# ============================================================================
def simulate_symbol(bot, symbol, d, use_st, weights, step_atr):
    """Bir coin icin islemleri sirayla simule eder (ayni anda tek islem + cooldown).
    Her islem: giris/cikis zamani, sonuc ve kademe bazli getiri bilgisi."""
    hi, lo, cl = d["high"].to_numpy(), d["low"].to_numpy(), d["close"].to_numpy()
    ts = d["ts"].to_numpy()
    tf_ms = bot.TF_MINUTES * 60_000
    max_bars = int(bot.MAX_TRADE_HOURS * 60 / bot.TF_MINUTES) if bot.MAX_TRADE_HOURS > 0 else 10**9
    cooldown_bars = int(np.ceil(bot.SIGNAL_COOLDOWN_MIN / bot.TF_MINUTES))
    trades, busy_until, last_sig = [], -1, -10**9

    for i, direction, entry, atr in signals(bot, d, use_st):
        if i <= busy_until or i - last_sig < cooldown_bars:
            continue
        sign = 1 if direction == "LONG" else -1
        risk = atr * bot.ATR_SL_MULT
        sl, tp = entry - sign * risk, entry + sign * risk * bot.RR_RATIO
        entries = [entry - sign * n * atr * step_atr for n in range(len(weights))]
        filled = [True] + [False] * (len(weights) - 1)
        outcome, exit_px, j = None, None, i
        for j in range(i + 1, min(len(d), i + 1 + max_bars)):
            hit_sl = lo[j] <= sl if sign == 1 else hi[j] >= sl
            hit_tp = hi[j] >= tp if sign == 1 else lo[j] <= tp
            if not hit_tp or hit_sl:           # hedef mumunda yeni kademe sayma (temkinli)
                for n, ep in enumerate(entries):
                    if not filled[n] and (lo[j] <= ep if sign == 1 else hi[j] >= ep):
                        filled[n] = True
            if hit_sl:
                outcome, exit_px = "SL", sl
                break
            if hit_tp:
                outcome, exit_px = "TP", tp
                break
        if outcome is None:
            if j - i >= max_bars:
                outcome, exit_px = "EXPIRED", float(cl[j])
            else:
                outcome, exit_px = "OPEN", float(cl[j])   # test sonunda hala acik
        trades.append({
            "symbol": symbol, "direction": direction, "sign": sign,
            "entry_time": pd.Timestamp(ts[i]) + pd.Timedelta(milliseconds=tf_ms),
            "exit_time": pd.Timestamp(ts[j]) + pd.Timedelta(milliseconds=tf_ms),
            "entries": entries, "weights": weights, "filled": filled,
            "sl": sl, "tp": tp, "exit": exit_px, "outcome": outcome,
        })
        busy_until, last_sig = j, i
    return trades


def run_portfolio(bot, trades):
    """Islemleri tek bir 2000$'lik hesapta zaman sirasina gore isler."""
    trades = sorted([t for t in trades if t["outcome"] != "OPEN"], key=lambda t: t["entry_time"])
    equity = BT_BALANCE
    open_pos = []                                 # (exit_time, margin, pnl)
    curve = [(trades[0]["entry_time"] if trades else datetime.now(timezone.utc), equity)]
    done, skipped = [], 0

    def settle(until):
        nonlocal equity
        for p in sorted([p for p in open_pos if p[0] <= until], key=lambda p: p[0]):
            equity += p[2]
            curve.append((p[0], equity))
            open_pos.remove(p)

    for t in trades:
        settle(t["entry_time"])
        margin = equity * BT_MARGIN_PCT / 100
        used = sum(p[1] for p in open_pos)
        if margin <= 0 or used + margin > equity:
            skipped += 1
            continue
        notional = margin * bot.LEVERAGE
        pnl = fees = 0.0
        for n, (ep, w, f) in enumerate(zip(t["entries"], t["weights"], t["filled"])):
            if not f:
                continue
            q = notional * w
            pnl += q * t["sign"] * (t["exit"] - ep) / ep
            fees += q * (BT_TAKER_FEE if n == 0 else BT_MAKER_FEE)                  # giris
            fees += q * (BT_MAKER_FEE if t["outcome"] == "TP" else BT_TAKER_FEE)   # cikis
        pnl -= fees
        pnl = max(pnl, -margin)                   # isolated: en fazla teminat kadar kayip
        open_pos.append((t["exit_time"], margin, pnl))
        done.append(dict(t, margin=margin, pnl=pnl, fees=fees, filled_n=sum(t["filled"])))
    settle(pd.Timestamp.max.tz_localize("UTC"))
    return done, curve, equity, skipped


def stats(done, curve, final, skipped):
    n = len(done)
    wins = [t for t in done if t["pnl"] > 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in done if t["pnl"] <= 0)
    eq = np.array([e for _, e in curve])
    peak = np.maximum.accumulate(eq) if len(eq) else eq
    max_dd = float(((peak - eq) / peak).max() * 100) if len(eq) else 0.0
    return {
        "n": n, "tp": sum(t["outcome"] == "TP" for t in done), "sl": sum(t["outcome"] == "SL" for t in done),
        "exp": sum(t["outcome"] == "EXPIRED" for t in done),
        "winrate": len(wins) / n * 100 if n else 0.0,
        "pnl": final - BT_BALANCE, "ret": (final / BT_BALANCE - 1) * 100, "final": final,
        "pf": gross_win / gross_loss if gross_loss > 0 else float("inf"),
        "max_dd": max_dd, "fees": sum(t["fees"] for t in done), "skipped": skipped,
        "avg_fill": np.mean([t["filled_n"] for t in done]) if n else 0.0,
    }


# ============================================================================
# CALISTIR
# ============================================================================
VARIANTS = [
    ("Mevcut: SuperTrend + kademeli", True, None),
    ("SuperTrend + tek giriş", True, [1.0]),
    ("SuperTrend yok + kademeli", False, None),
    ("İlk sürüm: ST yok + tek giriş", False, [1.0]),
]


def run(bot, ex=None, progress=None):
    ex = ex or bot.create_exchange()
    if not ex.markets:
        bot.with_retry(ex.load_markets, desc="load_markets")
    symbols = bot.get_top_symbols(ex, top_n=BT_TOP_N)
    log.info("Backtest: %d coin, %d gun veri cekiliyor", len(symbols), BT_DAYS)

    data = {}
    for idx, sym in enumerate(symbols, 1):
        try:
            df = fetch_history(bot, ex, sym, BT_DAYS)
            if df is not None:
                data[sym] = bot.add_indicators(df)
        except Exception as e:
            log.warning("Backtest veri hatasi %s: %s", sym, e)
        if progress and idx % 10 == 0:
            progress(f"{idx}/{len(symbols)} coin")
    if not data:
        raise RuntimeError("Backtest icin veri alinamadi")

    results = []
    for name, use_st, w in VARIANTS:
        weights = w or bot.LADDER_WEIGHTS
        step = bot.LADDER_STEP_ATR
        trades = []
        for sym, d in data.items():
            trades += simulate_symbol(bot, sym, d, use_st, weights, step)
        done, curve, final, skipped = run_portfolio(bot, trades)
        results.append((name, stats(done, curve, final, skipped), done, curve))
    start = min(d["ts"].iloc[WARMUP_BARS] for d in data.values())
    end = max(d["ts"].iloc[-1] for d in data.values())
    return results, len(data), start, end


def _money(v):
    return f"{'+' if v >= 0 else '-'}${abs(v):,.0f}"


def format_report(bot, results, n_coins, start, end):
    tz = bot.DISPLAY_TZ
    s0 = results[0][1]
    lines = [
        "🧪 BACKTEST SONUCU",
        f"🗓 {start.tz_convert(tz):%d.%m.%Y} → {end.tz_convert(tz):%d.%m.%Y} ({BT_DAYS} gün, {n_coins} coin, {bot.TF_LABEL})",
        f"💼 Başlangıç ${BT_BALANCE:,.0f} · işlem başı teminat %{BT_MARGIN_PCT:g} · {bot.LEVERAGE}x {bot.MARGIN_MODE}",
        "",
        f"⭐ {results[0][0]}",
        f"💰 Son bakiye: ${s0['final']:,.0f} ({_money(s0['pnl'])}, %{s0['ret']:+.1f})",
        f"📊 İşlem: {s0['n']} · TP {s0['tp']} / Stop {s0['sl']} / Süre doldu {s0['exp']}",
        f"🎯 Kazanma oranı: %{s0['winrate']:.0f} · Profit factor: {s0['pf']:.2f}",
        f"📉 Maks. düşüş: %{s0['max_dd']:.1f} · Komisyon: ${s0['fees']:,.0f}",
        f"🪜 Ortalama dolan kademe: {s0['avg_fill']:.1f}",
    ]
    if s0["skipped"]:
        lines.append(f"⏭ Teminat yetmediği için atlanan: {s0['skipped']}")
    lines += ["", "🔬 Karşılaştırma (son bakiye · işlem · kazanma · maks. düşüş)"]
    for name, s, _, _ in results:
        lines.append(f"• {name}: ${s['final']:,.0f} ({_money(s['pnl'])}) · {s['n']} · %{s['winrate']:.0f} · %{s['max_dd']:.0f}")

    done = results[0][2]
    if done:
        by = {}
        for t in done:
            by[t["symbol"]] = by.get(t["symbol"], 0) + t["pnl"]
        ranked = sorted(by.items(), key=lambda x: x[1], reverse=True)
        name = lambda s: s.split("/")[0]
        lines += ["", "🏆 En iyi: " + ", ".join(f"{name(s)} {_money(v)}" for s, v in ranked[:3]),
                  "💀 En kötü: " + ", ".join(f"{name(s)} {_money(v)}" for s, v in ranked[-3:][::-1])]
    lines += ["", "⚠️ Geçmiş performans geleceği garanti etmez. Funding ve kayma dahil değil;",
              "coinler bugünkü hacme göre seçildi (hayatta kalma yanlılığı)."]
    return "\n".join(lines)


def render_equity(bot, results):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    colors = ["#2962FF", "#43A047", "#FF9800", "#9E9E9E"]
    fig, ax = plt.subplots(figsize=(10, 5), dpi=110)
    fig.patch.set_facecolor("#FFFBE6")
    ax.set_facecolor("#FFFBE6")
    for (name, s, _, curve), c in zip(results, colors):
        if len(curve) < 2:
            continue
        x = [pd.Timestamp(t).tz_convert(bot.DISPLAY_TZ) for t, _ in curve]
        y = [e for _, e in curve]
        ax.step(x, y, where="post", color=c, linewidth=2.2 if c == colors[0] else 1.3,
                label=f"{name}  (${s['final']:,.0f})")
    ax.axhline(BT_BALANCE, color="#555", linewidth=0.8, linestyle=":")
    ax.grid(True, color="#E8E2C8")
    ax.set_title(f"Sermaye Eğrisi · ${BT_BALANCE:,.0f} başlangıç · {bot.LEVERAGE}x · %{BT_MARGIN_PCT:g} teminat/işlem",
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
    """Render'da botun icinden cagrilir: sonucu Telegram'a gonderir."""
    try:
        bot.send_telegram(f"🧪 Backtest başladı: son {BT_DAYS} gün, ilk {BT_TOP_N} coin. Birkaç dakika sürebilir...")
        t0 = time.time()
        results, n, start, end = run(bot)
        report = format_report(bot, results, n, start, end)
        log.info("Backtest bitti (%.0f sn)\n%s", time.time() - t0, report)
        bot.send_telegram(report)
        bot.send_telegram_photo(render_equity(bot, results), "📈 Sermaye eğrisi (4 varyant)")
    except Exception as e:
        log.exception("Backtest hatasi: %s", e)
        bot.send_telegram(f"⚠️ Backtest çalıştırılamadı: {e}")


if __name__ == "__main__":
    os.environ.setdefault("SEND_STARTUP_MESSAGE", "false")
    import main as bot
    results, n, start, end = run(bot, progress=lambda m: print("...", m))
    print(format_report(bot, results, n, start, end))
    with open("backtest_equity.png", "wb") as f:
        f.write(render_equity(bot, results))
    rows = [{k: v for k, v in t.items() if k not in ("entries", "weights", "filled")} for t in results[0][2]]
    pd.DataFrame(rows).to_csv("backtest_trades.csv", index=False)
    print("\nGrafik: backtest_equity.png · İşlemler: backtest_trades.csv")
