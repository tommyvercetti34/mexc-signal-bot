"""
BIST botu: gun sonu (gunluk mum) + gun ici (1 saatlik mum) kagit takip ve walk-forward testi
===========================================================================================
- Sadece ALIS yonlu (BIST'te aciga satis kisitli). SAT = elindekini sat, nakde gec.
- Stratejiler:
    mplus  : Matrix+ BIST indikatorunun Swing sinyali (likidite trend cizgisi kirilimi + hacim onayi)
    matrix : Matrix VWMA bulutu (VWMA(ohlc4) > SMA(ohlc4)) + likidite trend filtresi
    st     : SuperTrend donusu
- Sinyal mum kapanisinda, islem BIR SONRAKI mumun acilisinda (gun sonu: ertesi gun acilis).
- Gun ici defter: pozisyon gun sonunda (son saatlik mum kapanisinda) kapatilir, geceye tasinmaz.
- Komisyon BIST_FEE (varsayilan binde 1,5) + kayma BIST_SLIP (binde 0,5), alis ve satista ayri ayri.
- Veri: Yahoo Finance (THYAO.IS ...), ~15 dk gecikmeli.
- T+2 takas yok sayilir: satistan gelen para ayni gun yeni alimda kullanilir (toplam bakiye BIST_CAPITAL).

Test : Render'da RUN_BIST_RESEARCH=true   (sonuc Telegram'a gelir)
Canli: Render'da BIST_LIVE=true            (kagit takip, Telegram'da /bist)
"""

import json
import math
import os
import threading
import time
import logging
import itertools
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

log = logging.getLogger("bist")
TZ = ZoneInfo("Europe/Istanbul")


def _env(name, default, cast=str):
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        if cast is bool:
            return v.strip().lower() in ("1", "true", "yes", "evet", "on")
        return cast(v)
    except ValueError:
        return default


BIST_LIVE = _env("BIST_LIVE", False, bool)
RUN_BIST_RESEARCH = _env("RUN_BIST_RESEARCH", False, bool)
DEFAULT_SYMBOLS = ("AKBNK,ALARK,ASELS,ASTOR,BIMAS,CCOLA,EKGYO,ENKAI,EREGL,FROTO,GARAN,GUBRF,HEKTS,ISCTR,KCHOL,"
                   "KRDMD,MGROS,OYAKC,PETKM,PGSUS,SAHOL,SASA,SISE,TAVHL,TCELL,THYAO,TOASO,TTKOM,TUPRS,YKBNK")
BIST_SYMBOLS = [s.strip().upper() for s in _env("BIST_SYMBOLS", DEFAULT_SYMBOLS).split(",") if s.strip()]
BIST_BENCH = _env("BIST_BENCH", "XU100")
BIST_CAPITAL = _env("BIST_CAPITAL", 100000.0, float)        # toplam sermaye (TL)
BIST_DAILY_FRAC = _env("BIST_DAILY_FRAC", 0.5, float)       # sermayenin gun sonu defterine ayrilan kismi
BIST_SLOTS = _env("BIST_SLOTS", 5, int)                      # her defterde ayni anda en fazla hisse
BIST_FEE = _env("BIST_FEE", 0.0015, float)                   # komisyon (oran, tek yon)
BIST_SLIP = _env("BIST_SLIP", 0.0005, float)                 # kayma (oran, tek yon)
BIST_DAILY = _env("BIST_DAILY", "mplus:swk=1,vol=1,exit=sinyal,hlen=50")
BIST_INTRA = _env("BIST_INTRA", "mplus:swk=0.75,vol=1,exit=sinyal,hlen=30")
BIST_DELAY_MIN = _env("BIST_DELAY_MIN", 16, int)             # veri gecikmesi (dk)
BIST_DAILY_YEARS = _env("BIST_DAILY_YEARS", 10, int)
BIST_TRAIN_FRAC = _env("BIST_TRAIN_FRAC", 0.6, float)
BIST_MIN_TRADES = _env("BIST_MIN_TRADES", 30, int)
BIST_MIN_T = _env("BIST_MIN_T", 2.5, float)
STATE_FILE = "bist_state.json"
WARMUP = 220
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"}

# ============================================================================
# VERI
# ============================================================================


def fetch(sym, interval="1d", rng="10y", retries=3):
    """Yahoo Finance'ten BIST mumlari. sym: 'THYAO' ya da 'XU100'. Index Istanbul saati (mum ACILIS zamani)."""
    ticker = sym if "." in sym or sym.startswith("^") else f"{sym}.IS"
    last_err = None
    for k in range(retries):
        for host in ("query1", "query2"):
            try:
                r = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}",
                                 params={"interval": interval, "range": rng, "includePrePost": "false"},
                                 headers=UA, timeout=30)
                if r.status_code == 429:
                    last_err = "429 (cok istek)"
                    time.sleep(3 + 3 * k)
                    continue
                r.raise_for_status()
                res = r.json()["chart"]["result"][0]
                ts = res.get("timestamp") or []
                q = res["indicators"]["quote"][0]
                df = pd.DataFrame({c: q.get(c) for c in ("open", "high", "low", "close", "volume")},
                                  index=pd.to_datetime(ts, unit="s", utc=True).tz_convert(TZ))
                df = df.dropna(subset=["open", "high", "low", "close"]).astype(float)
                df["volume"] = df["volume"].fillna(0.0)
                df = df[~df.index.duplicated(keep="last")].sort_index()
                if interval == "1d":                  # gunluk: index'i gunun 00:00'ina normalize et
                    df.index = df.index.normalize()
                    df = df[~df.index.duplicated(keep="last")]
                return df
            except Exception as e:
                last_err = e
        time.sleep(1 + k)
    log.warning("BIST veri alinamadi %s %s: %s", sym, interval, last_err)
    return None


# ============================================================================
# GOSTERGELER VE SINYALLER  (hepsi mum kapanisinda, geleceği gormez)
# ============================================================================


def _sma(x, n):
    return pd.Series(x).rolling(n, min_periods=n).mean().values


def _ema(x, n):
    return pd.Series(x).ewm(span=n, adjust=False).mean().values


def _rma(x, n):
    return pd.Series(x).ewm(alpha=1.0 / n, adjust=False).mean().values


def _atr(h, l, c, n):
    pc = np.r_[np.nan, c[:-1]]
    tr = np.fmax(h - l, np.fmax(np.abs(h - pc), np.abs(l - pc)))
    tr[0] = h[0] - l[0]
    return _rma(tr, n)


def _arr(d):
    return (d["open"].values, d["high"].values, d["low"].values, d["close"].values, d["volume"].values)


def liq_mid(c, n):
    """Likidite trend cizgisi (BigBeluga): EMA(SMA(close, n), n)."""
    s = _sma(c, n)
    out = np.full(len(c), np.nan)
    ok = ~np.isnan(s)
    if ok.any():
        out[ok] = _ema(s[ok], n)
    return out


def sig_mplus(d, p):
    """Matrix+ BIST Swing: fiyat trend cizgisinin swk*ATR(100) otesinde kapaninca yon degisir;
    ok, hacim ortalamanin volk katini gecince basilir (en fazla `wait` mum beklenir). Pine koduyla ayni mantik."""
    o, h, l, c, v = _arr(d)
    hl, swk = int(p.get("hlen", 50)), float(p.get("swk", 1.0))
    volc, volk, wait = int(p.get("vol", 1)), float(p.get("volk", 1.2)), int(p.get("wait", 5))
    exitm = p.get("exit", "sinyal")
    mid, a, vavg = liq_mid(c, hl), _atr(h, l, c, 100), _sma(v, 20)
    n = len(c)
    ent, ex = np.zeros(n, bool), np.zeros(n, bool)
    sw = pend = pb = shown = 0
    for i in range(n):
        if math.isnan(mid[i]):
            continue
        prev = sw
        if c[i] > mid[i] + swk * a[i]:
            sw = 1
        elif c[i] < mid[i] - swk * a[i]:
            sw = -1
        up, dn = sw == 1 and prev == -1, sw == -1 and prev == 1
        if up:
            pend, pb = 1, i
        elif dn:
            pend, pb = -1, i
        if pend != 0 and i - pb > wait:
            pend = 0
        vok = (not volc) or math.isnan(vavg[i]) or vavg[i] <= 0 or v[i] >= volk * vavg[i]
        bs = pend == 1 and sw == 1 and vok and shown != 1
        ss = pend == -1 and sw == -1 and vok and shown != -1
        if bs or ss:
            pend = 0
        if bs:
            shown = 1
        elif ss:
            shown = -1
        ent[i] = bs
        ex[i] = ss if exitm == "sinyal" else dn
    return ent, ex


def sig_matrix(d, p):
    """Matrix bulutu: VWMA(ohlc4, fast) > SMA(ohlc4, 20) yesil. trend=lt: fiyat likidite cizgisinin ustunde olmali.
    Giris: kosul ilk saglandigi mum. Cikis: bulut kirmiziya donunce."""
    o, h, l, c, v = _arr(d)
    f, sl = int(p.get("fast", 12)), int(p.get("slow", 20))
    src = (o + h + l + c) / 4
    vs = pd.Series(v)
    fast = (pd.Series(src * v).rolling(f).sum() / vs.rolling(f).sum().replace(0, np.nan)).values
    slow = _sma(src, sl)
    bull = fast > slow
    ok = bull & (c > liq_mid(c, int(p.get("hlen", 50)))) if p.get("trend", "lt") == "lt" else bull
    ent = ok & ~np.r_[False, ok[:-1]]
    ex = ~bull & np.r_[False, bull[:-1]]
    return ent, ex


def sig_st(d, p):
    o, h, l, c, v = _arr(d)
    n_, m = int(p.get("n", 10)), float(p.get("m", 3.0))
    a = _atr(h, l, c, n_)
    hl2 = (h + l) / 2
    up, dn = hl2 - m * a, hl2 + m * a
    n = len(c)
    dirs = np.zeros(n, int)
    fu, fd, dr = up[0], dn[0], 1
    for i in range(1, n):
        fu = max(up[i], fu) if c[i - 1] > fu else up[i]
        fd = min(dn[i], fd) if c[i - 1] < fd else dn[i]
        if dr == -1 and c[i] > fd:
            dr = 1
        elif dr == 1 and c[i] < fu:
            dr = -1
        dirs[i] = dr
    prev = np.r_[0, dirs[:-1]]
    ent = (dirs == 1) & (prev == -1)
    ex = (dirs == -1) & (prev == 1)
    ent[:n_ * 3] = False
    return ent, ex


SIG = {"mplus": sig_mplus, "matrix": sig_matrix, "st": sig_st}
FAM_NAMES = {"mplus": "Matrix+ BIST (Swing + hacim)", "matrix": "Matrix bulutu", "st": "SuperTrend"}


def parse_spec(s):
    """'mplus:swk=1,vol=1,exit=sinyal' -> ('mplus', {...})"""
    fam, _, rest = s.partition(":")
    p = {}
    for kv in rest.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            k, v = k.strip(), v.strip()
            try:
                p[k] = float(v) if "." in v else int(v)
            except ValueError:
                p[k] = v
    return fam.strip(), p


def spec_str(fam, p):
    return fam + ":" + ",".join(f"{k}={v:g}" if isinstance(v, (int, float)) else f"{k}={v}" for k, v in p.items())


# ============================================================================
# BACKTEST (sadece alis)
# ============================================================================


def _net(entry, exit_):
    return exit_ * (1 - BIST_SLIP) * (1 - BIST_FEE) / (entry * (1 + BIST_SLIP) * (1 + BIST_FEE)) - 1


def last_of_day_mask(idx):
    day = np.array([t.date() for t in idx])
    return np.r_[day[1:] != day[:-1], True]


def backtest(d, ent, ex, eod=False, sym=""):
    """Islemler: (giris_ts, cikis_ts, net_getiri, mum_sayisi, sembol, acik_mi)."""
    o, c = d["open"].values, d["close"].values
    idx = d.index
    lod = last_of_day_mask(idx) if eod else np.zeros(len(c), bool)
    n = len(c)
    out = []
    pos, ei, epx = False, 0, 0.0
    for i in range(WARMUP, n - 1):
        if pos:
            if eod and lod[i]:
                out.append((idx[ei], idx[i], _net(epx, c[i]), i - ei + 1, sym, False))
                pos = False
            elif ex[i]:
                out.append((idx[ei], idx[i + 1], _net(epx, o[i + 1]), i + 1 - ei, sym, False))
                pos = False
            continue
        if ent[i] and not (eod and lod[i]):
            pos, ei, epx = True, i + 1, o[i + 1]
    if pos:
        if eod and lod[n - 1] or not eod:
            out.append((idx[ei], idx[n - 1], _net(epx, c[n - 1]), n - 1 - ei, sym, True))
    return out


def stats(trades):
    if not trades:
        return {"n": 0, "win": 0.0, "avg": 0.0, "t": 0.0, "pf": 0.0, "sum": 0.0, "bars": 0.0}
    r = np.array([t[2] for t in trades])
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    g, b = r[r > 0].sum(), -r[r < 0].sum()
    return {"n": len(r), "win": (r > 0).mean() * 100, "avg": r.mean() * 100,
            "t": r.mean() / sd * math.sqrt(len(r)) if sd > 0 else 0.0,
            "pf": g / b if b > 0 else float("inf"), "sum": r.sum() * 100,
            "bars": float(np.mean([t[3] for t in trades]))}


def portfolio(trades, capital, slots, t_from=None, t_to=None):
    """Esit dilimli portfoy: her islem (gerceklesmis bakiye / slots) TL; dilim doluysa sinyal atlanir."""
    tr = sorted(trades, key=lambda t: t[0])
    eq, open_, curve, taken, skipped = capital, [], [], 0, 0
    peak, mdd = capital, 0.0
    for t in tr:
        open_.sort(key=lambda x: x[0])
        while open_ and open_[0][0] <= t[0]:
            ex_ts, notional, ret = open_.pop(0)
            eq += notional * ret
            peak = max(peak, eq)
            mdd = max(mdd, 1 - eq / peak)
            curve.append((ex_ts, eq))
        if len(open_) >= slots:
            skipped += 1
            continue
        notional = eq / slots
        open_.append((t[1], notional, t[2]))
        taken += 1
    for ex_ts, notional, ret in sorted(open_, key=lambda x: x[0]):
        eq += notional * ret
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
        curve.append((ex_ts, eq))
    years = None
    if t_from is not None and t_to is not None:
        years = max((t_to - t_from).days / 365.25, 1 / 365.25)
    cagr = ((eq / capital) ** (1 / years) - 1) * 100 if years and eq > 0 else None
    return {"final": eq, "ret": (eq / capital - 1) * 100, "cagr": cagr, "mdd": mdd * 100,
            "taken": taken, "skipped": skipped, "curve": curve}


def buy_hold(df, t_from, t_to):
    if df is None:
        return None
    s = df[(df.index >= t_from) & (df.index <= t_to)]["close"]
    return (s.iloc[-1] / s.iloc[0] - 1) * 100 if len(s) > 1 else None


# ============================================================================
# WALK-FORWARD ARASTIRMA
# ============================================================================


def _grid(d):
    keys = list(d)
    return [dict(zip(keys, vals)) for vals in itertools.product(*(d[k] for k in keys))]


GRIDS = {
    "1d": {
        "mplus": _grid({"swk": [0.5, 1.0, 1.5], "vol": [1, 0], "exit": ["sinyal", "kirilim"], "hlen": [50]}),
        "matrix": _grid({"fast": [12, 20], "trend": ["lt", "off"]}),
        "st": [{"n": 10, "m": 3.0}, {"n": 10, "m": 2.0}, {"n": 7, "m": 3.0}],
    },
    "1h": {
        "mplus": _grid({"swk": [0.25, 0.5, 0.75, 1.0], "vol": [1, 0], "exit": ["sinyal", "kirilim"], "hlen": [20, 30, 50]}),
        "matrix": _grid({"fast": [12, 20], "trend": ["lt", "off"]}),
        "st": [{"n": 10, "m": 3.0}, {"n": 10, "m": 2.0}, {"n": 7, "m": 3.0}],
    },
}
INDICATOR_DEFAULT = {"1d": {"swk": 1.0, "vol": 1, "exit": "sinyal", "hlen": 50},       # indikatorun otomatik ayari
                     "1h": {"swk": 0.75, "vol": 1, "exit": "sinyal", "hlen": 30}}
TF_NAMES = {"1d": "GÜN SONU (günlük mum, pozisyon günlerce tutulur)",
            "1h": "GÜN İÇİ (1 saatlik mum, akşam her şey satılır)"}


def run_trades(data, fam, p, eod):
    out = []
    for sym, d in data.items():
        ent, ex = SIG[fam](d, p)
        out += backtest(d, ent, ex, eod=eod, sym=sym)
    return out


def research(progress=None):
    say = progress or (lambda m: log.info(m))
    t_start = time.time()
    rng_d = f"{BIST_DAILY_YEARS}y"
    data = {"1d": {}, "1h": {}}
    for i, s in enumerate(BIST_SYMBOLS):
        d1 = fetch(s, "1d", rng_d)
        h1 = fetch(s, "60m", "730d")
        if d1 is not None and len(d1) > WARMUP + 100:
            data["1d"][s] = d1
        if h1 is not None and len(h1) > WARMUP + 100:
            data["1h"][s] = h1
        if (i + 1) % 10 == 0:
            say(f"BIST veri {i + 1}/{len(BIST_SYMBOLS)}")
        time.sleep(0.3)
    bench = {"1d": fetch(BIST_BENCH, "1d", rng_d), "1h": fetch(BIST_BENCH, "60m", "730d")}
    results = {}
    for tf in ("1d", "1h"):
        dd = data[tf]
        if len(dd) < 5:
            results[tf] = {"error": f"veri yok ({len(dd)} hisse)"}
            continue
        eod = tf == "1h"
        t0 = min(d.index[WARMUP] for d in dd.values())
        t1 = max(d.index[-1] for d in dd.values())
        split = t0 + (t1 - t0) * BIST_TRAIN_FRAC
        fams = {}
        for fam, grid in GRIDS[tf].items():
            best = None
            for p in grid:
                tr = run_trades(dd, fam, p, eod)
                trn = [t for t in tr if t[0] < split]
                st = stats(trn)
                score = st["t"] if st["n"] >= BIST_MIN_TRADES else -99 + st["n"] / 1000
                if best is None or score > best[0]:
                    best = (score, p, tr, st)
            _, p, tr, st_tr = best
            tst = [t for t in tr if t[0] >= split]
            fams[fam] = {"p": p, "train": st_tr, "test": stats(tst),
                         "pf_test": portfolio(tst, BIST_CAPITAL, BIST_SLOTS, split, t1),
                         "pf_all": portfolio(tr, BIST_CAPITAL, BIST_SLOTS, t0, t1)}
            say(f"BIST {tf} {fam}: secilen {p}")
        # indikatorun kendi ayari (optimizasyonsuz)
        trd = run_trades(dd, "mplus", INDICATOR_DEFAULT[tf], eod)
        ind = {"all": stats(trd), "test": stats([t for t in trd if t[0] >= split]),
               "pf_test": portfolio([t for t in trd if t[0] >= split], BIST_CAPITAL, BIST_SLOTS, split, t1)}
        # karsilastirma: endeks ve esit agirlikli al-tut
        ew = [x for x in (buy_hold(d, split, t1) for d in dd.values()) if x is not None]
        results[tf] = {"fams": fams, "ind": ind, "t0": t0, "t1": t1, "split": split, "n_sym": len(dd),
                       "bench_test": buy_hold(bench[tf], split, t1), "ew_test": float(np.mean(ew)) if ew else None}
    results["_secs"] = time.time() - t_start
    return results


def _f(x, nd=1, sign=True):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "-"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def _tl(x):
    return f"{x:,.0f}".replace(",", ".") + " TL"


def format_report(res):
    parts = [f"🇹🇷 BIST strateji testi bitti ({res.get('_secs', 0) / 60:.0f} dk)\n"
             f"Sadece alış · komisyon %{BIST_FEE * 100:.2f} + kayma %{BIST_SLIP * 100:.2f} (her yön) · "
             f"{_tl(BIST_CAPITAL)}, en fazla {BIST_SLOTS} hisse aynı anda · işlem sinyalden sonraki mumun açılışında.\n"
             f"Ayar ilk %{BIST_TRAIN_FRAC * 100:.0f}'lik dönemde seçildi, SON dönemde (hiç görmediği veri) sınandı."]
    winners = {}
    for tf in ("1d", "1h"):
        r = res.get(tf, {})
        if "error" in r:
            parts.append(f"\n━━ {TF_NAMES[tf]} ━━\n⚠️ {r['error']}")
            continue
        fmt = "%d.%m.%Y"
        lines = [f"\n━━ {TF_NAMES[tf]} ━━",
                 f"{r['n_sym']} hisse · veri {r['t0']:{fmt}} → {r['t1']:{fmt}} · doğrulama {r['split']:{fmt}} sonrası",
                 f"Karşılaştırma (doğrulama döneminde al-tut): {BIST_BENCH} {_f(r['bench_test'])}% · "
                 f"hisselerin ortalaması {_f(r['ew_test'])}%"]
        bh = max([x for x in (r["bench_test"], r["ew_test"]) if x is not None], default=0.0)
        best = None
        for fam, x in r["fams"].items():
            te, tr, pt = x["test"], x["train"], x["pf_test"]
            ok = te["n"] >= BIST_MIN_TRADES and te["t"] >= BIST_MIN_T and pt["ret"] > bh
            mark = "✅" if ok else ("🟡" if te["t"] >= 1.5 and te["avg"] > 0 else "❌")
            lines.append(f"\n{mark} {FAM_NAMES[fam]}\n   ayar: {spec_str(fam, x['p'])}\n"
                         f"   eğitim: {tr['n']} işlem · ort {_f(tr['avg'], 2)}% · t {tr['t']:.1f}\n"
                         f"   DOĞRULAMA: {te['n']} işlem · kazanan %{te['win']:.0f} · ort {_f(te['avg'], 2)}%/işlem · "
                         f"t {te['t']:.1f} · PF {_f(te['pf'], 2, False)} · ort {te['bars']:.0f} mum\n"
                         f"   {_tl(BIST_CAPITAL)} → {_tl(pt['final'])} ({_f(pt['ret'])}%"
                         + (f", yıllık {_f(pt['cagr'])}%" if pt['cagr'] is not None and tf == '1d' else "")
                         + f") · en büyük düşüş %{pt['mdd']:.1f}")
            if ok and (best is None or te["t"] > best[1]):
                best = (fam, te["t"], x["p"])
        i = r["ind"]
        lines.append(f"\n📐 İndikatörün kendi ayarı ({spec_str('mplus', INDICATOR_DEFAULT[tf])}; optimizasyon yok):\n"
                     f"   tüm dönem {i['all']['n']} işlem · ort {_f(i['all']['avg'], 2)}% · t {i['all']['t']:.1f} | "
                     f"doğrulama {i['test']['n']} işlem · ort {_f(i['test']['avg'], 2)}% · t {i['test']['t']:.1f} · "
                     f"portföy {_f(i['pf_test']['ret'])}%")
        if best:
            winners[tf] = spec_str(best[0], best[2])
        parts.append("\n".join(lines))
    tail = ["\n━━ SONUÇ ━━",
            "✅ = doğrulamada anlamlı kâr (t ≥ %.1f) VE endeksi/al-tut'u geçti · 🟡 = umut verici ama yetersiz · ❌ = geçmedi"
            % BIST_MIN_T,
            "Not: TL bazlı; enflasyon yüzünden al-tut zaten yükseliyor, asıl ölçü onu geçmek."]
    if winners:
        env = []
        if "1d" in winners:
            env.append(f"BIST_DAILY={winners['1d']}")
        if "1h" in winners:
            env.append(f"BIST_INTRA={winners['1h']}")
        tail.append("Canlı kağıt takip için Render'a ekle:\n" + "\n".join(env) + "\nBIST_LIVE=true")
    else:
        tail.append("Hiçbir strateji iki şartı birden geçemedi. Kağıt takip yine de indikatör ayarıyla açılabilir "
                    "(BIST_LIVE=true) ama gerçek para önerilmez.")
    parts.append("\n".join(tail))
    return parts


def run_and_report(bot):
    try:
        bot.send_telegram(f"🇹🇷 BIST strateji testi başladı: {len(BIST_SYMBOLS)} hisse, günlük ({BIST_DAILY_YEARS} yıl) "
                          f"+ 1 saatlik (2 yıl), 3 strateji. 10-30 dk sürebilir...")
        res = research()
        for part in format_report(res):
            for chunk in _chunks(part):
                bot.send_telegram(chunk)
        log.info("BIST arastirma bitti:\n%s", "\n".join(format_report(res)))
    except Exception as e:
        log.exception("BIST arastirma hatasi: %s", e)
        bot.send_telegram(f"⚠️ BIST testi çalıştırılamadı: {e}")


def _chunks(text, n=3800):
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > n:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return out


# ============================================================================
# CANLI KAGIT TAKIP
# ============================================================================
S = {}
_lock = threading.RLock()
_RD = {"ok": False}
LASTPX = {}                                     # sembol -> son fiyat (gecikmeli)


def _new_book(cap):
    return {"cash": cap, "start": cap, "pos": {}, "pend": [], "trades": [], "last_bar": {}}


def _new_state():
    return {"books": {"gs": _new_book(BIST_CAPITAL * BIST_DAILY_FRAC),
                      "gi": _new_book(BIST_CAPITAL * (1 - BIST_DAILY_FRAC))},
            "gi_marker": None, "gs_day": None, "gi_report_day": None, "started": time.time()}


BOOK_NAMES = {"gs": "Gün sonu", "gi": "Gün içi"}


def _equity_cost(b):
    return b["cash"] + sum(p["qty"] * p["entry"] * (1 + BIST_FEE + BIST_SLIP) for p in b["pos"].values())


def _equity_live(b):
    v = b["cash"]
    for s, p in b["pos"].items():
        px = LASTPX.get(s, p["entry"])
        v += p["qty"] * px * (1 - BIST_FEE - BIST_SLIP)
    return v


def _buy(b, sym, px, t, spec_tf):
    if sym in b["pos"] or len(b["pos"]) >= BIST_SLOTS:
        return None
    notional = min(_equity_cost(b) / BIST_SLOTS, b["cash"])
    unit = px * (1 + BIST_SLIP) * (1 + BIST_FEE)
    qty = int(notional // unit)
    if qty <= 0:
        return None
    b["cash"] -= qty * unit
    b["pos"][sym] = {"qty": qty, "entry": px, "t": t, "cost": qty * unit}
    return b["pos"][sym]


def _sell(b, sym, px, t, reason):
    p = b["pos"].pop(sym, None)
    if not p:
        return None
    got = p["qty"] * px * (1 - BIST_SLIP) * (1 - BIST_FEE)
    b["cash"] += got
    tr = {"sym": sym, "qty": p["qty"], "entry": p["entry"], "exit": px, "t_in": p["t"], "t_out": t,
          "pnl": got - p["cost"], "ret": (got / p["cost"] - 1) * 100, "reason": reason}
    b["trades"].append(tr)
    b["trades"] = b["trades"][-2000:]
    return tr


def _fmt_px(x):
    return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _ts_txt(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M")


def process_book(bot, key, data, fam, p, closed_until, eod):
    """data: sembol -> mumlar (son mum yarim olabilir). closed_until: bu zamandan once BITEN mumlar kapali sayilir.
    Sinyal kapali mumda; islem sinyal mumundan sonraki ilk mumun acilisinda (varsa hemen, yoksa bekler)."""
    b = S["books"][key]
    msgs = []
    tf_min = 1440 if key == "gs" else 60
    for sym, d in data.items():
        if d is None or len(d) < WARMUP:
            continue
        LASTPX[sym] = float(d["close"].iloc[-1])
        end = d.index + pd.Timedelta(minutes=tf_min if key == "gi" else 0)
        closed = d[end <= closed_until] if key == "gi" else d[d.index < closed_until]
        if len(closed) < WARMUP:
            continue
        i = len(closed) - 1
        ts = closed.index[i].timestamp()
        if b["last_bar"].get(sym, 0) < ts:
            b["last_bar"][sym] = ts
            ent, ex = SIG[fam](closed, p)
            lod = eod and _is_last_bar_of_day(closed.index[i])
            if sym in b["pos"]:
                if lod:
                    tr = _sell(b, sym, float(closed["close"].iloc[i]), ts, "gün sonu")
                    if tr:
                        msgs.append(_sell_msg(key, tr))
                elif ex[i]:
                    b["pend"] = [x for x in b["pend"] if x["sym"] != sym] + [{"sym": sym, "side": "S", "t": ts}]
            elif ent[i] and not lod and not any(x["sym"] == sym for x in b["pend"]):
                b["pend"].append({"sym": sym, "side": "B", "t": ts})
        # bekleyen emirleri sinyal mumundan sonraki ilk mumun acilisinda doldur
        rest = []
        for x in b["pend"]:
            if x["sym"] != sym:
                rest.append(x)
                continue
            after = d[d.index.map(lambda z: z.timestamp()) > x["t"]]
            if not len(after):
                rest.append(x)
                continue
            px, t_fill = float(after["open"].iloc[0]), after.index[0].timestamp()
            if x["side"] == "B":
                pos = _buy(b, sym, px, t_fill, key)
                if pos:
                    msgs.append(f"🇹🇷 BIST {BOOK_NAMES[key]} · 🟢 AL {sym} {pos['qty']} lot @ {_fmt_px(px)} TL "
                                f"(≈{_tl(pos['cost'])})")
            else:
                tr = _sell(b, sym, px, t_fill, "SAT sinyali")
                if tr:
                    msgs.append(_sell_msg(key, tr))
        b["pend"] = rest
    return msgs


def _sell_msg(key, tr):
    e = "✅" if tr["pnl"] >= 0 else "🔻"
    pnl = f"{tr['pnl']:+,.0f}".replace(",", ".")
    return (f"🇹🇷 BIST {BOOK_NAMES[key]} · 🔴 SAT {tr['sym']} {tr['qty']} lot @ {_fmt_px(tr['exit'])} TL "
            f"({tr['reason']}) · {e} {pnl} TL ({tr['ret']:+.2f}%)")


def _is_last_bar_of_day(ts):
    """Saatlik mum gunun son mumu mu (17:00 ve sonrasi baslayan mumlar)."""
    return ts.hour >= 17


def _save(bot, remote=False):
    with _lock:
        payload = json.dumps(S, ensure_ascii=False, default=float)
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(payload)
    except Exception:
        pass
    if remote and bot._gist_enabled():
        def push():
            if not _RD["ok"]:
                try:
                    data = _fetch_remote(bot)
                    _RD["ok"] = True
                    if data and sum(len(b.get("trades", [])) for b in data.get("books", {}).values()) > \
                            sum(len(b["trades"]) for b in S["books"].values()):
                        with _lock:
                            S.update(data)
                        bot.log.warning("BIST: Gist sonradan okundu, kayitli gecmis geri yuklendi")
                        return
                except Exception as e:
                    bot.log.warning("BIST: Gist okunamiyor, uzerine yazilmadi: %s", e)
                    return
            try:
                requests.patch(f"https://api.github.com/gists/{bot.GIST_ID}", headers=bot._gist_headers(),
                               json={"files": {STATE_FILE: {"content": payload}}}, timeout=30)
            except Exception:
                pass
        threading.Thread(target=push, daemon=True).start()


def _fetch_remote(bot):
    r = requests.get(f"https://api.github.com/gists/{bot.GIST_ID}", headers=bot._gist_headers(), timeout=30)
    r.raise_for_status()
    f = r.json().get("files", {}).get(STATE_FILE)
    if not f:
        return None
    txt = f.get("content") or ""
    if f.get("truncated") and f.get("raw_url"):
        rr = requests.get(f["raw_url"], headers=bot._gist_headers(), timeout=60)
        rr.raise_for_status()
        txt = rr.text
    return json.loads(txt) if txt.strip() else None


def _load(bot):
    data = None
    if bot._gist_enabled():
        try:
            data = _fetch_remote(bot)
            _RD["ok"] = True
        except Exception as e:
            bot.log.warning("BIST durumu Gist'ten okunamadi: %s", e)
    if data is None and os.path.exists(STATE_FILE):
        try:
            data = json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:
            data = None
    S.clear()
    S.update(_new_state())
    if data:
        S.update(data)


def _send_all(bot, msgs):
    if msgs:
        for chunk in _chunks("\n".join(msgs)):
            bot.send_telegram(chunk)


def _fetch_many(syms, interval, rng):
    out = {}
    for s in syms:
        out[s] = fetch(s, interval, rng, retries=2)
        time.sleep(0.2)
    return out


def step(bot, now=None, fetcher=None):
    """Bir kontrol turu. Testlerde now/fetcher verilebilir."""
    now = now or datetime.now(TZ)
    fetcher = fetcher or _fetch_many
    if now.weekday() >= 5:
        return
    hm = now.hour * 60 + now.minute
    dirty = False
    fam_i, p_i = parse_spec(BIST_INTRA)
    fam_d, p_d = parse_spec(BIST_DAILY)
    eff = now - timedelta(minutes=BIST_DELAY_MIN)          # verinin ulastigi gercek zaman
    # ---- GUN ICI: her saat kapanisinda (10:00-18:00 mumlari) ----
    if 10 * 60 + BIST_DELAY_MIN <= hm <= 18 * 60 + 45:
        marker = eff.replace(minute=0, second=0, microsecond=0).isoformat()
        if S.get("gi_marker") != marker:
            data = fetcher(BIST_SYMBOLS, "60m", "120d")
            got = sum(1 for d in data.values() if d is not None and len(d))
            with _lock:
                msgs = process_book(bot, "gi", data, fam_i, p_i, pd.Timestamp(eff), eod=True)
                if got >= 0.8 * len(BIST_SYMBOLS):
                    S["gi_marker"] = marker
            _send_all(bot, msgs)
            dirty = True
    # ---- GUN SONU: aksam 18:50'den sonra gunluk kapanislarla sinyal; sabah acilista emirler ----
    today = now.date().isoformat()
    b = S["books"]["gs"]
    if hm >= 18 * 60 + 50 and S.get("gs_day") != today:
        data = fetcher(BIST_SYMBOLS, "1d", "2y")
        with _lock:
            before = {x["sym"]: x["side"] for x in b["pend"]}
            msgs = process_book(bot, "gs", data, fam_d, p_d, pd.Timestamp(now.date() + timedelta(days=1), tz=TZ),
                                eod=False)
            S["gs_day"] = today
            newp = [x for x in b["pend"] if before.get(x["sym"]) != x["side"]]
        if newp:
            al = [x["sym"] for x in newp if x["side"] == "B"]
            sat = [x["sym"] for x in newp if x["side"] == "S"]
            msgs.append("🇹🇷 BIST Gün sonu sinyalleri (yarın açılışta):"
                        + (f"\n🟢 AL: {', '.join(al)}" if al else "") + (f"\n🔴 SAT: {', '.join(sat)}" if sat else ""))
        msgs.append(summary_text(short=True))
        _send_all(bot, msgs)
        dirty = True
    elif b["pend"] and 10 * 60 + BIST_DELAY_MIN <= hm <= 18 * 60 and now.minute % 10 == 0:
        syms = sorted({x["sym"] for x in b["pend"]})
        data = fetcher(syms, "1d", "2y")
        with _lock:
            msgs = process_book(bot, "gs", data, fam_d, p_d, pd.Timestamp(now.date(), tz=TZ), eod=False)
        _send_all(bot, msgs)
        dirty = True
    if dirty:
        _save(bot, remote=True)


def summary_text(short=False):
    if not S:
        return "BIST kağıt takip başlamadı."
    lines = []
    tot_live, tot_start = 0.0, 0.0
    for key in ("gs", "gi"):
        b = S["books"][key]
        eq = _equity_live(b)
        tot_live += eq
        tot_start += b["start"]
        tr = b["trades"]
        w = sum(1 for t in tr if t["pnl"] > 0)
        lines.append(f"📒 {BOOK_NAMES[key]}: {_tl(eq)} ({(eq / b['start'] - 1) * 100:+.2f}%) · {len(tr)} işlem"
                     + (f" · kazanan %{w / len(tr) * 100:.0f}" if tr else ""))
        if not short:
            for s, p in b["pos"].items():
                px = LASTPX.get(s, p["entry"])
                lines.append(f"   • {s} {p['qty']} lot · giriş {_fmt_px(p['entry'])} · şimdi {_fmt_px(px)} · "
                             f"{(px / p['entry'] - 1) * 100:+.2f}% · {_ts_txt(p['t'])}")
            for x in b["pend"]:
                lines.append(f"   ⏳ {'AL' if x['side'] == 'B' else 'SAT'} {x['sym']} (açılışta)")
    head = f"🇹🇷 BIST kağıt takip · toplam {_tl(tot_live)} ({(tot_live / tot_start - 1) * 100:+.2f}%)"
    if not short:
        head += f"\nGün sonu: {BIST_DAILY}\nGün içi: {BIST_INTRA}"
    return head + "\n" + "\n".join(lines)


def api_data():
    with _lock:
        if not S:
            return {"enabled": False}
        out = {"enabled": True, "daily": BIST_DAILY, "intra": BIST_INTRA, "books": {}}
        for key in ("gs", "gi"):
            b = S["books"][key]
            out["books"][key] = {
                "name": BOOK_NAMES[key], "start": b["start"], "equity": _equity_live(b), "cash": b["cash"],
                "pos": [{"sym": s, "qty": p["qty"], "entry": p["entry"], "last": LASTPX.get(s, p["entry"]),
                         "t": p["t"]} for s, p in b["pos"].items()],
                "pend": b["pend"], "trades": b["trades"][-200:]}
        return out


def run(bot):
    _load(bot)
    bot.log.info("BIST kagit takip basladi: gun sonu %s | gun ici %s | %d hisse", BIST_DAILY, BIST_INTRA,
                 len(BIST_SYMBOLS))
    bot.send_telegram(f"🇹🇷 BIST kağıt takip başladı · {_tl(BIST_CAPITAL)} "
                      f"(gün sonu {_tl(BIST_CAPITAL * BIST_DAILY_FRAC)} + gün içi "
                      f"{_tl(BIST_CAPITAL * (1 - BIST_DAILY_FRAC))}) · {len(BIST_SYMBOLS)} hisse · /bist ile durum")
    while True:
        try:
            step(bot)
        except Exception as e:
            bot.log.exception("BIST dongu hatasi: %s", e)
        time.sleep(60 - datetime.now().second % 60 + 2)


def start(bot):
    started = False
    if RUN_BIST_RESEARCH:
        threading.Thread(target=run_and_report, args=(bot,), daemon=True, name="bist_research").start()
        started = True
    if BIST_LIVE:
        threading.Thread(target=run, args=(bot,), daemon=True, name="bist_live").start()
        started = True
        try:
            import flip_live
            flip_live.EXTRA_CMDS["/bist"] = lambda: summary_text()
            flip_live.start_commands(bot)
        except Exception as e:
            bot.log.warning("BIST /bist komutu baglanamadi: %s", e)
    return started


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    r = research(progress=print)
    print("\n".join(format_report(r)))
