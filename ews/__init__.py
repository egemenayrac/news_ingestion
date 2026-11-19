"""Kurumsal Kredi Riski Erken Uyarı Sistemi (EWS) çekirdek paketi.

Modüller:
  - ews.text       : Türkçe metin normalizasyonu ve offset eşleme
  - ews.eslestirme : Haber ve portföy şirketi eşleştirme motoru (Trie regex + snippet çıkarma)
  - ews.llm        : OpenAI uyumlu LLM istemcisi, /v1/moderations (embedding benzerlik) ve JSONL önbellek
  - ews.olay       : Olay sözlüğü, alaka/olay prompt'ları, alıntı doğrulama
  - ews.defter     : Şirket bazlı durumsal olay defteri (açık/kapandı) ve haber skoru
  - ews.financial  : Üç katmanlı finansal skorlama modeli
  - ews.portfoy    : Birleşik risk tablosu ve dinamik ortak tema keşfi (LLM + anlamsal doğrulama)
  - ews.tablolar   : Excel sayfaları, analist kontrol örneklem hasadı ve alias önerileri
  - ews.dashboard  : Sıfır bağımlılıklı tek dosya HTML erken uyarı paneli
"""

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
