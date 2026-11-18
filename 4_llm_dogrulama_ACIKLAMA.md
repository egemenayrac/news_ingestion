# `4_llm_dogrulama.py` — ne yapar, nasıl çalışır, sırada ne var

> Seviye ve skor kurallarının kısa ve kesin özeti: [SKORLAMA_KURALLARI.md](SKORLAMA_KURALLARI.md).
> Bu depodaki sürümde müşteri adları "Şirket A, B…" ile değiştirildi.
> Bütün projenin güncel açıklaması (motivasyon, gelişim, kararlar, açık sorunlar): [PROJE_ACIKLAMASI.md](PROJE_ACIKLAMASI.md). Bu belgenin bazı kısımları eskidir.

## 1. Tek cümleyle

**Eşleştirme geniş tarar, LLM okur ve karar verir, kod doğrular, insan örneklerle denetler.**

`3_eslestirme.py` haberleri alias'larla şirketlere bilerek geniş eşleştiriyor: bir haberin kaçmaması için
yanlış eşleşmeye razı olunuyor. Puan üretmiyor. Bu script her eşleşmeyi bankanın iç LLM'ine okutuyor ve
iki soru soruyor:

1. **Alaka:** Haberdeki ad gerçekten bu şirket mi, haber şirketle mi ilgili?
2. **Olay:** Ne oldu? Olay sabit bir listeden seçilir ve haberden birebir alıntıyla kanıtlanır.

Haber tarafındaki karar tamamen LLM'den geliyor (22 Ekim'de regex skoru kaldırıldı; bkz. karar kaydı 16-17).
Finansal skor yöneticinin `ews_financial` modelinden geliyor ve bu script içinde hesaplanıyor.

## 2. Hangi sorunları çözüyor

| Yöneticinin gördüğü sorun | Script ne yapıyor |
|---|---|
| **Yanlış eşleşme** ("ANKARA 2" ↔ "Ankara 2. Asliye Ceza Mahkemesi") | LLM eşleşmeye "başka varlık" diyor ve eşleşme alarm üretmiyor |
| **Puanın ne kadar doğru olduğu bilinmiyor** | Puan yerine **kanıtlı olay** üretiyor. "TMSF'ye devredildi" + haberden alıntı + link: analist açıp kontrol edebilir |
| **Alias'ları elle bakmak gerekiyor** | LLM'in kararları alias bazında toplanıyor. Hiç gerçek haber getirmeyen alias için ALIAS_RULES'a yapıştırılacak satır hazır geliyor |

**Neden LLM'e puan verdirmedik?** Temerrüt ya da rating düşüşü gibi bir hedef değişken olmadığı için hiçbir
puan kalibre edilemez. LLM'in kendi "güven yüzdesi" de güvenilir değil. Kanıtlı bir olay ise doğrulanabilir.

## 3. Akış

```
news.parquet + portfolio_config.py
        │
        ▼
3_eslestirme (yalnızca eşleştirme, bütün alias'lar)   ← script bunu kendisi çağırır, ayrıca çalıştırmaya gerek yok
        │  her (haber, şirket) eşleşmesi
        ▼
Önbellekte cevap var mı? ── evet ──► LLM'e sorulmaz
        │ hayır
        ▼
Soru 1 — ALAKA:  odak | ikincil | alakasiz | baska_varlik   (+ tek cümlelik gerekçe)
        │ yalnızca odak / ikincil ise
        ▼
Soru 2 — OLAY:   olay tipi + durum (gerçekleşti / iddia / yalanlandı-çözüldü) + risk türü (kredi / hukuki-itibar)
                 + alıntı + özet
        │
        ▼
Kod kontrolleri: alıntı haberde geçiyor mu? Olay yalanlanmış mı? Seviye ne?
        │  yalnızca alıntısı doğrulanmış CRITICAL / HIGH olaylar (kapanış haberleri dahil)
        ▼
Soru 3 — OLAY DEFTERİ (şirket başına, tarih sırasıyla):
         defterdeki olaylar gösterilir → yeni olay mı, mevcut olay mı, açık bir olayı kapatıyor mu?
        │
        ▼
Finansal skor (ews_financial, structured_data.parquet) ve birleştirme  → Birlesik sayfası
        │
        ▼
Soru 4 — TEMA (tek soru, bütün portföy): birden çok müşteriyi etkileyen ortak gelişmeler  → Temalar sayfası
        │
        ▼
llm_dogrulama.xlsx  (8 sayfa)
```

### Seviye kuralları (kararı LLM değil kod verir)
- Olay tipi → seviye eşlemesi sabittir: **CRITICAL → Stage 3**, **HIGH → Stage 2**, diğerleri Stage 1.
- Bir olay şu durumlarda sayılmaz: alıntı haberde geçmiyorsa (LLM uydurmuş demektir), olay "yalanlandı/çözüldü" ise ya da alaka "alakasız" veya "başka varlık" ise.
- Şirketin sinyaline **"odak"** haberlerin olayları girer. **CRITICAL** olaylar (TMSF devri, haciz, konkordato) "ikincil" haberde de sayılır.
- **Şirket sinyali olay defterinden hesaplanır** (bkz. aşağıdaki bölüm): açık CRITICAL olay → Stage 3; kapanmış CRITICAL ya da açık HIGH olay → Stage 2; kapanmış HIGH → Stage 1. Aynı olayı anlatan birden çok haber tek olay sayılır.

### Haber skoru (0-100)
Seviyeyi (Stage) olay defteri kuralı belirler; skor şirketleri o seviyenin bandı içinde sıralar: **Stage 1 → 0-24,9, Stage 2 → 25-64,9, Stage 3 → 65-100** (bir ondalık hassasiyet; dashboard da yuvarlamaz). Bantlar finansal skorun eşikleriyle (25 / 65) aynı olduğu için `Birlesik_Skor = max(Haber_Skoru, Finansal_Skor)` her zaman birleşik seviyeyle tutarlıdır. LLM'e sayı sorulmaz; skor koddaki kuralla hesaplanır:

- **Olay puanı:** açık kritik 80, kapanmış kritik 45, açık yüksek 45, kapanmış yüksek 15; odak haberdeki her farklı orta seviyeli olay tipi 8.
- **Tazelik:** olayın son haberi 90 gün içindeyse puanın tamamı; 90-180 gün arasında doğrusal azalır, 180. günde 0 (haber penceresi 180 gün).
- **Risk türü:** hukuki/itibar olaylarında puan ×0,8.
- **Birden çok olay:** en yüksek puan + diğerlerinin toplamının %25'i, sonra seviyenin bandına sığdırılır.
- `Skor_Hesabi` kolonu hesabı yazar, örneğin `[O1] haciz_el_koyma açık 79 + %25 x ([O2] dava_ceza açık 28, orta: kar_dususu 8; toplam 36) = 88`.

Değerler `ews_defter.py`'nin "Haber skoru" bölümünde ayardır (`OLAY_PUANI`, `ORTA_OLAY_PUANI`, `HUKUKI_ITIBAR_CARPANI`, `TAZELIK_INDIRIMI`, `EK_OLAY_ORANI`). Kesinlik iddiası yoktur: amaç aynı seviyedeki şirketleri makul bir sıraya koymak; ayarlar değişince LLM'e yeniden sorulmaz.

### Risk türü: kredi mi, hukuki-itibar mı
Olay sorusunda LLM, olayın **risk türünü** de seçer:
- `kredi`: olay şirketin borcunu ödeme gücünü doğrudan etkiler (temerrüt, zarar, üretimin durması, varlıklara el konması).
- `hukuki_itibar`: hukuki ya da itibar riski; ödeme gücüne etkisi henüz belirsiz (yöneticilere soruşturma, sahiplere yurt dışı yasağı).

Risk türü **stage'i değiştirmez**; analiste "bu alarm bugün mü ödemeye dokunuyor, yoksa izlenecek bir hukuki süreç mi?" bilgisini verir. Örnek: Şirket A'daki tedbir ve soruşturmalar ağır olaylar ama şirketin nakit akışı şu an sağlam; kredi açısından okunuşu bir haciz ya da temerrütten farklı. Bir olaya bağlanan haberlerden biri bile `kredi` derse olay `kredi` sayılır.

### Olay defteri: neden ve nasıl
**Sorun:** Haberler tek tek değerlendirilince sonradan gelen "tedbir kaldırıldı" haberi yalnızca kendisini etkisiz yapıyordu. Daha önceki "tedbir konuldu" haberi 90 gün boyunca kırmızı alarm üretmeye devam ediyordu. Örnek Şirket A: 26 Eylül'de şirketin ve sahiplerinin varlıklarına tedbir konuldu, 27 Eylül'de **şirketin** tedbiri kaldırıldı, 29 Eylül'de sahiplerine yurt dışı yasağı geldi. Sistem bunları ayrı olaylar olarak izleyemiyordu.

**Nasıl çalışır:**
1. Her şirket için riskli haberler (alıntısı doğrulanmış, tipi CRITICAL ya da HIGH) tarih sırasıyla işlenir. Haberlerde saat bilgisi olmadığı için aynı gün içinde açılış haberleri kapanış haberlerinden önce gelir.
2. Defter boşsa haber doğrudan yeni bir olay açar ve LLM'e sorulmaz. Defter doluysa LLM'e o şirketin olayları (kod, tip, durum, açılış tarihi, kısa tanım) gösterilir ve haberin **yeni** bir olay mı açtığı, **mevcut** bir olayı mı anlattığı, yoksa açık bir olayı mı **kapattığı** sorulur. Prompt, şirkete konan tedbirle sahibine konan tedbirin ayrı olaylar olduğunu ve itirazın olay kapatmadığını özellikle belirtir.
3. Kod, LLM'in cevabını şu güvenlik kurallarıyla uygular:
   - Bir olay yalnızca haberin kendi durumu "yalanlandı/çözüldü" ise kapanır. LLM "kapat" dese bile bu şart yoksa olay açık kalır.
   - Aynı olaya bağlanan daha ağır bir haber (örneğin davadan hacze) olayın seviyesini yükseltir. Kapanış haberi yükseltmez ("konkordato iddiası yalanlandı" açık bir davayı konkordatoya çevirmez).
   - Bir kapanış haberi defterde karşılığı olmayan bir olayı kapatıyorsa (açılış haberi 90 günlük pencerenin dışında kalmış olabilir), olay deftere "kapandı" olarak girer.
4. Kapanan olay silinmez; açılışı, kapanışı ve kapanış kanıtıyla görünür kalır.
5. Defter cevapları da önbelleğe yazılır. Önbellek anahtarı defterin o anki halini içerdiği için defter değişmedikçe soru tekrar sorulmaz. LLM hata verirse haber yeni olay sayılır, hiçbir olay kapatılmaz ve soru sonraki koşuda tekrar sorulur.

### Olay tipleri

| Seviye | Tipler |
|---|---|
| CRITICAL | konkordato_iflas, temerrut, haciz_el_koyma (TMSF devri ve sahibin mal varlığına el konması dahil), faaliyet_sona_erme (bütün üretimin/faaliyetin süresiz durması, bütün tesislerin kapanması) |
| HIGH | borc_yapilandirma, dava_ceza (ceza soruşturması, gözaltı, devam eden kurum soruşturması), finansal_bozulma (zarar, sermaye kaybı), faaliyet_kesintisi (günlerce süren duruş, tek bir tesisin ya da hattın kapanması), not_dusurme, yonetim_krizi |
| MODERATE | kar_dususu (şirket hâlâ kârlı), dis_baski (talep, kur, anti-damping, sektör soruşturması), operasyonel_aksaklik (kısa arıza, tek seferlik iptal, tekil personel olayı), idari_ceza (sonuçlanmış ve ödenmiş/uzlaşılmış ceza) |
| sinyal değil | olumlu, rutin (planlı bakım, olağan atama dahil), hicbiri |

Her tipin bir **"DAHİL DEĞİL"** listesi var. Bu listeler ilk koşudaki aşırı alarmlardan çıkarıldı.

## 4. Girdi ve çıktı

**Girdi**
- Haber dosyası (`news.parquet`; yolu `3_eslestirme.py`'deki `PARQUET_PATH`) ve `portfolio_config.py`
- Bankanın LLM'i (dosyanın başındaki `LLM = {...}` ayarı)
- `llm_cache.jsonl` (varsa)
- `structured_data.parquet` (varsa): `2_sql_to_parquet.py`'nin Oracle'dan ürettiği finansallar ve bakiye. Script Oracle'a kendisi bağlanmaz; dosya yoksa uyarı verir ve Birlesik sayfası yalnızca haber sinyaliyle üretilir.

**Çıktı:** `llm_dogrulama.xlsx`. Dosya Excel'de açıksa script yeni sonucu `llm_dogrulama_TARİH.xlsx` adıyla yazar. Ayrıca `dashboard.html` (aşağıdaki "Dashboard" bölümü) ve `dashboard_gecmis.jsonl` (günlük sinyal geçmişi).

| Sayfa | İçinde ne var | Ne için |
|---|---|---|
| **Birlesik** | Portföydeki her şirket için tek satır: haber sinyali (LLM) + finansal skor + bakiye, ikisinin yükseği birleşik seviye. Sıralama: seviye, sonra bakiye | **Yöneticiye gidecek tablo:** "kimde sorun var ve orada ne kadar riskimiz var?" |
| **Şirketler** | Şirket başına bir satır: LLM sinyali (olay defterinden), açık ve kapanmış olay sayıları, `Risk_Olaylari` (defterdeki olaylar durumlarıyla ve orta seviyeli olaylar), `Olumlu_Olay`, en ağır olay ve onun ilk haberinin kanıtı | Ana sonuç |
| **Olay_Defteri** | Olay başına bir satır: tip, seviye, durum (açık / kapandı), sinyale etkisi, tanım, açılış ve kapanış tarihi, haber ve kaynak sayısı, zaman çizelgesi, kapanış kanıtı | Bir şirkette ne olduğunun hikâyesi |
| **Temalar** | Birden çok müşteriyi etkileyen ortak gelişmeler (örneğin bir fon krizi, akaryakıt şoku): etkilenen şirketler, toplam bakiye | Portföy düzeyinde yoğunlaşma |
| **Kayıtlar** | Her haber × şirket eşleşmesi için LLM'in bütün kararları (en yeni haber önce) | Ayrıntı |
| **Kontrol** | LLM kararlarından örnek: risk olayı, orta seviye, elenen eşleşme ve olaysız haber gruplarından 15'er kayıt. `Analist_Notu` boş gelir; daha önce not yazılmış kayıt bir daha gelmez | Analistin LLM'i denetlediği liste |
| **Kontrol_Etiketleri** | Şimdiye kadar yazılmış bütün notlar, `Sonuc` (doğru / yanlış / diğer) ve `Etiket_Tarihi` ile | İsabet ölçümü; not burada düzeltilebilir |
| **Alias** | Alias başına eşleşme sayısı, reddedilme oranı ve `Cikar_Oner` | Alias kalitesi |
| **Alias_Onerisi** | `2_sql_to_parquet.py` ALIAS_RULES'a doğrudan yapıştırılacak satırlar, şirket başına bir satır | Alias bakımı |

Kayıtlar sayfasının önemli kolonları:
- `LLM_Alaka` ve `LLM_Alaka_Gerekce`
- `LLM_Olay`, `LLM_Durum`, `LLM_Risk_Turu` ve `LLM_Olay_Seviyesi`
- `LLM_Seviye` (kayıt düzeyinde stage)
- `LLM_Alinti` ve `Alinti_Dogrulandi`
- `Olay_No` ve `Defter_Islem`: haberin defterde hangi olaya bağlandığı ve yeni / mevcut / kapat işlemi
- `LLM_Hata`

### Kolon sözlüğü

**Birlesik**
- `Birlesik_Seviye`: haber ve finansal seviyenin yükseği (yöneticinin eski 3_scoring'deki birleşik tablosunun max kuralı; haber tarafı LLM'den).
- `Birlesik_Skor` (0-100): haber ve finansal skorun yükseği; sayfa buna göre, sonra bakiyeye göre sıralıdır.
- `Haber_Skoru`, `Skor_Hesabi`: yukarıdaki "Haber skoru" bölümü.
- `Birlesik_Kural`: seviyenin nereden geldiği: `haber skoru`, `finansal skor`, `haber skoru ve finansal skor`, `kâr düşüşü + zayıf finansal` ya da `risk sinyali yok`.
- **Kâr düşüşü kuralı:** Odak haberde kâr düşüşü varsa ve finansallar zayıfsa (DSCR < 1,2 ya da özkaynak veya FAVÖK negatif) haber sinyali Stage 2'ye çıkar. Kâr düşüşünün ne kadar önemli olduğuna bilanço karar verir: borcu yüksek şirkette önemlidir, nakdi bol şirkette değildir.
- `Haber_Sinyali`, `Risk_Olaylari`, `En_Agir_Olay`, `En_Agir_Durum`, `En_Agir_Risk_Turu`: Şirketler sayfasından. Haberi olmayan şirkette boş.
- `Finansal_Skor`, `Finansal_Seviye`, `Finansal_Puan_Detay`, `DSCR_Degeri`, `Finansal_Tarih`: yöneticinin `ews_financial` hesabı; seviye `FIN_STAGE2_ESIK` (50) ve `FIN_STAGE3_ESIK` (80) eşikleriyle (`ews_financial.py`).
- `Not`: "haberi yok" ya da "finansal veri yok".

**Temalar**
- `Tema`, `Aciklama`: LLM'in verdiği ad ve tek cümlelik açıklama.
- `Sirket_Sayisi`, `Sirketler`, `Olay_Tipleri`: temaya giren müşteriler ve olay tipleri. En az 2 farklı şirket içeren temalar tutulur.
- `Toplam_Bakiye_TRY`: temadaki şirketlerin toplam bakiyesi (finansal veri varsa). `En_Yuksek_Haber_Sinyali`: temadaki en ağır haber sinyali.
- Nasıl üretilir: alıntısı doğrulanmış bütün risk olayları (şirket, olay tipi, kısa özet ve haberden alıntı; en fazla 300 tanesi, en ağırları önce) tek bir soruda LLM'e verilir ve ortak gelişmeleri gruplaması istenir. Prompt'ta örnek tema yoktur ve satırlarda yazmayan bir konu eklemek yasaktır. Her olay en fazla bir temaya girer. Tema sorusu hata verirse sayfa boş gelir, diğer sayfalar etkilenmez.
- **Uydurmaya karşı kontrol:** LLM her tema için bir `Ortak_Ifade` yazar. Kod bu ifadenin anlamlı kelimelerinden birinin her maddenin özetinde ya da alıntısında geçtiğini arar (ek farkı yok sayılır); geçmeyen madde temadan çıkar, 2'den az şirket kalan tema atılır.
- `Kanit`: temaya giren her haber (şirket, başlık, tarih). Dashboard'da ayrıca link ve alıntı görünür.
**Şirketler** (sıralama: LLM sinyali → haber skoru → kritik olay → odak haber sayısı)
- `LLM_Sinyali`: şirketin LLM'e göre en ağır stage'i. Yalnızca odak haberler ve ikincil haberlerdeki CRITICAL olaylar sayılır.
- `Haber_Skoru` (0-100) ve `Skor_Hesabi`: bkz. "Haber skoru".
- `Acik_Kritik` / `Acik_Yuksek` / `Kapanan_Olay`: defterdeki açık kritik, açık yüksek ve kapanmış olay sayısı.
- `Eslesme`, `Odak_Haber`, `Reddedilen`: eşleşen haber sayısı; bunlardan "odak" olanlar; "alakasız" ya da "başka varlık" olanlar.
- `Risk_Olaylari`: defterdeki olaylar, örneğin `[O1] haciz_el_koyma (kapandı 2026-09-27); [O2] dava_ceza (açık)`. Ardından odak haberlerdeki orta seviyeli olay tipleri `orta: ...` olarak gelir. `Olumlu_Olay`: olumlu olay içeren haber sayısı.
- `En_Agir_Olay` / `En_Agir_Durum` / `En_Agir_Risk_Turu`: sinyali belirleyen olay ve durumu. `En_Agir_Haber/Tarih/URL/Alinti`: o olayın ilk haberinin kanıtı.

**Olay_Defteri**
- `Olay`: şirket içindeki olay kodu (O1, O2…). `Sinyale_Etkisi`: olayın şirket sinyaline katkısı (kapanan ve son haberi 90 günü geçen olay birer kademe düşer). `Son_Haber`: olaya bağlanan son haberin tarihi. `Risk_Turu`: kredi ya da hukuki_itibar.
- `Tanim`: olayı açan haberin LLM özeti. `Haber_Sayisi` / `Kaynak_Sayisi`: olaya bağlanan haber ve farklı kaynak sayısı.
- `Zaman_Cizelgesi`: örneğin `2026-09-26 açıldı (CNBC-e) → 2026-09-27 haber (Ekonomim) → 2026-09-27 KAPANDI (Ekonomim)`.
- `Kapanis_URL` / `Kapanis_Alinti`: olayı kapatan haber ve alıntısı.

**Kayıtlar** (en yeni haber önce)
- `LLM_Alaka` (+ `LLM_Alaka_Gerekce`), `LLM_Olay`, `LLM_Durum`, `LLM_Olay_Seviyesi` (boşsa risk yok), `LLM_Seviye`.
- `LLM_Seviye` kayıt düzeyindedir: ikincil haberdeki HIGH bir olay burada Stage 2 görünür ama şirket sinyaline girmez.
- `Metin_Snippet`: 3_eslestirme'nin kestiği pencere (şirket adından 25 kelime önce, 60 kelime sonra; başlık başa eklenir). Cümle ortasından başlayıp bitmesi tasarım gereğidir. LLM'in alaka sorusunda kullanılır; olay sorusunda haberin kendisi (en fazla 6.000 karakter) gönderilir.
- Alanların üst sınırları: alıntı 300, gerekçe 250, özet 200 karakter. LLM uzun bir alıntıyı kendisi "..." ile de kısaltabilir.

**`Alinti_Dogrulandi`**
- TRUE: alıntı haberde geçiyor, olay sayılır.
- FALSE: alıntı haberde yok (LLM uydurmuş ya da metni bozmuş), olay sayılmaz.
- Boş: kontrol edilecek bir risk olayı yok. Ya olay sorusu hiç sorulmadı (alakasız / başka varlık) ya da olay olumlu, rutin veya "olay yok" çıktı.

**Kontrol:** Kayıtlar sayfasının kolonları, başta `Grup`, sonda `Analist_Notu`. Analist her satıra "doğru" ya da "yanlış: ..." yazar. Hedef değişken olmadığı için LLM'in isabetini ölçmenin yolu budur; dolan sayfalar sonraki prompt değişikliklerinde test seti olarak da kullanılır (değişiklik bir şeyi bozdu mu?). Seçim rastgele ama tekrarlanabilir.

**Notlar kaybolmaz:** Excel her koşuda baştan yazılır; bu yüzden betik yazmadan önce önceki Excel'in Kontrol ve Kontrol_Etiketleri sayfalarındaki dolu notları `kontrol_etiketleri.jsonl` dosyasına ekler (Excel açıkken zaman damgalı adla yazılmış kopyalar da okunur; aynı kayıtta en son kaydedilen dosya geçerlidir). Not yazılmış kayıt bir daha örneklenmez, böylece her koşuda yeni kayıtlar gelir. Notun başı "doğru" / "d" ya da "yanlış" / "y" ise isabete sayılır; koşu sonunda log grup başına isabeti yazar ("risk olayı 14/15 doğru"). Eski bir not Kontrol_Etiketleri sayfasında değiştirilerek düzeltilir.

### Diğer çıktılar

**`llm_cache.jsonl`:** LLM'in her cevabı buraya yazılır. Bir sonraki koşuda yalnızca yeni eşleşmeler sorulur; script yarıda kesilirse kaldığı yerden devam eder. `PROMPT_SURUMU` değişirse bütün eşleşmeler yeniden sorulur. Tema sorusu da önbelleğe yazılır; olay listesi değişmedikçe tekrar sorulmaz.

**Konsol:** Yalnızca ilerleme satırları ve hatalar. En sonda şu iki satır çıkar:
`Bitti (X dk): N yeni cevap önbelleğe yazıldı, H hata. Toplam T eşleşmenin C tanesinin LLM cevabı var. Olay defteri: E olay (A açık, K kapandı). Tema: M.`
ve çıktı dosyasının yolu. C = T ise bütün eşleşmeler cevaplanmış demektir.

### Dashboard
`dashboard.html` her koşunun sonunda yeniden yazılır. Tarayıcıda çift tıklanarak açılır; sunucu, kurulum ya da internet gerekmez (dış kütüphane yok). Excel gibi müşteri adı ve bakiye içerir.

| Sekme | İçinde ne var |
|---|---|
| **Özet** | Stage 3/2/1 şirket sayısı ve bakiyesi, bakiyenin seviyelere dağılımı, Stage 2+ şirketlerde seviyenin kaynağı (haber mi finansal mı), günlük trend, haberden alarm alan şirketler, temalar |
| **Ne değişti** | Bugünkü koşu seçilen bir geçmiş koşuyla karşılaştırılır: önceki koşu, 1 hafta, 1 ay, 3 ay, 6 ay ya da listeden bir tarih (o tarihte koşu yoksa önceki en yakın koşu). Seviyesi yükselen ve düşen şirketler; yeni, kapanan, ağırlaşan, 90 günü geçen ve defterden çıkan olaylar. Analistin her sabah bakacağı sayfa |
| **Şirketler** | Portföyün tamamı: arama, seviye filtresi, sıralama |
| **Temalar** | Ortak temalar; şirket adına tıklanınca detay açılır |
| **Sistem kalitesi** | LLM'in elediği eşleşme oranı, alıntı doğrulama, ilgili haberlerde bulunan olay tipleri, en çok reddedilen alias'lar |

Herhangi bir şirkete tıklanınca **şirket detayı** açılır: olay defteri (zaman çizelgesi, alıntı ve haber linkleriyle), orta seviyeli olaylar ve olumlu haberler, finansal puan detayı.

**Geçmiş:** `dashboard_gecmis.jsonl` her gün için bir satır tutar (en fazla 400 gün); aynı gün tekrar koşulursa o günün satırı yenilenir. Son 200 gün dashboard'un içine gömülür, karşılaştırma tarayıcıda yapılır; bu yüzden dashboard açıkken dönem değiştirmek için script'i yeniden çalıştırmak gerekmez. Olaylar haberleri üzerinden eşleştirilir (iki günde ortak haberi olan olay aynı olaydır), çünkü O1, O2 kodları 90 günlük pencere kaydıkça değişebilir. Dosya silinirse trend ve karşılaştırma sıfırdan başlar. LLM hatası olan bir koşuda bazı sinyaller eksik kalabilir; sayfa bunu üstte uyarı olarak gösterir.

## 5. Ayarlar (`4_llm_dogrulama.py`'nin başı)

| Ayar | Anlamı |
|---|---|
| `LLM` | url (`/v1/chat/completions` dahil tam adres), key (API anahtarı), model (boş bırakılır) |
| `LLM_VERIFY_SSL` | `"windows"`: bankanın sertifikası Windows deposundan alınır. `False`: yalnızca deneme için |
| `LLM_WORKERS` | Aynı anda kaç soru sorulacağı. `HTTP 429` (çok fazla istek) hatası alınırsa 1'e düşürülür |
| `LIMIT` | 0 = hepsi. Deneme için örneğin 20 |
| `ALIAS_MIN_ESLESME` | Bir alias'ın çıkarılması önerilmeden önce en az kaç eşleşmesi olmalı |
| `ZAYIF_DSCR` | Kâr düşüşü kuralında "zayıf finansal" eşiği (1,2) |

Prompt sürümleri, prompt'ların yanında durur. Prompt değişince ilgili sürüm artırılır ve yalnızca o sorular yeniden sorulur:
- `ews_olay.PROMPT_SURUMU` (şu an 5): alaka ve olay soruları
- `ews_defter.DEFTER_SURUMU`: defter soruları
- `ews_portfoy.TEMA_SURUMU`: tema sorusu

## 6. Dosyalar ve fonksiyonlar

Kod sekiz dosyadır ve hepsi aynı klasörde durur (yöneticinin `ews_text.py`, `ews_financial.py` ve `portfolio_config.py`'si de). Çalıştırılan dosya yalnızca `4_llm_dogrulama.py`'dir.

| Dosya | Görevi | Önemli fonksiyonlar |
|---|---|---|
| `3_eslestirme.py` | Haberleri okur (90 günlük pencere), alias'larla şirketlere eşleştirir, metin kesiti çıkarır, tekrarları ayıklar. Skor üretmez | `run_matching`, `match_record`, `build_dedup_keys` |
| `4_llm_dogrulama.py` | Ayarlar ve akış: eşleştir → sor → defter → tablolar → Excel | `run`, `load_matches`, `ask_matches` |
| `ews_llm.py` | LLM istemcisi ve önbellek | `LLMClient.ask`: tek soru sorar, JSON okur. Geçici hatalarda 3 kez dener; `HTTP 429` gelirse sunucunun `Retry-After` süresi, yoksa 30/60 sn bekler (üstüne 0-3 sn rastgele pay). `Cache`: `llm_cache.jsonl`'u okur, her cevabı anında yazar. `resolve_ssl_verify`: `"windows"` ise sertifikaları dosyaya yazar |
| `ews_olay.py` | Olay sözlüğü (`OLAY_TIPLERI`), alaka ve olay prompt'ları, cevapların kayıtlara işlenmesi | `ask_pair`: alaka, ilgiliyse olay sorusu; tanımsız cevap güvenli tarafa. `quote_found`: alıntı haberde geçiyor mu. `apply_results`: seviye kuralları |
| `ews_defter.py` | Olay defteri | `Olay`: tek olay (durum, seviye, haberler; `stage` sinyale etkisi). `build_ledgers`: aday haberleri seçer, şirket başına defteri kurar. `annotate`: kayıtlara `Olay_No` ve `Defter_Islem` ekler |
| `ews_tablolar.py` | Excel sayfaları | `build_company_table`, `build_ledger_table`, `build_record_table`, `build_check_table`, `build_alias_tables`, `write_excel` |
| `ews_portfoy.py` | Finansal skor ve portföy görünümleri | `load_financials`: `structured_data.parquet`'ten `ews_financial` ile finansal skor; `financial_stage`: skordan stage; `build_combined_table`: Birlesik; `build_themes`: Temalar (tek LLM sorusu) |
| `ews_dashboard.py` | Dashboard ve sinyal geçmişi | `write_dashboard`: `dashboard.html`'i yazar, geçmişe bugünün satırını ekler. Sayfa şablonu (HTML, CSS, JS) dosyanın sonunda |

Ne nerede değişir:
- Olay tipi ya da seviyesi → `ews_olay.py`'deki `OLAY_TIPLERI`.
- Şirket sinyali kuralı → `ews_defter.py`'deki `Olay.stage`.
- Birleşik seviye kuralı → `ews_portfoy.py`.
- Excel kolonları → `ews_tablolar.py`.

## 7. Güvenlik ve kontrol önlemleri
- **Serbest cevap yok.** LLM yalnızca verilen listeden seçebilir; tanımsız bir cevap güvenli tarafa çekilir.
- **Kanıt zorunlu.** Alıntının haberde geçip geçmediğini kod kontrol eder; geçmeyen olay sayılmaz.
- **Tekrarlanabilir.** `temperature = 0` kullanılır, her cevap önbellekte saklanır.
- **İnsan denetimi.** Kontrol sayfası her koşuda LLM kararlarından örnek sunar; her olayın kanıtı (alıntı ve link) Excel'de ve dashboard'da görünür.
- **Veri banka içinde kalır.** Model şirket içinde çalışan bir model.

## 8. Sonuçlar (bankadaki 90 günlük veri, 1.982 eşleşme)

Bu ölçümler regex skoru kaldırılmadan önce yapıldı; regex ile karşılaştırma bundan sonra tekrarlanamaz. Sunumda bu tablo kullanılabilir.

| | 1. sürüm | 2. sürüm (olay tanımları düzeltildi) |
|---|---|---|
| "Başka varlık" (yanlış eşleşme) | 599 (%30) | 598 |
| Doğrulanamayan alıntı | 7 / 132 | 5 / 150 |
| Kayıt düzeyinde Stage 2 / Stage 3 | 79 / 8 | 50 / 8 |
| Uyuşmazlık sayfası | 83 | 67 |
| **Şirket düzeyinde Stage 3** | 0 | **2** (Şirket B: TMSF devri; Şirket A: sahibin mal varlığına el konması) |
| Şirket düzeyinde Stage 2 | 19 | 13 |
| 20 | **Tema prompt'u örneksiz; ortak ifade kodla aranır** (tema 2. sürüm, 23 Ekim) | Bankada "anti-damping" teması, ilgili haberlerde bu bilgi görünmeden çıktı. Prompt'taki örnek tema adı ("AB çelik anti-damping vergisi") modeli yönlendiriyordu; temaların dayandığı ikincil haberler de dashboard'da görünmüyordu | `ews_portfoy.py` tema bölümü |
| 21 | **Tazelik: 90 güne kadar tam puan, 180. günde 0; haber penceresi 180 gün** (23 Ekim) | Eski olayın sıralamadaki ağırlığı zamanla azalsın. Pencere 90 günde kalsaydı kural hiç çalışmazdı | `ews_defter.TAZELIK_TAM_GUN` / `TAZELIK_SIFIR_GUN`, `3_eslestirme.AGGREGATION_WINDOW_DAYS` |
| 22 | **Son haberi 90 günü geçen olay seviyeye bir kademe düşük katkı yapar** (23 Ekim) | Üç ay yeni haber çıkmayan sorun için yeni kanıt yok; açık kritik olay Stage 2'de (yakın izleme) kalır. Seviye ve skor aynı yönde hareket eder | `ews_defter.ESKI_OLAY_GUN`, `Olay.stage` |
| LLM hatası | 0 | 13 (`HTTP 429`, sonraki koşuda kendiliğinden yeniden sorulur) |

**2. sürümde düzelenler:**
- Kârlı üç büyük şirketin kâr düşüşü Stage 2'den çıktı.
- Bir şirketin planlı bakımı ve bir başkasındaki olağan atama artık "rutin".
- Haberde geçen başka bir grubun krizi artık müşteriye yazılmıyor.
- Şirket B'nin TMSF devri şirket tablosunda Stage 3 olarak görünüyor.

**Yanlış eşleşmelerin kaynağı:** 12 alias, yanlış eşleşmelerin %88'ini üretiyor. Bunlar kişi adına benzeyen ya da kelime içinde geçen kısa alias'lardı.

**Kalan küçük aşırı alarmlar:**
- Tekil ve küçük olaylar "faaliyet kesintisi" sayılıyor: alkollü pilot olayı, kısa süreli baca dumanı, tek gecelik sefer iptali.
- Küçük idari para cezaları "dava/ceza" sayılıyor: 8 milyon TL'lik Rekabet cezası gibi.

## 9. Bilinen sınırlar
- Çıktı bir temerrüt olasılığı değil, **kanıtlı olay listesi**.
- Eşleşmeyen haberi LLM göremez. Basında kullanılan kısa adlar `2_sql_to_parquet`'teki ilk kelime kuralı ve `ALIAS_RULES` ile ekleniyor.
- Grup şirketlerinin haberleri "başka varlık" sayılıyor. Örneğin bir grup şirketinin haberi, aynı gruptaki başka bir müşteri için elendi. Bkz. karar kaydı, 5. madde. (13 Kasım'dan beri payı %50'den büyük ortakların haberleri eşleştiriliyor.)
- Aynı olay birden çok kaynakta çıkarsa alaka ve olay soruları her haber için ayrı soruluyor; defter ise bu haberleri tek olayın altında topluyor.
- Defter LLM'in bağlama kararına dayanıyor: yanlış olayı kapatabilir ya da aynı olayı iki kez açabilir. Kapatma şartı ve kapanmış kritik olayın yine Stage 2 vermesi bu riski sınırlıyor; Olay_Defteri sayfasındaki zaman çizelgesi kontrol için var.
- Haberlerin saati yok; aynı gün içindeki sıra yaklaşık.
- İlk tam koşu yaklaşık 50 dakika sürüyor. Sonraki koşularda yalnızca yeni haberler sorulduğu için birkaç dakika yetiyor.

## 10. Karar kaydı (yönetici izindeyken alındı; hepsi geri alınabilir)

| # | Karar | Gerekçe | Nasıl geri alınır |
|---|---|---|---|
| 1 | İki yeni orta seviyeli tip: `operasyonel_aksaklik`, `idari_ceza` (prompt 3. sürüm) | 2. sürümde kısa süreli baca dumanı, alkollü pilot olayı ve ödenmiş 8 milyon TL Rekabet cezası Stage 2 vermişti. Ayrım tutara göre değil olayın niteliğine göre yapıldı, çünkü LLM şirketin büyüklüğünü bilmiyor. Olaylar Excel'de görünmeye devam eder | `OLAY_TIPLERI`'nde seviyeyi `"HIGH"` yapmak. LLM'e yeniden sormak gerekmez |
| 2 | Suçlananın itirazı "yalanlandı" sayılmaz (prompt 3. sürüm) | Genel bir güvenlik kuralı: itiraz bir kararı ortadan kaldırmaz. (Not: Şirket A'nın "yalanlandı" sayılan 4 haberi aslında yetkili makamın şirket tedbirini kaldırdığını aktarıyordu; LLM onları doğru okumuştu. Kural bu haberleri değiştirmez.) | Prompt'taki cümleyi silmek |
| 3 | `kar_dususu` orta seviyede kalır | Kârlı şirketin kâr düşüşü kredi alarmı sayılmaz | `OLAY_TIPLERI`'nde `"HIGH"` |
| 4 | İkincil haberlerde yalnızca kritik olaylar şirketin sinyaline girer | Ana konusu başka olan haberdeki yan bilgiye dayalı alarmlar azalır | `ews_defter.build_ledgers`'taki aday koşulu |
| 5 | Grup kuralı **eklenmedi**, bekliyor | Grup bilgisi LLM'in hafızasına bırakılmamalı (küçük grupları bilmez, uydurabilir). DWH'ta risk grubu alanı bulunursa her şirketin grubu prompt'a veriden yazılacak | — |
| 6 | 429'da `Retry-After` ya da 30/60 sn bekleme | İlk tam koşuda 13 soru 5/10 sn bekleyip yine reddedildi | Yalnızca kod |
| 7 | Prompt'un sabit kısmı başa alınmadı | Kazanç belirsiz ve küçük. Prompt'un düzeni değişince cevaplar da kayabilir | — |
| 8 | Şirket sinyali olay defterinden hesaplanır | Kapanan olaylar ("tedbir kaldırıldı") sinyale yansımıyordu (Şirket A). Defter aynı olayın tekrar haberlerini de tek olayda topluyor | şirket sinyalini yeniden haber bazında hesaplamak |
| 9 | Kapanmış kritik olay Stage 2 verir, sıfırlanmaz | Kaldırılan tedbir de geçmişteki sıkıntının işaretidir; kredi açısından yakın izleme gerektirir | `Olay.stage`'de kapanan olay için 2 kademe düşürmek |
| 10 | Yeni kritik tip `faaliyet_sona_erme` (prompt 4. sürüm) | Şirket D'nin bütün üretimini durdurması "faaliyet kesintisi" (HIGH) sayılmıştı. Gelirin tamamen kesilmesi kredi açısından konkordato ile aynı ağırlıkta. Tek tesisin kapanması HIGH kalır | `OLAY_TIPLERI`'nde `"HIGH"` |
| 11 | Risk türü etiketi (`kredi` / `hukuki_itibar`); stage'i **değiştirmez** | Şirket A ve Şirket C'deki soruşturma ve tedbirler ağır olaylar ama ödeme gücüne etkileri henüz belirsiz. Etiket analiste bilgi verir; seviyeyi düşürmek ise yönetici kararı olmalı | Prompt'taki RİSK TÜRÜ bloğunu silmek |
| 12 | Birleşik seviye = max(haber, finansal) | Yöneticinin 3_scoring'deki birleşik tablosuyla aynı kural; yalnızca haber tarafı LLM'den | — |
| 13 | Kâr düşüşü + zayıf finansal (DSCR < 1,2 ya da negatif özkaynak/FAVÖK) → haber sinyali Stage 2 | `kar_dususu` orta seviyede kaldı (karar 3); ağırlığına bilanço karar verir. Güçlü bilançolu şirketlerde alarm üretmez | `ZAYIF_DSCR = 0` |
| 14 | Temalar en az 2 farklı müşteri içermeli | Tek şirketlik "tema" Şirketler sayfasında zaten var. Tema, portföy yoğunlaşmasını göstermek için (örneğin 2. sürümdeki fon krizi ~11 müşteriye dokunuyordu) | `ews_portfoy.build_themes`'teki koşul |
| 15 | Borsadaki "temerrüt satışı" temerrüt sayılmaz; kontrol altına alınan yangın `operasyonel_aksaklik` (prompt 5. sürüm) | 100 şirketlik banka koşusunda "… paylarında temerrüt satışı" haberi bir şirkete Stage 3 verdi; bu, payı alan yatırımcının takas yükümlülüğüdür. Bir yerleşkede söndürülen yangın Stage 2 vermişti | Prompt'taki DAHİL DEĞİL cümleleri |
| 16 | **Strict alias kaldırıldı, bütün alias'lar loose** (yöneticiyle, 22 Ekim) | Eşleştirme kaçırmamalı, yanlışları LLM elemeli. Yerel ölçüm: 100 şirkette eşleşme ×3 (bankada ~1.200 → ~3.600); yeni eşleşmelerin çoğu çöp (sıradan kelimeye benzeyen kısa adlar) ama strict yüzünden kaçan gerçek haberler de geliyor (marka adlarıyla onlarca haber). Config'teki "aday bekleyen" alias'lar ("AVRUPA", "PETROL", "BÜYÜK") eklenmedi: eşleşmeyi ×15-67 artırıp neredeyse yalnızca çöp getiriyordu | `3_eslestirme.build_alias_index` |
| 17 | **Regex haber skoru kaldırıldı; 3_scoring → 3_eslestirme** (yöneticiyle, 22 Ekim) | Haber riski LLM'den geliyor; ikinci bir skor kafa karıştırıyordu. Finansal skor 4_llm tarafına taşındı. Uyuşmazlık sayfası yerine Kontrol sayfası geldi | Yöneticinin `3_scoring.py`'si arşivde |
| 18 | **Haber skoru 0-100, olay defterinden kuralla** (22 Ekim) | Yönetici sayı istedi. LLM'e sayı uydurtulmadı; skor seviyenin bandı içinde kalır, böylece Stage ile çelişmez ve birleşik skor = max(haber, finansal) anlamlı olur. Parametreler ayardır | `ews_defter.py` "Haber skoru" bölümü |
| 19 | **Ortak alias'lar bütün sahiplerine verilir; 2_sql_to_parquet sadeleşti** (22 Ekim) | Eski kural ortak adı (aynı grubun birden çok şirketinde geçen ad) hiçbir şirkete vermiyordu: regex hangi şirket olduğunu ayıramazdı. Artık bu ayrımı LLM yapıyor. Kelime-içi eşleşmeye göre yazılmış içerme kuralı da kalktı. Kaybolan alias 0; 100 şirkette +129 eşleşme | `2_sql_to_parquet.py` |

## 11. Sıradaki adımlar

Güncel liste: [SONRAKI_ADIMLAR.md](SONRAKI_ADIMLAR.md).
