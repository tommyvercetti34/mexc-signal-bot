"""
BIST botu: gun sonu (gunluk mum) + gun ici (1 saatlik mum) kagit takip ve walk-forward testi
===========================================================================================
- Sadece ALIS yonlu (BIST'te aciga satis kisitli). SAT = elindekini sat, nakde gec.
- Stratejiler:
    mplus  : Matrix+ BIST indikatorunun Swing sinyali (likidite trend cizgisi kirilimi + hacim onayi)
    matrix : Matrix VWMA bulutu (VWMA(ohlc4) > SMA(ohlc4)) + likidite trend filtresi
    st     : SuperTrend donusu
- Sinyal mum kapanisinda, islem BIR SONRAKI mumun acilisinda (gun sonu: ertesi gun acilis).
- Gun ici defter (15/30/60 dk): cikis = hedef (tp x ATR) / stop (sl x ATR) / ters sinyal / en fazla `maxd` islem gunu.
  Hedef gelmediyse pozisyon ertesi gune sarkar (maxd=1: ayni gun kapanista kapanir).
- Sermaye: islem basina (bakiye / BIST_SLOTS); kar geldikce islem tutari buyur (BIST_COMPOUND=true).
  Iki defter toplam en fazla BIST_SLOTS (5) hisse. BIST_COMPOUND=false: sabit BIST_TRADE_TL.
- Komisyon BIST_FEE (varsayilan onbinde 1) + kayma BIST_SLIP (binde 0,5), alis ve satista ayri ayri.
- Veri: Yahoo Finance (THYAO.IS ...), ~15 dk gecikmeli.
- T+2 takas yok sayilir: satistan gelen para ayni gun yeni alimda kullanilir (toplam bakiye BIST_CAPITAL).

Test : Render'da RUN_BIST_RESEARCH=true   (sonuc Telegram'a gelir)
ABD  : Render'da RUN_US_RESEARCH=true     (en buyuk 100 ABD hissesi, ayni motor)
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
# BIST 100 (Kasim 2026 civari; endeks her ceyrek degisir. Kesin liste icin BIST_SYMBOLS ile ver)
DEFAULT_SYMBOLS = ("AEFES,AGROT,AHGAZ,AKBNK,AKCNS,AKSA,AKSEN,ALARK,ALFAS,ALTNY,ANSGR,ARCLK,ARDYZ,ASELS,ASTOR,AVPGY,BERA,"
                   "BIMAS,BRSAN,BRYAT,BSOKE,BTCIM,CANTE,CCOLA,CIMSA,CWENE,DOAS,DOHOL,DSTKF,ECILC,EGEEN,EKGYO,ENERY,"
                   "ENJSA,ENKAI,EREGL,EUPWR,EUREN,FENER,FROTO,GARAN,GESAN,GOLTS,GRSEL,GUBRF,HALKB,HEKTS,ISCTR,ISMEN,"
                   "IZENR,KARSN,KCAER,KCHOL,KLRHO,KONTR,KONYA,KOZAA,KRDMD,KTLEV,LMKDC,MAGEN,MAVI,MGROS,MIATK,MPARK,"
                   "OBAMS,ODAS,ODINE,OTKAR,OYAKC,PAHOL,PASEU,PETKM,PGSUS,PSGYO,QUAGR,REEDR,RGYAS,SAHOL,SARKY,SASA,SELEC,"
                   "SISE,SKBNK,SMRTG,SOKM,TAVHL,TCELL,THYAO,TKFEN,TMSN,TOASO,TRALT,TRGYO,TRMET,TSKB,TTKOM,TTRAK,TUKAS,"
                   "TUPRS,TURSG,ULKER,VAKBN,VESBE,VESTL,YEOTK,YKBNK,ZOREN")
BIST_SYMBOLS = [s.strip().upper() for s in _env("BIST_SYMBOLS", DEFAULT_SYMBOLS).split(",") if s.strip()]
BIST_BENCH = _env("BIST_BENCH", "XU100")

# ---- ABD hisseleri testi (RUN_US_RESEARCH=true). Ayarlar is parcacigina ozel: BIST canli takibini etkilemez ----
RUN_US_RESEARCH = _env("RUN_US_RESEARCH", False, bool)
US_DEFAULT = ("AAPL,MSFT,NVDA,AMZN,GOOGL,META,AVGO,TSLA,BRK-B,JPM,LLY,V,UNH,XOM,MA,COST,HD,PG,JNJ,WMT,NFLX,ABBV,BAC,"
              "CRM,ORCL,CVX,KO,MRK,AMD,PEP,TMO,ADBE,LIN,ACN,CSCO,MCD,ABT,WFC,DIS,INTU,IBM,GE,TXN,QCOM,AMGN,CAT,VZ,PM,"
              "NOW,ISRG,DHR,SPGI,GS,AXP,NEE,PFE,RTX,UNP,LOW,CMCSA,T,BKNG,HON,UBER,AMAT,BLK,PGR,SYK,COP,ELV,TJX,LMT,"
              "SCHW,BSX,ETN,C,MS,VRTX,PLD,ADP,MDT,CB,PANW,REGN,MU,ADI,SBUX,GILD,BMY,DE,LRCX,KLAC,MMC,SO,CI,ANET,NKE,"
              "UPS,INTC,PLTR")
US_SYMBOLS = [s.strip().upper() for s in _env("US_SYMBOLS", US_DEFAULT).split(",") if s.strip()]
US_CFG = {"market": "us", "symbols": US_SYMBOLS, "bench": _env("US_BENCH", "SPY"), "suffix": "",
          "fee": _env("US_FEE", 0.0, float), "slip": _env("US_SLIP", 0.0003, float),
          "sfee": _env("US_SHORT_FEE", 0.0, float), "title": "🇺🇸 ABD hisse", "short_label": "açığa satış",
          "ccy": "$"}
_TL = threading.local()                         # is parcacigina ozel pazar ayari (bos = BIST)


def _c(key, default):
    cfg = getattr(_TL, "cfg", None)
    return cfg.get(key, default) if cfg else default
BIST_CAPITAL = _env("BIST_CAPITAL", 100000.0, float)        # toplam sermaye (TL)
BIST_TRADE_TL = _env("BIST_TRADE_TL", 20000.0, float)       # BIST_COMPOUND=false iken islem basina sabit tutar
BIST_COMPOUND = _env("BIST_COMPOUND", True, bool)           # true: islem tutari = (baslangic + gerceklesen kar) / yer
BIST_SLOTS = _env("BIST_SLOTS", 5, int)                      # ayni anda en fazla hisse (iki defter ORTAK)
BIST_FEE = _env("BIST_FEE", 0.0001, float)                   # spot komisyon (oran, tek yon): onbinde 1
BIST_SLIP = _env("BIST_SLIP", 0.0005, float)                 # kayma (oran, tek yon)
BIST_FEE_LOW = _env("BIST_FEE_LOW", 0.0, float)              # raporda karsilastirma: sifir komisyon (kayma dahil)
BIST_SHORT_TEST = _env("BIST_SHORT_TEST", True, bool)        # testte short (VIOP, kaldiracsiz) tarafini da dene
BIST_VIOP_FEE = _env("BIST_VIOP_FEE", 0.0003, float)         # VIOP komisyon (oran, tek yon): onbinde 3
BIST_DAILY = _env("BIST_DAILY", "mplus:swk=1,vol=1,exit=sinyal,hlen=50")
BIST_INTRA = _env("BIST_INTRA", "mplus:swk=0.75,vol=1,exit=sinyal,hlen=30,tp=0,sl=0,maxd=3")
BIST_INTRA_TF = _env("BIST_INTRA_TF", "60m")                # 15m | 30m | 60m
INTRA_ON = BIST_INTRA.strip().lower() not in ("off", "kapali", "kapalı", "0", "false")
DAILY_ON = BIST_DAILY.strip().lower() not in ("off", "kapali", "kapalı", "0", "false")
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


def fetch(sym, interval="1d", rng="10y", retries=3, suffix=None):
    """Yahoo Finance'ten BIST mumlari. sym: 'THYAO' ya da 'XU100'. Index Istanbul saati (mum ACILIS zamani)."""
    suffix = _c("suffix", ".IS") if suffix is None else suffix
    ticker = sym if "." in sym or sym.startswith("^") or not suffix else f"{sym}{suffix}"
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
    ex[:n_ * 3] = False
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
# MOTOR (test ve canli AYNI kodu kullanir)
# ============================================================================
EXIT_KEYS = ("tp", "sl", "maxd")
TF_MIN = {"1d": 1440, "60m": 60, "30m": 30, "15m": 15}


def exit_params(p):
    return {"tp": float(p.get("tp", 0)), "sl": float(p.get("sl", 0)), "maxd": int(p.get("maxd", 0))}


def sig_params(p):
    return {k: v for k, v in p.items() if k not in EXIT_KEYS}


def _net(entry, exit_, fee=None, slip=None):
    fee = _c("fee", BIST_FEE) if fee is None else fee
    slip = _c("slip", BIST_SLIP) if slip is None else slip
    return exit_ * (1 - slip) * (1 - fee) / (entry * (1 + slip) * (1 + fee)) - 1


def _net_dir(entry, exit_, short=False, fee=None):
    """Long: spot komisyonu. Short (VIOP, kaldiracsiz: teminat = pozisyon degeri): VIOP komisyonu."""
    if not short:
        return _net(entry, exit_, fee=fee)
    fee = _c("sfee", BIST_VIOP_FEE) if fee is None else fee
    slip = _c("slip", BIST_SLIP)
    return (entry * (1 - slip) * (1 - fee) - exit_ * (1 + slip) * (1 + fee)) / entry


def _gross(t):
    return (t[6] - t[7]) / t[6] if len(t) > 9 and t[9] else t[7] / t[6] - 1


def mirror(d):
    """Fiyat aynasi: short sinyalleri = aynalanmis seride long sinyalleri (tum gostergeler simetrik)."""
    return pd.DataFrame({"open": -d["open"].values, "high": -d["low"].values, "low": -d["high"].values,
                         "close": -d["close"].values, "volume": d["volume"].values}, index=d.index)


class Bars:
    """Bir hissenin mum dizileri + islem gunu sayaci."""

    def __init__(self, d):
        self.idx = d.index
        self.o, self.h, self.l, self.c = (d[k].values for k in ("open", "high", "low", "close"))
        self.atr = _atr(self.h, self.l, self.c, 14)
        v = d["volume"].values
        va = _sma(v, 20)
        with np.errstate(divide="ignore", invalid="ignore"):
            self.vr = np.nan_to_num(np.where(va > 0, v / va, 0.0))        # hacim / 20 mum ort. (oncelik puani)
        self.dord = np.array([t.date().toordinal() for t in d.index])
        newday = np.r_[True, self.dord[1:] != self.dord[:-1]]
        self.dayn = np.cumsum(newday)
        self.lod = np.r_[self.dord[1:] != self.dord[:-1], True]     # gunun son mumu
        self.n = len(self.c)

    def held_days(self, j, d0):
        k = int(np.searchsorted(self.dord, d0))
        return int(self.dayn[j] - self.dayn[min(k, self.n - 1)] + 1)


def eng_fill(st, j, X, P, price=None):
    """Bekleyen emri j mumunun acilisinda doldur. Olay listesi doner."""
    ev = []
    pend = st.get("pend")
    if not pend:
        return ev
    st["pend"] = None
    px = X.o[j] if price is None else price
    if pend["side"] == "B" and not st.get("pos"):
        a = pend.get("atr") or 0.0
        st["pos"] = {"e": px, "tp": px + P["tp"] * a if P["tp"] > 0 and a > 0 else None,
                     "sl": px - P["sl"] * a if P["sl"] > 0 and a > 0 else None,
                     "d0": int(X.dord[j]) if j < X.n else int(X.dord[-1]), "j0": j}
        ev.append(("B", px, ""))
    elif pend["side"] == "S" and st.get("pos"):
        st["pos"] = None
        ev.append(("S", px, "SAT sinyali"))
    return ev


def eng_check(st, j, X, P):
    """Acik pozisyon icin j mumunda stop / hedef / gun siniri."""
    p = st.get("pos")
    if not p:
        return []
    px = reason = None
    if p.get("sl") is not None and X.l[j] <= p["sl"]:              # ayni mumda ikisi de olduysa: once stop (temkinli)
        px, reason = min(X.o[j], p["sl"]), "stop"
    elif p.get("tp") is not None and X.h[j] >= p["tp"]:
        px, reason = max(X.o[j], p["tp"]), "hedef"
    elif P["maxd"] > 0 and X.lod[j] and X.held_days(j, p["d0"]) >= P["maxd"]:
        px, reason = X.c[j], ("gün sonu" if P["maxd"] == 1 else f"{P['maxd']} gün doldu")
    if px is None:
        return []
    st["pos"] = None
    return [("S", float(px), reason)]


def eng_signal(st, j, X, ent, ex):
    if st.get("pos"):
        if ex[j]:
            st["pend"] = {"side": "S"}
    elif ent[j] and not st.get("pend"):
        st["pend"] = {"side": "B", "atr": float(X.atr[j]), "score": float(X.vr[j])}


def backtest(d, ent, ex, P, sym="", X=None, short=False):
    """Islemler: (giris_ts, cikis_ts, net, mum, sembol, acik_mi, giris_px, cikis_px, neden, short_mu).
    short=True: d, X ve sinyaller AYNALANMIS seriden gelir; fiyatlar geri cevrilir."""
    X = X or Bars(d)
    st = {"pos": None, "pend": None}
    out, ein = [], None
    for j in range(WARMUP, X.n):
        sc = st["pend"].get("score", 0.0) if st["pend"] else 0.0
        evs = eng_fill(st, j, X, P) + eng_check(st, j, X, P)
        for kind, px, reason in evs:
            if kind == "B":
                ein = (j, px, sc)
            else:
                e_, x_ = (-ein[1], -px) if short else (ein[1], px)
                if short and reason == "SAT sinyali":
                    reason = "AL sinyali"
                out.append((X.idx[ein[0]], X.idx[j], _net_dir(e_, x_, short), j - ein[0] + 1, sym, False, e_, x_,
                            reason, short, ein[2]))
        eng_signal(st, j, X, ent, ex)
    if st["pos"]:
        e_, x_ = (-ein[1], -X.c[-1]) if short else (ein[1], X.c[-1])
        out.append((X.idx[ein[0]], X.idx[-1], _net_dir(e_, x_, short), X.n - ein[0], sym, True, e_, x_, "açık", short,
                    ein[2]))
    return out


def stats(trades):
    z = {"n": 0, "win": 0.0, "avg": 0.0, "t": 0.0, "pf": 0.0, "sum": 0.0, "bars": 0.0, "gross": 0.0, "low": 0.0}
    if not trades:
        return z
    r = np.array([t[2] for t in trades])
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    g, b = r[r > 0].sum(), -r[r < 0].sum()
    return {"n": len(r), "win": (r > 0).mean() * 100, "avg": r.mean() * 100,
            "t": r.mean() / sd * math.sqrt(len(r)) if sd > 0 else 0.0,
            "pf": g / b if b > 0 else float("inf"), "sum": r.sum() * 100,
            "bars": float(np.mean([t[3] for t in trades])),
            "gross": float(np.mean([_gross(t) for t in trades])) * 100,
            "low": float(np.mean([_net_dir(t[6], t[7], len(t) > 9 and t[9], fee=BIST_FEE_LOW) for t in trades])) * 100}


def portfolio(trades, t_from=None, t_to=None, slots=None, notional=None, capital=None, compound=None):
    """Portfoy: ayni anda en fazla `slots` hisse; dolu ise sinyal atlanir. compound: islem tutari =
    (baslangic + o ana kadar gerceklesen kar) / slots; degilse sabit `notional`."""
    slots, notional, capital = slots or BIST_SLOTS, notional or BIST_TRADE_TL, capital or BIST_CAPITAL
    compound = BIST_COMPOUND if compound is None else compound
    tr = sorted(trades, key=lambda t: (t[0], -(t[10] if len(t) > 10 else 0.0)))   # ayni anda: hacmi guclu once
    pnl, open_, curve, taken, skipped = 0.0, [], [], 0, 0
    peak, mdd = capital, 0.0

    def realize(upto):
        nonlocal pnl, peak, mdd
        open_.sort(key=lambda x: x[0])
        while open_ and (upto is None or open_[0][0] <= upto):
            ex_ts, ret, size = open_.pop(0)
            pnl += size * ret
            eq = capital + pnl
            peak = max(peak, eq)
            mdd = max(mdd, 1 - eq / peak)
            curve.append((ex_ts, eq))

    for t in tr:
        realize(t[0])
        if len(open_) >= slots:
            skipped += 1
            continue
        open_.append((t[1], t[2], (capital + pnl) / slots if compound else notional))
        taken += 1
    realize(None)
    years = max((t_to - t_from).days / 365.25, 1 / 365.25) if t_from is not None and t_to is not None else None
    final = capital + pnl
    if years and compound:
        yearly = ((final / capital) ** (1 / years) - 1) * 100 if final > 0 else -100.0
    else:
        yearly = (pnl / capital * 100 / years) if years else None
    return {"final": final, "ret": pnl / capital * 100, "mdd": mdd * 100, "taken": taken, "skipped": skipped,
            "years": years, "yearly": yearly, "curve": curve}


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


SIG_GRIDS = {
    "1d": {
        "mplus": _grid({"swk": [0.5, 1.0, 1.5], "vol": [1, 0], "exit": ["sinyal", "kirilim"], "hlen": [50]}),
        "matrix": _grid({"fast": [12, 20], "trend": ["lt", "off"]}),
        "st": [{"n": 10, "m": 3.0}, {"n": 10, "m": 2.0}, {"n": 7, "m": 3.0}],
    },
    "intra": {
        "mplus": _grid({"swk": [0.25, 0.5, 1.0], "vol": [1, 0], "exit": ["sinyal", "kirilim"], "hlen": [20, 50]}),
        "matrix": _grid({"fast": [12, 20], "trend": ["lt", "off"]}),
        "st": [{"n": 10, "m": 3.0}, {"n": 10, "m": 2.0}, {"n": 7, "m": 3.0}],
    },
}
EXIT_GRIDS = {"1d": [{"tp": 0, "sl": 0, "maxd": 0}],
              "intra": _grid({"tp": [0, 1.5, 3.0], "sl": [0, 1.5], "maxd": [1, 3]})}
INDICATOR_DEFAULT = {"1d": {"swk": 1.0, "vol": 1, "exit": "sinyal", "hlen": 50},        # indikatorun otomatik ayari
                     "60m": {"swk": 0.75, "vol": 1, "exit": "sinyal", "hlen": 30},
                     "30m": {"swk": 0.5, "vol": 1, "exit": "sinyal", "hlen": 20, "wait": 2},
                     "15m": {"swk": 0.5, "vol": 1, "exit": "sinyal", "hlen": 20, "wait": 2}}
RESEARCH_TFS = [t.strip() for t in _env("BIST_RESEARCH_TFS", "1d,60m,30m,15m").split(",") if t.strip()]
TF_RANGE = {"1d": f"{BIST_DAILY_YEARS}y", "60m": "730d", "30m": "60d", "15m": "60d"}
TF_NAMES = {"1d": "GÜN SONU (günlük mum, pozisyon günlerce tutulur)",
            "60m": "GÜN İÇİ · 60 dk", "30m": "GÜN İÇİ · 30 dk", "15m": "GÜN İÇİ · 15 dk"}


def research(progress=None):
    say = progress or (lambda m: log.info(m))
    t_start = time.time()
    results = {}
    for tf in RESEARCH_TFS:
        dd = {}
        for i, s in enumerate(_c("symbols", BIST_SYMBOLS)):
            d = fetch(s, tf, TF_RANGE[tf], retries=2)
            if d is not None and len(d) > WARMUP + 100:
                dd[s] = d
            time.sleep(0.3)
        say(f"BIST {tf}: {len(dd)} hisse verisi")
        bench = fetch(_c("bench", BIST_BENCH), tf, TF_RANGE[tf], suffix=_c("suffix", ".IS"))
        if len(dd) < 5:
            results[tf] = {"error": f"veri yok ({len(dd)} hisse)"}
            continue
        XS = {s: Bars(d) for s, d in dd.items()}
        t0 = min(d.index[WARMUP] for d in dd.values())
        t1 = max(d.index[-1] for d in dd.values())
        split = t0 + (t1 - t0) * BIST_TRAIN_FRAC
        kind = "1d" if tf == "1d" else "intra"
        fams = {}
        dirs = [(False, dd, XS)]
        if BIST_SHORT_TEST:
            ddm = {s: mirror(d) for s, d in dd.items()}
            dirs.append((True, ddm, {s: Bars(d) for s, d in ddm.items()}))
        for (short, DD, XX), (fam, sgrid) in itertools.product(dirs, SIG_GRIDS[kind].items()):
            best = None
            for sp in sgrid:
                sigs = {s: SIG[fam](d, sp) for s, d in DD.items()}
                for xp in EXIT_GRIDS[kind]:
                    tr = []
                    for s, d in DD.items():
                        tr += backtest(d, sigs[s][0], sigs[s][1], xp, sym=s, X=XX[s], short=short)
                    st = stats([t for t in tr if t[0] < split])
                    score = st["t"] if st["n"] >= BIST_MIN_TRADES else -99 + st["n"] / 1000
                    if best is None or score > best[0]:
                        best = (score, {**sp, **xp}, tr, st)
            _, p, tr, st_tr = best
            tst = [t for t in tr if t[0] >= split]
            fams[fam + ("_short" if short else "")] = {"p": p, "train": st_tr, "test": stats(tst), "tst": tst,
                                                       "pf_test": portfolio(tst, split, t1), "short": short}
            say(f"BIST {tf} {fam}{' short' if short else ''}: secilen {spec_str(fam, p)}")
        # en iyi long + en iyi short birlikte (ortak 5 yer)
        bl = max((x for x in fams.values() if not x["short"]), key=lambda x: x["test"]["t"], default=None)
        bs = max((x for x in fams.values() if x["short"]), key=lambda x: x["test"]["t"], default=None)
        combo = portfolio(bl["tst"] + bs["tst"], split, t1) if bl and bs and bs["test"]["t"] > 0 else None
        ip = INDICATOR_DEFAULT[tf]
        xp = EXIT_GRIDS[kind][0] if kind == "1d" else {"tp": 0, "sl": 0, "maxd": 0}
        trd = []
        for s, d in dd.items():
            e, x = SIG["mplus"](d, ip)
            trd += backtest(d, e, x, xp, sym=s, X=XS[s])
        tst = [t for t in trd if t[0] >= split]
        ind = {"p": {**ip, **xp}, "all": stats(trd), "test": stats(tst), "pf_test": portfolio(tst, split, t1)}
        ew = [x for x in (buy_hold(d, split, t1) for d in dd.values()) if x is not None]
        for x in fams.values():
            x.pop("tst", None)
        results[tf] = {"fams": fams, "ind": ind, "combo": combo, "t0": t0, "t1": t1, "split": split, "n_sym": len(dd),
                       "bench_test": buy_hold(bench, split, t1), "ew_test": float(np.mean(ew)) if ew else None}
        del dd, XS
    results["_secs"] = time.time() - t_start
    return results


def _f(x, nd=1, sign=True):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "-"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def _tl(x):
    return f"{x:,.0f}".replace(",", ".") + " TL"


def _exit_txt(p):
    if p.get("maxd", 0) == 0 and p.get("tp", 0) == 0 and p.get("sl", 0) == 0:
        return "çıkış: sadece SAT sinyali"
    parts = []
    parts.append(f"hedef {p['tp']:g} ATR" if p.get("tp") else "hedef yok")
    parts.append(f"stop {p['sl']:g} ATR" if p.get("sl") else "stop yok")
    parts.append("akşam kapat" if p.get("maxd") == 1 else f"en fazla {p['maxd']} gün" if p.get("maxd") else "gün sınırı yok")
    return "çıkış: " + " · ".join(parts) + " · ters sinyal"


def format_report(res):
    ccy = _c("ccy", "TL")
    money = (lambda x: "$" + f"{x:,.0f}") if ccy == "$" else _tl
    parts = [f"{_c('title', '🇹🇷 BIST')} strateji testi bitti ({res.get('_secs', 0) / 60:.0f} dk)\n"
             f"Başlangıç {money(BIST_CAPITAL)} · aynı anda en fazla {BIST_SLOTS} hisse · işlem başı "
             + (f"bakiye/{BIST_SLOTS} (kâr eklendikçe büyür)" if BIST_COMPOUND else _tl(BIST_TRADE_TL)) + " · "
             f"komisyon %{_c('fee', BIST_FEE) * 100:.2f} + kayma %{_c('slip', BIST_SLIP) * 100:.2f} (her yön) · "
             f"işlem sinyalden sonraki mumun açılışında.\n"
             f"Ayar ilk %{BIST_TRAIN_FRAC * 100:.0f}'lik dönemde seçildi, SON dönemde (hiç görmediği veri) sınandı.\n"
             f"'brüt' = komisyonsuz ve kaymasız, 'düşük kom.' = %{BIST_FEE_LOW * 100:.2f} komisyon + kayma ile işlem başı ortalama."]
    winners = {}
    for tf in RESEARCH_TFS:
        r = res.get(tf, {})
        if "error" in r:
            parts.append(f"\n━━ {TF_NAMES[tf]} ━━\n⚠️ {r['error']}")
            continue
        fmt = "%d.%m.%Y"
        days = (r["t1"] - r["split"]).days
        lines = [f"\n━━ {TF_NAMES[tf]} ━━",
                 f"{r['n_sym']} hisse · veri {r['t0']:{fmt}} → {r['t1']:{fmt}} · doğrulama {r['split']:{fmt}} sonrası "
                 f"({days} gün)",
                 f"Aynı dönemde al-tut: {_c('bench', BIST_BENCH)} {_f(r['bench_test'])}% · hisselerin ortalaması {_f(r['ew_test'])}%"]
        if tf in ("30m", "15m"):
            lines.append("⚠️ Ücretsiz veri bu zaman diliminde sadece son 60 günü veriyor: sonuç kısa döneme dayanıyor, "
                         "temkinli oku.")
        bh = max([x for x in (r["bench_test"], r["ew_test"]) if x is not None], default=0.0)
        longs = [(f, x) for f, x in r["fams"].items() if not x.get("short")]
        shorts = [(f, x) for f, x in r["fams"].items() if x.get("short")]
        rows = longs + [("_ind", r["ind"])] + ([("_hdr", None)] if shorts else []) + shorts
        for fam, x in rows:
            if fam == "_hdr":
                lines.append(f"\n🔻 SHORT tarafı ({_c('short_label', 'VİOP')}, kaldıraçsız; "
                             f"komisyon %{_c('sfee', BIST_VIOP_FEE) * 100:.2f} + kayma, "
                             f"SAT sinyalinde short aç, AL sinyali/hedef/stop/gün sınırında kapat):")
                continue
            te, pt = x["test"], x["pf_test"]
            short = bool(x.get("short"))
            ok_t = te["n"] >= BIST_MIN_TRADES and te["t"] >= BIST_MIN_T
            ok = ok_t and (short or pt["ret"] > bh)
            mark = "✅" if ok else ("🟡" if ok_t or (te["t"] >= 1.5 and te["avg"] > 0) else "❌")
            fam_key = "mplus" if fam == "_ind" else fam.replace("_short", "")
            fam_name = (("SHORT · " if short else "") + FAM_NAMES[fam_key]) if fam != "_ind" else \
                "📐 İndikatörün kendi sinyalleri (optimizasyonsuz)"
            yr = (f", yılda ~{_f(pt['yearly'])}%" if pt.get("yearly") is not None and days >= 180 else "")
            tr_line = (f"   eğitim: {x['train']['n']} işlem · ort {_f(x['train']['avg'], 2)}% · t {x['train']['t']:.1f}\n"
                       if "train" in x else "")
            lines.append(f"\n{mark} {fam_name}\n   ayar: {spec_str(fam_key, x['p'])}\n   {_exit_txt(x['p'])}\n" + tr_line +
                         f"   DOĞRULAMA: {te['n']} işlem · kazanan %{te['win']:.0f} · ort {_f(te['avg'], 2)}% "
                         f"(brüt {_f(te['gross'], 2)}%, düşük kom. {_f(te['low'], 2)}%) · t {te['t']:.1f} · "
                         f"PF {_f(te['pf'], 2, False)} · ort {te['bars']:.0f} mum\n"
                         f"   {money(BIST_CAPITAL)} → {money(pt['final'])} (dönemde {_f(pt['ret'])}%{yr}) · "
                         f"en büyük düşüş %{pt['mdd']:.1f}" + (f" · {pt['skipped']} sinyal yer yok diye atlandı"
                                                             if pt["skipped"] else ""))
            if ok and fam != "_ind":
                cand = (te["t"], spec_str(fam_key, x["p"]), tf)
                key = ("short_" if short else "") + ("1d" if tf == "1d" else "intra")
                if key not in winners or cand[0] > winners[key][0]:
                    winners[key] = cand
        c = r.get("combo")
        if c:
            lines.append(f"\n🔀 En iyi long + en iyi short BİRLİKTE (ortak {BIST_SLOTS} yer): {money(BIST_CAPITAL)} → "
                         f"{money(c['final'])} (dönemde {_f(c['ret'])}%) · en büyük düşüş %{c['mdd']:.1f}")
        parts.append("\n".join(lines))
    tail = ["\n━━ SONUÇ ━━",
            f"✅ = doğrulamada anlamlı kâr (t ≥ {BIST_MIN_T:g}) VE al-tut'u geçti · 🟡 = kâr anlamlı ama al-tut'u geçemedi "
            f"ya da umut verici · ❌ = geçmedi",
            ("Not: dolar bazlı. Test bugünkü en büyük 100 ABD hissesiyle yapıldı: geçmişte bunların kazanan olacağı belli "
             "değildi, sonuçlar (al-tut dahil) olduğundan iyi görünür." if _c("market", "tr") == "us" else
             "Not: TL bazlı. Enflasyon yüzünden hisseler zaten yükseliyor; asıl ölçü al-tut'u geçmek.")]
    if winners and _c("market", "tr") == "us":
        tail.append("✅ Geçen strateji var: Tradier / TradeStation kâğıt hesabına bağlanıp gerçek kurumda sanal parayla "
                    "denenebilir. Geçenler: " + " | ".join(f"{'SHORT ' if k.startswith('short_') else ''}{v[2]}: {v[1]}"
                                                         for k, v in winners.items()))
    elif winners:
        env = []
        if "1d" in winners:
            env.append(f"BIST_DAILY={winners['1d'][1]}")
        if "intra" in winners:
            env += [f"BIST_INTRA={winners['intra'][1]}", f"BIST_INTRA_TF={winners['intra'][2]}"]
        if env:
            tail.append("Canlı kâğıt takip için Render'a ekle:\n" + "\n".join(env) + "\nBIST_LIVE=true")
        sh = [f"{'gün sonu' if k.endswith('1d') else 'gün içi ' + v[2]}: {v[1]}" for k, v in winners.items()
              if k.startswith("short_")]
        if sh:
            tail.append("🔻 Short tarafında geçen: " + " | ".join(sh) + "\n(Canlı short kâğıt takibini istersen ekleyebilirim.)")
    else:
        tail.append("Hiçbir strateji iki şartı birden geçemedi. Kâğıt takip açılabilir ama gerçek para önerilmez.")
    parts.append("\n".join(tail))
    return parts


def run_and_report(bot):
    try:
        bot.send_telegram(f"🇹🇷 BIST strateji testi başladı: {len(BIST_SYMBOLS)} hisse · {', '.join(RESEARCH_TFS)} · "
                          f"3 strateji + hedef/stop/gün sınırı" + (" · long + short (VİOP)" if BIST_SHORT_TEST else "") +
                          ". Hisse sayısına göre 30-120 dk sürebilir...")
        res = research()
        for part in format_report(res):
            for chunk in _chunks(part):
                bot.send_telegram(chunk)
        log.info("BIST arastirma bitti:\n%s", "\n".join(format_report(res)))
    except Exception as e:
        log.exception("BIST arastirma hatasi: %s", e)
        bot.send_telegram(f"⚠️ BIST testi çalıştırılamadı: {e}")


def run_us_and_report(bot):
    """ABD hisseleri testi: ayni motor, ABD ayarlari (sadece bu is parcaciginda gecerli)."""
    _TL.cfg = US_CFG
    try:
        bot.send_telegram(f"🇺🇸 ABD hisse strateji testi başladı: {len(US_SYMBOLS)} hisse · {', '.join(RESEARCH_TFS)} · "
                          f"long + short · komisyon %{US_CFG['fee'] * 100:.2f}, kayma %{US_CFG['slip'] * 100:.2f}. "
                          f"30-120 dk sürebilir...")
        res = research()
        for part in format_report(res):
            for chunk in _chunks(part):
                bot.send_telegram(chunk)
        log.info("ABD arastirma bitti:\n%s", "\n".join(format_report(res)))
    except Exception as e:
        log.exception("ABD arastirma hatasi: %s", e)
        bot.send_telegram(f"⚠️ ABD testi çalıştırılamadı: {e}")
    finally:
        _TL.cfg = None


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
BOOK_NAMES = {"gs": "Gün sonu", "gi": "Gün içi"}


def _new_book():
    return {"pos": {}, "pend": {}, "trades": [], "last_bar": {}, "realized": 0.0}


def _new_state():
    return {"v": 2, "books": {"gs": _new_book(), "gi": _new_book()},
            "gi_marker": None, "gs_day": None, "started": time.time()}


def _trade_size():
    """Yeni islemin tutari: bilesikte (baslangic + gerceklesen kar) / yer sayisi."""
    if not BIST_COMPOUND:
        return BIST_TRADE_TL
    return max(0.0, (BIST_CAPITAL + sum(b["realized"] for b in S["books"].values())) / BIST_SLOTS)


def _n_open():
    return sum(len(b["pos"]) for b in S["books"].values())


def _unreal(b):
    return sum(p["qty"] * LASTPX.get(s, p["e"]) * (1 - BIST_FEE - BIST_SLIP) - p["cost"] for s, p in b["pos"].items())


def _book_pnl(b):
    return b["realized"] + _unreal(b)


def _equity_live():
    return BIST_CAPITAL + sum(_book_pnl(b) for b in S["books"].values())


def _fmt_px(x):
    return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _ts_txt(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M")


def _sell_msg(key, tr):
    e = "✅" if tr["pnl"] >= 0 else "🔻"
    pnl = f"{tr['pnl']:+,.0f}".replace(",", ".")
    return (f"🇹🇷 BIST {BOOK_NAMES[key]} · 🔴 SAT {tr['sym']} {tr['qty']} lot @ {_fmt_px(tr['exit'])} TL "
            f"({tr['reason']}) · {e} {pnl} TL ({tr['ret']:+.2f}%)")


def _apply(key, sym, st, evs, t, msgs):
    """Motor olaylarini deftere isle (lot, maliyet, ortak 5 yer siniri)."""
    b = S["books"][key]
    for kind, px, reason in evs:
        if kind == "B":
            unit = px * (1 + BIST_SLIP) * (1 + BIST_FEE)
            qty = int(_trade_size() // unit)
            if _n_open() >= BIST_SLOTS or qty <= 0:
                st["pos"] = None
                msgs.append(f"🇹🇷 BIST {BOOK_NAMES[key]} · ⏭ {sym} AL sinyali atlandı (aynı anda {BIST_SLOTS} hisse dolu)")
                continue
            st["pos"].update({"qty": qty, "cost": qty * unit, "t": t})
            b["pos"][sym] = st["pos"]
            msgs.append(f"🇹🇷 BIST {BOOK_NAMES[key]} · 🟢 AL {sym} {qty} lot @ {_fmt_px(px)} TL (≈{_tl(qty * unit)})"
                        + (f" · hedef {_fmt_px(st['pos']['tp'])}" if st["pos"].get("tp") else "")
                        + (f" · stop {_fmt_px(st['pos']['sl'])}" if st["pos"].get("sl") else ""))
        else:
            p = b["pos"].pop(sym, None)
            if not p:
                continue
            got = p["qty"] * px * (1 - BIST_SLIP) * (1 - BIST_FEE)
            tr = {"sym": sym, "qty": p["qty"], "entry": p["e"], "exit": px, "t_in": p["t"], "t_out": t,
                  "pnl": got - p["cost"], "ret": (got / p["cost"] - 1) * 100, "reason": reason}
            b["realized"] += tr["pnl"]
            b["trades"].append(tr)
            b["trades"] = b["trades"][-2000:]
            msgs.append(_sell_msg(key, tr))


def process_book(key, data, spec, tf_min, closed_until):
    """data: sembol -> mumlar (son mum yarim olabilir). 1) Kapali mumlar motordan gecer. 2) Bekleyen emirler sonraki
    (yarim) mumun acilisinda doldurulur; yer azsa hacmi en guclu sinyal once. Mesaj listesi doner."""
    fam, p = parse_spec(spec)
    P, SP = exit_params(p), sig_params(p)
    b = S["books"][key]
    msgs, later = [], []
    for sym, d in data.items():
        if d is None or len(d) < WARMUP:
            continue
        LASTPX[sym] = float(d["close"].iloc[-1])
        if tf_min >= 1440:
            closed = d[d.index < closed_until]
        else:
            closed = d[d.index + pd.Timedelta(minutes=tf_min) <= closed_until]
        if len(closed) < WARMUP:
            continue
        X = Bars(closed)
        ent, ex = SIG[fam](closed, SP)
        st = {"pos": b["pos"].get(sym), "pend": b["pend"].get(sym)}
        last = b["last_bar"].get(sym)
        ts_arr = np.array([t.timestamp() for t in closed.index])
        js = [X.n - 1] if last is None else list(np.nonzero(ts_arr > last)[0][-300:])   # ilk calisma: sadece son mum
        for j in js:
            t = float(ts_arr[j])
            _apply(key, sym, st, eng_fill(st, j, X, P), t, msgs)
            _apply(key, sym, st, eng_check(st, j, X, P), t, msgs)
            eng_signal(st, j, X, ent, ex)
            b["last_bar"][sym] = t
        after = d[d.index > closed.index[-1]]
        if st["pend"] and len(after):
            later.append((sym, st, after))
        _store(b, sym, st)
    # 2) bekleyen emirler: once satislar (yer acar), sonra alislar hacim puanina gore
    later.sort(key=lambda x: (x[1]["pend"]["side"] != "S", -x[1]["pend"].get("score", 0.0)))
    for sym, st, after in later:
        Xp = Bars(after.iloc[:1])
        evs = eng_fill(st, 0, Xp, P, price=float(after["open"].iloc[0]))
        _apply(key, sym, st, evs, after.index[0].timestamp(), msgs)
        _store(b, sym, st)
    return msgs


def _store(b, sym, st):
    if st["pend"]:
        b["pend"][sym] = st["pend"]
    else:
        b["pend"].pop(sym, None)
    if st["pos"] is None:
        b["pos"].pop(sym, None)


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
                    if data and data.get("v") == 2 and sum(len(x.get("trades", [])) for x in data["books"].values()) > \
                            sum(len(x["trades"]) for x in S["books"].values()):
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
    if data and data.get("v") == 2:                    # eski (v1) kayit yeni yapiyla uyumsuz: temiz baslanir
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
    tfm = TF_MIN.get(BIST_INTRA_TF, 60)
    eff = now - timedelta(minutes=BIST_DELAY_MIN)          # verinin ulastigi gercek zaman
    # ---- GUN ICI: her mum kapanisinda (10:00-18:00 seansi) ----
    if INTRA_ON and 10 * 60 + BIST_DELAY_MIN <= hm <= 18 * 60 + 45:
        mins = eff.hour * 60 + eff.minute
        marker = eff.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=mins - mins % tfm)
        marker = marker.isoformat()
        if S.get("gi_marker") != marker:
            data = fetcher(BIST_SYMBOLS, BIST_INTRA_TF, "60d" if tfm < 60 else "120d")
            got = sum(1 for d in data.values() if d is not None and len(d))
            with _lock:
                msgs = process_book("gi", data, BIST_INTRA, tfm, pd.Timestamp(eff))
                if got >= 0.8 * len(BIST_SYMBOLS):
                    S["gi_marker"] = marker
            _send_all(bot, msgs)
            dirty = True
    # ---- GUN SONU: aksam 18:50'den sonra gunluk kapanislarla sinyal; sabah acilista emirler ----
    today = now.date().isoformat()
    b = S["books"]["gs"]
    if not DAILY_ON:
        pass
    elif hm >= 18 * 60 + 50 and S.get("gs_day") != today:
        data = fetcher(BIST_SYMBOLS, "1d", "2y")
        with _lock:
            before = {k: v["side"] for k, v in b["pend"].items()}
            msgs = process_book("gs", data, BIST_DAILY, 1440, pd.Timestamp(now.date() + timedelta(days=1), tz=TZ))
            S["gs_day"] = today
            newp = {k: v["side"] for k, v in b["pend"].items() if before.get(k) != v["side"]}
        if newp:
            al = [k for k, v in newp.items() if v == "B"]
            sat = [k for k, v in newp.items() if v == "S"]
            msgs.append("🇹🇷 BIST Gün sonu sinyalleri (yarın açılışta):"
                        + (f"\n🟢 AL: {', '.join(al)}" if al else "") + (f"\n🔴 SAT: {', '.join(sat)}" if sat else ""))
        msgs.append(summary_text(short=True))
        _send_all(bot, msgs)
        dirty = True
    elif b["pend"] and 10 * 60 + BIST_DELAY_MIN <= hm <= 18 * 60 and now.minute % 10 == 0:
        data = fetcher(sorted(b["pend"]), "1d", "2y")
        with _lock:
            msgs = process_book("gs", data, BIST_DAILY, 1440, pd.Timestamp(now.date(), tz=TZ))
        _send_all(bot, msgs)
        dirty = True
    if dirty:
        _save(bot, remote=True)


def summary_text(short=False):
    if not S:
        return "BIST kâğıt takip başlamadı."
    eq = _equity_live()
    lines = [f"🇹🇷 BIST kâğıt takip · {_tl(eq)} ({(eq / BIST_CAPITAL - 1) * 100:+.2f}%) · "
             f"{_n_open()}/{BIST_SLOTS} hisse açık · sıradaki işlem {_tl(_trade_size())}"]
    if not short:
        lines.append(f"Gün sonu: {BIST_DAILY if DAILY_ON else 'kapalı'}\n"
                     f"Gün içi: {(BIST_INTRA_TF + ' · ' + BIST_INTRA) if INTRA_ON else 'kapalı'}")
    for key in ("gs", "gi"):
        b = S["books"][key]
        tr = b["trades"]
        w = sum(1 for t in tr if t["pnl"] > 0)
        lines.append(f"📒 {BOOK_NAMES[key]}: K/Z {_book_pnl(b):+,.0f} TL".replace(",", ".") + f" · {len(tr)} işlem"
                     + (f" · kazanan %{w / len(tr) * 100:.0f}" if tr else ""))
        if not short:
            for s, p in b["pos"].items():
                px = LASTPX.get(s, p["e"])
                lines.append(f"   • {s} {p['qty']} lot · giriş {_fmt_px(p['e'])} · şimdi {_fmt_px(px)} · "
                             f"{(px / p['e'] - 1) * 100:+.2f}% · {_ts_txt(p['t'])}"
                             + (f" · hedef {_fmt_px(p['tp'])}" if p.get("tp") else "")
                             + (f" · stop {_fmt_px(p['sl'])}" if p.get("sl") else ""))
            for s, x in b["pend"].items():
                lines.append(f"   ⏳ {'AL' if x['side'] == 'B' else 'SAT'} {s} (sonraki açılışta)")
    return "\n".join(lines)


def api_data():
    with _lock:
        if not S:
            return {"enabled": False}
        out = {"enabled": True, "daily": BIST_DAILY if DAILY_ON else "kapalı",
               "intra": f"{BIST_INTRA_TF} · {BIST_INTRA}" if INTRA_ON else "kapalı",
               "capital": BIST_CAPITAL, "equity": _equity_live(), "slots": BIST_SLOTS, "open": _n_open(),
               "trade_tl": _trade_size(), "compound": BIST_COMPOUND, "books": {}}
        for key in ("gs", "gi"):
            b = S["books"][key]
            out["books"][key] = {
                "name": BOOK_NAMES[key], "pnl": _book_pnl(b), "realized": b["realized"],
                "pos": [{"sym": s, "qty": p["qty"], "entry": p["e"], "last": LASTPX.get(s, p["e"]), "t": p["t"],
                         "tp": p.get("tp"), "sl": p.get("sl")} for s, p in b["pos"].items()],
                "pend": [{"sym": s, "side": x["side"]} for s, x in b["pend"].items()],
                "trades": b["trades"][-200:]}
        return out


def run(bot):
    _load(bot)
    bot.log.info("BIST kagit takip basladi: gun sonu %s | gun ici %s %s | %d hisse", BIST_DAILY, BIST_INTRA_TF,
                 BIST_INTRA, len(BIST_SYMBOLS))
    bot.send_telegram(f"🇹🇷 BIST kâğıt takip başladı · {_tl(BIST_CAPITAL)} · işlem başı "
                      + (f"bakiye/{BIST_SLOTS} (kâr eklendikçe büyür)" if BIST_COMPOUND else _tl(BIST_TRADE_TL)) + ", "
                      f"aynı anda en fazla {BIST_SLOTS} hisse · {len(BIST_SYMBOLS)} hisse izleniyor · /bist ile durum")
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
    if RUN_US_RESEARCH:
        threading.Thread(target=run_us_and_report, args=(bot,), daemon=True, name="us_research").start()
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
