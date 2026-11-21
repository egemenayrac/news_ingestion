# EWS: sıradaki adımlar

Son güncelleme: 2 Kasım 2026. Liste, 26 Ekim'deki sistem incelemesinden çıktı ve önem sırasına göre dizildi. Her maddede neyin, neden yapılacağı, kimden ne gerektiği ve durumu yazıyor.

## Yapıldı (ofise aktarılmayı bekliyor)
- **Yalanlama ve kapanış haberi artık olayı ağırlaştırmıyor** (`ews_defter`). Önceden "konkordato iddiası yalanlandı" haberi, açık bir dava olayını açık bir konkordato olayına çevirip şirkete Stage 3 verebiliyordu.
- **Kontrol notları kaybolmuyor** (`ews_tablolar`, `4_llm_dogrulama`). Notlar her koşuda `kontrol_etiketleri.jsonl` dosyasına alınıyor. Not yazılmış kayıt bir daha örneklenmiyor. Yeni Kontrol_Etiketleri sayfası biriken notları gösteriyor; log da grup başına isabeti yazıyor.

## 1. Şimdi
1. **Üç dosyayı ofise aktar ve koş:** `4_llm_dogrulama`, `ews_defter`, `ews_tablolar`.
2. **Kontrol sayfasını analistlere doldurt.** Her koşuda 4 gruptan 15'er yeni kayıt geliyor. Her grupta 50 civarı not birikince isabet oranı bir anlam taşımaya başlar.
3. **Resmi ilanlar için hukuk sorusunu şimdiden sor** (bkz. 3. madde). Cevap zaman alır; bekleme süresinde geriye dönük teste odaklanırız.

## 2. Geriye dönük test (en önemli iş)
**Ne:** Sistemin geçmiş tarihlerde vereceği uyarıları, bankanın kendi kayıtlarındaki kötü sonuçlarla karşılaştırmak. Örnek: sistem 1 Temmuz'da bir şirkete Stage 2 dediyse ve şirket Eylül'de yapılandırmaya girdiyse, uyarı isabetli olmuş demektir. Yöneticinin "skor doğrulanamıyor" sorununa doğrudan cevap veren iş bu. Elle etiketleme gerektirmiyor.

**Nasıl:** Bir `5_geriye_test.py` yazılacak. Betik her ay sonu için yalnızca o tarihe kadarki haberlerle koşacak (`run_matching(as_of=...)`) ve sonuçları bankanın kayıtlarıyla birleştirecek. Ölçülecekler:
- olaydan önceki 6 ayda alarm verilen şirketlerin oranı,
- alarmın olaydan ne kadar önce geldiği,
- yanlış alarm sayısı (100 şirket-ay başına),
- haber skoru tek başına, finansal skor tek başına ve ikisi birlikte.

**Gerekenler (senden):**
- **Sonuç tablosu.** Müşteri numarası (`counterparty_id` ile eşleşebilen bir alan) ve olay tarihi gerekiyor. Aşağıdakilerden biri başlamak için yeterli: TFRS 9 stage geçmişi, izleme listesine alınma, yapılandırma, 90+ gün gecikme. En kolay bulunacak olan büyük ihtimalle TFRS 9 geçmişi; önce karşılık hesabını yapan ekibe sor. Aday tabloları DWH'ta aramak için:
  ```sql
  SELECT c.owner, c.table_name,
         LISTAGG(c.column_name, ', ') WITHIN GROUP (ORDER BY c.column_name) AS sutunlar
  FROM all_tab_columns c
  WHERE REGEXP_LIKE(c.column_name, 'STAGE|DPD|GECIK|WATCH|IZLEME|YAPILAND|RESTRUCT|TAKIP|NPL|DEFAULT')
    AND EXISTS (SELECT 1 FROM all_tab_columns k
                WHERE k.owner = c.owner AND k.table_name = c.table_name
                  AND REGEXP_LIKE(k.column_name, 'COUNTERPARTY|CUST|MUSTERI'))
  GROUP BY c.owner, c.table_name
  ORDER BY c.owner, c.table_name;
  ```
- **Haber arşivinin ne kadar geriye gittiği:** haber parquet'indeki en eski tarih. Scraper her koşuda yalnızca son 10 günü çekiyor, yani arşiv scraper'ın başladığı günden başlıyor. Test bu süreyle sınırlı kalır; daha eski haber için sitelerin arşivini bir kez geriye doğru taramak gerekir.

**Sınırlar:**
- **Az olay:** En büyük 100 şirkette kötü sonuç yılda birkaç kez yaşanır. 100 şirketle yapılan test birkaç örnek gösterir ama sağlam bir oran vermez; daha geniş bir müşteri kümesi gerekebilir. Bu, ilk koşuda daha fazla LLM sorusu demek.
- **Finansal skor:** Geçmiş tarihli finansal tablolar yoksa test her tarihte en son tabloyu kullanır, yani o tarihte bilinmeyen bir bilgiyle çalışır. Bu durumda yalnızca haber skoru adil biçimde test edilebilir.

## 3. Resmi ilan pilotu (ilan.gov.tr, Ticaret Sicil Gazetesi)
**Neden:**
- Haberi hiç çıkmayan şirketlerin konkordatosu da ilan edilir.
- İlanlarda tam unvan geçiyor ve Ticaret Sicil Gazetesi vergi numarasıyla aranabiliyor, yani eşleştirme hatası neredeyse yok.
- Konkordato ve iflas tarihleri hem canlı uyarıdır hem 2. maddedeki test için hazır etikettir.

**İlk kapsam:** konkordato ve mühlet kararları, iflas, tasfiye, sermaye azaltımı. Sonra eklenebilecekler: ihaleden yasaklama (Resmî Gazete), SPK bülteni, Rekabet Kurulu kararları.

**Adımlar:**
1. **Hukuk ve uyum onayı.** Portalların kullanım şartları kontrol edilmeli. Bütün arşivi çekmek yerine yalnızca portföydeki şirketler için günlük sorgu yapmak daha savunulabilir.
2. **Banka bu veriyi zaten alıyor mu?** KKB ya da bir ticari istihbarat firması bu ilanları vergi numarasıyla sağlıyorsa kaynak o olur ve sıfırdan kurmaya gerek kalmaz.
3. **Erişim testi bankadan.** ilan.gov.tr ve ticaretsicil.gov.tr bankanın ağından açılıyor mu, proxy gerekiyor mu? Bu siteler geliştirme makinesinden sertifika hatası yüzünden açılamadı. Arama ve ilan sayfalarının yapısının ofiste görülmesi gerekiyor.
4. **Kod.** LLM ilanı alanlara çevirir: unvan, ilan türü, mahkeme, esas no, tarih, mühlet sonu. Eşleştirmeyi kod vergi numarası ya da unvanla yapar. İlan olay defterine kesin olay olarak girer.

**Gerekenler:** Hukuk onayı ve portföy şirketlerinin vergi numarası (`2_sql_to_parquet` sorgusuna bir sütun eklenir).

## 4. Küçük iyileştirmeler (sıra önemli değil)
| İş | Neden | Efor |
|---|---|---|
| Borsa bülteni ön filtresi | LLM sorularının yaklaşık %43'ü "alakasız" ya da "başka varlık" çıkıyor; çoğu "günün en çok kazandıranları" türü haber. 10 şirket geçen bir liste haberi 10 ayrı soru oluyor. Önce başlık kuralı denenecek; embedding seçeneği 7. bölümde | küçük |
| Kopya haber kümeleme | Aynı ajans haberi 3-5 sitede çıkıyor ve her biri ayrı soruluyor | orta |
| Modül sürüm kontrolü | 10 dosya elle yapıştırılıyor; biri eski kalırsa kimse fark etmez | küçük |
| Kaynak sağlık uyarısı | Scraper hataları sessizce yutuyor (`except: pass`). 10 günden uzun bir kesinti kalıcı boşluk bırakıyor. Dashboard'a kaynak × gün haber sayısı eklenmeli | küçük-orta |
| "Sinyal yok" etiketi | Haberi olmayan şirket "Düşük Risk" görünüyor | küçük |
| Kritik olayda ikinci soru ("olayın öznesi kim?") | Stage 3 tek bir LLM cevabına dayanıyor. Borsadaki "temerrüt satışı" haberinin bir şirkete Stage 3 vermesi bu türden bir hataydı | küçük |
| `yalanlandi_cozuldu`'nun ikiye bölünmesi (`yalanlandi` / `cozuldu`) | `tekil_kapanis` kuralı sahte yalanlamaları susturuyor ama açılış haberi 180 gün dışındaki gerçek bir konkordato/haciz çözümünü de Stage 1'e indiriyor. `PROMPT_SURUMU` artacağı için bir sonraki prompt geçişiyle birlikte `yalanlandi` (Stage 1) ve `cozuldu` (bir kademe düşük) olarak ayrılmalı | küçük |
| Kalıcı olay deposu | Konkordato 180 gün haber çıkmazsa defterden düşüyor, oysa süreç bir yılı aşabiliyor | orta |
| Token ve süre logu, bağlam sınırının ölçülmesi | Tema sorusu sunucunun bağlam sınırını aşarsa Temalar sayfası uyarı vermeden boş gelir | küçük |
| Dosya yolu kontrolü | Scraper `F:\...` altına yazıyor, eşleştirme `G:\...` altından okuyor | 5 dk |
| `4_llm_dogrulama_ACIKLAMA` güncellemesi | Hâlâ 90 günlük pencereden ve kaldırılmış ayarlardan söz ediyor | küçük |

## 5. Yöneticide ya da başka birimlerde
- **Finansal skor (11 Kasım):** Yöneticinin üç katmanlı modeli (seviye, dayanıklılık, trend) koda işlendi; kurallar SKORLAMA_KURALLARI §7'de. İlk banka koşusundan sonra bakılacaklar:
  - Log'daki skor dağılımı ve Stage 2 sayısı (14 Ekim'de şirketlerin çoğunluğu Stage 2'deydi).
  - `bank_fx_deposit` ile döviz kredi kolonlarının para birimi aynı mı (tek bir müşteride elle karşılaştırılmalı). Değilse banka kapsama kuralı yanlış direnç ya da hiç direnç üretir.
  - `interestexpense_try` = 0 olan şirket sayısı. Gider negatif tutuluyorsa sorgudaki `greatest(x, 0)` hepsini 0 yapar ve ICR hiç hesaplanmaz (log uyarır).
  - Trend nominal TL karşılaştırıyor: enflasyonda çoğu şirket "büyüyen" çıkar. Trend dağılımına bakılmalı.
  - Sorgu `ccy_code` ve `prev_ccy_code`'u temp8'e eklerse kod para birimi kontrolünü kendiliğinden yapar.
  - DSCR'ın paydası hâlâ doğrulanmadı; `DSCR_ICR_Ayrisma` sayısı ipucu verir.
  - Kapsamı %50'nin altındaki şirketlerde ne yapılacağı (tasarım §5.2) karar bekliyor; şimdilik yalnızca Not'ta işaretleniyor.
- **Birleşik tablonun tasarımı:** max(haber, finansal) yerine iki eksen (finansal zayıflık × haber olayı).
- **"Stage" adı:** TFRS 9 terimleriyle karışıyor; "alarm: kırmızı / sarı / yeşil" önerisi.
- **İşletme:**
  - platformun test adresinden canlı adrese geçiş.
  - Oracle şifresinin dosyadan çıkarılması.
  - Günlük otomatik çalıştırma.
  - Portföyün kişisel bir tablo (`<SEMA>.temp7`) yerine bir DWH view'ından gelmesi.
- **Diğer onaylar ve sorular:**
  - KAP taraması için hukuk onayı ya da resmi API.
  - DWH'ta risk grubu alanı.
  - Platform ekibine iki soru (bkz. 7. bölüm): moderations endpoint'i sınıflandırma için kullanılabilir mi, vektör döndüren bir `/v1/embeddings` endpoint'i var mı?

## 6. Daha sonra: yeni kullanım alanları
Elimizde yalnızca LLM endpoint'i varken en çok değer verecek üç iş:
1. **İzleme ve revizyon notu taslağı.** Rakamları kod DWH'tan koyar, LLM anlatıyı yazar ve her cümleye kaynak verir. Sektörde en yaygın kullanım bu.
2. **Denetim raporu kırmızı bayrakları.** Süreklilik şüphesi, şartlı görüş, ortaklardan alacak ve koşullu borç gibi bulgular alıntıyla çıkarılır.
3. **Covenant çıkarma.** Proje finansmanı sözleşmelerinden eşik ve bildirim tarihleri çıkarılır, hesabı kod yapar. Genel DSCR kuralı otoyol ve havalimanı gibi projelerde anlamsız kalıyor.

Diğer adaylar: ziyaret notlarından sinyal çıkarma, rating override gerekçelerinin analizi, sektör ve tema radarı, mevzuat soru-cevabı (platformun RAG servisiyle).

## 7. Sonra bakılacak: embedding (bülten filtresi, temalar)
**Fikir:** platformun `/v1/moderations` adresi aslında bir içerik denetim modeli değil, çok dilli bir cümle embedding modeli (`çok dilli bir embedding modeli`). Kategorileri serbest metin olarak alıyor (Türkçe de olur) ve metnin her kategoriye benzerlik skorunu döndürüyor. Haber başlığı LLM'e gitmeden önce buraya sorulabilir. Başlık "borsa günlük bülteni" kategorisine çok benziyorsa LLM'e hiç sorulmaz. Önbellek anahtarları değişmez.

**Dikkat:**
- **Amaç dışı kullanım.** Servis zararlı içeriği engellemek için kurulmuş. Kullanmadan önce platform ekibine sorulmalı; günde birkaç bin başlık onlar için yük olabilir.
- **Yalnızca başlık gönderilmeli.** Model girdiyi yaklaşık 128 token'da kesiyor.
- **Asıl risk gerçek bir risk haberini elemek.** Ölçmek için etiket gerekmiyor: filtrenin eleyeceği başlıklar önbellekteki LLM alaka cevaplarıyla karşılaştırılır. Eşik, elenenler arasında LLM'in "odak" deyip olay bulduğu haber kalmayacak şekilde seçilir.
- **Önce basit başlık kuralı denenmeli** ("en çok kazandıran", "BIST 100" gibi). Embedding ancak kuralın kaçırdığı farklı ifadeler çoksa eklenir.

**Ayrı bir API anahtarı gerekmiyor gibi görünüyor.** Dokümanda iki endpoint aynı sunucuda ve aynı `Bearer` anahtarıyla gösteriliyor. Ama bu teyit edilmedi; ayrıca moderations test sunucusunda kurulu olmayabilir. Ofiste denemek için bu kodu `4_llm_dogrulama.py` ile aynı klasörde `moderations_dene.py` olarak kaydet:
```python
import importlib
import requests
from ews_llm import resolve_ssl_verify

cfg = importlib.import_module("4_llm_dogrulama")
url = cfg.LLM["url"].replace("/chat/completions", "/moderations")
r = requests.post(url, headers={"Authorization": f"Bearer {cfg.LLM['key']}"},
                  json={"input": "Borsa İstanbul'da günün en çok kazandıran hisseleri",
                        "categories": ["borsa günlük bülteni, en çok yükselen ve düşen hisseler",
                                       "şirketin borcunu ödeyememesi, konkordato, iflas"],
                        "flag_threshold": 0.6, "moderations_disabled": False},
                  verify=resolve_ssl_verify(cfg.LLM_VERIFY_SSL), timeout=60)
print(url, r.status_code, r.text[:1000])
```
Sonuç nasıl okunur:
- **200 ve `category_scores` geldi:** Aynı anahtar çalışıyor.
- **401 ya da 403:** Anahtarın bu endpoint'e yetkisi yok.
- **404:** Test sunucusunda bu endpoint yok; canlı adres denenmeli ya da platform ekibine sorulmalı.

**Temalar için.** Moderations endpoint'i vektör döndürmüyor; yalnızca bizim verdiğimiz kategorilere benzerlik skoru veriyor. Bu yüzden klasik kümeleme bununla yapılamaz.
- **Hemen yapılabilecek (endpoint'in kendi kullanım biçimi):** Temaları LLM bulmaya devam eder, üyelik kontrolünü embedding yapar. Her olay özeti girdi, tema açıklamaları kategori olur. Bunun iki faydası var:
  - Bugünkü kelime kökü kontrolü "akaryakıt zammı" ile "benzin fiyat artışı" gibi aynı anlama gelen farklı ifadeleri eşleştiremiyor.
  - LLM'e sığmayan maddeler (300'ün ötesi) de mevcut temalara atanabilir.
- **Asıl çözüm (vektör gerekiyor):** Her olay özeti vektöre çevrilir, benzerler kümelenir ve LLM her kümeye yalnızca ad koyar. Böylece 300 madde sınırı ve bağlam riski kalkar. Tema LLM'in tahmininden değil veriden çıktığı için uydurma riski de azalır. Bunun için bir `/v1/embeddings` endpoint'i gerekiyor; RAG servisi içeride muhtemelen bir tane kullanıyor. Modeli kendimiz indiremeyiz.
- **Sabit tema listesi** ("kur şoku", "akaryakıt fiyatı", "fon krizi" gibi): kolay, ama yalnızca önceden düşünülen temaları bulur. Beklenmedik ortak gelişmeleri yakalamaz. Ancak tamamlayıcı olarak kullanılabilir.
- **Eşik ölçülmeli.** Dokümandaki örnekte ilgisiz kategoriler 0,30-0,50, ilgili kategori 0,66 skor alıyor; aradaki fark dar.
