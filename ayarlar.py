"""Kurumsal Kredi Riski Erken Uyarı Sistemi (EWS) - Merkezi Ayarlar (ayarlar.py).

Bütün betikler (1_scraper, 2_sql_to_parquet, 3_llm_dogrulama, 4_resmi_ilanlar, 5_geriye_test, baslat)
sunucu adreslerini, dosya yollarını ve eşik değerlerini bu dosyadan okur.

ŞİFRE VE API ANAHTARI GÜVENLİĞİ:
  - Şifreleri ve API anahtarlarını bu dosyaya yazıp GitHub'a göndermeyin.
  - Eğer `LLM_API_KEY` veya `ORACLE_PASSWORD` boş bırakılırsa:
      1. Önce ortam değişkenlerine (`EWS_LLM_KEY`, `EWS_ORACLE_PASS`, `EWS_PROXY_USER`, `EWS_PROXY_PASS`),
      2. Sonra aynı klasördeki `.llm_key.txt` (gitignore'dadır) dosyasına bakılır,
      3. İkisi de yoksa program çalıştığında konsoldan (Spyder / terminal) Türkçe olarak sorulur.
  - Kendi bilgisayarınızda kalıcı ayar yapmak isterseniz `ayarlar_local.py` adında bir dosya oluşturup
    sadece değiştirmek istediğiniz değişkenleri içine yazabilirsiniz (`ayarlar_local.py` gitignore'dadır
    ve otomatik olarak buradaki değerlerin üzerine yazılır).
"""

from __future__ import annotations

import getpass
import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent

# =============================================================================
# 1. AĞ VE PROXY AYARLARI (1_scraper.py & 4_resmi_ilanlar.py)
# =============================================================================
NETWORK_MODE = "BANK"                  # "BANK" (kurumsal proxy) veya "HOME" (doğrudan internet)
PROXY_HOST = "<PROXY_HOST>:<PORT>"     # Örn: "proxy.banka.com.tr:8080"
PROXY_BYPASS_DOMAINS = ["cnbce.com"]   # BANK modunda bile proxy'siz gidilecek siteler
LOOKBACK_DAYS = 10                     # Haber ve resmi ilan taramasında geriye dönük gün sayısı
ENABLE_KAP_CRAWLING = False            # KAP bildirim taraması (True / False)
ENABLE_RESMI_ILANLAR = False           # İlan.gov.tr & Resmî Gazete taraması (ayarlar_local.py'de True yapılabilir)
SCRAPER_MAX_WORKERS = 8                # Eşzamanlı haber indirme iş parçacığı sayısı

# =============================================================================
# 2. ORACLE VERİ AMBARI (DWH) AYARLARI (2_sql_to_parquet.py)
# =============================================================================
ORACLE_HOST = "<ORACLE_HOST>"
ORACLE_PORT = 1521
ORACLE_SERVICE = "<SERVICE_NAME>"
ORACLE_USER = "<ORACLE_USER>"
ORACLE_PASSWORD = ""                   # Boş bırakın; çalıştırıldığında sorulur
ORACLE_SCHEMA = "<SEMA>"               # temp8 ve company_partners tablolarının bulunduğu şema

# Eğer aynı klasörde `sorgu.txt` varsa, SQL Developer'da elle çalıştırmak yerine doğrudan Python içinden
# o sorguyu çalıştırabilir veya temp8 tablosunun kaç günlük olduğunu kontrol edebiliriz.
SORGU_TXT_PATH = ROOT_DIR / "sorgu.txt"
STALE_DATA_WARN_DAYS = 30              # structured_data.parquet bu günden eskiyse kullanıcı uyarılır

# =============================================================================
# 3. KURUM İÇİ LLM SERVİSİ AYARLARI (3_llm_dogrulama.py & 5_geriye_test.py)
# =============================================================================
LLM_URL = "https://<SUNUCU>/v1/chat/completions"
LLM_API_KEY = ""                       # Boş bırakılırsa .llm_key.txt'den okunur veya çalışırken sorulur
LLM_MODEL = ""                         # Kurumsal endpoint "model" alanını reddediyorsa boş bırakın
LLM_VERIFY_SSL = "windows"             # "windows" (Windows sertifika deposu), True, False veya PEM dosya yolu
LLM_TIMEOUT = 300                      # İstek zaman aşımı (saniye)
LLM_WORKERS = 1                        # Kişisel API anahtarında paralel limit 1 ise 1 yapın (429 almamak için)
LLM_RETRIES = 3                        # Geçici ağ/5xx/429 hatalarında tekrar deneme sayısı
LLM_MAX_TOKENS = 1024                  # Yanıt başına azami token
LLM_LIMIT = 0                          # 0 = tüm yeni eşleşmeleri sor; test için örn. 20

# =============================================================================
# 4. EŞLEŞTİRME VE SKORLAMA EŞİKLERİ
# =============================================================================
AGGREGATION_WINDOW_DAYS = 180          # Haber eşleştirme penceresi (son 180 gün)
ALIAS_MIN_ESLESME = 3                  # Hiçbiri kabul edilmeyen alias için çıkarma önerisi alt sınırı
ZAYIF_DSCR = 1.2                       # Altındaki DSCR "zayıf finansal" sayılır (kâr düşüşüyle birleşince Stage 2)

# =============================================================================
# 5. DOSYA YOLLARI (Tümü proje kök dizininde; ağ klasörüyse buradan değiştirilebilir)
# =============================================================================
NEWS_PARQUET_PATH = ROOT_DIR / "news.parquet"
STRUCTURED_PARQUET_PATH = ROOT_DIR / "structured_data.parquet"
PORTFOLIO_CONFIG_PATH = ROOT_DIR / "portfolio_config.py"
CACHE_PATH = ROOT_DIR / "llm_cache.jsonl"
KONTROL_PATH = ROOT_DIR / "kontrol_etiketleri.jsonl"
GECMIS_PATH = ROOT_DIR / "dashboard_gecmis.jsonl"
EXCEL_OUTPUT_PATH = ROOT_DIR / "llm_dogrulama.xlsx"
DASHBOARD_OUTPUT_PATH = ROOT_DIR / "dashboard.html"
KEY_FILE_PATH = ROOT_DIR / ".llm_key.txt"


# =============================================================================
# 6. Yerel Override (ayarlar_local.py varsa buradaki ayarların üzerine yazar)
# =============================================================================
try:
    from ayarlar_local import *  # noqa: F401, F403
except ImportError:
    pass


# =============================================================================
# 7. Kullanıcı Dostu ("Idiot-Proof") Kimlik Bilgisi Yardımcıları
# =============================================================================
def get_llm_api_key(interactive: bool = True) -> str:
    """LLM API anahtarını sırasıyla ayarlar.py, EWS_LLM_KEY ortam değişkeni veya .llm_key.txt dosyasından okur.
    Hiçbiri yoksa ve interactive=True ise kullanıcıya konsoldan sorup .llm_key.txt'ye kaydeder."""
    if LLM_API_KEY and LLM_API_KEY.strip():
        return LLM_API_KEY.strip()
    env_key = os.getenv("EWS_LLM_KEY") or os.getenv("IBTHINK_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()
    if KEY_FILE_PATH.exists():
        saved = KEY_FILE_PATH.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    if not interactive:
        return ""
    print("\n" + "=" * 70)
    print("[AYAR GEREKLİ] LLM API Anahtarı bulunamadı.")
    print("İpucu: Anahtarınız '.llm_key.txt' dosyasına kaydedilecek (Git'e yüklenmez),")
    print("böylece bir sonraki çalıştırmada tekrar girmeniz gerekmeyecek.")
    print("=" * 70)
    entered = getpass.getpass("LLM API Anahtarınızı girin (veya Enter ile geçin): ").strip()
    if entered:
        try:
            KEY_FILE_PATH.write_text(entered, encoding="utf-8")
            print("[BİLGİ] API anahtarı '.llm_key.txt' dosyasına kaydedildi.")
        except OSError:
            pass
    return entered


def get_llm_url(interactive: bool = True) -> str:
    """LLM sunucu adresi yer tutucu (<SUNUCU>) içeriyorsa kullanıcıyı uyarır ve adresi sorar."""
    url = os.getenv("EWS_LLM_URL") or LLM_URL
    if "<SUNUCU>" not in url or not interactive:
        return url
    print("\n" + "=" * 70)
    print("[AYAR GEREKLİ] `ayarlar.py` içindeki LLM_URL henüz doldurulmamış:")
    print(f"  Mevcut değer: {url}")
    print("  Örnek: https://llm.banka.com.tr/v1/chat/completions")
    print("=" * 70)
    entered = input("LLM Endpoint URL adresini girin: ").strip()
    return entered if entered else url


def get_oracle_password(interactive: bool = True) -> str:
    """Oracle şifresini ayarlar.py veya ortam değişkeninden alır; yoksa kullanıcıya sorar (dosyaya kaydetmez)."""
    if ORACLE_PASSWORD and ORACLE_PASSWORD.strip():
        return ORACLE_PASSWORD.strip()
    env_pass = os.getenv("EWS_ORACLE_PASS")
    if env_pass and env_pass.strip():
        return env_pass.strip()
    if not interactive:
        return ""
    print(f"\n[ORACLE BAĞLANTISI] Kullanıcı: {ORACLE_USER} @ {ORACLE_HOST}:{ORACLE_PORT}/{ORACLE_SERVICE}")
    return getpass.getpass("Oracle Şifresi (boş bırakırsanız bağlantı denenmez): ").strip()


def get_proxy_url(interactive: bool = True) -> str | None:
    """BANK ağ modunda kurumsal proxy URL'sini oluşturur. Yer tutucu (<PROXY_HOST>) duruyorsa doğrudan bağlanır."""
    if NETWORK_MODE.strip().upper() not in ("BANK", "BANKA"):
        return None
    host = os.getenv("EWS_PROXY_HOST") or PROXY_HOST
    if not host or "<PROXY_HOST>" in host:
        print("[BİLGİ] PROXY_HOST henüz ayarlanmamış (<PROXY_HOST>); doğrudan bağlantı kullanılacak.")
        return None
    from urllib.parse import quote
    user = os.getenv("EWS_PROXY_USER") or (input("Kurumsal Proxy Kullanıcı Adı (boş = şifresiz proxy): ").strip() if interactive else "")
    if not user:
        return f"http://{host}"
    pw = os.getenv("EWS_PROXY_PASS") or (getpass.getpass("Kurumsal Proxy Şifresi: ") if interactive else "")
    return f"http://{quote(user, safe='')}:{quote(pw, safe='')}@{host}"
