"""Kurumsal Kredi Riski Erken Uyarı Sistemi (EWS) - LLM doğrulama ve skorlama orkestratörü (3_llm_dogrulama.py).

ews.eslestirme haberleri alias'larla şirketlere bilerek geniş eşleştirir: bir haberin kaçmaması için yanlış
eşleşmeye ("ANKARA 2" -> "Ankara 2. Asliye Ceza Mahkemesi") razı olunur. Bu betik her (haber, şirket)
eşleşmesini bankanın LLM'ine sorar (alaka ve olay), alıntıyı haberde arar, şirket başına olay defteri tutar
(haber sinyali), finansal skoru hesaplar ve ikisini bakiyeyle birleştirir. Puan yerine kanıtlı olay üretilir:
hedef değişken olmadan puan kalibre edilemez, alıntılı olay ise kontrol edilebilir.

Modüller (ews/ paketi altında):
  ews/eslestirme.py   haber ve şirket eşleştirme (bu betik çağırır, ayrıca çalıştırmaya gerek yok)
  ews/llm.py          LLM istemcisi, /v1/moderations benzerlik servisi ve önbellek
  ews/olay.py         olay sözlüğü, alaka/olay soruları, alıntı kontrolü
  ews/defter.py       şirket başına olay defteri (açılan / kapanan olaylar)
  ews/tablolar.py     Şirketler, Olay_Defteri, Kayıtlar, Kontrol, Alias sayfaları
  ews/portfoy.py      finansal skor (ews.financial), Birlesik (haber + finansal + bakiye + 2B matris) ve Temalar sayfaları
  ews/dashboard.py    dashboard.html (tarayıcıda açılan panel) ve günlük sinyal geçmişi
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

import ayarlar
from ews import dashboard as ews_dashboard  # noqa: E402
from ews import defter as ews_defter  # noqa: E402
from ews import olay as ews_olay  # noqa: E402
from ews import portfoy as ews_portfoy  # noqa: E402
from ews import tablolar as ews_tablolar  # noqa: E402
from ews.llm import Cache, LLMClient, resolve_ssl_verify  # noqa: E402

logger = logging.getLogger("ews_llm")


# =============================================================================
# Ayarlar (ayarlar.py üzerinden okunur)
# =============================================================================
LLM = {"url": ayarlar.LLM_URL, "key": ayarlar.LLM_API_KEY, "model": ayarlar.LLM_MODEL}
LLM_VERIFY_SSL = ayarlar.LLM_VERIFY_SSL
LLM_TIMEOUT = ayarlar.LLM_TIMEOUT
LLM_WORKERS = ayarlar.LLM_WORKERS
LLM_RETRIES = ayarlar.LLM_RETRIES
LLM_MAX_TOKENS = ayarlar.LLM_MAX_TOKENS
LIMIT = ayarlar.LLM_LIMIT
ALIAS_MIN_ESLESME = ayarlar.ALIAS_MIN_ESLESME
ZAYIF_DSCR = ayarlar.ZAYIF_DSCR

CACHE_PATH = ayarlar.CACHE_PATH
CIKTI_PATH = ayarlar.EXCEL_OUTPUT_PATH
DASHBOARD_PATH = ayarlar.DASHBOARD_OUTPUT_PATH
GECMIS_PATH = ayarlar.GECMIS_PATH
KONTROL_PATH = ayarlar.KONTROL_PATH


# =============================================================================
# Adımlar
# =============================================================================
def load_matches() -> pd.DataFrame:
    """ews.eslestirme ile haberleri şirketlerle eşleştirir; her kayda önbellek anahtarı ekler."""
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
    df = load_matches()
    if df.empty:
        logger.warning("Eşleşen kayıt yok.")
        return pd.DataFrame(), pd.DataFrame()

    # 1. Alaka ve olay soruları (yalnızca önbellekte olmayanlar; en yeni haber önce)
    cache = Cache(CACHE_PATH)
    todo = [r for r in df.to_dict("records") if r["_Anahtar"] not in cache]
    todo = todo[:limit] if limit else todo

    url = LLM["url"]
    key = LLM["key"]
    if todo:
        url = ayarlar.get_llm_url(interactive=True) if "<SUNUCU>" in url else url
        key = key or ayarlar.get_llm_api_key(interactive=True)
        if "<SUNUCU>" in url:
            raise SystemExit("ayarlar.py: LLM_URL ayarını doldurun.")

    llm = LLMClient(url=url, key=key, model=LLM["model"], verify=resolve_ssl_verify(LLM_VERIFY_SSL),
                    timeout=LLM_TIMEOUT, retries=LLM_RETRIES, max_tokens=LLM_MAX_TOKENS)
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
