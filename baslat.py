"""baslat.py - Kurumsal Kredi Riski Erken Uyarı Sistemi (EWS) Tek Tuş Başlatıcı (Spyder F5 / Terminal).

Projeyi ilk kez çalıştıracak analistler veya başka kullanıcılar için "idiot-proof" (hataya dayanıklı) giriş noktası:
  1. Gerekli Python paketlerinin (`pandas`, `pyarrow`, `openpyxl`, `requests`, `bs4`) kurulu olup olmadığını denetler.
  2. `ayarlar.py` içindeki bağlantı bilgilerini, `news.parquet`, `structured_data.parquet` ve `portfolio_config.py`
     dosyalarının varlığını ve güncelliğini (bayat veri olup olmadığını) kontrol eder.
  3. Etkileşimli Türkçe menü ile istenen adımı (veya tam günlük akışı) tek tuşla çalıştırır ve bitince
     üretilen `dashboard.html` dosyasını varsayılan tarayıcıda otomatik açar.
"""

from __future__ import annotations

import importlib
import logging
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

REQUIRED_PACKAGES = {
    "pandas": "pandas",
    "pyarrow": "pyarrow",
    "openpyxl": "openpyxl",
    "requests": "requests",
    "urllib3": "urllib3",
    "bs4": "beautifulsoup4",
}


def check_dependencies() -> bool:
    """Zorunlu Python kütüphanelerini kontrol eder; eksik varsa Spyder ve terminal kurulum komutunu gösterir."""
    missing = []
    for mod_name, pkg_name in REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(mod_name)
        except ImportError:
            missing.append(pkg_name)
    if missing:
        pkgs = " ".join(missing)
        print("\n" + "!" * 72)
        print("[EKSİK KÜTÜPHANE] Aşağıdaki Python paketleri kurulu değil:")
        print(f"  -> {', '.join(missing)}")
        print("\nSpyder konsolunda kurmak için şu komutu çalıştırın:")
        print(f"  %pip install {pkgs}")
        print("\nTerminalde kurmak için:")
        print(f"  pip install {pkgs}")
        print("!" * 72 + "\n")
        return False
    return True


def _file_status(path: Path, stale_days: int = 30) -> str:
    if not path.exists():
        return "YOK (henüz üretilmedi)"
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    age_days = (datetime.now() - mtime).days
    size_kb = path.stat().st_size / 1024
    unit = f"{size_kb / 1024:.1f} MB" if size_kb >= 1024 else f"{size_kb:.0f} KB"
    warn = f" [UYARI: {age_days} günlük bayat veri!]" if age_days > stale_days else ""
    return f"MEVCUT ({unit}, son güncelleme: {mtime:%Y-%m-%d %H:%M}{warn})"


def print_system_health() -> None:
    """Mevcut veri dosyalarının, önbelleğin ve ayarların durumunu ekrana özetler."""
    import ayarlar

    print("\n" + "=" * 72)
    print("  KURUMSAL KREDİ RİSKİ ERKEN UYARI SİSTEMİ (EWS) - SİSTEM DURUMU")
    print("=" * 72)
    print(f"  Ağ Modu                : {ayarlar.NETWORK_MODE} (Proxy: {ayarlar.PROXY_HOST})")
    llm_ok = "<SUNUCU>" not in ayarlar.LLM_URL
    key_ok = bool(ayarlar.get_llm_api_key(interactive=False))
    print(f"  LLM Servisi            : {ayarlar.LLM_URL} [{'AYARLI' if llm_ok else 'AYARLANMADI'}]")
    print(f"  LLM API Anahtarı       : {'MEVCUT (.llm_key.txt / ayarlar)' if key_ok else 'YOK (çalışırken sorulacak)'}")
    print("-" * 72)
    print(f"  Haber Havuzu (Parquet) : {_file_status(ayarlar.NEWS_PARQUET_PATH, stale_days=3)}")
    print(f"  Portföy Konfigürasyonu : {_file_status(ayarlar.PORTFOLIO_CONFIG_PATH, stale_days=ayarlar.STALE_DATA_WARN_DAYS)}")
    print(f"  Finansal Veri (DWH)    : {_file_status(ayarlar.STRUCTURED_PARQUET_PATH, stale_days=ayarlar.STALE_DATA_WARN_DAYS)}")
    print(f"  LLM Önbelleği (Cache)  : {_file_status(ayarlar.CACHE_PATH, stale_days=999)}")
    print("=" * 72)


def run_step(choice: str) -> None:
    import ayarlar

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    if choice in ("1", "gunluk"):
        print("\n>>> [ADIM 1/3] Ekonomi Basını Haberleri Taranıyor (1_scraper.py)...")
        scraper = importlib.import_module("1_scraper")
        scraper.run_ingestion_pipeline()

        if getattr(ayarlar, "ENABLE_RESMI_ILANLAR", False):
            print("\n>>> [ADIM 2/3] Resmi İlanlar (İlan.gov.tr & Resmî Gazete) Taranıyor (4_resmi_ilanlar.py)...")
            resmi = importlib.import_module("4_resmi_ilanlar")
            resmi.run_official_gazette_pipeline()
        else:
            print("\n>>> [ADIM 2/3] Resmi İlanlar kapalı (ayarlar.ENABLE_RESMI_ILANLAR = False); atlandı.")

        print("\n>>> [ADIM 3/3] LLM Doğrulama, Olay Defteri, Finansal Skor ve Dashboard Üretiliyor (3_llm_dogrulama.py)...")
        dogrulama = importlib.import_module("3_llm_dogrulama")
        dogrulama.run()

        if ayarlar.DASHBOARD_OUTPUT_PATH.exists():
            print(f"\n[BAŞARILI] Dashboard hazır: {ayarlar.DASHBOARD_OUTPUT_PATH}")
            try:
                webbrowser.open(ayarlar.DASHBOARD_OUTPUT_PATH.as_uri())
            except Exception:
                pass

    elif choice in ("2", "llm"):
        print("\n>>> Mevcut haber havuzuyla LLM Doğrulama ve Dashboard çalıştırılıyor (3_llm_dogrulama.py)...")
        dogrulama = importlib.import_module("3_llm_dogrulama")
        dogrulama.run()
        if ayarlar.DASHBOARD_OUTPUT_PATH.exists():
            try:
                webbrowser.open(ayarlar.DASHBOARD_OUTPUT_PATH.as_uri())
            except Exception:
                pass

    elif choice in ("3", "haber"):
        print("\n>>> Ekonomi basını haberleri taranıyor...")
        importlib.import_module("1_scraper").run_ingestion_pipeline()
        if getattr(ayarlar, "ENABLE_RESMI_ILANLAR", False):
            print("\n>>> Resmi ilanlar taranıyor...")
            importlib.import_module("4_resmi_ilanlar").run_official_gazette_pipeline()
        else:
            print("\n>>> Resmi ilanlar kapalı (ayarlar.ENABLE_RESMI_ILANLAR = False); atlandı.")

    elif choice in ("4", "oracle"):
        print("\n>>> Oracle DWH üzerinden portföy, finansallar ve ortaklar güncelleniyor (2_sql_to_parquet.py)...")
        dwh = importlib.import_module("2_sql_to_parquet")
        portfolio_df, partners_df = dwh.fetch_data()
        portfolio_df.to_parquet(dwh.STRUCTURED_PARQUET_PATH, index=False, engine="pyarrow")
        dwh.create_portfolio_config(portfolio_df, partners_df)

    elif choice in ("6", "incele"):
        importlib.import_module("6_sirket_inceleme").main()

    elif choice in ("5", "backtest"):
        print("\n>>> Backtest (Lead Time & Performans Analizi) çalıştırılıyor (5_backtest.py)...")
        bt = importlib.import_module("5_backtest")
        bt.run_backtest()

    else:
        print("Çıkış yapıldı.")


def main() -> None:
    if not check_dependencies():
        return
    print_system_health()

    if len(sys.argv) > 1:
        run_step(sys.argv[1].strip().lower())
        return

    print("\nYapmak istediğiniz işlemi seçin:")
    print("  [1] Günlük Tam Akış (Haberleri Çek + Resmi İlanları Çek + LLM Doğrulama & Dashboard)")
    print("  [2] Yalnızca LLM Doğrulama, Excel ve Dashboard Üret (Mevcut haberlerle)")
    print("  [3] Yalnızca Haber ve Resmi İlan Havuzunu Güncelle (LLM'e sormadan)")
    print("  [4] Oracle DWH'tan Portföy ve Finansalları Güncelle (2_sql_to_parquet.py)")
    print("  [5] Geriye Dönük Test (Backtest & Kurşun Süresi Raporu) Çalıştır")
    print("  [6] Şirket İncele (ajan: EWS + ilan.gov.tr + KAP + Google News; kaynaklı not)")
    print("  [0] Çıkış")

    secim = input("\nSeçiminiz [Varsayılan=1]: ").strip() or "1"
    if secim != "0":
        run_step(secim)


if __name__ == "__main__":
    main()
