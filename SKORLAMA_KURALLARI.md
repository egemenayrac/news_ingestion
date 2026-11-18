# Skorlama kuralları

Haber ve finansal sinyalin nasıl Stage'e ve 0-100 skora dönüştüğü. Sayılar koddaki ayarlarla aynıdır (son kontrol: 23 Ekim 2026; prompt sürümü 5, tema sürümü 2; haber penceresi 180 gün).

## 1. Akış

1. `3_eslestirme.py` haberleri alias'larla şirketlere eşleştirir. Bilerek geniş tutulur, yanlış eşleşmeleri LLM eler.
2. LLM her eşleşme için **alaka**yı, ilgiliyse **olay tipini** belirler ve haberden alıntı verir. Seviye ya da puan vermez.
3. Kod, olay tipinden **seviyeyi** okur ve alıntıyı haberde arar.
4. **Olay defteri** aynı olayın haberlerini birleştirir ve olayın açık mı kapanmış mı olduğunu tutar. Şirketin **haber seviyesi** buradan gelir.
5. **Haber skoru** aynı olaylardan hesaplanır ve seviyenin aralığına sığdırılır.
6. **Finansal skor** yöneticinin `ews_financial` modelinden gelir.
7. **Birleşik** seviye ve skor, haber ile finansalın büyüğüdür.

## 2. Haber başına karar

**Alaka** (LLM):

| Değer | Anlamı |
|---|---|
| `odak` | Haber esas olarak bu şirket hakkında |
| `ikincil` | Şirket hakkında somut bilgi var, ana konu başka |
| `alakasiz` | Şirket kastediliyor ama yalnızca adı geçiyor (borsa bülteni, sponsorluk) |
| `baska_varlik` | Eşleşen ifade bu şirket değil (mahkeme, kişi, başka şirket) |

Şirketin sahibi, hakim ortağı ya da üst yöneticisi hakkındaki haber şirket hakkında sayılır.

**Olay** (yalnızca `odak` ve `ikincil` haberlerde, LLM):
- olay tipi (bölüm 3'teki listeden);
- durum: `gerceklesti`, `iddia` ya da `yalanlandi_cozuldu`;
- risk türü: `kredi` ya da `hukuki_itibar`;
- haberden birebir alıntı.

**Bir olay yalnızca şu üç şart sağlanırsa sayılır:**
1. alaka `odak` ya da `ikincil`;
2. alıntı haberde geçiyor (büyük/küçük harf, noktalama ve boşluk farkı yok sayılır; "..." ile bölünmüş alıntıda en az 15 karakterlik her parça aranır);
3. durum `yalanlandi_cozuldu` değil.

**Tanımsız LLM cevabı güvenli tarafa çekilir:**
- alaka tanımsızsa `ikincil` sayılır;
- olay tipi tanımsızsa `hicbiri`;
- durum tanımsızsa `iddia`;
- risk olayında risk türü tanımsızsa `kredi`.

## 3. Olay tipleri ve seviyeleri

| Seviye | Tip | Kapsam |
|---|---|---|
| **CRITICAL** | konkordato_iflas | Konkordato, iflas, tasfiye, borca batıklık |
| | temerrut | Borç/kupon ödenmemesi, karşılıksız çek, protesto. *Borsada payda takas temerrüdü sayılmaz* |
| | haciz_el_koyma | Şirkete ya da sahibine haciz, el koyma, tedbir, kayyum, TMSF devri |
| | faaliyet_sona_erme | Bütün faaliyetin süresiz durması, bütün tesislerin kapanması |
| **HIGH** | borc_yapilandirma | Yapılandırma, vade uzatımı talebi, covenant ihlali, icra takibi |
| | dava_ceza | Şirkete, sahibine ya da yöneticilerine soruşturma, gözaltı; kurumun devam eden soruşturması |
| | finansal_bozulma | Net zarar, zarara geçiş, sermaye kaybı, olumsuz denetim görüşü |
| | faaliyet_kesintisi | Günlerce duruş, tek tesisin ya da hattın kapanması, üretimi durduran yangın, grev |
| | not_dusurme | Not ya da görünüm düşürülmesi |
| | yonetim_krizi | Ani üst yönetici ayrılığı, ortak anlaşmazlığı |
| **MODERATE** | kar_dususu | Kâr, satış ya da FAVÖK düşüşü; şirket hâlâ kârlı |
| | dis_baski | Talep, maliyet, kur, tedarik baskısı; vergi; sektör geneli soruşturma |
| | idari_ceza | Sonuçlanmış ve ödenmiş idari ceza |
| | operasyonel_aksaklik | Kısa arıza, tek seferlik iptal, söndürülen yangın |
| seviyesiz | olumlu, rutin, hicbiri | Risk sinyali değil |

Prompt'ta her tip için bir "DAHİL DEĞİL" listesi de vardır (örneğin başka şirketin olayı, planlı bakım).

## 4. Odak ve ikincil haberin etkisi

| Nerede | Odak | İkincil |
|---|---|---|
| Olay defteri (seviye ve skor) | CRITICAL ve HIGH | **yalnızca CRITICAL** |
| Orta seviyeli olayların skora katkısı | evet | hayır |
| Kâr düşüşü + zayıf finansal kuralı | evet | hayır |
| Temalar | evet | evet |
| Dashboard "Diğer haberler" (defterde olmayan, olay tipi "hiçbiri" olmayan haberler) | evet | evet |
| Kayıtlar sayfasındaki `LLM_Seviye` | evet | evet (yalnızca bilgi) |

## 5. Olay defteri ve haber seviyesi

- **Deftere giriş:** Bölüm 4'teki haberler, alıntısı doğrulanmış olanlar, tarih sırasıyla. Aynı gün içinde açılış haberleri kapanış haberlerinden önce işlenir.
- **Bağlama:** Defter boşsa haber yeni bir olay açar. Doluysa LLM'e defter gösterilir ve haberin `yeni`, `mevcut` ya da `kapat` olduğu sorulur. Şirkete konan tedbir ile sahibine konan tedbir ayrı olaylardır.
- **Kapanış:** Bir olay yalnızca haberin kendi durumu `yalanlandi_cozuldu` ise kapanır. Suçlananın itirazı olay kapatmaz. Kapanan olay silinmez.
- **Ağırlaşma:** Aynı olaya daha ağır bir haber bağlanırsa olayın seviyesi yükselir (dava → haciz). Bağlanan haberlerden biri `kredi` derse olayın risk türü `kredi` olur. Kapanış (yalanlandı/çözüldü) haberi ikisini de yapmaz: "konkordato iddiası yalanlandı" açık bir davayı konkordatoya çevirmez.
- **LLM hatası:** Haber yeni olay sayılır, hiçbir olay kapanmaz; soru bir sonraki koşuda yeniden sorulur.

**Olayın seviyeye etkisi** (yaş = olayın son haberinden bugüne geçen gün):

| Olay | Son haberi 90 gün içinde | 90–180 gün | 180 günden eski |
|---|---|---|---|
| Açık CRITICAL | Stage 3 | Stage 2 | defterden düşer |
| Kapanmış CRITICAL | Stage 2 | Stage 1 | defterden düşer |
| Açık HIGH | Stage 2 | Stage 1 | defterden düşer |
| Kapanmış HIGH | Stage 1 | Stage 1 | defterden düşer |

Kural: kapanan olay bir kademe, son haberi 90 günü geçen olay bir kademe daha düşer (en az Stage 1). Üç ay yeni haber çıkmayan sorun için yeni kanıt yoktur; kritik olay yine de yakın izlemede kalır. Eskiyen olay defterde ve dashboard'da "son haber 90+ gün önce" işaretiyle görünür.

**Haber seviyesi = defterdeki en ağır olay.** Defter boşsa Stage 1. Orta seviyeli olaylar seviyeyi tek başına yükseltmez; tek istisna bölüm 8'deki kâr düşüşü kuralıdır.

## 6. Haber skoru (0-100)

Seviyeyi değiştirmez; aynı seviyedeki şirketleri sıraya koyar.

**1. Her olayın puanı = temel puan × tazelik × risk türü çarpanı**

| Temel puan | |
|---|---|
| Açık CRITICAL | 80 |
| Kapanmış CRITICAL | 45 |
| Açık HIGH | 45 |
| Kapanmış HIGH | 15 |
| Odak haberdeki her farklı orta seviyeli olay tipi | 8 (risk türü çarpanı uygulanmaz; tazelik o tipin son haberine göre) |

- **Tazelik:** olayın son haberi 90 gün içindeyse 1; 90 ile 180 gün arasında doğrusal azalır (135 gün 0,5); 180 günde 0. Haber penceresi 180 gündür.
- **Risk türü çarpanı:** `hukuki_itibar` için 0,8, `kredi` için 1.

**2. Ham skor = en yüksek puan + 0,25 × (diğer puanların toplamı)**

**3. Skor = min( max(ham skor, alt sınır), üst sınır ),** bir ondalık

| Seviye | Alt sınır | Üst sınır |
|---|---|---|
| Stage 1 | 0 | 24,9 |
| Stage 2 | 25 | 64,9 |
| Stage 3 | 65 | 100 |

Bu aralıklar skorun Stage ile hiç çelişmemesini sağlar. Bedeli şu: birçok yüksek olayın birikimi şirketi Stage 3'e taşımaz.

**Örnek:** Dün açılmış haciz, 10 gün önce açılmış kredi türünde soruşturma ve bir kâr düşüşü haberi.
- Puanlar: 80 × 1 = 80; 45 × 1 = 45; 8.
- Ham skor: 80 + 0,25 × (45 + 8) = 93,25 → **93,2** (bir ondalık). Stage 3 aralığında olduğu için olduğu gibi kalır.
- Aynı haciz 145 gün önce olsaydı: haciz 90 günü geçtiği için seviyeye Stage 2 katkısı yapar, soruşturma da Stage 2; şirket Stage 2. Haczin puanı 80 × 0,39 = 31,1 olur ve en yüksek puan artık soruşturmanın 45'idir: ham skor 45 + 0,25 × (31,1 + 8) = **54,8**, Stage 2 aralığında kalır.

Excel'deki `Skor_Hesabi` kolonu her şirketin hesabını yazar.

## 7. Finansal skor (0-100, `ews_financial.py`, üç katmanlı model)

Yöneticinin 5 Kasım tasarımı. Her alt puan = tavan × risk oranı. Hesaplanamayan alt puan 0 sayılır, skor ölçeklenmez (skor bir alt sınırdır); hiçbiri hesaplanamazsa skor yoktur. `Finansal_Veri_Kapsami` = hesaplanabilen tavanlar / 100; %50'nin altındaysa Birlesik'te `Not` "skor alt sınır" der.

| Katman | Alt puan (tavan) | Puan |
|---|---|---|
| Seviye (60) | Özkaynak (15) | negatif 15 · sıfır 7,5 · pozitif 0 |
| | FAVÖK (10) | negatif 10 · sıfır 5 · pozitif 0 |
| | Kur (15) | İlk uyan: 1) bankadaki döviz mevduatı + 1 mn ≥ bankadaki döviz kredisi (`cash_bal_bank_usd + noncash_bal_bank_usd`) → dirençli 0 · 2) net döviz pozisyonu > 0 ve ihracat payı > %80 → dirençli 0 · 3) pozisyon < 0 ve ihracat payı < %40 → dayanıksız 15 · 4) diğer → nötr 7,5. Net pozisyon da banka kapsaması da bilinmiyorsa hesaplanmaz |
| | DSCR (20) | < 1: 20 · = 1: 18 · 1–1,2: 14 · 1,2–1,5: 6 · 1,5–2: 2 · > 2: 0 |
| Dayanıklılık (20) | Likidite (15): likit aktif / KV borç | ≤ 0,5 kritik 15 · ≤ 0,8 zayıf 10,5 · ≤ 1 marjinal 7,5 · ≤ 1,5 yeterli 3 · üstü güçlü 0. KV borç ≤ 0 ya da boşsa hesaplanmaz |
| | ICR (5): FAVÖK / faiz gideri | FAVÖK < 0 operasyonel zarar 5 · < 1 faiz ödenemiyor 5 · ≤ 1,5: 4 · ≤ 2,5: 2,5 · ≤ 4: 1 · üstü 0. Faiz gideri ≤ 0 ya da boşsa hesaplanmaz |
| Trend (20) | Özkaynak Trend (10), FAVÖK Trend (10) | Sorgudaki etiket: pozitife dönüş / büyüme 0 · sabit 3 · negatifte iyileşme 4 · pozitifte kötüleşme 7 · negatifte kötüleşme 8,5 · negatife geçiş 10 |

- **Trend ön koşulu:** önceki tablo 11-13 ay önce olmalı; sorgu `ccy_code` ve `prev_ccy_code` verirse para birimi de aynı olmalı. Değilse trend hesaplanmaz ve detayda nedeni yazar ("tablolar arası 24 ay"). Bilinmeyen etiket de hesaplanmaz.
- **Sınır değerleri** alt banda girer (likidite 0,80 → zayıf; ICR 1,5 → çok düşük).
- **Yalnızca raporlanan:** sert likidite (nakit / KV borç), bankadaki döviz mevduatı ve kredisi, ICR değeri, `DSCR_ICR_Ayrisma` (DSCR > 1,2 ama ICR < 1,5: DSCR kolonu anaparayı dışarıda bırakıyor olabilir).
- **Log:** alt puanı borçluların yarısından fazlasında hesaplanamayan kolonlar (kaynak sorunu), skor dağılımı (p25-p90), ayrışan şirket sayısı ve banka kapsamasıyla dirençli sayılan şirket sayısı.

**Finansal seviye: 50 ve üstü Stage 2, 80 ve üstü Stage 3** (yönetici kararı, 11 Kasım; `ews_financial.FIN_STAGE2_ESIK` / `FIN_STAGE3_ESIK`). Haber skorunun eşikleri farklı kalır (25 / 65).

**Doğrulanmamış girdiler:** `bank_fx_deposit`, döviz kredisi kolonları ve 1 mn tamponun para birimi; `dscrebitda_try`'nin paydası; `interestexpense_try`'nin işareti (sorgu `greatest(x, 0)` alıyor, gider negatif tutuluyorsa ICR hiç hesaplanmaz); `cash_in_banks` likit aktiflerin içinde mi.

## 8. Birleşik seviye ve skor

1. **Kâr düşüşü + zayıf finansal:** Odak haberde kâr düşüşü varsa, haber seviyesi Stage 1 ise ve finansallar zayıfsa (DSCR < 1,2 ya da özkaynak veya FAVÖK negatif), haber seviyesi Stage 2'ye, haber skoru en az 25'e çıkar. Haberi finansalla bağlayan tek kural budur.
2. **Birleşik seviye** = max(haber seviyesi, finansal seviye).
3. **Birleşik skor** = max(haber skoru, haber ölçeğine çevrilmiş finansal skor). Finansal skor kendi bandı içinde doğrusal olarak haber bandına taşınır (0-50 → 0-25, 50-80 → 25-65, 80-100 → 65-100); böylece birleşik skor her zaman birleşik seviyenin aralığına düşer. `Finansal_Skor` sütunu çevrilmemiş değeri gösterir.
4. `Birlesik_Kural`: seviyenin kaynağı. Değerleri: `haber skoru`, `finansal skor`, `haber skoru ve finansal skor`, `kâr düşüşü + zayıf finansal`, `risk sinyali yok`.

## 9. Ayarlar

Seviye, puan ve eşik ayarları değişince LLM'e yeniden sorulmaz, bir sonraki koşuda yalnızca hesap değişir. Olay tiplerinin tanımı (prompt metni) değişirse `ews_olay.PROMPT_SURUMU` artırılır ve sorular yeniden sorulur.

| Ne | Nerede |
|---|---|
| Olay tipleri ve seviyeleri | `ews_olay.py` → `OLAY_TIPLERI` |
| Skor eşikleri (25 / 65) | `ews_olay.py` → `STAGE2_ESIK`, `STAGE3_ESIK` |
| Olay puanları, tazelik (`TAZELIK_TAM_GUN` 90, `TAZELIK_SIFIR_GUN` 180), risk türü çarpanı, %25 | `ews_defter.py` → "Haber skoru" bölümü |
| Kapanan ve eskiyen olayın seviyesi; eskime günü (`ESKI_OLAY_GUN` 90) | `ews_defter.py` → `Olay.stage`, "Haber skoru" bölümü |
| Zayıf finansal eşiği (1,2) | `4_llm_dogrulama.py` → `ZAYIF_DSCR` |
| Finansal alt puanlar ve DSCR aralıkları | `ews_financial.py` |
| Haber penceresi (180 gün; tazeliğin sıfır günüyle aynı tutulmalı) | `3_eslestirme.py` → `AGGREGATION_WINDOW_DAYS` |

## 10. Sınırlar

- Skor bir temerrüt olasılığı değil, kanıtlı olaylara dayanan bir sıralamadır. Parametreler kalibre edilmedi; Kontrol sayfasındaki analist notları biriktikçe ayarlanmalıdır.
- 180 günden eski olaylar defterden düşer; 90 günü geçen olay seviyeye bir kademe düşük katkı yapar. Süren ama haberi kesilen bir süreç (örneğin konkordato mühleti) bu yüzden zamanla görünmez olur; ayrıca izlenmelidir.
- Grup şirketlerinin olayları müşteriye yansımaz (grup bilgisi bekleniyor).
