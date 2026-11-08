"""
Backtest - MEXC Futures Sinyal Botu
===================================
Canli botla (main.py) AYNI indikatorleri ve AYNI giris/stop/hedef kurallarini kullanir.

Iki sekilde calisir:
  1) Render'da: ortam degiskeni RUN_BACKTEST=true -> bot acilista testi arka planda calistirir,
     raporu ve sermaye egrisini Telegram'a gonderir. (Bittikten sonra RUN_BACKTEST'i kaldir.)
  2) Bilgisayarda:  python backtest.py   (rapor konsola, grafik ve islem listesi dosyaya yazilir)

Varsayimlar (hepsi ortam degiskeniyle degistirilebilir):
  - Baslangic bakiyesi BT_BALANCE (2000$); botun MAX_OPEN_TRADES / SLOT_PCT / ENTRY_WEIGHTS / LEVERAGE ayarlari
    (varsayilan: en fazla 2 islem, her biri bakiyenin yarisi; kademeler slotun %40/%20/%20'si)
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
BT_MAX_OPEN = _env("BT_MAX_OPEN", 0, int)                # 0 = botun MAX_OPEN_TRADES ayari
BT_MAKER_FEE = _env("BT_MAKER_FEE", 0.0, float)           # MEXC standart: %0
BT_TAKER_FEE = _env("BT_TAKER_FEE", 0.0002, float)        # MEXC standart: %0.02 (coine gore %0-0.10 degisir)
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
def triggers(bot, d, use_st):
    """Giris tetikleri (bot.trigger_direction ile ayni mantik, vektorel): [(i, yon, adx)]."""
    k, dd, adx = d["k"].to_numpy(), d["d"].to_numpy(), d["adx"].to_numpy()
    pk, pd_ = np.r_[np.nan, k[:-1]], np.r_[np.nan, dd[:-1]]
    close, vwap, atr = d["close"].to_numpy(), d["vwap"].to_numpy(), d["atr"].to_numpy()
    st, st_dir = d["st"].to_numpy(), d["st_dir"].to_numpy()
    valid = ~(np.isnan(atr) | np.isnan(adx) | np.isnan(k) | np.isnan(dd) | np.isnan(vwap) | np.isnan(pk) | np.isnan(pd_))
    with np.errstate(invalid="ignore"):
        strong = adx >= bot.ADX_THRESHOLD
        cross_up = (pk <= pd_) & (k > dd) & (np.maximum(pk, pd_) < bot.STOCH_OVERSOLD)
        cross_dn = (pk >= pd_) & (k < dd) & (np.minimum(pk, pd_) > bot.STOCH_OVERBOUGHT)
        long_ = cross_up & (close > vwap)
        short = cross_dn & ~long_ & (close < vwap)
    if use_st:
        long_ &= (st_dir == 1) & ~np.isnan(st)
        short &= (st_dir == -1) & ~np.isnan(st)
    ok = valid & strong & (long_ | short)
    return [(int(i), "LONG" if long_[i] else "SHORT", float(adx[i])) for i in np.where(ok)[0] if i >= WARMUP_BARS]


def signals(bot, d, use_st):
    """Geriye uyum / test icin: tetik + kademe plani gecen sinyaller [(i, yon, plan)]."""
    H, L, C, A = (d[c].to_numpy() for c in ("high", "low", "close", "atr"))
    out = []
    for i, direction, adx in triggers(bot, d, use_st):
        plan = bot.plan_trade(H, L, C, A, i, direction)
        if plan:
            out.append((i, direction, plan))
    return out


def simulate_symbol(bot, symbol, d, var):
    """Bir coindeki HER gecerli sinyalin sonucunu canli botun motoruyla bagimsiz hesaplar."""
    H, L, C, A = (d[c].to_numpy() for c in ("high", "low", "close", "atr"))
    ts = d["ts"].to_numpy()
    tf = pd.Timedelta(minutes=bot.TF_MINUTES)
    max_bars = int(bot.MAX_TRADE_HOURS * 60 / bot.TF_MINUTES) if bot.MAX_TRADE_HOURS > 0 else 10**9
    out = []
    for i, direction, adx in triggers(bot, d, var["st"]):
        plan = bot.plan_trade(H, L, C, A, i, direction, weights=var["weights"], mode=var["mode"])
        if not plan:
            continue
        s = bot.new_state(plan, tp1_atr=var["tp1"], tp2_atr=var["tp2"], taker=BT_TAKER_FEE)
        j = i
        for j in range(i + 1, min(len(d), i + 1 + max_bars)):
            bot.process_bar(s, H[j], L[j], maker=BT_MAKER_FEE, taker=BT_TAKER_FEE)
            if s["closed"]:
                break
        if not s["closed"]:
            if j - i >= max_bars:
                bot.close_market(s, float(C[j]), taker=BT_TAKER_FEE)
            else:
                continue                          # test sonunda hala acik -> sayma
        ws = sum(w for w, f in zip(s["weights"], s["filled"]) if f)
        out.append({
            "symbol": symbol, "direction": direction, "adx": adx, "outcome": s["outcome"],
            "entry_time": pd.Timestamp(ts[i]) + tf, "exit_time": pd.Timestamp(ts[j]) + tf,
            "ret": s["ret"], "fee": s["fee"], "filled_n": sum(s["filled"]), "filled_w": ws,
            "levels": len(s["weights"]), "tp1_hit": s["tp1_done"], "used_support": plan["used_support"],
        })
    return out


def run_portfolio(bot, trades, fees=True):
    """Sinyalleri zaman sirasina gore tek hesapta isler: en fazla MAX_OPEN islem, coin basina 1 islem,
    cooldown; ayni anda gelenlerde ADX'i yuksek olan secilir. Teminat = bakiye x slot orani."""
    max_open = BT_MAX_OPEN or bot.MAX_OPEN_TRADES or 10**6
    cooldown = pd.Timedelta(minutes=bot.SIGNAL_COOLDOWN_MIN)
    trades = sorted(trades, key=lambda t: (t["entry_time"], -t["adx"]))
    equity = BT_BALANCE
    open_pos, last_sig, done, skipped = [], {}, [], 0
    curve = [(trades[0]["entry_time"] if trades else pd.Timestamp.now(tz="UTC"), equity)]

    def settle(until):
        nonlocal equity
        for p in sorted([p for p in open_pos if p["exit_time"] <= until], key=lambda p: p["exit_time"]):
            equity += p["pnl"]
            curve.append((p["exit_time"], equity))
            open_pos.remove(p)

    for t in trades:
        settle(t["entry_time"])
        if any(p["symbol"] == t["symbol"] for p in open_pos):
            continue
        if t["symbol"] in last_sig and t["entry_time"] - last_sig[t["symbol"]] < cooldown:
            continue
        if len(open_pos) >= max_open:
            skipped += 1
            continue
        margin = equity * bot.SLOT_FRACTION * bot.LADDER_ALLOC
        if margin <= 1:
            break                                 # hesap bitti
        notional = margin * bot.LEVERAGE
        fee_usd = notional * t["fee"] if fees else 0.0
        pnl = max(notional * t["ret"] - fee_usd, -margin)   # isolated: en fazla teminat kadar kayip
        open_pos.append({"exit_time": t["exit_time"], "pnl": pnl, "symbol": t["symbol"]})
        last_sig[t["symbol"]] = t["entry_time"]
        done.append(dict(t, margin=margin, pnl=pnl, fees=fee_usd))
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
    cnt = lambda o: sum(t["outcome"] == o for t in done)
    side = lambda dname: sum(t["pnl"] for t in done if t["direction"] == dname)
    return {
        "n": n, "tp": cnt("TP"), "tp1be": cnt("TP1_BE"), "sl": cnt("SL"), "exp": cnt("EXPIRED"),
        "winrate": len(wins) / n * 100 if n else 0.0,
        "pnl": final - BT_BALANCE, "ret": (final / BT_BALANCE - 1) * 100, "final": final,
        "pf": gross_win / gross_loss if gross_loss > 0 else float("inf"),
        "max_dd": max_dd, "fees": sum(t["fees"] for t in done), "skipped": skipped,
        "avg_fill": np.mean([t["filled_n"] for t in done]) if n else 0.0,
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "avg_loss": gross_loss / max(1, n - len(wins)),
        "long_pnl": side("LONG"), "short_pnl": side("SHORT"),
        "long_n": sum(t["direction"] == "LONG" for t in done), "short_n": sum(t["direction"] == "SHORT" for t in done),
    }


# ============================================================================
# CALISTIR
# ============================================================================
def variants(bot):
    w = bot.LADDER_WEIGHTS
    wt = "/".join(f"{x * 100:.0f}" for x in w)
    t1, t2 = bot.TP1_ATR, bot.TP2_ATR
    return [
        {"name": f"⭐ Destek kademeli {wt} · TP {t1:g}/{t2:g} ATR", "st": bot.USE_SUPERTREND, "mode": "support", "weights": w, "tp1": t1, "tp2": t2},
        {"name": f"Destek kademeli · TP 1/2 ATR", "st": bot.USE_SUPERTREND, "mode": "support", "weights": w, "tp1": 1.0, "tp2": 2.0},
        {"name": f"Destek kademeli · TP 2/4 ATR", "st": bot.USE_SUPERTREND, "mode": "support", "weights": w, "tp1": 2.0, "tp2": 4.0},
        {"name": f"ATR kademeli {wt} · TP {t1:g}/{t2:g} ATR", "st": bot.USE_SUPERTREND, "mode": "atr", "weights": w, "tp1": t1, "tp2": t2},
        {"name": f"Tek giriş · TP {t1:g}/{t2:g} ATR", "st": bot.USE_SUPERTREND, "mode": "support", "weights": [1.0], "tp1": t1, "tp2": t2},
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
    for var in variants(bot):
        trades = []
        for sym, d in data.items():
            trades += simulate_symbol(bot, sym, d, var)
        done, curve, final, skipped = run_portfolio(bot, trades)
        st = stats(done, curve, final, skipped)
        st["final_nofee"] = run_portfolio(bot, trades, fees=False)[2]
        results.append((var["name"], st, done, curve))
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
        f"💼 Başlangıç ${BT_BALANCE:,.0f} · en fazla {BT_MAX_OPEN or bot.MAX_OPEN_TRADES} açık işlem · "
        f"işlem başı teminat %{bot.SLOT_FRACTION * bot.LADDER_ALLOC * 100:g} (ilk ${BT_BALANCE * bot.SLOT_FRACTION * bot.LADDER_ALLOC:,.0f}) · {bot.LEVERAGE}x",
        f"🪜 Kademeler: " + " / ".join(f"${BT_BALANCE * bot.SLOT_FRACTION * bot.LADDER_ALLOC * w:,.0f}" for w in bot.LADDER_WEIGHTS)
        + f" · Hedef 1'de %{bot.TP1_CLOSE_PCT:.0f} kapat" + (" + stop girişe" if bot.MOVE_SL_TO_BE else ""),
        "",
        f"{results[0][0]}",
        f"💰 Son bakiye: ${s0['final']:,.0f} ({_money(s0['pnl'])}, %{s0['ret']:+.1f})",
        f"📊 İşlem: {s0['n']} · Hedef 2 {s0['tp']} / Hedef 1+giriş {s0['tp1be']} / Stop {s0['sl']} / Süre doldu {s0['exp']}",
        f"🎯 Kazanma oranı: %{s0['winrate']:.0f} · Profit factor: {s0['pf']:.2f}",
        f"⚖️ Ort. kazanç {_money(s0['avg_win'])} · ort. kayıp -${s0['avg_loss']:,.0f}",
        f"↕️ LONG {s0['long_n']} işlem {_money(s0['long_pnl'])} · SHORT {s0['short_n']} işlem {_money(s0['short_pnl'])}",
        f"📉 Maks. düşüş: %{s0['max_dd']:.1f} · Komisyon: ${s0['fees']:,.0f}",
        f"🧾 Komisyon varsayımı: limit %{BT_MAKER_FEE*100:g} / market %{BT_TAKER_FEE*100:g} · komisyonsuz olsaydı: ${s0['final_nofee']:,.0f} ({_money(s0['final_nofee']-BT_BALANCE)})",
        f"🪜 Ortalama dolan kademe: {s0['avg_fill']:.1f}",
    ]
    if s0["skipped"]:
        lines.append(f"⏭ Slot dolu olduğu için atlanan sinyal: {s0['skipped']}")
    lines += ["", "🔬 Karşılaştırma (son bakiye · işlem · kazanma · PF · maks. düşüş)"]
    for name, s, _, _ in results:
        pf = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        lines.append(f"• {name}: ${s['final']:,.0f} ({_money(s['pnl'])}) · {s['n']} · %{s['winrate']:.0f} · {pf} · %{s['max_dd']:.0f}")

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
    colors = ["#2962FF", "#43A047", "#FF9800", "#8E24AA", "#9E9E9E"]
    fig, ax = plt.subplots(figsize=(10, 5), dpi=110)
    fig.patch.set_facecolor("#FFFBE6")
    ax.set_facecolor("#FFFBE6")
    for (name, s, _, curve), c in zip(results, colors):
        if len(curve) < 2:
            continue
        x = [pd.Timestamp(t).tz_convert(bot.DISPLAY_TZ) for t, _ in curve]
        y = [e for _, e in curve]
        ax.step(x, y, where="post", color=c, linewidth=2.2 if c == colors[0] else 1.3,
                label=f"{name.replace('⭐ ', '')}  (${s['final']:,.0f})")
    ax.axhline(BT_BALANCE, color="#555", linewidth=0.8, linestyle=":")
    ax.grid(True, color="#E8E2C8")
    ax.set_title(f"Sermaye Eğrisi · ${BT_BALANCE:,.0f} başlangıç · {bot.LEVERAGE}x · en fazla {BT_MAX_OPEN or bot.MAX_OPEN_TRADES} işlem",
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
        bot.send_telegram_photo(render_equity(bot, results), f"📈 Sermaye eğrisi ({len(results)} varyant)")
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
    rows = list(results[0][2])
    pd.DataFrame(rows).to_csv("backtest_trades.csv", index=False)
    print("\nGrafik: backtest_equity.png · İşlemler: backtest_trades.csv")
