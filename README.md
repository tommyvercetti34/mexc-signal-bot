# MEXC Futures Sinyal Botu – Kurulum Rehberi

Bu bot MEXC USDT-M perpetual (futures) piyasalarını 15 dakikalık mumlarla tarar ve **VWAP + ADX + Stochastic RSI** koşulları sağlandığında Telegram kanalına sinyal gönderir. **Emir açmaz**, API anahtarı istemez; sadece herkese açık piyasa verisini kullanır.

Dosyalar:
- `main.py` – botun kendisi
- `requirements.txt` – Python bağımlılıkları

---

## 1. Telegram botunu hazırla (5 dk)

1. Telegram'da **@BotFather**'a yaz → `/newbot` → isim ve kullanıcı adı ver.
2. Verdiği **token**'ı kaydet (örnek: `7123456789:AAH...`). Bu `TELEGRAM_BOT_TOKEN` olacak.
3. Kanalını aç → **Yöneticiler → Yönetici ekle** → botunu ekle, "Mesaj gönderme" yetkisi ver.
4. **Chat ID**'yi bul:
   - Kanal herkese açıksa: doğrudan `@kanal_adin` yazabilirsin.
   - Özel kanalsa: kanala herhangi bir mesaj at, sonra tarayıcıda
     `https://api.telegram.org/bot<TOKEN>/getUpdates` adresini aç. Yanıtta `"chat":{"id":-100xxxxxxxxxx` şeklinde bir sayı görürsün. Bu `TELEGRAM_CHAT_ID` olacak (başındaki `-100` dahil).

---

## 2. Kodu GitHub'a yükle

1. github.com'da yeni bir **private** repo aç (örn. `mexc-signal-bot`).
2. "Add file → Upload files" ile `main.py` ve `requirements.txt` dosyalarını yükle, commit et.

> Token'ı **asla** koda yazma; bir sonraki adımda ortam değişkeni olarak gireceğiz.

---

## 3. Render'da ücretsiz çalıştır (önerilen)

Render'ın ücretsiz planında "Background Worker" yok, ama **ücretsiz Web Service** var. Bot bu yüzden içinde küçük bir sağlık sunucusu çalıştırır (`PORT` değişkeni gelince otomatik açılır).

**Kısıt:** Ücretsiz web servisi 15 dakika boyunca dışarıdan istek almazsa uykuya geçer. Bunu 4. adımdaki ücretsiz ping servisiyle çözüyoruz. Ayda 750 saat ücretsiz hak var; 7/24 çalışan **tek** servis buna sığar (ikinci bir ücretsiz servis açma).

### Adımlar
1. render.com → GitHub ile kayıt ol.
2. **New + → Web Service** → reponu seç.
3. Ayarlar:
   | Alan | Değer |
   |---|---|
   | Region | **Frankfurt (EU Central)** (Türkiye'ye yakın, MEXC erişimi için ABD bölgelerinden daha güvenli) |
   | Runtime | Python 3 |
   | Build Command | `pip install -r requirements.txt` |
   | Start Command | `python main.py` |
   | Instance Type | **Free** |
4. **Environment Variables** bölümüne ekle:
   | Key | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | BotFather token'ın |
   | `TELEGRAM_CHAT_ID` | `-100...` veya `@kanal_adin` |
   | `PYTHON_VERSION` | `3.12.7` |
5. **Create Web Service**. Loglarda şunu görmelisin:
   `Saglik sunucusu 0.0.0.0:10000 uzerinde calisiyor` → `Tarama basladi: 50 sembol`
   Telegram'a da "🤖 Sinyal botu başlatıldı" mesajı düşer.
6. Sayfanın üstündeki servis URL'sini kopyala (örn. `https://mexc-signal-bot.onrender.com`). Tarayıcıda açınca `OK` ve son tarama zamanını göstermeli.

---

## 4. Uyumasını engelle (cron-job.org, ücretsiz)

1. cron-job.org → ücretsiz hesap aç.
2. **Create cronjob**:
   - URL: Render servis URL'in
   - Schedule: **Every 10 minutes**
3. Kaydet. Artık servis hiç 15 dakika boşta kalmaz ve bot 7/24 taramaya devam eder.

(UptimeRobot'un ücretsiz 5 dakikalık HTTP monitörü de aynı işi görür.)

---

## 5. Railway / Replit hakkında dürüst not

- **Railway:** Yeni hesaba 30 günlük tek seferlik 5$ deneme kredisi veriyor; sonrasında ücretsiz plan ayda yalnızca 1$ kredi içeriyor. Bu, 7/24 çalışan bir servis için yetmez. Deneme süresinde kullanmak istersen: New Project → Deploy from GitHub → aynı ortam değişkenlerini gir → Start Command `python main.py`. Railway'de PORT gerekmez, bot "worker" modunda çalışır. Kalıcı kullanım için Hobby planı (5$/ay) gerekir.
- **Replit:** Ücretsiz Repl'ler sekme kapanınca durur. 7/24 çalışma için ücretli "Deployment" gerekir; bu bot için önermiyorum.

**Sonuç:** Tamamen ücretsiz 7/24 için **Render + cron-job.org** kombinasyonu en mantıklısı.

---

## 6. Önce bilgisayarında test et (isteğe bağlı)

```bash
pip install -r requirements.txt

# Telegram'a göndermeden, sadece konsola yaz:
DRY_RUN=true python main.py          # macOS/Linux
set DRY_RUN=true && python main.py   # Windows CMD
```

---

## 7. Ayarlar (Render → Environment'tan değiştirilebilir, kod değişmez)

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `TOP_N_SYMBOLS` | 50 | 24s hacme göre taranacak coin sayısı |
| `MIN_QUOTE_VOLUME_USDT` | 5000000 | Bu hacmin altındaki coinler atlanır |
| `EXCLUDE_SYMBOLS` | – | Hariç tutulacak coinler, örn. `USDC,PEPE` |
| `TIMEFRAME` | 15m | Zaman dilimi |
| `ADX_THRESHOLD` | 25 | Altındaysa piyasa yatay sayılır, sinyal yok |
| `STOCH_OVERSOLD` / `STOCH_OVERBOUGHT` | 20 / 80 | Stoch RSI bölgeleri |
| `LEVERAGE` | 15 | Kaldıraç (likidasyon kontrolü buna göre yapılır) |
| `ENTRY_LEVELS` | 3 | Kademe sayısı (`1` → tek giriş) |
| `ENTRY_WEIGHTS` | 20,30,50 | Her kademe, işlem bütçesinin yüzde kaçı |
| `LEVEL_MODE` | support | `support` = destek/direnç seviyeleri, `atr` = sabit ATR adımı |
| `PIVOT_K` / `LEVEL_LOOKBACK` | 5 / 400 | Destek tespiti: pivot genişliği / kaç mum geriye bakılır |
| `MAX_LEVEL_DEPTH_ATR` | 4 | En derin kademe girişten en fazla kaç ATR uzakta |
| `MIN_TIER_GAP_ATR` / `MIN_TIER_GAP_PCT` | 0.5 / 0.3 | Kademeler arası en az mesafe |
| `SL_BUFFER_ATR` | 0.5 | Stop, son desteğin bu kadar ATR ötesinde |
| `MAX_SL_FROM_AVG_PCT` | 2.5 | Hepsi dolarsa ortalamadan stopa en fazla % (15x'te ~%37 teminat) |
| `MIN_SL_PCT` | 0.4 | ... ve en az % |
| `MMR_PCT` / `LIQ_BUFFER_PCT` | 0.6 / 0.5 | Likidasyon hesabı: bakım teminatı / güvenlik payı |
| `TP1_ATR` / `TP2_ATR` | 1.5 / 3 | Hedefler, ortalama girişten kaç ATR uzakta |
| `TP1_CLOSE_PCT` | 50 | Hedef 1'de pozisyonun yüzde kaçı kapansın |
| `MOVE_SL_TO_BE` | true | Hedef 1 sonrası stop ortalama girişe çekilsin |
| `BE_TRIGGER_ATR` | 1 | Fiyat ortalamadan bu kadar ATR lehe gidince stop girişe çekilir (0 = kapalı) |
| `BE_FEE_BUFFER_PCT` | 0.05 | Başa baş stopu girişin bu kadar % kârlı tarafına konur (komisyonu karşılar) |
| `SIGNAL_COOLDOWN_MIN` | 60 | Aynı coin için tekrar sinyal bekleme süresi (dk) |
| `VWAP_ANCHOR_TZ` | UTC | VWAP'ın günlük sıfırlandığı saat dilimi (TradingView varsayılanı UTC) |
| `SCAN_DELAY_SEC` | 15 | Mum kapanışından kaç sn sonra taransın |
| `USE_SUPERTREND` | true | LONG için SuperTrend yukarı (yeşil), SHORT için aşağı (kırmızı) olmalı |
| `ST_PERIOD` / `ST_MULT` | 10 / 3 | SuperTrend ayarları |
| `ACCOUNT_BALANCE` | 2000 | Hesap bakiyesi ($); mesajdaki teminat tutarları buna göre |
| `MAX_OPEN_TRADES` | 2 | Aynı anda en fazla açık işlem |
| `SLOT_PCT` | 0 | İşlem başı ayrılan bakiye %'si (0 = 100 ÷ MAX_OPEN_TRADES) |
| `COMPOUND` | true | Kapanan işlemlerin $ K/Z'si bakiyeye eklensin |
| `FEE_MAKER` / `FEE_TAKER` | 0 / 0.0002 | $ K/Z hesabındaki komisyon |
| `NOTIFY_ENTRY_FILLS` | true | 2./3. kademe dolunca mesaj gönder |
| `TRACK_TRADES` | true | Sinyalleri izle, TP / Stop olunca mesaj gönder |
| `MAX_TRADE_HOURS` | 48 | Bu süre içinde TP/Stop gelmezse "süre doldu" diye kapat |
| `ONE_TRADE_PER_SYMBOL` | true | Açık işlem varken aynı coine yeni sinyal verme |
| `SEND_CHART` | true | Sinyalle birlikte grafik görseli gönder (`false` → sadece metin) |
| `CHART_BARS` | 80 | Grafikte gösterilen mum sayısı |
| `CHART_EMA_FAST` / `CHART_EMA_SLOW` | 50 / 200 | Grafikteki EMA çizgileri (sadece görsel, sinyali etkilemez) |
| `DRY_RUN` | false | `true` → Telegram'a göndermez |
| `SEND_STARTUP_MESSAGE` | true | Açılışta Telegram'a bilgi mesajı |

---

## 8a. Çoklu strateji

Bot aynı anda birden fazla stratejiyi, farklı zaman dilimlerinde çalıştırabilir. Hangilerinin açık olacağı `STRATEGIES` ayarıyla seçilir (virgülle ayrılmış):

| Kimlik | Strateji | Zaman dilimi |
|---|---|---|
| `breakout_1h` | Trend kırılımı | 1 saat (varsayılan) |
| `breakout_4h` | Trend kırılımı | 4 saat |
| `pullback_1h` | Trend içi geri çekilme | 1 saat |
| `pullback_4h` | Trend içi geri çekilme | 4 saat |
| `reversal_15m` | Dönüş sinyali + BTC/1s filtre | 15 dk |
| `reversal_1h` | Dönüş sinyali + BTC filtre | 1 saat |
| `scalein_15m` | Eski kademeli dönüş sistemi | 15 dk |

- Bot her 15 dakikalık kapanışta, o an kapanan zaman dilimlerinin stratejilerini tarar (15 dk her seferinde, 1 saat saat başı, 4 saat 00/04/08/12/16/20 UTC'de).
- **Slotlar ortaktır:** Tüm stratejiler toplamda en fazla `MAX_OPEN_TRADES` (2) işlem açar, aynı coine tek işlem. Aynı anda birden fazla aday varsa önce büyük zaman dilimi, sonra trend gücü (ADX) seçilir.
- Her işlem %1 risk bazlı (stop olursa bakiyenin %1'i), kaldıraç stop mesafesine göre otomatik.
- **Parametreler** `STRATEGY_PARAMS` ile JSON olarak verilir. Örnek: `{"breakout_4h":{"lookback":55,"sl":3,"tp":[3,9],"be":1.5}}`. Araştırma raporu, geçen stratejiler için bu iki satırı (`STRATEGIES` ve `STRATEGY_PARAMS`) hazır olarak verir; Key/Value olarak Render'a kopyalaman yeterli.
- Dashboard'da strateji bazında sonuç tablosu vardır.
- Araştırma ve canlı bot aynı sinyal fonksiyonlarını kullanır; test edilen ile çalışan birebir aynıdır.

## 8. Strateji nasıl işliyor?

Bot iki strateji içerir; `STRATEGY` ayarıyla seçilir. **Varsayılan: `breakout` (trend kırılımı)**. Bu strateji araştırmada (bölüm 9b) hiç görmediği veriyle yapılan doğrulamayı geçen tek stratejidir.

### Trend kırılımı (`STRATEGY=breakout`, 1 saat) — varsayılan
Her saat başı (mum kapanışından ~15 sn sonra), sadece kapanmış mumlarla:
- **LONG:** Kapanış, önceki 20 mumun en yükseğinin üstünde + fiyat EMA200 üstünde + ADX ≥ 20 + BTC 4 saatlik kapanışı EMA50 üstünde (yukarı rejim). **SHORT:** tersi.
- **Giriş:** Piyasa fiyatından tek giriş. **Stop:** 2,5 ATR. **Hedef 1:** 2 ATR (%50 kapat) · **Hedef 2:** 5 ATR.
- **Başa baş:** Fiyat 1 ATR lehe gidince stop girişe çekilir. Hedef 1'den sonra da stop girişte kalır.
- **Pozisyon büyüklüğü (risk bazlı):** Stop olursa bakiyenin `RISK_PCT`'si (varsayılan %1 → 2000$'da 20$) kaybedilir. Kaldıraç en fazla 15x; stop likidasyondan önce gelecek şekilde gerekirse otomatik düşürülür. Aynı anda en fazla 2 işlem. En uzun işlem süresi 72 saat.

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `STRATEGY` | breakout | `breakout` = trend kırılımı, `reversal` = eski dönüş sistemi |
| `RISK_PCT` | 1 | İşlem başı risk (bakiyenin %'si) |
| `BO_LOOKBACK` | 20 | Kaç mumluk tepe/dip kırılımı |
| `BO_ADX_MIN` | 20 | En az trend gücü |
| `BO_SL_ATR` | 2.5 | Stop mesafesi (ATR) |
| `BO_TP1_ATR` / `BO_TP2_ATR` | 2 / 5 | Hedefler (ATR) |
| `BO_BE_ATR` | 1 | Bu kadar ATR kârda stop girişe (0 = kapalı) |
| `BO_MAX_HOURS` | 72 | En uzun işlem süresi |
| `BTC_REGIME_EMA` | 50 | BTC 4 saatlik rejim filtresi EMA uzunluğu |

> Araştırma sonucu (90 gün, 29 coin): doğrulamada PF 1,18 · 84 işlem · %77 kazanma · %+3,5 · maks. düşüş %3,8. Avantaj küçüktür ve 84 işlem istatistiksel olarak sınırlıdır. Gerçek parayla başlamadan önce birkaç hafta kâğıt üzerinde takip edin.

### Dönüş sistemi (`STRATEGY=reversal`, 15 dk) — eski sistem
Araştırmada doğrulamayı **geçemedi**; sadece karşılaştırma için duruyor. `RUN_BACKTEST` bu sistemi test eder.

Dönüş sisteminin kuralları:

**Sinyal (tetik):** ADX(14) ≥ 25 · Stoch RSI (14,14,3,3) 20 altında yukarı (LONG) / 80 üstünde aşağı (SHORT) kesişim · LONG için fiyat VWAP üstünde, SHORT için altında · SuperTrend (10,3) aynı yönde.

**Kademeli giriş (%20 / %30 / %50, limit emir):**
- Giriş 1 = sinyal mumunun kapanışı.
- Giriş 2 ve 3 = altındaki (SHORT'ta üstündeki) **pivot destek/direnç** seviyelerinin hemen önü. Destek bulunamazsa ATR adımı kullanılır. En derin kademe girişten en fazla 4 ATR uzakta olur.
- **Stop** = son kademenin dayandığı desteğin 0,5 ATR ötesinde.

**15x likidasyon kontrolü (her sinyalde):** Her kademe dolduğunda oluşacak yeni ortalamanın likidasyon fiyatı hesaplanır. Bir sonraki kademe ve stop, likidasyondan önce gelmek zorundadır. Hepsi dolarsa ortalamadan stopa mesafe %2,5'i geçemez. Bu kontrolleri geçemeyen sinyal gönderilmez.

**Başa baş stop:** Fiyat ortalama girişten 1 ATR lehe gidince stop girişe (komisyonu karşılayacak kadar kârlı tarafa) çekilir ve dolmamış kademeler iptal edilir; işlem artık zararla kapanamaz.

**Kademeli kâr alma:** Hedef 1 = ortalama giriş ± 1,5 ATR → pozisyonun %50'si kapanır, stop ortalama girişe çekilir ve dolmayan kademeler iptal edilir. Hedef 2 = ortalama ± 3 ATR → kalan kapanır. Hedefler ortalamaya göre olduğu için her kademe dolumunda bot yeni hedefleri bildirir.

**Pozisyon büyüklüğü:** Aynı anda en fazla 2 işlem. Her işlem bakiyenin yarısını kullanır (2000$ → 1000$: 200$ / 300$ / 500$). Aynı anda birden fazla sinyal gelirse ADX'i en yüksek olan alınır. Mesajda en kötü senaryodaki maksimum zarar ($) yazar.

**Takip mesajları** (sinyal mesajına yanıt olarak): ➕ kademe alındı (yeni ortalama ve hedefler) · 🎯 Hedef 1 alındı (stop girişe) · ✅ Hedef 2 / ✅ Hedef 1 + girişte kapandı · ❌ Stop · ⌛ Süre doldu (48 saat).

## 9. Backtest (geçmiş veriyle test)

`backtest.py` dosyasını da GitHub'a yükle. Sonra:

**Render'da (önerilen):** Environment'a `RUN_BACKTEST` = `true` ekle ve kaydet. Bot açılınca testi arka planda çalıştırır; birkaç dakika sonra Telegram'a rapor ve sermaye eğrisi gelir. Canlı tarama bu sırada devam eder. Rapor geldikten sonra `RUN_BACKTEST`'i sil, yoksa her yeniden başlatmada test tekrar çalışır.

**Bilgisayarda:** `python backtest.py` → rapor konsola yazılır; `backtest_equity.png` ve `backtest_trades.csv` dosyaları oluşur.

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `BT_DAYS` | 30 | Kaç günlük geçmiş |
| `BT_TOP_N` | 30 | Kaç coin (bugünkü hacme göre) |
| `BT_BALANCE` | 2000 | Başlangıç bakiyesi ($) |
| `BT_MAX_OPEN` | 0 | Aynı anda en fazla işlem (0 = botun `MAX_OPEN_TRADES` ayarı) |
| `BT_MAKER_FEE` / `BT_TAKER_FEE` | 0 / 0.0002 | Komisyon oranları (MEXC standart %0 / %0,02; bazı coinlerde daha yüksek) |

Test 4 varyantı karşılaştırır: SuperTrend açık/kapalı × kademeli/tek giriş.

## 9b. Strateji araştırması (walk-forward)

`research.py` dosyasını da GitHub'a yükle. Render → Environment: `RUN_RESEARCH` = `true` → kaydet. Bot açılışta araştırmayı arka planda çalıştırır; 10–20 dakika sonra Telegram'a rapor ve doğrulama dönemi grafiği gelir. Rapor gelince `RUN_RESEARCH`'i sil.

4 strateji ailesi yarışır: trend kırılımı (1 saat), trend içi geri çekilme (1 saat), dönüş sinyali + BTC/1 saat filtresi (15 dk), mevcut sistem (referans). Her ailenin ayarı ilk ~60 günde seçilir, son ~30 günde (strateji bu veriyi hiç görmedi) sınanır. **Geçti** şartı: eğitim ve doğrulamada profit factor ≥ 1.1, doğrulamada ≥ 30 işlem, maksimum düşüş ≤ %20 ve işlem başı getirinin t-istatistiği ≥ 2.5 (sonucun şanstan kaynaklanmadığını gösterir; rastgele veriyle yapılan testlerde yanlış pozitif oranı ~%1). Pozisyonlar risk bazlıdır: stop olursa bakiyenin %1'i kaybedilir.

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `RS_DAYS` | 90 | Kaç günlük veri |
| `RS_TOP_N` | 30 | Kaç coin |
| `RS_RISK_PCT` | 1 | İşlem başı risk (bakiyenin %'si) |
| `RS_TRAIN_FRAC` | 0.67 | Eğitim döneminin oranı |
| `RS_MODE` | standard | `long4h` = sadece 4 saatlik stratejiler, uzun geçmiş, sabit coin listesi · `combo` = seçilmiş stratejileri ayarlarına dokunmadan, daha önce hiç kullanılmamış bir dönemde tek tek ve birlikte test eder |
| `RS_FAMILIES` | (boş = hepsi) | Sadece belirli stratejileri test et, örn. `sweep_15m,sweep_1h` |
| `RS_COMBO` | breakout_1h,pullback_1h,breakout_4h | `combo` modunda test edilecek stratejiler (ayarlar `STRATEGY_PARAMS` ile değiştirilebilir) |
| `RS_COMBO_END_DAYS_AGO` / `RS_COMBO_DAYS` | 180 / 540 | `combo` test penceresi: 180 gün önce biten 540 günlük dönem |
| `RS_LONG_DAYS` | 730 | `long4h` modunda kaç günlük 4 saatlik veri |
| `RS_SYMBOLS` | BTC,ETH,SOL,... | `long4h` modunda kullanılacak sabit coin listesi |
| `RS_EXCHANGE` | mexc | Veri ve coin listesinin alınacağı borsa: `mexc` veya `binance` (USDT-M vadeli) |

## 10. Dashboard

Adres: `https://<servis-adın>.onrender.com/dashboard`

Gösterdikleri: net kâr/zarar, kazanma oranı, işlem sayısı, profit factor, maksimum düşüş, sermaye eğrisi, coin bazında K/Z, açık işlemler (anlık K/Z ile) ve kapanan işlemler (TP / Stop / Süre doldu filtreli). Bakiye ve işlem başı teminat sayfadan değiştirilebilir. Sayfa dakikada bir kendini yeniler.

**Erişimi kilitle (önerilir):** Render'da `DASHBOARD_KEY` = istediğin bir şifre (örn. `k8s2x9`). Sonra adres `.../dashboard?key=k8s2x9` olur; şifresiz açılmaz.

### Geçmişin silinmemesi için: GitHub Gist (ücretsiz)

Render her deploy'da dosyaları siler. İşlem geçmişini Gist'te tutmak için:

1. **Gist oluştur:** gist.github.com → dosya adı `trades.json`, içerik `{}` → **Create secret gist**. Adres çubuğundaki son kısım (`gist.github.com/kullanici/` sonrası) senin **GIST_ID**'n.
2. **Token oluştur:** GitHub → Settings → Developer settings → Personal access tokens → **Tokens (classic)** → Generate new token → sadece **gist** kutusunu işaretle → Generate. Token'ı kopyala.
3. Render → Environment:
   | Key | Value |
   |---|---|
   | `GITHUB_TOKEN` | kopyaladığın token |
   | `GIST_ID` | 1. adımdaki ID |

Bot her işlem açılışında, kademe dolumunda ve kapanışta Gist'e yazar; açılışta oradan yükler. Dashboard'daki sarı uyarı kaybolursa kalıcı kayıt çalışıyor demektir.

## ⚠️ Önemli uyarılar

- Bu bir sinyal aracıdır, yatırım tavsiyesi değildir. Gerçek parayla kullanmadan önce stratejiyi TradingView'da geriye dönük test et veya birkaç hafta `DRY_RUN` / küçük pozisyonla izle.
- Not: Stoch RSI dipte yukarı kesişim (dönüş sinyali) ile "fiyat VWAP üstünde" (trend) şartı aynı anda nadiren oluşur. Bu da sinyal sayısını azaltır. Bu sana verdiğin kuralların bilinçli sonucudur: az ama filtrelenmiş sinyal.
- Render'ın dosya sistemi kalıcı değildir. GitHub Gist ayarlanmadıysa işlem geçmişi ve açık işlemler her **yeni deploy**'da silinir (bkz. bölüm 10).
- Loglarda sürekli `Ag hatasi` görürsen MEXC o bölgeden erişimi kısıtlıyor olabilir; Render'da bölgeyi Frankfurt/Singapur seç.


## HTF Sweep + CISD (deneysel)

`sweep_15m` (15 dk grafik, 1 saatlik üst zaman) ve `sweep_1h` (1 saat grafik, 4 saatlik üst zaman).
Fiyat önceki tamamlanmış üst zaman mumunun tepesini/dibini süpürür; ardından 12 mum içinde, uç noktayı oluşturan
mum serisinin ilk açılışının ötesinde gövdesi ≥ 0,5 ATR bir mum kapanırsa (CISD) ters yönde sinyal verilir.
Stop süpürülen ucun 0,1 ATR ötesindedir; hedefler R cinsindendir (`tp_r`, örn. `[1,2]`). `kz: 1` sadece
Londra (07–10 UTC) ve New York (12–16 UTC) seanslarında sinyal verir. Ayar örneği:
`STRATEGY_PARAMS={"sweep_15m":{"tp_r":[1,3],"kz":1}}`

## TradingView fikirleri — toplu test (deneysel)

Her biri 15 dk (`_15m`) ve 1 saat (`_1h`) olarak: `ha_pb` (Heikin Ashi geri çekilme, gerçek fiyatlarla),
`harmony` (Reversal Pressure), `impulse` (Momentum itki devamı), `diverg` (CCI/RSI uyumsuzluğu, pivot 5 mum gecikmeli),
`ribbon` (ALMA trend şeridi), `tdseq` (TD Sequential 9/13), `cprofile` (Mum profili, fiyat baskısı modu).
Hepsi yapısal stop + R hedefleri kullanır; yazarların tanımlamadığı ayrıntılar kod içindeki açıklamalarda yazılıdır.
Toplu araştırma için:
`RS_FAMILIES=ha_pb_15m,ha_pb_1h,harmony_15m,harmony_1h,impulse_15m,impulse_1h,diverg_15m,diverg_1h,ribbon_15m,ribbon_1h,tdseq_15m,tdseq_1h,cprofile_15m,cprofile_1h`
