# Kurumsal Kredi Riski Erken Uyarı Sistemi (EWS)

Bir bankanın kurumsal kredi portföyü için erken uyarı sistemi. Ekonomi basınındaki haberleri ve resmi ilanları
(İlan.gov.tr konkordato/iflas tebligatları, Resmî Gazete ihale yasaklıları) portföy şirketleriyle (ve hakim
ortaklarıyla) eşleştirir, her eşleşmeyi bankanın iç LLM'ine okutup **kanıtlı olaylar** (konkordato, haciz,
soruşturma, zarar…) çıkarır, olayları şirket başına bir defterde açık/kapanmış olarak izler ve bunu DWH'taki
finansallardan hesaplanan üç katmanlı finansal skorla birleştirir. Çıktı bir Excel dosyası ve tek dosyalık bir
HTML paneldir.

LLM seviye ya da puan vermez: kapalı bir listeden olay tipi seçer ve haberden birebir alıntı yapar; alıntıyı ve seviyeyi
kod denetler.

## Proje Yapısı

```text
news_ingestion/
├── baslat.py                # Tek tuş ("idiot-proof") başlatıcı: paket/veri sağlığı kontrolü + etkileşimli menü (Spyder F5)
├── ayarlar.py               # Merkezi ayarlar (LLM, Oracle, Proxy, dosya yolları ve akıllı şifre/anahtar yardımcıları)
├── 1_scraper.py             # Ekonomi basınından haber toplama -> news.parquet (kümülatif)
├── 2_sql_to_parquet.py      # Oracle DWH: portföy, finansallar, hakim ortaklar -> structured_data.parquet, portfolio_config.py
├── 3_llm_dogrulama.py       # Ana EWS orkestratörü: eşleştirme -> LLM -> olay defteri -> finansal skor -> Excel + dashboard.html
├── 4_resmi_ilanlar.py       # İlan.gov.tr (Konkordato/İflas) ve Resmî Gazete (İhale Yasaklıları) tarayıcısı -> news.parquet
├── 5_backtest.py            # Backtesting, kurşun süresi (lead time) ve dwh_agent (kb.sqlite) entegrasyonu
├── ews/                     # Çekirdek kütüphane paketi (doğrudan çalıştırılmaz, import edilir)
│   ├── __init__.py
│   ├── text.py              # Türkçe karakter normalizasyonu ve offset koruma
│   ├── eslestirme.py        # Trie regex ile şirket/ortak/ticker eşleştirme ve çoklu kesit (snippet) çıkarma
│   ├── llm.py               # OpenAI uyumlu LLM istemcisi, /v1/moderations anlamsal benzerlik ve JSONL önbellek
│   ├── olay.py              # Olay sözlüğü, alaka/olay prompt'ları, alıntı kontrolü
│   ├── defter.py            # Şirket bazlı durumsal olay defteri (yeni / mevcut / kapat) ve haber skoru
│   ├── financial.py         # Üç katmanlı finansal skorlama modeli (Seviye, Dayanıklılık, Trend)
│   ├── portfoy.py           # Birlesik tablo (2B Risk Matrisi dahil) ve açık uçlu dinamik Temalar keşfi
│   ├── tablolar.py          # Excel sayfaları, analist kontrol örneklem hasadı ve alias önerileri
│   └── dashboard.py         # Sıfır bağımlılıklı tek dosya HTML erken uyarı paneli
└── docs/                    # Tasarım, kural ve yol haritası belgeleri
    ├── DEGISIKLIKLER.md     # v2 mimari yeniden yapılandırma, hata düzeltmeleri ve yeni özellik kayıtları
    ├── PROJE_ACIKLAMASI.md
    ├── SKORLAMA_KURALLARI.md
    ├── 4_llm_dogrulama_ACIKLAMA.md
    └── SONRAKI_ADIMLAR.md
```

## Belgeler
| Belge | İçerik |
|---|---|
| [docs/DEGISIKLIKLER.md](docs/DEGISIKLIKLER.md) | **Yeni (v2):** Paket mimarisi, `ayarlar.py`, `baslat.py`, `build_themes` tamiri, resmi ilanlar ve backtest notları |
| [docs/PROJE_ACIKLAMASI.md](docs/PROJE_ACIKLAMASI.md) | Motivasyon, mimari, gelişim kronolojisi, kararlar ve gerekçeleri, ölçümler, açık sorunlar |
| [docs/SKORLAMA_KURALLARI.md](docs/SKORLAMA_KURALLARI.md) | Seviye ve skor kurallarının kısa, kesin hali |
| [docs/4_llm_dogrulama_ACIKLAMA.md](docs/4_llm_dogrulama_ACIKLAMA.md) | LLM katmanının ayrıntısı, Excel kolon sözlüğü, karar kaydı |
| [docs/SONRAKI_ADIMLAR.md](docs/SONRAKI_ADIMLAR.md) | Yol haritası |

## Nasıl Çalıştırılır? (Hızlı Başlangıç)
1. Sunucu adreslerinizi [`ayarlar.py`](ayarlar.py) içine (veya Git'e girmeyen kişisel `ayarlar_local.py` dosyanıza) yazın. Şifre ve API anahtarını dosyaya yazmanıza gerek yoktur; ilk çalıştırmada sorulur.
2. Spyder'da (F5) veya terminalde doğrudan **`baslat.py`** dosyasını çalıştırın:
   ```bash
   python baslat.py
   ```
   `baslat.py` eksik kütüphaneleri ve veri dosyalarının güncelliğini kontrol edip etkileşimli menüyü açar.

## Gereksinimler
Python 3.11+; `pandas`, `pyarrow`, `openpyxl`, `requests`, `urllib3`, `beautifulsoup4`, `oracledb`.
