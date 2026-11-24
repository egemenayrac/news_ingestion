# -*- coding: utf-8 -*-
"""6_sirket_inceleme.py - Tek bir şirket için inceleme ajanını çalıştırır.

Ajan EWS çıktılarını (olay defteri, haberler, ortaklar), ilan.gov.tr'yi, KAP'ı, Google News'i ve (tanımlıysa) onaylı DWH
şablonlarını tarar; kaynak gösterilmiş bir kredi izleme notu yazar. Not data/incelemeler/ altına .md ve .html olarak
kaydedilir ve tarayıcıda açılır. Ajan seviye/skor vermez.

Çalıştırma: Spyder'da F5 (şirket sorulur) ya da baslat.py -> 6. Komut satırı: python 6_sirket_inceleme.py 9902647
Süre: KAP'sız ~1 dk, KAP (30 gün) ile +2-3 dk. Önce 3_llm_dogrulama.py en az bir kez çalışmış olmalı.
"""
from __future__ import annotations

import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ayarlar  # noqa: E402,F401
from ews import inceleme  # noqa: E402


def main(sirket: str = "") -> None:
    sirket = (sirket or "").strip() or input("İncelenecek şirket (müşteri numarası ya da unvan): ").strip()
    if not sirket:
        print("Şirket girilmedi.")
        return
    print(f"\n>>> {sirket} inceleniyor (1-4 dakika)...")

    def adim(s):
        print(f"  - {s['arac']} {s['girdi'] if s['girdi'] not in ('{}', '') else ''}".rstrip())

    oturum, araclar = inceleme.incele(sirket, on_step=adim)
    if oturum.durum != "bitti":
        print(f"\n[HATA] {oturum.hata}")
        return
    md, ht = inceleme.kaydet(oturum, araclar)
    print(f"\n[BAŞARILI] Not: {ht}\n{len(araclar.kaynaklar)} kaynak · {oturum.n_llm} LLM isteği · "
          f"denetim: {'temiz' if not araclar.denetim else str(len(araclar.denetim)) + ' uyarı (notta sarı kutu)'}")
    try:
        webbrowser.open(ht.as_uri())
    except Exception:
        pass


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
