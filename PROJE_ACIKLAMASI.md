# Kurumsal Kredi Riski Erken Uyarı Sistemi (EWS): proje açıklaması

> **Bu belge kimin için:** Projeyi ilk kez görecek ve kodu eleştirel gözle inceleyecek bir okuyucu (insan ya da model).
> Motivasyonu, mimariyi, gelişim sırasını, alınan kararları ve gerekçelerini, bilinen sorunları ve açık soruları
> tek yerde toplar. Son güncelleme: 13 Kasım 2026.
>
> **Bu depodaki sürüm:** Kodun ve belgelerin paylaşılabilir kopyasıdır. Banka altyapısına ait adlar (sunucu, şema,
> kullanıcı, proxy, ağ klasörü) `<ORACLE_HOST>`, `<SEMA>` gibi yer tutucularla değiştirildi; müşteri adları ve
> numaraları, portföye ait sayılar çıkarıldı; `ALIAS_RULES` örnek satırlardan oluşuyor. Müşteri verisi içeren
> dosyalar (`portfolio_config.py`, Excel çıktıları, önbellekler) depoda yok.
>
> **Gerçeğin kaynağı koddur.** Bu klasördeki diğer belgelerin bir kısmı eskidir (bkz. §10). Bir belgeyle kod
> çeliştiğinde kod geçerlidir; çelişkiyi bulmak da incelemenin bir parçasıdır.

---

## 1. Bağlam ve motivasyon

### 1.1 Kurum ve amaç
Bir Türk bankasının kredi riski biriminde, kurumsal müşteriler için bir **erken uyarı sistemi (EWS)**. Soru şu:
*"Portföydeki hangi şirkette kredi riskini artıran bir gelişme var ve orada ne kadar riskimiz var?"*

Sistem iki sinyali birleştirir:
- **Haber sinyali:** Türk ekonomi basınında şirket (ya da hakim ortağı) hakkında çıkan haberlerden kanıtlı olaylar
  (konkordato, haciz, TMSF devri, soruşturma, zarar…).
- **Finansal sinyal:** DWH'taki son iki finansal tablodan kural bazlı bir skor (özkaynak, FAVÖK, kur, DSCR,
  likidite, faiz karşılama, trend).

Çıktı bir **temerrüt olasılığı değildir**; analistin önce bakacağı şirketleri sıralayan, her alarmın kanıtını
(haber linki ve alıntı) gösteren bir listedir.

### 1.2 Başlangıç noktası: yöneticinin sistemi
Proje, birim yöneticisinin yazdığı bir pipeline'ın üzerine kuruldu (Ekim 2026 başı):
`1_scaper` (haber toplama) → `2_sql_to_parquet` (Oracle'dan portföy + otomatik alias config) →
`3_scoring` (regex sözlüğüyle haber skoru, Stage 1/2/3) + `ews_financial` (finansal skor). Yaklaşık 4.100 satır
kod ve 10 bin satırlık üretilmiş bir config.

Yöneticinin dile getirdiği üç sorun, öncelik sırasıyla:
1. **Yanlış eşleşme.** Örnek: "ANKARA 2" gibi bir alias "Ankara 2. Asliye Ceza Mahkemesi" ile eşleşiyordu.
2. **Skor doğrulanamıyor.** Temerrüt ya da rating düşüşü gibi bir hedef değişken yok; regex puanının doğru olup
   olmadığı bilinmiyor.
3. **Alias bakımı elle.** 1.000 şirketin 677'sinde onay bekleyen alias adayı vardı.

Yöneticinin genel tercihi: **kısa, sade, anlaşılır kod.** Uzun (1.000-2.000 satırlık) betikleri sevmiyor.

### 1.3 Kısıtlar (tasarımı belirleyen)
| Kısıt | Sonucu |
|---|---|
| Yalnızca bankanın iç LLM platformu kullanılabilir: ~27B parametreli açık kaynak bir model (OpenAI uyumlu `/v1/chat/completions`). Dışarıdan model indirmek yasak, dış API yok | Bütün LLM işi tek bir 27B modelle; fine-tune yok, embedding modeli yok (bkz. §8.4) |
| Çıktı en fazla 4.096 token; bağlam geniş; uygulama başına varsayılan paralel istek limiti 1 | Prompt'lar kısa, tek görevli; tema sorusu en fazla 300 madde |
| Etiketli veri yok, kimse etiketleme yapamıyor | Puan yerine kanıtlı olay; analist örnek denetimi (Kontrol sayfası); DWH sonuçlarıyla geriye dönük test planlandı ama yapılmadı |
| Veri bankadan çıkamaz | Dashboard tek dosya HTML, dış kütüphane yok; geliştirme ev bilgisayarında (Mac) sahte/yerel veriyle, çalıştırma bankada |
| Geliştirme ile çalıştırma ayrı makinelerde | Kod bankaya `.txt` dosyalarıyla elle (Not Defteri ile) taşınıyor (`ofise_aktar/`); banka çıktıları (Excel) geri gelmiyor, yalnızca kullanıcının aktardığı log satırları ve sayılar geliyor |
| Banka ağı: kurumsal proxy, bazı siteler Cloudflare arkasında | Scraper'da site başına proxy kararı; KAP taraması hukuki nedenle kapalı |
| Oracle kullanıcısı kişisel şema (`<SEMA>`), portföy kişisel bir çalışma tablosundan (`temp7`/`temp8`) geliyor | Kaynak tablonun bakımı yöneticide; tanımlar (ör. DSCR) doğrulanmadı |

---

## 2. Mimari

### 2.1 Akış
```
(1) 1_scaper.py ── 5 ekonomi sitesi (sitemap/RSS), son 10 gün, yalnızca yeni URL'ler ──► news.parquet (kümülatif)

(2) 2_sql_to_parquet.py ── Oracle:
        <SEMA>.temp8 (bakiyeye göre ilk 100 şirket + finansallar)  ──► structured_data.parquet
        <SEMA>.company_partners (payı %50'den büyük ortaklar)       ┐
        unvandan otomatik alias + ALIAS_RULES + TEK_KELIME_YASAK    ┴► portfolio_config.py

(3) 4_llm_dogrulama.py  (tek çalıştırılan betik)
        3_eslestirme.run_matching   haber × şirket eşleşmeleri (son 180 gün, tam kelime, geniş)
            │
        ews_olay.ask_pair          LLM soru 1 ALAKA → (odak/ikincil ise) soru 2 OLAY + alıntı   [önbellekli]
            │
        ews_olay.apply_results     kod: alıntı haberde geçiyor mu, seviye (tip → sabit tablo)
            │
        ews_defter.build_ledgers   şirket başına olay defteri; LLM soru 3: yeni / mevcut / kapat   [önbellekli]
            │
        ews_tablolar               Şirketler (haber sinyali + 0-100 haber skoru), Kayıtlar, Kontrol, Alias
        ews_portfoy                finansal skor (ews_financial), Birlesik (haber + finansal + bakiye),
                                   Temalar (LLM soru 4, tek soru, bütün portföy)                 [önbellekli]
        ews_dashboard              dashboard.html (tek dosya) + dashboard_gecmis.jsonl
            │
        llm_dogrulama.xlsx (9 sayfa) · llm_cache.jsonl · kontrol_etiketleri.jsonl
```
Günlük kullanım: `1_scaper` → `4_llm_dogrulama`. `2_sql_to_parquet` yalnızca portföy, finansallar ya da ortaklar
değiştiğinde çalışır. `3_eslestirme` ayrıca çalıştırılmaz.

### 2.2 Dosyalar
| Dosya | Satır | Kim yazdı | Görev |
|---|---|---|---|
| `1_scaper.py` | ~450 | yönetici (+ küçük değişiklik) | 5 site: CNBC-e, Bloomberg HT, Dünya, Ekonomim, Patronlar Dünyası. Sitemap/RSS'ten son `LOOKBACK_DAYS` (10) günün URL'leri, yalnızca havuzda olmayanlar indirilir; başlık + gövde metni; parquet'e eklenir (URL'ye göre tekilleştirme). KAP kodu var ama kapalı |
| `2_sql_to_parquet.py` | ~400 | yeniden yazıldı (986 → ~400) | Oracle sorguları, alias üretimi, ortaklar, `portfolio_config.py`. Bağlantı bilgileri yer tutucu; şifre boş bırakılır ve dosyayla dolaşmaz |
| `3_eslestirme.py` | ~400 | `3_scoring`'den türetildi | Yalnızca eşleştirme: alias indeksi (trie regex), tam kelime eşleşme, kesit (snippet) çıkarma, tekilleştirme, tarih penceresi |
| `4_llm_dogrulama.py` | ~180 | yeni | Ayarlar (LLM url/key, işçi sayısı, eşikler) ve akış |
| `ews_llm.py` | ~140 | yeni | LLM istemcisi (temperature 0, `<think>` bloğunu atar, JSON ayıklar, 429/5xx'te bekleyip dener) ve `llm_cache.jsonl` önbelleği |
| `ews_olay.py` | ~280 | yeni | Olay sözlüğü (`OLAY_TIPLERI`), alaka/olay prompt'ları, `ask_pair`, alıntı doğrulama, kayıt düzeyi seviye |
| `ews_defter.py` | ~240 | yeni | Olay defteri (`Olay`, `build_ledgers`) ve 0-100 haber skoru (`news_score`) |
| `ews_tablolar.py` | ~235 | yeni | Excel sayfaları, Kontrol örneklemi, analist etiketlerinin kalıcılaştırılması, alias raporu |
| `ews_portfoy.py` | ~220 | yeni | Finansal skorun yüklenmesi, Birlesik sayfası, Temalar |
| `ews_financial.py` | ~540 | yönetici; 11 Kasım'da yöneticinin tasarımıyla genişletildi | Üç katmanlı finansal skor |
| `ews_dashboard.py` | ~650 | yeni | Tek dosya HTML panel (Özet, Ne değişti, Şirketler, Temalar, Sistem kalitesi) ve günlük geçmiş |
| `ews_text.py` | ~140 | yönetici | Türkçe normalizasyon (`normalize_text`, offset eşleme). İçindeki `LOOSE_SINGLE_WORDS` artık kullanılmıyor |
| `portfolio_config.py` | — | üretilir | Müşteri listesi içerdiği için depoda yok; `2_sql_to_parquet` üretir |

Yöneticinin eski regex skoru (`3_scoring.py`), bankaya taşıma kopyaları ve yöneticiye yazılan notlar bu depoda yok.

---

## 3. Aşama aşama ayrıntı

### 3.1 Haber toplama (`1_scaper.py`)
- Kaynaklar ölçülerek seçildi. Bigpara hacmin %69'u olduğu için ayrı bir betiğe ayrıldı (bu klasörde değil).
  investing.com Cloudflare yüzünden imkânsız. Ekonomim'in sitemap'i ay başına 1.000 URL ile sınırlı, delikli.
- Bankada `NETWORK_MODE = "BANK"`: proxy kullanıcı/şifre sorulur; `cnbce.com` proxy'siz, diğerleri proxy'den.
- Havuz kümülatif: her koşu yalnızca yeni URL'leri indirir ve dosyaya ekler. Yerel ölçüm: 1 günlük koşu ~4 dk,
  627 haber.
- **Açık nokta:** Kullanıcı bankada `PARQUET_DIR = SCRIPT_DIR` yaptı; `3_eslestirme.PARQUET_PATH` ise
  ağ klasöründeki sabit bir yol. İkisinin aynı dosya olup olmadığı (betiklerin o klasörde
  durup durmadığı) **doğrulanmadı** (bkz. §9).

### 3.2 Portföy, finansallar ve aranacak adlar (`2_sql_to_parquet.py`)
**Portföy:** `<SEMA>.temp8`'den toplam bakiyeye göre ilk 100 karşı taraf (`FETCH FIRST 100`, bilinçli karar).
`temp8` yöneticinin `sorgu.txt`'siyle üretilir: banka bakiyeleri (TL ve döviz), bankadaki döviz mevduatı, Merkez Bankası
risk (memzuç) toplamları, son ve bir önceki yıllık finansal tablo (IFRS varsa IFRS, yoksa VUK), TL'ye çevrim ve
trend etiketleri.

**Aranacak adlar (alias) — 13 Kasım hali:**
1. Unvandan otomatik: tam unvan; hukuki eki (A.Ş., LTD. ŞTİ.) atılmış hali; ticari ekleri (SANAYİ, TİCARET) de
   atılmış çekirdek isim; ilk 2 ve 3 kelime.
2. **Unvanın ilk kelimesi** otomatik eklenir ("ABC", "ÖRNEK"), `TEK_KELIME_YASAK`'taki 19 sıradan
   kelime/yer adı hariç (TÜRKİYE, TÜRK, AVRUPA, PETROL, YAPI, İSTANBUL…). Gerekçe: §5, karar K-13.
3. `ALIAS_RULES`: unvandan çıkmayan, basının kullandığı 9 ad (kısa marka adları, bir borsa kodu). Depodaki liste örnektir.
4. **Hakim ortaklar:** `<SEMA>.company_partners`'tan payı %50'den büyük ortak. Pay oranının 0-1 mi yüzde mi
   tutulduğu kodla tespit edilir. Tüzel kişi (unvanında hukuki ek ya da HOLDİNG, GRUP… varsa) şirketlerle aynı
   kuralla, gerçek kişi yalnızca tam adıyla aranır. Config'te şirketin `partners` alanına yazılır.
- Birden fazla şirketin ortak adı ("ABC GRUP") hepsine verilir; hangisi olduğuna LLM karar verir.
- `portfolio_config.py`'nin sonunda denetim satırları, otomatik eklenen tek kelimelerin listesi (gözden geçirme
  için) ve önceki config'e göre farklar yazılır.

### 3.3 Eşleştirme (`3_eslestirme.py`)
- **İlke: recall önce.** Bir haberin kaçması yanlış eşleşmeden pahalıdır; yanlışları LLM eler.
- Bütün adlar tek bir trie regex'te; tam kelime, Türkçe karakter ve büyük/küçük harf farkı yok sayılır, kesme
  işaretli ek sayılır ("ABC'nin"). Aynı konumda en uzun ad seçilir. Borsa kodu yalnızca tam büyük harfle ve
  büyük harfli başlık içinde değilse.
- Ortak adıyla eşleşen haber şirketin kaydı olur (`Ortak` kolonu); şirketin kendi adı da geçiyorsa kendi adıyla
  eşleşme tutulur.
- Her eşleşme için kesit: adın 25 kelime öncesi, 60 kelime sonrası; başka bir portföy şirketine gelince durur
  (başka borçlunun olayı yanlış şirkete yazılmasın), bağlaçla bağlı ortak özneler ("ABC ve XYZ") hariç.
- Pencere: son 180 gün. Tekilleştirme: normalize URL, yoksa başlık + gün.
- Havuz 30 günden azını kapsıyorsa uyarı yazılır (4 Kasım'daki "dashboard boş" olayından sonra eklendi).

### 3.4 LLM soruları (`ews_olay.py`)
**Soru 1, alaka** (kesit + haberin ilk 1.000 karakteri): `odak` | `ikincil` | `alakasiz` | `baska_varlik`.
Emin değilse `ikincil` (güvenli taraf: olay sorusuna devam edilir). "Şirketin sahibi, hakim ortağı ya da üst
yöneticisi hakkındaki haber şirket hakkında sayılır."

**Soru 2, olay** (yalnızca odak/ikincil; haberin ilk 6.000 karakteri):
- `olay_tipi`: kapalı listeden (17 tip). Her tipin tanımı ve **"DAHİL DEĞİL"** listesi var; bu listeler banka
  koşularındaki aşırı alarmlardan çıkarıldı (ör. borsadaki "temerrüt satışı" temerrüt değildir).
- `durum`: gerçekleşti | iddia | yalanlandı/çözüldü. Suçlananın itirazı yalanlama sayılmaz.
- `risk_turu`: kredi | hukuki_itibar (stage'i değiştirmez, skoru ×0,8 yapar).
- `alinti`: haberden kelimesi kelimesine; `ozet`.

**LLM seviye ya da puan seçmez.** Seviye tipten sabit tabloyla gelir: CRITICAL (konkordato_iflas, temerrut,
haciz_el_koyma, faaliyet_sona_erme) → Stage 3; HIGH (borc_yapilandirma, dava_ceza, finansal_bozulma,
faaliyet_kesintisi, not_dusurme, yonetim_krizi) → Stage 2; MODERATE (kar_dususu, dis_baski, operasyonel_aksaklik,
idari_ceza) → Stage 1; olumlu/rutin/hicbiri → sinyal değil.

**Kod kontrolü:** alıntı (normalize edilip) haberde geçmiyorsa olay sayılmaz; yalanlandı/çözüldü ise seviye
sayılmaz (ama defterde kapanış için kullanılır).

**Ayarlar:** `/no_think`, `temperature=0`, JSON'u kod ayıklar (tool calling kırılgan olduğu için kullanılmadı).
Önbellek anahtarı: haber tekilleştirme anahtarı | şirket | `PROMPT_SURUMU` (şu an 5). Prompt değişince sürüm
artırılır ve yalnızca o sorular yeniden sorulur. Ortak üzerinden gelen kayıtta prompt şablonu değişmez, yalnızca
unvan satırına "(hakim ortağı: X (%62); haberde ortağın adı geçiyor)" eklenir; bu yüzden önbellek bozulmadı.

### 3.5 Olay defteri (`ews_defter.py`)
**Neden:** Haberler tek tek değerlendirilince "tedbir kaldırıldı" haberi önceki "tedbir konuldu" haberini
etkilemiyordu; kritik alarm haftalarca kalıyordu. Ayrıca aynı olayın 5 sitedeki haberi 5 ayrı alarm gibiydi.
Tetikleyen örnek: bir şirkette şirkete konan tedbir ertesi gün kaldırılmış, sahiplerine konan tedbir ise
sürmüştü; sistem bu ikisini ayıramıyordu.

**Nasıl:** Şirket başına, alıntısı doğrulanmış CRITICAL/HIGH olaylar (odak haberlerde; ikincil haberlerde yalnızca
CRITICAL) tarih sırasıyla işlenir. Defter boşsa yeni olay açılır. Doluysa LLM'e defterdeki olaylar gösterilir ve
**yeni / mevcut / kapat** sorulur (soru 3). Kod güvenlik kuralları:
- Olay yalnızca haberin kendi durumu "yalanlandı/çözüldü" ise kapanır; LLM "kapat" dese de.
- Aynı olaya bağlanan daha ağır haber olayı ağırlaştırır (dava → haciz); **kapanış haberi ağırlaştırmaz**
  (2 Kasım'da düzeltilen hata: "konkordato iddiası yalanlandı" haberi açık bir davayı Stage 3 konkordatoya
  çeviriyordu).
- Kapanmış olay silinmez.

**Şirket haber seviyesi:** açık CRITICAL → 3; kapanmış CRITICAL ya da açık HIGH → 2; kapanmış HIGH → 1. Son haberi
90 günden eski olay bir kademe düşük katkı yapar.

**Haber skoru (0-100)**, yöneticinin sayı istemesi üzerine (22 Ekim). LLM'e sayı sorulmaz. Olay puanı (açık kritik
80, kapanmış kritik 45, açık yüksek 45, kapanmış yüksek 15, odaktaki orta seviyeli tip 8) × tazelik (90 güne kadar
1, 180. günde 0) × (hukuki/itibar ise 0,8); en yüksek + diğerlerinin %25'i; sonuç seviyenin bandına sığdırılır
(Stage 1: 0-24,9; 2: 25-64,9; 3: 65-100). `Skor_Hesabi` hesabı metin olarak yazar.

### 3.6 Finansal skor (`ews_financial.py`, 11 Kasım'dan beri üç katman)
Yöneticinin tasarımı. Her alt skor bir durum etiketi üretir → risk oranı (0-1) → puan = tavan × oran.
| Katman | Alt skor (tavan) | Özet kural |
|---|---|---|
| Seviye (60) | Özkaynak (15), FAVÖK (10) | negatif 1, sıfır 0,5, pozitif 0 |
| | Kur (15) | 1) bankadaki döviz mevduatı + 1 mn ≥ bankadaki döviz kredisi → dirençli; 2) net döviz pozisyonu > 0 ve ihracat payı > %80 → dirençli; 3) pozisyon < 0 ve ihracat payı < %40 → dayanıksız; 4) diğer → nötr |
| | DSCR (20) | < 1 → 1,0 … > 2 → 0 (6 bant) |
| Dayanıklılık (20) | Likidite (15) | likit aktif / KV borç, 5 bant |
| | ICR (5) | FAVÖK / faiz gideri; FAVÖK < 0 "operasyonel zarar"; faiz ≤ 0 ise hesaplanmaz |
| Trend (20) | Özkaynak ve FAVÖK trendi (10+10) | SQL'in ürettiği durum etiketleri (pozitife dönüş 0 … negatife geçiş 1; "sabit" 0,3). Önceki tablo 11-13 ay önceyse kullanılır |

- Hesaplanamayan alt skor 0 puan alır, skor ölçeklenmez; skor bir **alt sınırdır**. `Finansal_Veri_Kapsami`
  raporlanır; %50'nin altındaysa Birlesik'te not düşülür. Hiçbir alt skor yoksa skor `None`.
- **Finansal Stage eşikleri 50 / 80** (yönetici kararı, 11 Kasım). Haber skoru 25 / 65'te kaldı.
- Raporlanan ama skora girmeyenler: sert likidite, `DSCR_ICR_Ayrisma` (DSCR > 1,2 ama ICR < 1,5 → DSCR kolonu
  anaparayı dışarıda bırakıyor olabilir), banka kapsamasıyla dirençli sayılan şirket sayısı.

### 3.7 Birleştirme (`ews_portfoy.build_combined_table`)
- **Birleşik seviye = max(haber seviyesi, finansal seviye).** Yöneticinin eski tablosundaki kural.
- **Birleşik skor = max(haber skoru, haber ölçeğine çevrilmiş finansal skor).** Eşikler farklı olduğu için finansal
  skor bandı içinde doğrusal olarak haber bandına taşınır (0-50 → 0-25, 50-80 → 25-65, 80-100 → 65-100); böylece
  birleşik skor her zaman birleşik seviyenin bandındadır (1.001 değerle test edildi).
- Tek bağlantı kuralı: odak haberde kâr düşüşü varken finansallar zayıfsa (DSCR < 1,2 ya da negatif
  özkaynak/FAVÖK) haber seviyesi Stage 2'ye çıkar.

### 3.8 Temalar (`ews_portfoy.build_themes`)
Bütün doğrulanmış olaylar (en fazla 300) tek soruda LLM'e verilir; birden çok müşteriyi etkileyen **ortak**
gelişmeleri (aynı soruşturma, aynı vergi, aynı fiyat şoku) gruplaması istenir. Uydurmaya karşı: prompt'ta örnek
yok (örnek tema adı modeli "anti-damping" temasına yönlendirmişti); LLM her tema için bir `ortak_ifade` yazar ve
kod bu ifadenin her maddenin özetinde ya da alıntısında geçtiğini 5 harflik kökle kontrol eder; tutmayan madde
atılır, 2'den az şirketi kalan tema düşer.

### 3.9 İnsan denetimi ve çıktılar
- **Kontrol sayfası:** her koşuda 4 gruptan (risk olayı, orta seviye, elenen eşleşme, olaysız) 15'er rastgele
  kayıt. Analist "doğru" / "yanlış: …" yazar. Notlar bir sonraki koşuda Excel üzerine yazılmadan önce
  `kontrol_etiketleri.jsonl`'a alınır; etiketli kayıt bir daha örneklenmez; log grup başına isabeti yazar.
  Hedef değişken olmadığı için LLM isabetinin tek ölçüsü bu.
- **Alias sayfası:** alias başına eşleşme ve red oranı; hiçbir haberi kabul edilmeyen alias için hazır
  `ALIAS_RULES` "remove" satırı.
- **Dashboard:** tek dosya, internet gerektirmez. "Ne değişti" sekmesi bugünü seçilen bir geçmiş koşuyla
  karşılaştırır (önceki koşu, 1 hafta, 1/3/6 ay). Olaylar günler arasında ortak haberleri üzerinden eşleştirilir
  (O1, O2 kodları pencere kaydıkça değişebildiği için).

---

## 4. Gelişim kronolojisi

| Tarih | Ne oldu | Neden |
|---|---|---|
| 8 Eki | Yöneticinin pipeline'ı temel alındı. `4_llm_dogrulama` ilk sürümü: alaka → olay → alıntı kontrolü, jsonl önbellek | Üç sorunun ortak çözümü: geniş eşleştir, LLM doğrulasın, kod kanıtı kontrol etsin |
| 8 Eki | Yerel ölçüm: 1.834 eşleşmenin çoğu yanlış; alias'lar kelime sınırı olmadan eşleşiyordu (kısa adlar kelime içinde ve sıradan kelimelerle yüzlerce kez eşleşiyordu; "KAR" → "karar" türü) | Yanlış eşleşmenin kök nedeni |
| 12 Eki | Öneri: tam kelime eşleşme (2 regex satırı), marka adlarının gevşetilmesi. 603 kelime içi yanlış eşleşme gitti, 26 kaçan başlık geldi | Önce mekanik hatayı düzelt |
| 13 Eki | Yönetici izinde; geçici kararlar `4_llm_dogrulama_ACIKLAMA.md` §10'a gerekçesi ve geri alma yoluyla yazıldı. **Grup kuralı eklenmedi:** grup bilgisi LLM'in hafızasından alınmamalı, veriden gelmeli | Geri alınabilirlik; uydurma riski |
| 14 Eki | Prompt 4-5: `faaliyet_sona_erme`, risk türü, "temerrüt satışı" istisnası. Birlesik ve Temalar sayfaları. Modüllere bölündü (8 dosya). Dashboard | Banka koşusundaki aşırı alarmlar; yöneticinin "sade kod" isteği |
| 14 Eki | **Bankada ilk gerçek koşu** (100 şirket): 1.198 LLM sorusu; %43'ü alakasız/başka varlık (çoğu borsa bülteni); alıntıların ~%97'si haberde bulundu; şirketlerin yaklaşık yarısının hiç haberi yok; defterde birkaç olay | İlk gerçek veri |
| 22 Eki | Yönetici kararı: regex haber skoru ve strict alias kaldırıldı (`3_scoring` → `3_eslestirme`); haber skoru 0-100 olay defterinden; `2_sql_to_parquet` 986 → 470 satır; ortak adlar bütün sahiplerine | Tek bir haber skoru; recall önce |
| 23 Eki | Tema prompt'u örneksiz + ortak ifade kontrolü; tazelik (90/180 gün); 90 günü geçen olay bir kademe düşük | Uydurma tema; eski olayın ağırlığı |
| 26 Eki | Eleştirel inceleme (bkz. §7): defter hatası bulundu, haber katmanının birleşik seviyeye etkisinin çok küçük olduğu ölçüldü, Kontrol notlarının kaybolduğu fark edildi | |
| 2 Kas | Defter hatası düzeltildi (kapanış haberi ağırlaştırmaz); Kontrol notları kalıcı; `SONRAKI_ADIMLAR.md` | İncelemenin hızlı düzeltmeleri |
| 4 Kas | Kullanıcı 1 günlük scraper + 4 çalıştırdı, dashboard boş geldi. Havuz kapsama uyarısı eklendi; yol sorunu tartışıldı (çözümü kesinleşmedi) | §9 |
| 11 Kas | Yöneticinin üç katmanlı finansal modeli işlendi; finansal eşikler 50/80; birleşik skorda ölçek çevrimi | Yönetici tasarımı |
| 13 Kas | İlk kelime kuralı + 19 kelimelik yasak listesi; `ALIAS_RULES` 153 → 9 satır. Hakim ortak (>%50) eşleştirmesi | Yönetici "sadeleştir" dedi; kullanıcı "recall önce, FP'yi LLM elesin" dedi |

---

## 5. Temel kararlar ve gerekçeleri

| # | Karar | Gerekçe | Reddedilen alternatif |
|---|---|---|---|
| K-1 | LLM puan ya da seviye vermez; kapalı listeden olay seçer, seviyeyi kod verir | Hedef değişken yok, puan kalibre edilemez; kanıtlı olay denetlenebilir. Literatür LLM'i karar verici değil özellik çıkarıcı olarak konumluyor | LLM'e 0-100 risk puanı sordurmak |
| K-2 | Her olay haberden birebir alıntıyla, alıntıyı kod doğrular | Uydurmaya karşı ucuz ve kesin kontrol | LLM'in kendi güven yüzdesi |
| K-3 | `/no_think`, temperature 0, önbellek, prompt sürümü | Tekrarlanabilirlik; finansal sınıflandırmada düşünme modunun isabeti düşürdüğüne dair bulgu; maliyet | Düşünme modu |
| K-4 | Eşleştirme geniş (recall), yanlışları LLM eler | Kaçan haber, fazladan bir LLM sorusundan pahalı | Sıkı (strict) alias'lar |
| K-5 | Grup/ortak bilgisi yalnızca veriden (DWH) | Model küçük grupları bilmez, uydurabilir | LLM'e "bu şirketin grubu ne?" diye sormak |
| K-6 | Olay defteri (yeni/mevcut/kapat) ve kapanış şartı kodda | Kapanan olaylar sinyale yansımıyordu; aynı olayın tekrar haberleri | Haber bazında sinyal |
| K-7 | Kapanmış kritik olay Stage 2 verir (sıfırlanmaz) | Kaldırılan tedbir de geçmişteki sıkıntının işareti | Kapanınca tamamen düşürmek |
| K-8 | Risk türü stage'i değiştirmez | Seviyeyi düşürmek yönetici kararı olmalı | Hukuki/itibar olaylarını bir kademe düşürmek |
| K-9 | Birleşik seviye = max(haber, finansal) | Yöneticinin mevcut kuralı | İki eksenli gösterim (önerildi, bkz. §7) |
| K-10 | Portföy = bakiyeye göre ilk 100 | Kullanıcı kararı (LLM süresi, odak) | 1.000 şirket |
| K-11 | Dashboard tek dosya, dış kütüphane yok | Veri bankadan çıkmaz, kurulum yok | Web sunucusu, BI aracı |
| K-12 | Kod bankaya `.txt` ile; taşınan kopyalarda şifreler boş | Ortam kısıtı; şifrenin dosyalarla dolaşmaması | Sürüm kontrolü (yok) |
| K-13 | Unvanın ilk kelimesi otomatik + 19 kelimelik yasak listesi | Ölçüm (100 şirket, 3 aylık yerel arşiv): sınırsız ilk kelime eşleşmeyi 2.108'den 33.320'ye çıkarıyordu (günde ~366 soru), fazlalığın neredeyse tamamı birkaç kelimeden ("TÜRKİYE" 14.739, "TÜRK" 5.206). Yasak listesiyle: 2.227 eşleşme, kaybolan 0, haberi olan şirket 69 → 72 | 153 satırlık elle tutulan liste; sınırsız ilk kelime |
| K-14 | Hakim ortak eşiği %50, yalnızca doğrudan ortak | Kullanıcı kararı; bir üst kademe (ortağın ortağı) reddedildi | Patron/aile zinciri |
| K-15 | Prompt şablonu değiştirmeden bağlam eklemek (ortak bilgisi unvan satırında) | Önbelleği bozmamak (aksi halde binlerce soru yeniden sorulur) | Prompt sürümünü artırmak |

---

## 6. Ölçümler ve kanıtlar

| Ölçüm | Değer | Kaynak |
|---|---|---|
| Alıntı doğrulama oranı | ~%97 | Banka koşuları |
| Yanlış eşleşme payı (o zamanki alias'larla) | %30; bunların %88'i 12 alias'tan | 90 günlük banka verisi, 1.982 eşleşme |
| LLM sorularının alakasız/başka varlık payı | %43 (1.198'in 515'i), çoğu borsa bülteni | 14 Eki banka koşusu |
| Haber katmanının birleşik seviyeye etkisi | Şirketlerin çoğunluğu finansal skordan zaten Stage 2'deydi (eski 25/65 eşikleriyle); haber birleşik seviyeyi çok az şirkette değiştirdi | 14 Eki banka Excel'i |
| DSCR dağılımı | medyan 1 civarı, şirketlerin yaklaşık yarısı 1'in altında, uç değerler yüzlerce → alan tanımı şüpheli | aynı |
| Haberi olmayan şirket | 100'ün 54'ü (2,5 ay) | aynı |
| Basit başlık kuralıyla bülten filtresi | 346 soruyu (%29) atlar; atlananlarda 0 odak, 0 yüksek/kritik olay | 14 Eki Excel'i üzerinde deneme (uygulanmadı) |
| ALIAS_RULES'un tamamen silinmesi | eşleşme 2.108 → 1.127; 15 şirketin hiç haberi kalmaz | yerel arşiv |
| Hakim ortak (sahte veriyle) | +232 eşleşme; bir holding şirket başına 3 ayda ~80 eşleşme | yerel arşiv, sahte ortaklar |

**Önemli:** 22 Ekim sonrasındaki değişikliklerin (gevşek eşleştirme, yeni finansal model, ilk kelime kuralı, ortaklar)
hiçbirinin bankadaki gerçek sonucu henüz görülmedi.

---

## 7. Eleştirel inceleme bulguları (26 Ekim) ve durumları

| Bulgu | Durum |
|---|---|
| Defterde kapanış haberi olayı ağırlaştırıyordu | Düzeltildi (2 Kas) |
| Tek başına gelen "yalanlandı" haberi kapanmış kritik olay açıyor → Stage 2 | **Açık.** Çözüm önerisi: `yalanlandi_cozuldu`'yu "yalanlandı" ve "çözüldü" olarak ikiye ayırmak (prompt sürümü değişir) |
| Kontrol notları her koşuda siliniyordu | Düzeltildi (2 Kas) |
| Haber katmanı birleşik seviyeyi neredeyse hiç değiştirmiyor (finansal skor çoğunluğu zaten Stage 2'ye koyuyor) | Finansal model ve eşikler değişti (11 Kas); etkisi ölçülmedi. İki eksenli gösterim (finansal zayıflık × haber olayı) önerildi, karar verilmedi |
| Sistem gerçek sonuçla hiç karşılaştırılmadı | **Açık, en önemli iş.** DWH'taki sonuç kayıtlarıyla (izleme listesi, yapılandırma, 90+ gün gecikme, TFRS 9 stage geçişi) geriye dönük test planlandı; tablo henüz bulunmadı. `run_matching(as_of=...)` buna hazır. 100 şirkette olay az olduğu için istatistiksel güç sınırlı |
| Açık bir konkordato 180 günde görünmez olur (defter her koşuda pencereden yeniden kuruluyor) | Açık. Kalıcı olay deposu önerildi |
| "Stage 3" adı TFRS 9 terminolojisiyle çakışıyor | Açık; renk/alarm adlandırması önerildi |
| Haberi olmayan şirket "Düşük Risk" görünüyor; doğrusu "sinyal yok" | Açık |
| Kaynak sağlığı izlenmiyor (`except: pass`, bir site bozulursa sessizlik) | Açık |
| CRITICAL olaylarda ikinci bir doğrulama sorusu (özne kim?) | Açık |
| Modül sürüm kontrolü yok (elle taşınan dosyalardan biri eski kalabilir) | Açık; ihtiyaç 13 Kas'ta tekrar ortaya çıktı (§9) |
| Belgelerde kayma | Kısmen; bkz. §10 |

---

## 8. Platform ve veri kaynakları hakkında bilinenler

### 8.1 LLM platformu
- Şirket içi, OpenAI uyumlu bir uç noktadan sunulan ~27B parametreli açık kaynak bir model. `model` alanını
  reddediyor; `max_tokens` 1-4.096; bağlam geniş (yüz binlerce token).
- Platformda daha küçük ve hızlı bir model de var; ana modelden önce ucuz bir alaka ön filtresi olabilir (denenmedi).
- Uygulama başına varsayılan paralel istek limiti 1. `LLM_WORKERS = 2` bunu aşınca HTTP 429 alınıyor; istemci
  `Retry-After` ya da 30/60 sn bekleyip yeniden deniyor. Günlük toplu çalışma için ayrı bir uygulama anahtarı gerekir.
- İlk tam koşu ~50-80 dk; sonraki koşularda yalnızca yeni eşleşmeler sorulduğu için birkaç dakika.

### 8.2 İçerik denetim uç noktası
Platformun içerik denetim (moderation) uç noktası aslında çok dilli bir cümle embedding modeliyle çalışan bir
benzerlik sınıflandırıcısı; kategorileri serbest metin (Türkçe) alıyor, kategori skorları döndürüyor, vektör
döndürmüyor. Bülten ön filtresi ve tema üyeliği doğrulaması için düşünüldü; amaç dışı kullanım olduğu için ertelendi.

### 8.3 Veri tanımları (doğrulanmamış)
- `dscrebitda_try`: paydası bilinmiyor; `_try` eki bir orana takılmış (kurla çarpılmış olabilir). Yönetici bakıyor.
- `bank_fx_deposit` (her döviz cinsinden `bal_curr` toplamı, büyük ihtimalle orijinal para birimi) ile döviz kredi
  kolonlarının ve 1 mn tamponun para birimi aynı mı? Değilse kur kuralının 1. maddesi bozuk çalışır.
- `interestexpense_try`: SQL `greatest(x, 0)` alıyor; gider negatif tutuluyorsa ICR hiç hesaplanmaz.
- Trend nominal TL karşılaştırıyor: yüksek enflasyonda ve döviz bazlı tablolarda "büyüme" abartılı.
- `temp8`'de `ccy_code`/`prev_ccy_code` yok; trendin para birimi kontrolü bu yüzden çalışmıyor (kod sütun gelirse
  kendiliğinden yapar).

### 8.4 Değerlendirilip ertelenen fikirler (`SONRAKI_ADIMLAR.md`)
Resmi ilan taraması (ilan.gov.tr konkordato/iflas, Ticaret Sicil Gazetesi; hukuk onayı ve erişim testi bekliyor),
risk kelimesiyle haber araması (Google News RSS; uyum onayı gerekir), embedding ile bülten filtresi ve tema
kümeleme, KAP (hukuki olarak kapalı; resmi API sözleşmesi gerekir), izleme notu taslağı, denetim raporu
kırmızı bayrakları.

---

## 9. Bilinen açık sorunlar (inceleme için en değerli yerler)

1. **Banka ile yerel kod uyumu bilinmiyor.** Bankadaki dosyaların hangi sürümde olduğu kesin değil (özellikle
   `4_llm_dogrulama.py`'nin 2 Kasım sürümü). Sürüm kontrolü yok.
2. **Haber dosyasının yolu.** `1_scaper` betik klasörüne, `3_eslestirme` G: yoluna bakıyor. 4 Kasım'daki boş
   dashboard'un sebebi kesinleşmedi (G: dosyasında 90 günlük havuz vardı; LLM anahtarı ya da önbellek dosyası
   sorunu olabilir; log gelmedi).
3. **Hakim ortak eşleştirmesinin hacmi ve isabeti** gerçek veriyle ölçülmedi. Holdingler birden çok portföy
   şirketinin ortağıysa her haber şirket sayısı kadar soru üretir. Kişi ortaklarda ad çakışması (yaygın isimler)
   olası.
4. **Finansal modelin girdileri** (§8.3) ve yeni dağılım bilinmiyor; eşikler (50/80) yeni dağılım görülmeden
   seçildi.
5. **Birleşik skor çevrimi** (§3.7) seviye tutarlılığını sağlıyor ama bir finansal skor 65 iken Birleşik'te 45
   görünmesi kullanıcıyı şaşırtabilir.
6. **Kod içi eski yorumlar:** `ews_olay.py`'deki `STAGE2_ESIK` yorumu ("haber ve finansal skor için aynı") ve
   `ews_defter.py`'deki "Bantlar finansal skorla aynı eşiklerden gelir" yorumu 11 Kasım'dan beri yanlış.
7. **Değerlendirme yok.** LLM isabeti yalnızca Kontrol notlarıyla ölçülebiliyor ve notlar henüz birikmedi.

### İncelemeciden özellikle istenenler
- `ews_defter.build_ledger` ve `_link`: defter mantığında başka kenar durumları (aynı gün açılış/kapanış, yanlış
  olaya bağlama, LLM hatasında davranış).
- `3_eslestirme.match_companies`: ortak eşleşmesi + çakışma çözümü + başlık/gövde mantığının birlikte doğru
  çalışıp çalışmadığı; kesit sınırları.
- `ews_financial`: üç katmanın tasarımla uyumu; "veri yok" ile "nötr" ayrımı; sınır değerleri.
- `ews_portfoy._haber_olcegi` ve birleşik seviye/skor tutarlılığı.
- Prompt'larda (ews_olay, ews_defter, ews_portfoy) belirsizlik ya da modeli yönlendiren ifadeler.
- Önbellek anahtarlarının doğru şeyleri kapsayıp kapsamadığı (ör. ortak bilgisi değişince eski cevap kullanılır mı?).
- Genel tasarım: max kuralı, kapanmış kritik olayın Stage 2 kalması, 180 günlük pencere, recall-önce ilkesinin
  LLM hacmiyle dengesi, geriye dönük test planının uygulanabilirliği.

---

## 10. Belge haritası

| Belge | Ne anlatır | Güncellik |
|---|---|---|
| **Bu belge** | Bütün proje | 13 Kas |
| `SKORLAMA_KURALLARI.md` | Seviye ve skor kurallarının kısa, kesin hali | Finansal bölüm (§7-8) 11 Kas'ta güncellendi; eşleştirme değişiklikleri (13 Kas) yok |
| `4_llm_dogrulama_ACIKLAMA.md` | LLM katmanının ayrıntısı, kolon sözlüğü, karar kaydı (§10), banka sonuçları (§8) | Kısmen eski: 90 günlük pencere, `TAZELIK_INDIRIMI`, finansal eşik 25/65, `financial_stage` geçiyor; §8 tablosunun içine karar satırları (20-22) karışmış; §11 eski |
| `SONRAKI_ADIMLAR.md` | Yol haritası: geriye dönük test, resmi ilan, küçük iyileştirmeler, embedding | 13 Kas |

Yöneticiye yazılan notlar ve bankaya taşıma talimatları bu depoda yok.
