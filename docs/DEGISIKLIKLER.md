# EWS v2 Mimari Yeniden Yapılandırma ve İyileştirme Notları

**Tarih:** 18 Kasım 2026  
**Branch:** `feature/ews-v2-mimari-ve-iyilestirmeler`

Bu belge, Kurumsal Kredi Riski Erken Uyarı Sistemi (EWS) projesinde yapılan mimari düzenlemeleri,
çözülen tasarım/kod hatalarını ve yeni eklenen modülleri (`ayarlar.py`, `baslat.py`, `4_resmi_ilanlar.py`, `5_backtest.py`) kayıt altına alır.

---

## 1. Estetik ve Klasör Mimarisi Temizliği (`ews/` Paketi ve `docs/`)

* **Sorun:** Kök dizinde 12 adet Python dosyası (`1_scaper.py`, `3_eslestirme.py`, `ews_*.py`) ve 5 adet büyük harfli `.md` belgesi dağınık halde duruyordu. Ayrıca `3_eslestirme.py` doğrudan çalıştırılan bir adım değil, içeriden import edilen bir kütüphane olduğu halde başında `3_` numarası taşıyor ve Python'da rakamla başlayan modül import edilemediği için `importlib.import_module("3_eslestirme")` ile çağrılıyordu.
* **Yapılan Değişiklik:**
  1. Tüm iç kütüphane modülleri [`ews/`](ews/__init__.py) paketi altına taşındı (`ews/text.py`, `ews/eslestirme.py`, `ews/llm.py`, `ews/olay.py`, `ews/defter.py`, `ews/financial.py`, `ews/portfoy.py`, `ews/tablolar.py`, `ews/dashboard.py`).
  2. Tüm dokümantasyon dosyaları [`docs/`](docs/) klasörü altına toplandı.
  3. `1_scaper.py` dosyasındaki yazım hatası düzeltilerek [`1_scraper.py`](1_scraper.py) yapıldı; `4_llm_dogrulama.py` ise [`3_llm_dogrulama.py`](3_llm_dogrulama.py) olarak yeniden numaralandırıldı.

---

## 2. Merkezi Konfigürasyon ([`ayarlar.py`](ayarlar.py)) ve "Idiot-Proof" Başlatıcı ([`baslat.py`](baslat.py))

* **Merkezi Ayarlar (`ayarlar.py`):**
  * Eskiden Proxy ayarları `1_scraper.py` içinde, Oracle ayarları `2_sql_to_parquet.py` içinde, LLM ayarları ise `3_llm_dogrulama.py` içinde dağınıktı.
  * Şimdi tüm sunucu adresleri, eşikler ve dosya yolları [`ayarlar.py`](ayarlar.py) içinde toplandı.
  * Kişisel/yerel ayarların Git'e karışmadan ezilebilmesi için `ayarlar_local.py` (gitignore'da) desteği eklendi.
  * **Akıllı Anahtar Yönetimi:** LLM API anahtarı koda yazılmasa bile `.llm_key.txt` (gitignore'da) veya ortam değişkeninden okunur; hiçbiri yoksa program çökmek yerine ilk çalışmada kullanıcıya Türkçe sorup `.llm_key.txt`'ye kaydeder.
* **Tek Tuş Başlatıcı (`baslat.py`):**
  * Projeyi ilk kez açan bir kullanıcının Spyder'da `F5` ile çalıştırabileceği etkileşimli başlatıcı eklendi.
  * Çalışmadan önce eksik kütüphane kontrolü (`check_dependencies`) ve veri dosyalarının (`news.parquet`, `structured_data.parquet`, `portfolio_config.py`) **kaç günlük / bayat** olduğu kontrolünü (`print_system_health`) yapar.
* **Oracle `TEMP8` Bayat Veri Denetimi (`2_sql_to_parquet.py`):**
  * `temp8` tablosu SQL Developer'da `sorgu.txt` ile manuel oluşturulduğu için, [`2_sql_to_parquet.py`](2_sql_to_parquet.py) içine `_check_temp8_freshness()` eklendi. Tablonun Oracle `ALL_OBJECTS.LAST_DDL_TIME` tarihini ve içindeki en güncel `part_id` dönemini kontrol eder; tablo 30 günden eskiyse *"DİKKAT: TEMP8 tablosu X gün önce oluşturulmuş, SQL Developer'da sorgu.txt çalıştırılmamış olabilir!"* uyarısı verir.

---

## 3. `build_themes` (Ortak Tema Keşfi) Tamiri ve `/v1/moderations` Entegrasyonu

* **Sorun:** `build_themes` (`ews/portfoy.py`) hiç tema üretemiyordu. Bunun 3 nedeni vardı:
  1. `_theme_items()` içindeki `.drop_duplicates(["Şirket", "LLM_Olay"])` satırı bir şirketin aynı olay tipindeki (ör. `dava_ceza` veya `dis_baski`) tüm yeni haberlerini silip yalnızca en eski tek bir haberi bırakıyordu.
  2. `TEMA_PROMPT` LLM'den *"bütün maddelerde kelimesi kelimesine geçen tek bir `ortak_ifade`"* istiyordu; farklı gazeteler aynı olayı farklı kelimelerle ("soruşturma" vs "inceleme", "kur baskısı" vs "döviz artışı") yazdığı için LLM boş liste döndürüyordu.
  3. `_mentions()` fonksiyonu 4 harften kısa kelimeleri ("kur", "ab", "spk" vb.) eliyor ve tek bir kelime köküne bakıyordu.
* **Yapılan Değişiklik ([`ews/portfoy.py`](ews/portfoy.py) & [`ews/llm.py`](ews/llm.py)):**
  1. Öngörülemeyen (emergent) temaları yakalayabilmek için **açık uçlu LLM keşfi** korundu; ancak tek bir `ortak_ifade` yerine temayı doğrulayan **`kanit_kelimeler` (2-5 anahtar kavram listesi)** üretilmesi sağlandı.
  2. `_theme_items()` artık şirket başına farklı konulu olayları silmiyor; yalnızca aynı özete sahip kopya haberleri tekilleştiriyor.
  3. **İkili Doğrulama (Kelime Kökü Kümesi + `/v1/moderations` Embedding):**
     * [`ews/llm.py`](ews/llm.py) içindeki `LLMClient` sınıfına `similarity()` metodu eklendi. Bu metot kurumsal `/v1/moderations` (`paraphrase-multilingual-mpnet-base-v2`) uç noktasını kullanarak metinler arası kosinüs benzerliği skorunu döndürür.
     * `build_themes` önce maddenin başlık, özet ve alıntısında `kanit_kelimeler` köklerinden herhangi birini arar (`_mentions_any`); bulamazsa `llm.similarity()` ile anlamsal benzerliği (`>= 0.55`) kontrol eder.

---

## 4. Çekirdek Kod İncelemesinde Tespit Edilip Düzeltilen Diğer Bulgular

1. **Çoklu Kesit (Multi-Snippet) Birleştirme ([`ews/eslestirme.py`](ews/eslestirme.py)):**
   * Bir şirket aynı haberin ilk paragrafında sadece bir listede, 4. paragrafında ise bir dava/soruşturma cümlesinde geçtiğinde yalnızca ilk geçişin kesiti alınıyordu. Artık `match_record()` aynı haberdeki ilk 3 farklı geçişin kesitini `"\n...\n"` ile birleştiriyor.
2. **Tekil Yalanlama Haberi Alarmı ([`ews/defter.py`](ews/defter.py)):**
   * Öncesinde açık bir olay haberi olmadan tek başına gelen `"konkordato iddiası yalanlandı"` haberi kapalı kritik olay açıp şirkete doğrudan Stage 2 (45 puan) veriyordu. `Olay` sınıfına `tekil_kapanis` özelliği eklendi; öncesinde açık haber olmadan doğrudan yalanlama/kapanışla oluşan tekil kayıtlar Stage 1'de kalır (sonradan gerçek bir olay haberi bağlanırsa otomatik yükselir).
3. **Hakim Ortak Önbellek Anahtarı ([`ews/olay.py`](ews/olay.py)):**
   * `pair_keys()` fonksiyonu ortak üzerinden gelen eşleşmelerde ortak adını da anahtara (`...|Şirket:OrtakAdi|Sürüm`) dahil eder; böylece ortak bilgisi değiştiğinde eski önbellek cevabı yanlışlıkla kullanılmaz.
4. **2 Boyutlu Risk Matrisi ([`ews/portfoy.py`](ews/portfoy.py)):**
   * `Birlesik` sayfasına tekil `max()` skorunun yanına finansal dayanıklılık ile haber şokunu ayrı eksenlerde gösteren `Risk_Matrisi` (`Bilanço Kırılgan × Kritik Haber Şoku` vb.) kolonu eklendi.
5. **Import Anında `input()` Bekleme Hatası ([`1_scraper.py`](1_scraper.py)):**
   * `1_scraper.py` import edildiği anda modül seviyesinde `create_configured_session()` çağrıldığı için `Proxy Kullanıcı Adı:` sorup programı durduruyordu. Oturum oluşturma `get_session()` ile tembel (lazy) hale getirildi.

---

## 5. Yeni Eklenen Modüller

1. **Resmi İlanlar Tarayıcısı ([`4_resmi_ilanlar.py`](4_resmi_ilanlar.py)):**
   * **İlan.gov.tr** (Konkordato, Geçici/Kesin Mühlet, İflas/Tasfiye tebligatları) ve **Resmî Gazete** (İhalelere Katılmaktan Yasaklama ve mahkeme kararları) kaynaklarını tarar.
   * Metin içindeki **10 haneli VKN** ve **16 haneli MERSİS** numaralarını otomatik ayıklar ve `news.parquet` havuzuna ekler.
2. **Geriye Dönük Test ve `dwh_agent` Entegrasyonu ([`5_backtest.py`](5_backtest.py)):**
   * Haber arşivindeki geçmiş dönemler (`as_of_date`) için gelecekteki haberleri ve gelecekteki finansal tabloları gizleyerek (look-ahead bias olmadan) sistemi çalıştırır.
   * Gerçekleşen kredi olaylarıyla karşılaştırarak **Yakalama Oranı (Recall)**, **Kurşun Süresi (Lead Time - olaydan kaç gün önce uyarı verildiği)** ve **100 Şirket-Ay Başına Yanlış Alarm Oranı** metriklerini Haber, Finansal ve Birleşik katmanlar için ayrı ayrı hesaplar (`backtest_raporu.xlsx`).
   * Bilgisayarda `dwh_agent/kb/kb.sqlite` varsa, DWH içindeki aday sonuç tablolarını (`STAGE`, `DPD`, `WATCH`, `IZLEME`, `NPL`) otomatik keşfeder.

