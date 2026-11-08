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
| `ATR_SL_MULT` | 1.5 | Stop mesafesi = ATR × bu katsayı |
| `RR_RATIO` | 1.5 | Hedef = risk × bu oran |
| `MAX_SL_PCT` | 4.0 | Stop %'si bundan büyükse sinyal gönderilmez (15x'te tasfiye ≈ %6,6) |
| `MIN_SL_PCT` | 0.25 | Çok dar (gürültüye takılacak) stopları eler |
| `LEVERAGE` | 15 | Mesajdaki örnek kaldıraç |
| `SIGNAL_COOLDOWN_MIN` | 60 | Aynı coin için tekrar sinyal bekleme süresi (dk) |
| `VWAP_ANCHOR_TZ` | UTC | VWAP'ın günlük sıfırlandığı saat dilimi (TradingView varsayılanı UTC) |
| `SCAN_DELAY_SEC` | 15 | Mum kapanışından kaç sn sonra taransın |
| `USE_SUPERTREND` | true | LONG için SuperTrend yukarı (yeşil), SHORT için aşağı (kırmızı) olmalı |
| `ST_PERIOD` / `ST_MULT` | 10 / 3 | SuperTrend ayarları |
| `ENTRY_LEVELS` | 3 | Kademe sayısı (`1` → tek giriş) |
| `ENTRY_STEP_ATR` | 0.5 | Kademeler arası mesafe (ATR cinsinden) |
| `ENTRY_WEIGHTS` | 33,33,34 | Her kademenin pozisyondaki % payı |
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

## 8. Strateji nasıl işliyor?

Her 15 dakikalık mum kapandıktan ~15 sn sonra, **sadece kapanmış mumlar** üzerinden (sinyal geriye dönük değişmez):

1. **ADX(14) ≥ 25** değilse → coin atlanır.
2. **Stoch RSI (14,14,3,3)**: K çizgisi D'yi **20'nin altında yukarı** keserse LONG adayı, **80'in üstünde aşağı** keserse SHORT adayı.
3. **VWAP filtresi**: LONG için kapanış VWAP üstünde, SHORT için altında olmalı.
4. **SuperTrend (10,3)** sinyal yönüyle aynı yönde olmalı.
5. **Kademeli giriş:** Giriş 1 = sinyal mumunun kapanışı, Giriş 2 = Giriş 1 ∓ 0.5×ATR, Giriş 3 = Giriş 1 ∓ 1.0×ATR.
   **Stop** = Giriş 1 ∓ 1.5×ATR(14) ve **Hedef** = Giriş 1 ± 1.5×Risk; ikisi de kademelerden bağımsız olarak **sabit** kalır.
   Sadece 1. kademe dolarsa R:R 1.5 olur, üçü de dolarsa ortalama iyileştiği için R:R ~2.8'e çıkar.
6. **Takip:** Bot dakikada bir 1 dakikalık mumlara bakar. Kademe dolunca, TP veya Stop olunca orijinal sinyal mesajına yanıt olarak bildirim gönderir. Aynı dakikada hem TP hem Stop görülürse temkinli davranıp Stop sayar.

Mesajdaki "**%X zarar**", fiyatın stopa kadar hareket yüzdesidir. 15x kaldıraçta teminat kaybı bunun yaklaşık 15 katıdır (örn. %1 stop ≈ %15 teminat kaybı).

---

## ⚠️ Önemli uyarılar

- Bu bir sinyal aracıdır, yatırım tavsiyesi değildir. Gerçek parayla kullanmadan önce stratejiyi TradingView'da geriye dönük test et veya birkaç hafta `DRY_RUN` / küçük pozisyonla izle.
- Not: Stoch RSI dipte yukarı kesişim (dönüş sinyali) ile "fiyat VWAP üstünde" (trend) şartı aynı anda nadiren oluşur. Bu da sinyal sayısını azaltır. Bu sana verdiğin kuralların bilinçli sonucudur: az ama filtrelenmiş sinyal.
- Render'ın dosya sistemi kalıcı değildir. Açık işlemler `trades.json`'a yazılır; uygulama yeniden başlarsa korunur ama **yeni deploy** yapılırsa silinir. Deploy'u açık işlem yokken yap; yoksa o işlemlerin TP/Stop mesajı gelmez.
- Loglarda sürekli `Ag hatasi` görürsen MEXC o bölgeden erişimi kısıtlıyor olabilir; Render'da bölgeyi Frankfurt/Singapur seç.
