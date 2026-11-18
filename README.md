# Kurumsal Kredi Riski Erken Uyarı Sistemi (EWS)

Bir bankanın kurumsal kredi portföyü için erken uyarı sistemi. Ekonomi basınındaki haberleri portföy şirketleriyle
(ve hakim ortaklarıyla) eşleştirir, her eşleşmeyi bankanın iç LLM'ine okutup **kanıtlı olaylar** (konkordato, haciz,
soruşturma, zarar…) çıkarır, olayları şirket başına bir defterde açık/kapanmış olarak izler ve bunu DWH'taki
finansallardan hesaplanan kural bazlı bir finansal skorla birleştirir. Çıktı bir Excel dosyası ve tek dosyalık bir
HTML paneldir.

LLM seviye ya da puan vermez: kapalı bir listeden olay tipi seçer ve haberden birebir alıntı yapar; alıntıyı ve seviyeyi
kod denetler.

## Belgeler
| Belge | İçerik |
|---|---|
| [PROJE_ACIKLAMASI.md](PROJE_ACIKLAMASI.md) | **Buradan başlayın.** Motivasyon, mimari, gelişim kronolojisi, kararlar ve gerekçeleri, ölçümler, açık sorunlar |
| [SKORLAMA_KURALLARI.md](SKORLAMA_KURALLARI.md) | Seviye ve skor kurallarının kısa, kesin hali |
| [4_llm_dogrulama_ACIKLAMA.md](4_llm_dogrulama_ACIKLAMA.md) | LLM katmanının ayrıntısı, Excel kolon sözlüğü, karar kaydı (kısmen eski) |
| [SONRAKI_ADIMLAR.md](SONRAKI_ADIMLAR.md) | Yol haritası |

## Akış
```
1_scaper.py          haber sitelerinden son günlerin haberleri  -> news.parquet (kümülatif)
2_sql_to_parquet.py  Oracle: portföy, finansallar, hakim ortaklar -> structured_data.parquet, portfolio_config.py
4_llm_dogrulama.py   eşleştirme (3_eslestirme) -> LLM soruları -> olay defteri -> finansal skor -> Excel + dashboard.html
```
Günlük: `1_scaper.py`, sonra `4_llm_dogrulama.py`. `2_sql_to_parquet.py` portföy ya da finansallar değiştiğinde.

## Bu depo hakkında
Paylaşılabilir kopyadır:
- Banka altyapısına ait adlar yer tutucudur: `<ORACLE_HOST>`, `<ORACLE_USER>`, `<SERVICE_NAME>`, `<SEMA>`, `<PROXY_HOST>:<PORT>`,
  LLM adresi `<SUNUCU>`. Çalıştırmadan önce doldurulur; şifreler ve API anahtarları dosyaya yazılıp depoya gönderilmez.
- `2_sql_to_parquet.py`'deki `ALIAS_RULES` örnek satırlardan oluşur.
- Bankanın ve iç platformun adları çıkarıldı; bankaya özgü DWH sütun adları genelleştirildi
  (`cash_bal_bank`, `bank_fx_deposit`…). Gerçek kurulumda sorgu ve sütun adları kurumun DWH'ına göre uyarlanır.
- Müşteri verisi içeren dosyalar (`portfolio_config.py`, `structured_data.parquet`, `news.parquet`, Excel çıktıları,
  `llm_cache.jsonl`, `kontrol_etiketleri.jsonl`, `dashboard*.html/jsonl`) depoda yoktur ve `.gitignore`'dadır.
- Belgelerde müşteri adları "Şirket A, B…" ile değiştirildi, portföye ait sayılar genelleştirildi.

## Gereksinimler
Python 3.11+; `pandas`, `pyarrow`, `openpyxl`, `requests`, `urllib3`, `beautifulsoup4`, `oracledb`.
