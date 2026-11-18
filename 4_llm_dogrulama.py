"""Kurumsal Kredi Riski Erken Uyarı Sistemi (EWS) - LLM doğrulama katmanı.

3_eslestirme.py haberleri alias'larla şirketlere bilerek geniş eşleştirir: bir haberin kaçmaması için yanlış
eşleşmeye ("ANKARA 2" -> "Ankara 2. Asliye Ceza Mahkemesi") razı olunur. Bu betik her (haber, şirket)
eşleşmesini bankanın LLM'ine sorar (alaka ve olay), alıntıyı haberde arar, şirket başına olay defteri tutar
(haber sinyali), finansal skoru hesaplar ve ikisini bakiyeyle birleştirir. Puan yerine kanıtlı olay üretilir:
hedef değişken olmadan puan kalibre edilemez, alıntılı olay ise kontrol edilebilir.

Dosyalar (hepsi aynı klasörde):
  3_eslestirme.py     haber ve şirket eşleştirme (bu betik çağırır, ayrıca çalıştırmaya gerek yok)
  4_llm_dogrulama.py  ayarlar ve akış (bu dosya)
  ews_llm.py          LLM istemcisi ve önbellek
  ews_olay.py         olay sözlüğü, alaka/olay soruları, alıntı kontrolü
  ews_defter.py       şirket başına olay defteri (açılan / kapanan olaylar)
  ews_tablolar.py     Şirketler, Olay_Defteri, Kayıtlar, Kontrol, Alias sayfaları
  ews_portfoy.py      finansal skor (ews_financial), Birlesik (haber + finansal + bakiye) ve Temalar sayfaları
  ews_dashboard.py    dashboard.html (tarayıcıda açılan panel) ve günlük sinyal geçmişi

Girdi:  haber parquet'i (yolu 3_eslestirme.py'de) ve portfolio_config.py, LLM; varsa structured_data.parquet
        (2_sql_to_parquet çıktısı; finansal skor ve bakiye).
Çıktı:  llm_dogrulama.xlsx (Birlesik, Şirketler, Olay_Defteri, Temalar, Kayıtlar, Kontrol, Kontrol_Etiketleri, Alias,
        Alias_Onerisi), llm_cache.jsonl (LLM cevapları: aynı soru bir daha sorulmaz, yarıda kalan koşu devam eder),
        kontrol_etiketleri.jsonl (Kontrol sayfasına yazılan analist notları; her koşuda Excel'den toplanır),
        dashboard.html ve dashboard_gecmis.jsonl (günlük sinyal geçmişi; "Ne değişti" sayfası bunu kullanır).
Çalıştırma: LLM ayarını doldurun ve bu dosyayı çalıştırın.
"""

from __future__ import annotations

import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import ews_dashboard  # noqa: E402  (modüller SCRIPT_DIR'den yüklenir)
import ews_defter  # noqa: E402
import ews_olay  # noqa: E402
import ews_portfoy  # noqa: E402
import ews_tablolar  # noqa: E402
from ews_llm import Cache, LLMClient, resolve_ssl_verify  # noqa: E402

logger = logging.getLogger("ews_llm")


# =============================================================================
# Ayarlar
# =============================================================================
# OpenAI uyumlu endpoint: tam adres (/v1/chat/completions dahil) ve API anahtarı. Kurumsal endpoint
# "model" alanını reddettiği için model boş bırakılır (boşsa gönderilmez).
LLM = {"url": "https://<SUNUCU>/v1/chat/completions", "key": "", "model": ""}
# "windows": bankanın kök sertifikası Windows sertifika deposundan alınır (önerilen).
# Diğerleri: True, False (doğrulama kapalı, yalnızca deneme) ya da r"C:\...\banka_ca.pem".
LLM_VERIFY_SSL = "windows"
LLM_TIMEOUT = 300            # saniye
LLM_WORKERS = 2              # paralel istek; HTTP 429 alınırsa 1
LLM_RETRIES = 3
LLM_MAX_TOKENS = 1024
LIMIT = 0                    # 0 = hepsi; denemede ör. 20 (en yeni haberler önce sorulur)
ALIAS_MIN_ESLESME = 3        # bu kadar eşleşmenin hiçbiri kabul edilmeyen alias için "remove" önerilir
ZAYIF_DSCR = 1.2             # Birlesik: altındaki DSCR "zayıf finansal" (kâr düşüşü Stage 2'ye çıkar)

CACHE_PATH = SCRIPT_DIR / "llm_cache.jsonl"
CIKTI_PATH = SCRIPT_DIR / "llm_dogrulama.xlsx"
DASHBOARD_PATH = SCRIPT_DIR / "dashboard.html"
GECMIS_PATH = SCRIPT_DIR / "dashboard_gecmis.jsonl"  # silinirse "Ne değişti" ve trend sıfırdan başlar
KONTROL_PATH = SCRIPT_DIR / "kontrol_etiketleri.jsonl"  # Kontrol sayfasındaki analist notları; silinirse kaybolur
# Prompt sürümleri modüllerdedir: ews_olay.PROMPT_SURUMU, ews_defter.DEFTER_SURUMU, ews_portfoy.TEMA_SURUMU.


# =============================================================================
# Adımlar
# =============================================================================
def load_matches() -> pd.DataFrame:
    """3_eslestirme ile haberleri şirketlerle eşleştirir; her kayda önbellek anahtarı ekler."""
    df = ews_olay.eslestirme.run_matching()
    if df.empty:
        return pd.DataFrame()
    df = df.reset_index(drop=True)
    df["_Anahtar"] = ews_olay.pair_keys(df)
    return df


def ask_matches(todo: List[dict], llm: LLMClient, cache: Cache) -> Dict[str, str]:
    """Eşleşmeleri paralel sorar, cevapları önbelleğe yazar; hata verenleri (anahtar -> mesaj) döndürür."""
    errors: Dict[str, str] = {}
    started = time.time()
    with ThreadPoolExecutor(max_workers=LLM_WORKERS) as pool:
        futures = {pool.submit(ews_olay.ask_pair, llm, r): r["_Anahtar"] for r in todo}
        for i, future in enumerate(as_completed(futures), 1):
            key = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # beklenmeyen hata tek eşleşmeyi düşürür, koşuyu değil
                result = {"hata": f"{type(exc).__name__}: {exc}"}
            if "hata" in result:
                errors[key] = result["hata"]
            else:
                cache.put(key, result)
            if i % 200 == 0:
                logger.info("  %s/%s (%.0f dk)", i, len(todo), (time.time() - started) / 60)
    if errors:
        logger.warning("%s eşleşme hata verdi, sonraki çalışmada tekrar sorulur. Örnek: %s",
                       len(errors), next(iter(errors.values())))
    return errors


def run(limit: int = LIMIT) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Eşleştirir, yeni eşleşmeleri LLM'e sorar, defteri kurar, Excel'i yazar; (kayıtlar, şirketler) döndürür."""
    started = time.time()
    llm = LLMClient(url=LLM["url"], key=LLM["key"], model=LLM["model"], verify=resolve_ssl_verify(LLM_VERIFY_SSL),
                    timeout=LLM_TIMEOUT, retries=LLM_RETRIES, max_tokens=LLM_MAX_TOKENS)
    df = load_matches()
    if df.empty:
        logger.warning("Eşleşen kayıt yok.")
        return pd.DataFrame(), pd.DataFrame()

    # 1. Alaka ve olay soruları (yalnızca önbellekte olmayanlar; en yeni haber önce)
    cache = Cache(CACHE_PATH)
    todo = [r for r in df.to_dict("records") if r["_Anahtar"] not in cache]
    todo = todo[:limit] if limit else todo
    if todo and "<SUNUCU>" in llm.url:
        raise SystemExit("4_llm_dogrulama.py: LLM ayarını (url, key) doldurun.")
    logger.info("%s eşleşme, %s tanesi LLM'e sorulacak.", len(df), len(todo))
    errors = ask_matches(todo, llm, cache)
    df = ews_olay.apply_results(df, cache, errors)

    # 2. Olay defteri
    ledgers, defter_errors = ews_defter.build_ledgers(df, llm, cache, LLM_WORKERS)
    if defter_errors:
        logger.warning("%s defter sorusu hata verdi (haber yeni olay sayıldı, sonraki çalışmada tekrar sorulur). "
                       "Örnek: %s", len(defter_errors), next(iter(defter_errors.values())))
    df = ews_defter.annotate(df, ledgers)

    # 3. Tablolar, Excel ve dashboard
    companies = ews_tablolar.build_company_table(df, ledgers)
    fin = ews_portfoy.load_financials()
    combined = ews_portfoy.build_combined_table(df, companies, fin, ZAYIF_DSCR)
    themes = ews_portfoy.build_themes(df, companies, fin, llm, cache)
    aliases, alias_rules = ews_tablolar.build_alias_tables(df, ALIAS_MIN_ESLESME)
    labels = ews_tablolar.harvest_labels(CIKTI_PATH, KONTROL_PATH)  # Excel üzerine yazılmadan önce
    path = ews_tablolar.write_excel({
        "Birlesik": combined,
        "Şirketler": companies,
        "Olay_Defteri": ews_tablolar.build_ledger_table(df, ledgers),
        "Temalar": themes,
        "Kayıtlar": ews_tablolar.build_record_table(df),
        "Kontrol": ews_tablolar.build_check_table(df, labels),
        "Kontrol_Etiketleri": ews_tablolar.build_label_table(labels),
        "Alias": aliases,
        "Alias_Onerisi": alias_rules,
    }, CIKTI_PATH)
    try:  # dashboard hatası Excel'i etkilemesin
        ews_dashboard.write_dashboard(df, companies, combined, ledgers, themes, aliases, len(errors),
                                      DASHBOARD_PATH, GECMIS_PATH)
    except Exception as exc:
        logger.warning("Dashboard yazılamadı (%s: %s); Excel çıktısı etkilenmedi.", type(exc).__name__, exc)

    events = [e for evs in ledgers.values() for e in evs]
    logger.info("Bitti (%.0f dk): %s yeni cevap önbelleğe yazıldı, %s hata. Toplam %s eşleşmenin %s tanesinin "
                "LLM cevabı var. Olay defteri: %s olay (%s açık, %s kapandı). Tema: %s.", (time.time() - started) / 60,
                len(todo) - len(errors), len(errors), len(df), int(ews_olay.answered(df).sum()),
                len(events), sum(e.acik for e in events), sum(not e.acik for e in events), len(themes))
    logger.info("Analist notları (%s): %s.", len(labels), ews_tablolar.label_summary(labels))
    logger.info("Çıktı: %s ve %s", path, DASHBOARD_PATH)
    return df, companies


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("ews_eslestirme").setLevel(logging.WARNING)  # eşleştirmenin bilgi satırları gizlenir
    df_llm_kayitlar, df_llm_sirketler = run()
