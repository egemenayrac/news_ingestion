"""5_backtest.py - Backtesting, Kurşun Süresi (Lead Time) ve dwh_agent Entegrasyonu.

Sistemin geçmiş tarihlerde (`as_of`) vereceği erken uyarıları, bankanın DWH kayıtlarındaki gerçekleşen kötü
sonuçlarla (TFRS 9 Stage 2/3 geçişi, Yakın İzleme / Watchlist, Yapılandırma, 90+ gün gecikme / NPL) karşılaştırır.

Ölçülen 4 Temel Metrik:
  1. Yakalama Oranı (Recall / Hit Rate): Gerçekte kredi olayı yaşayan şirketlerin yüzde kaçına olay tarihinden
     önceki 180 gün içinde Stage 2 veya Stage 3 uyarısı verildi?
  2. Kurşun Süresi (Lead Time): İlk erken uyarı sinyali, bankadaki resmi temerrüt/izleme tarihinden kaç gün
     ÖNCE üretildi?
  3. Yanlış Alarm Oranı (False Alarm Rate): Hiçbir kredi sorunu yaşamayan şirketlerde 100 şirket-ay başına
     kaç kez Stage 2+ uyarısı verildi?
  4. Bileşen Katkı Analizi: Haber sinyali tek başına, Finansal skor tek başına ve Birleşik model birlikte
     nasıl performans gösteriyor?

dwh_agent Entegrasyonu:
  Eğer bilgisayarda `dwh_agent` bilgi tabanı (`kb.sqlite`) varsa, `find_candidate_outcome_tables()` fonksiyonu
  DWH içinde TFRS 9 Stage / DPD / NPL / Yakın İzleme tutan tabloları ve geçmiş ekip SQL'lerini otomatik listeler.
"""

from __future__ import annotations

import importlib
import json
import logging
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

import ayarlar  # noqa: E402
from ews import defter as ews_defter  # noqa: E402
from ews import eslestirme  # noqa: E402
from ews import olay as ews_olay  # noqa: E402
from ews import portfoy as ews_portfoy  # noqa: E402
from ews import tablolar as ews_tablolar  # noqa: E402
from ews.llm import Cache, LLMClient, resolve_ssl_verify  # noqa: E402

logger = logging.getLogger("ews_backtest")

# =============================================================================
# 1. Ayarlar
# =============================================================================
CACHE_PATH = ayarlar.CACHE_PATH
OUTCOMES_PATH = ROOT_DIR / "gerceklesen_olaylar.csv"   # veya .parquet (kolonlar: counterparty_id, outcome_dt, outcome_type)
BACKTEST_EXCEL_PATH = ROOT_DIR / "backtest_raporu.xlsx"
LEAD_WINDOW_DAYS = 180                                 # olaydan önceki kaç gün içinde verilen alarm "erken uyarı" sayılır

# dwh_agent kb.sqlite olası konumları
DWH_AGENT_KB_CANDIDATES = [
    ROOT_DIR.parent / "dwh_agent" / "kb" / "kb.sqlite",
    ROOT_DIR / "kb.sqlite",
]

# Oracle'da sonuç tablosu aramak için hazır katalog sorgusu (dwh_agent yoksa doğrudan Oracle'da çalıştırılabilir)
ORACLE_OUTCOME_DISCOVERY_SQL = """
SELECT c.owner, c.table_name,
       LISTAGG(c.column_name, ', ') WITHIN GROUP (ORDER BY c.column_name) AS sutunlar
FROM all_tab_columns c
WHERE REGEXP_LIKE(c.column_name, 'STAGE|DPD|GECIK|WATCH|IZLEME|YAPILAND|RESTRUCT|TAKIP|NPL|DEFAULT', 'i')
  AND EXISTS (
      SELECT 1 FROM all_tab_columns k
      WHERE k.owner = c.owner AND k.table_name = c.table_name
        AND REGEXP_LIKE(k.column_name, 'COUNTERPARTY|CUST|MUSTERI', 'i')
  )
GROUP BY c.owner, c.table_name
ORDER BY c.owner, c.table_name
"""


# =============================================================================
# 2. dwh_agent Bilgi Tabanından (kb.sqlite) Sonuç Tablosu Keşfi
# =============================================================================
def _card_summary(card_json: object) -> str:
    if not card_json:
        return ""
    try:
        return str(json.loads(str(card_json)).get("ozet") or "")
    except (ValueError, TypeError, AttributeError):
        return str(card_json)[:200]


def find_candidate_outcome_tables(kb_path: Optional[Path] = None) -> pd.DataFrame:
    """dwh_agent'ın kb.sqlite veritabanında Stage geçişi, Yakın İzleme, Yapılandırma ve NPL tablolarını arar."""
    target = Path(kb_path) if kb_path else next((p for p in DWH_AGENT_KB_CANDIDATES if p.exists()), None)
    if not target or not target.exists():
        logger.info("dwh_agent kb.sqlite bulunamadı. Oracle'da sonuç tablosu aramak için ORACLE_OUTCOME_DISCOVERY_SQL sorgusunu kullanın.")
        return pd.DataFrame()

    try:
        with sqlite3.connect(str(target)) as conn:
            conn.create_function(
                "REGEXP", 2, lambda pat, val: bool(val is not None and re.search(pat, str(val)))
            )
            has_cards = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='table_cards'"
            ).fetchone() is not None
            card_select = "COALESCE(tc.card, '') AS tablo_karti_ozeti" if has_cards else "'' AS tablo_karti_ozeti"
            card_join = (
                "LEFT JOIN table_cards tc ON tc.owner = o.owner AND tc.table_name = o.name AND tc.status = 'ok'"
                if has_cards else ""
            )
            query = f"""
            SELECT o.owner, o.name AS table_name, o.num_rows,
                   {card_select},
                   GROUP_CONCAT(c.column_name, ', ') AS ilgili_kolonlar
            FROM objects o
            JOIN columns c ON c.owner = o.owner AND c.table_name = o.name
            {card_join}
            WHERE c.column_name REGEXP '(?i)(STAGE|DPD|GECIK|WATCH|IZLEME|YAPILAND|RESTRUCT|TAKIP|NPL|DEFAULT|CLASSIFYING_NOTE)'
            GROUP BY o.owner, o.name
            ORDER BY COALESCE(o.num_rows, 0) DESC
            LIMIT 50
            """
            df = pd.read_sql_query(query, conn)
            if "tablo_karti_ozeti" in df.columns:
                df["tablo_karti_ozeti"] = df["tablo_karti_ozeti"].map(_card_summary)
            logger.info("dwh_agent kb.sqlite üzerinden %d aday sonuç tablosu bulundu.", len(df))
            return df
    except Exception as exc:
        logger.warning("kb.sqlite okunurken hata: %s", exc)
        return pd.DataFrame()


# =============================================================================
# 3. Belirli Bir Tarih (as_of) İçin Zaman Makinesi Koşusu
# =============================================================================
def run_snapshot_as_of(as_of_date: str, llm: LLMClient, cache: Cache, fin_df: pd.DataFrame,
                       zayif_dscr: float = 1.2, workers: int = 2,
                       prematched_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Sistemi sanki `as_of_date` günündeymişiz gibi yalnızca o tarihe kadarki haberlerle çalıştırır."""
    if prematched_df is not None:
        start, end = eslestirme.news_window(eslestirme.AGGREGATION_WINDOW_DAYS, as_of=as_of_date)
        dt_series = eslestirme.to_naive_datetime(prematched_df["Tarih"])
        df = prematched_df[(dt_series >= start) & (dt_series <= end)].copy()
    else:
        df = eslestirme.run_matching(as_of=as_of_date)
    if df.empty:
        return pd.DataFrame()
    df = df.reset_index(drop=True)
    if "_Anahtar" not in df.columns:
        df["_Anahtar"] = ews_olay.pair_keys(df)

    # Önbellekte olmayan eşleşmeler varsa ve LLM ayarlıysa sor
    todo = [r for r in df.to_dict("records") if r["_Anahtar"] not in cache]
    errors: Dict[str, str] = {}
    if todo and "<SUNUCU>" not in llm.url:
        cfg = importlib.import_module("3_llm_dogrulama")
        errors = cfg.ask_matches(todo, llm, cache)

    df = ews_olay.apply_results(df, cache, errors)
    ledgers, _ = ews_defter.build_ledgers(df, llm, cache, workers=workers, bugun=as_of_date)
    df = ews_defter.annotate(df, ledgers)
    companies = ews_tablolar.build_company_table(df, ledgers, bugun=as_of_date)

    # Finansal tabloda gelecekten bilgi sızmasını (look-ahead bias) önle: yalnızca statement_dt <= as_of_date olanlar
    as_of_ts = pd.Timestamp(as_of_date)
    fin_valid = fin_df
    if not fin_df.empty and "Finansal_Tarih" in fin_df.columns:
        f_dates = pd.to_datetime(fin_df["Finansal_Tarih"], errors="coerce")
        fin_valid = fin_df[f_dates.isna() | (f_dates <= as_of_ts)].copy()

    combined = ews_portfoy.build_combined_table(df, companies, fin_valid, zayif_dscr=zayif_dscr)
    combined.insert(0, "As_Of_Tarih", as_of_date)
    return combined


def _has_historical_financials(fin_df: Optional[pd.DataFrame]) -> bool:
    """Finansal tabloda şirket başına birden fazla tarihsel dönem (statement_dt) var mı?"""
    if fin_df is None or fin_df.empty or "Şirket" not in fin_df.columns or "Finansal_Tarih" not in fin_df.columns:
        return False
    valid = fin_df.dropna(subset=["Şirket", "Finansal_Tarih"])
    if valid.empty:
        return False
    return bool((valid.groupby("Şirket")["Finansal_Tarih"].nunique() > 1).any())


def build_limitations_table(snapshots: pd.DataFrame, fin_df: Optional[pd.DataFrame],
                            outcomes: Optional[pd.DataFrame], news_span_days: Optional[int] = None) -> pd.DataFrame:
    """backtest_raporu.xlsx içine eklenecek metodoloji sınırları ve yorumlama notları sayfası."""
    has_hist_fin = _has_historical_financials(fin_df)
    has_outcomes = outcomes is not None and not outcomes.empty
    n_periods = int(snapshots["As_Of_Tarih"].nunique()) if not snapshots.empty and "As_Of_Tarih" in snapshots.columns else 0
    if news_span_days is not None:
        arsiv_notu = (f"Haber arşivi {news_span_days} günlük dönemi kapsıyor "
                      + (f"(erken uyarı penceresi {LEAD_WINDOW_DAYS} günden kısa; ilk aylarda eksik pencere etkisi vardır)."
                         if news_span_days < LEAD_WINDOW_DAYS else f"(erken uyarı penceresi: {LEAD_WINDOW_DAYS} gün)."))
    else:
        arsiv_notu = f"Test {n_periods} as_of dönemi üzerinden çalıştırıldı (erken uyarı penceresi: {LEAD_WINDOW_DAYS} gün)."

    rows = [
        {
            "Sinir_Alani": "Hayatta Kalma Yanlılığı (Survivorship Bias)",
            "Durum": "UYARI",
            "Aciklama": "Portföy yalnızca bugün aktif (performing) olan en büyük 100 müşteriden oluşuyorsa geçmişte batmış/takibe düşmüş şirketler testte yer almaz; yakalama oranı (Recall) gerçek performansı temsil etmez.",
        },
        {
            "Sinir_Alani": "Finansal Veri Yapısı",
            "Durum": "TARİHSEL" if has_hist_fin else "TEK ANLIK GÖRÜNTÜ",
            "Aciklama": ("Şirket başına birden fazla tarihsel bilanço dönemi tespit edildi." if has_hist_fin else
                         "Finansal veriler tek bir güncel bilançodan oluşuyor (tarihsel seri ve yayın gecikmesi yok). Finansal kurşun süresi bu yüzden boş (None) bırakılmıştır; Birleşik kurşun süresi de finansal tetiklenen şirketlerde sabit bilanço etkisi taşır."),
        },
        {
            "Sinir_Alani": "Haber Arşivi Kapsamı",
            "Durum": "KISA ARŞİV" if (news_span_days is not None and news_span_days < LEAD_WINDOW_DAYS) else "BİLGİ",
            "Aciklama": arsiv_notu,
        },
        {
            "Sinir_Alani": "Raporun Kullanım Amacı",
            "Durum": "SİNYAL DAĞILIMI" if (not has_outcomes or not has_hist_fin) else "BACKTEST",
            "Aciklama": ("Gerçekleşen olay tablosu (outcomes) veya tarihsel portföy/bilanço serisi tamamlanana kadar bu çıktı yalnızca dönemsel sinyal dağılımı raporu olarak yorumlanmalıdır."
                         if (not has_outcomes or not has_hist_fin) else
                         "Gerçekleşen olay tablosu ve tarihsel finansal seriyle karşılaştırma yapılmıştır."),
        },
    ]
    return pd.DataFrame(rows)


# =============================================================================
# 4. Gerçekleşen Kredi Olaylarıyla Karşılaştırma ve Kurşun Süresi (Lead Time)
# =============================================================================
def evaluate_backtest(snapshots: pd.DataFrame, outcomes: pd.DataFrame,
                      lead_window_days: int = LEAD_WINDOW_DAYS,
                      fin_df: Optional[pd.DataFrame] = None,
                      news_span_days: Optional[int] = None) -> Dict[str, pd.DataFrame]:
    """Aylık/dönemlik EWS çıktıları (`snapshots`) ile bankadaki gerçekleşen olayları (`outcomes`) karşılaştırır.

    outcomes zorunlu kolonlar:
      - counterparty_id (veya Şirket): müşteri numarası
      - outcome_dt (veya Olay_Tarihi): bankada Stage 2/3, izleme ya da yapılandırma tarihi
      - outcome_type (isteğe bağlı): olay türü ("Stage 2 geçişi", "Yapılandırma", "NPL" vb.)
    """
    sinirlar = build_limitations_table(snapshots, fin_df, outcomes, news_span_days=news_span_days)
    if snapshots.empty:
        return {"Ozet_Metrikler": pd.DataFrame(), "Olay_Bazli_Analiz": pd.DataFrame(),
                "Tum_Donemler": snapshots, "Sinirlar": sinirlar}

    has_hist_fin = _has_historical_financials(fin_df)
    snap = snapshots.copy()
    snap["As_Of_DT"] = pd.to_datetime(snap["As_Of_Tarih"], errors="coerce")
    snap["Haber_Stage"] = snap["Haber_Sinyali"].map(ews_olay.STAGE_NO).fillna(1).astype(int)
    snap["Fin_Stage"] = snap["Finansal_Seviye"].map(ews_olay.STAGE_NO).fillna(1).astype(int)
    snap["Birlesik_Stage"] = snap["Birlesik_Seviye"].map(ews_olay.STAGE_NO).fillna(1).astype(int)

    if outcomes is None or outcomes.empty:
        logger.warning("Gerçekleşen olay tablosu (outcomes) boş; yalnızca dönemlik sinyal dağılımı üretildi.")
        ozet = (snap.groupby("As_Of_Tarih", as_index=False)
                .agg(Sirket_Sayisi=("Şirket", "nunique"),
                     Haber_Stage2_Ustu=("Haber_Stage", lambda s: int((s >= 2).sum())),
                     Haber_Stage3=("Haber_Stage", lambda s: int((s == 3).sum())),
                     Finansal_Stage2_Ustu=("Fin_Stage", lambda s: int((s >= 2).sum())),
                     Birlesik_Stage2_Ustu=("Birlesik_Stage", lambda s: int((s >= 2).sum()))))
        return {"Ozet_Metrikler": ozet, "Olay_Bazli_Analiz": pd.DataFrame(),
                "Tum_Donemler": snap.drop(columns=["As_Of_DT"]), "Sinirlar": sinirlar}

    cols = {c.lower(): c for c in outcomes.columns}
    id_col = cols.get("counterparty_id") or cols.get("şirket") or cols.get("sirket")
    dt_col = cols.get("outcome_dt") or cols.get("olay_tarihi") or cols.get("tarih")
    type_col = cols.get("outcome_type") or cols.get("olay_turu")
    if not id_col or not dt_col:
        raise ValueError("Sonuç tablosunda 'counterparty_id' (veya 'Şirket') ve 'outcome_dt' (veya 'Olay_Tarihi') kolonları olmalı.")

    out_df = outcomes.assign(
        _id=outcomes[id_col],
        _dt=pd.to_datetime(outcomes[dt_col], errors="coerce"),
        _tur=outcomes[type_col] if type_col else "Kredi Bozulması",
    ).dropna(subset=["_id", "_dt"])

    event_rows = []
    for _, ev in out_df.iterrows():
        cid, ev_dt, ev_type = ev["_id"], ev["_dt"], ev["_tur"]
        # Olay tarihinden önceki [ev_dt - lead_window_days, ev_dt] penceresindeki snapshot'lar
        prior = snap[(snap["Şirket"] == cid) & (snap["As_Of_DT"] <= ev_dt)
                     & (snap["As_Of_DT"] >= ev_dt - pd.Timedelta(days=lead_window_days))].sort_values("As_Of_DT")

        haber_hits = prior[prior["Haber_Stage"] >= 2]
        fin_hits = prior[prior["Fin_Stage"] >= 2]
        comb_hits = prior[prior["Birlesik_Stage"] >= 2]

        first_haber_dt = haber_hits["As_Of_DT"].iloc[0] if len(haber_hits) else pd.NaT
        first_fin_dt = fin_hits["As_Of_DT"].iloc[0] if (has_hist_fin and len(fin_hits)) else pd.NaT
        first_comb_dt = comb_hits["As_Of_DT"].iloc[0] if len(comb_hits) else pd.NaT

        event_rows.append({
            "Şirket": cid,
            "Şirket_Adi": prior["Şirket_Adi"].iloc[-1] if len(prior) else str(cid),
            "Gerceklesen_Olay_Tarihi": ev_dt.strftime("%Y-%m-%d"),
            "Gerceklesen_Olay_Turu": ev_type,
            "Haber_Yakaladi": bool(len(haber_hits)),
            "Finansal_Yakaladi": bool(len(fin_hits)),
            "Birlesik_Yakaladi": bool(len(comb_hits)),
            "Ilk_Haber_Alarm_Tarihi": first_haber_dt.strftime("%Y-%m-%d") if pd.notna(first_haber_dt) else None,
            "Haber_Kursun_Suresi_Gun": int((ev_dt - first_haber_dt).days) if pd.notna(first_haber_dt) else None,
            "Finansal_Kursun_Suresi_Gun": int((ev_dt - first_fin_dt).days) if pd.notna(first_fin_dt) else None,
            "Birlesik_Kursun_Suresi_Gun": int((ev_dt - first_comb_dt).days) if pd.notna(first_comb_dt) else None,
            "Alarm_Kaniti": haber_hits["En_Agir_Olay"].iloc[0] if len(haber_hits) else None,
        })

    ev_table = pd.DataFrame(event_rows)
    n_events = len(ev_table)

    # Hiç kredi olayı yaşamayan şirket-aylarda yanlış alarm oranı (100 şirket-ay başına)
    bad_companies = set(out_df["_id"])
    clean_snaps = snap[~snap["Şirket"].isin(bad_companies)]
    n_clean = max(1, len(clean_snaps))

    metrics = pd.DataFrame([
        {
            "Model_Katmani": "Yalnızca Haber Sinyali (Olay Defteri)",
            "Toplam_Kredi_Olayi": n_events,
            "Yakalanan_Olay": int(ev_table["Haber_Yakaladi"].sum()) if n_events else 0,
            "Yakalama_Orani_Yuzde": round(100 * ev_table["Haber_Yakaladi"].mean(), 1) if n_events else 0.0,
            "Medyan_Kursun_Suresi_Gun": float(ev_table["Haber_Kursun_Suresi_Gun"].median()) if n_events and ev_table["Haber_Kursun_Suresi_Gun"].notna().any() else None,
            "Yanlis_Alarm_100_Sirket_Ay": round(100 * (clean_snaps["Haber_Stage"] >= 2).sum() / n_clean, 1),
            "Not": "",
        },
        {
            "Model_Katmani": "Yalnızca Finansal Skor (3 Katmanlı)",
            "Toplam_Kredi_Olayi": n_events,
            "Yakalanan_Olay": int(ev_table["Finansal_Yakaladi"].sum()) if n_events else 0,
            "Yakalama_Orani_Yuzde": round(100 * ev_table["Finansal_Yakaladi"].mean(), 1) if n_events else 0.0,
            "Medyan_Kursun_Suresi_Gun": (float(ev_table["Finansal_Kursun_Suresi_Gun"].median())
                                         if has_hist_fin and n_events and ev_table["Finansal_Kursun_Suresi_Gun"].notna().any() else None),
            "Yanlis_Alarm_100_Sirket_Ay": round(100 * (clean_snaps["Fin_Stage"] >= 2).sum() / n_clean, 1),
            "Not": "" if has_hist_fin else "Finansal veri tarihsel değil (tek bilanço); kurşun süresi hesaplanmadı",
        },
        {
            "Model_Katmani": "Birleşik EWS (Haber + Finansal)",
            "Toplam_Kredi_Olayi": n_events,
            "Yakalanan_Olay": int(ev_table["Birlesik_Yakaladi"].sum()) if n_events else 0,
            "Yakalama_Orani_Yuzde": round(100 * ev_table["Birlesik_Yakaladi"].mean(), 1) if n_events else 0.0,
            "Medyan_Kursun_Suresi_Gun": float(ev_table["Birlesik_Kursun_Suresi_Gun"].median()) if n_events and ev_table["Birlesik_Kursun_Suresi_Gun"].notna().any() else None,
            "Yanlis_Alarm_100_Sirket_Ay": round(100 * (clean_snaps["Birlesik_Stage"] >= 2).sum() / n_clean, 1),
            "Not": "" if has_hist_fin else "Finansal katman tek bilanço (bkz. Sinirlar sayfası)",
        },
    ])
    return {"Ozet_Metrikler": metrics, "Olay_Bazli_Analiz": ev_table,
            "Tum_Donemler": snap.drop(columns=["As_Of_DT"]), "Sinirlar": sinirlar}


def _month_ends(start_dt: pd.Timestamp, end_dt: pd.Timestamp) -> pd.DatetimeIndex:
    """pandas 2.2+ ('ME') ve eski pandas ('M') sürümleriyle uyumlu ay sonu tarih aralığı."""
    try:
        return pd.date_range(start_dt, end_dt, freq="ME")
    except ValueError:
        return pd.date_range(start_dt, end_dt, freq="M")


def run_backtest(as_of_dates: Optional[List[str]] = None) -> Dict[str, pd.DataFrame]:
    """Haber arşivindeki ayların son günleri için geriye dönük testi çalıştırır ve Excel raporu üretir."""
    cfg = importlib.import_module("3_llm_dogrulama")
    url = ayarlar.get_llm_url(interactive=False) if "<SUNUCU>" in cfg.LLM["url"] else cfg.LLM["url"]
    key = cfg.LLM["key"] or ayarlar.get_llm_api_key(interactive=False)
    llm = LLMClient(url=url, key=key, model=cfg.LLM["model"],
                    verify=resolve_ssl_verify(cfg.LLM_VERIFY_SSL), timeout=cfg.LLM_TIMEOUT,
                    retries=cfg.LLM_RETRIES, max_tokens=cfg.LLM_MAX_TOKENS)
    cache = Cache(CACHE_PATH)
    fin_df = ews_portfoy.load_financials()
    news_span_days: Optional[int] = None

    if not as_of_dates:
        if not Path(eslestirme.PARQUET_PATH).exists():
            logger.error("Haber dosyası bulunamadı (%s); geriye dönük test çalıştırılamadı.", eslestirme.PARQUET_PATH)
            return {}
        news_df = pd.read_parquet(eslestirme.PARQUET_PATH, columns=["Tarih"])
        dates = eslestirme.to_naive_datetime(news_df["Tarih"]).dropna()
        if dates.empty:
            return {}
        news_span_days = int((dates.max().normalize() - dates.min().normalize()).days) + 1
        # Arşivdeki her ayın son günü + en son haber günü
        month_ends = _month_ends(dates.min().normalize(), dates.max().normalize())
        as_of_dates = sorted({d.strftime("%Y-%m-%d") for d in month_ends} | {dates.max().strftime("%Y-%m-%d")})

    # Tüm dönemler için eşleştirmeyi bir kez yapıp bellekte dilimle (disk/regex tekrarını önler)
    prematched = None
    if as_of_dates:
        ts_dates = [pd.Timestamp(d) for d in as_of_dates]
        span_days = max(0, (max(ts_dates) - min(ts_dates)).days) + eslestirme.AGGREGATION_WINDOW_DAYS + 1
        prematched = eslestirme.run_matching(as_of=max(ts_dates), window_days=span_days)
        if not prematched.empty:
            prematched = prematched.reset_index(drop=True)
            prematched["_Anahtar"] = ews_olay.pair_keys(prematched)

    logger.info("Geriye dönük test %d dönem için çalıştırılıyor: %s", len(as_of_dates), ", ".join(as_of_dates))
    snaps = []
    for dt_str in as_of_dates:
        comb = run_snapshot_as_of(
            dt_str, llm, cache, fin_df, zayif_dscr=cfg.ZAYIF_DSCR, workers=cfg.LLM_WORKERS, prematched_df=prematched
        )
        if not comb.empty:
            snaps.append(comb)

    all_snaps = pd.concat(snaps, ignore_index=True) if snaps else pd.DataFrame()
    outcomes = pd.read_csv(OUTCOMES_PATH) if OUTCOMES_PATH.exists() else pd.DataFrame()
    results = evaluate_backtest(all_snaps, outcomes, fin_df=fin_df, news_span_days=news_span_days)

    cand_tables = find_candidate_outcome_tables()
    if not cand_tables.empty:
        results["DWH_Aday_Tablolar"] = cand_tables

    if any(not tbl.empty for tbl in results.values()):
        ews_tablolar.write_excel({k: v for k, v in results.items() if not v.empty}, BACKTEST_EXCEL_PATH)
        logger.info("Geriye dönük test raporu yazıldı: %s", BACKTEST_EXCEL_PATH)
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    logging.getLogger("ews_eslestirme").setLevel(logging.WARNING)
    run_backtest()
