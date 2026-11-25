"""DuckDB tabanlı hızlı SQL analitik ve kaynak sağlık modülü (ews/analytics.py).

`news.parquet` ve `structured_data.parquet` dosyalarını sunucusuz (in-memory) DuckDB ile
doğrudan disk üzerinden sorgular; scraper kesintilerini yakalayan kaynak sağlık özetleri,
günlük kaynak matrisleri ve portföy finansal özetleri üretir.

Docker / Postgres gerektirmez; Windows + Anaconda/Spyder ve Python 3.9+ uyumludur.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb
import pandas as pd

_READ_ONLY_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|copy|attach|detach|export|import|pragma|call|load|install)\b",
    re.IGNORECASE,
)

# Farklı Tarih biçimlerini (ISO, YYYY-MM-DD HH:MM, DD.MM.YYYY, TIMESTAMP) güvenli DATE'e çeviren DuckDB ifadesi:
_DUCKDB_DATE_EXPR = (
    "COALESCE("
    "TRY_CAST(\"Tarih\" AS DATE), "
    "TRY_CAST(SUBSTR(CAST(\"Tarih\" AS VARCHAR), 1, 10) AS DATE), "
    "CAST(TRY_STRPTIME(SUBSTR(CAST(\"Tarih\" AS VARCHAR), 1, 10), '%d.%m.%Y') AS DATE)"
    ")"
)


def _sql_path(path: Path) -> str:
    return str(Path(path).resolve()).replace("'", "''")


def _normalize_ref_date(as_of: Optional[object]) -> date:
    if as_of is None:
        return datetime.now().date()
    if isinstance(as_of, datetime):
        return as_of.date()
    if isinstance(as_of, date):
        return as_of
    return pd.Timestamp(as_of).date()


def _parquet_columns(con: duckdb.DuckDBPyConnection, path: Path) -> List[str]:
    rows = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{_sql_path(path)}')").fetchall()
    return [str(r[0]) for r in rows]


def news_pool_stats(news_parquet: Path) -> Dict[str, Any]:
    """news.parquet havuzunun tek geçişte hızlı DuckDB özet istatistiklerini döndürür."""
    p = Path(news_parquet)
    if not p.exists():
        return {"mevcut": False, "toplam_haber": 0}
    con = duckdb.connect(":memory:")
    try:
        cols = set(_parquet_columns(con, p))
        url_expr = 'COUNT(DISTINCT NULLIF(TRIM(CAST("URL" AS VARCHAR)), \'\'))' if "URL" in cols else "0"
        src_expr = 'COUNT(DISTINCT NULLIF(TRIM(CAST("Kaynak" AS VARCHAR)), \'\'))' if "Kaynak" in cols else "0"
        resmi_expr = (
            "SUM(CASE WHEN LOWER(COALESCE(CAST(\"Kaynak\" AS VARCHAR), '')) "
            "IN ('ilan.gov.tr', 'resmî gazete', 'resmi gazete', 'kap') THEN 1 ELSE 0 END)"
            if "Kaynak" in cols
            else "0"
        )
        dt_expr = _DUCKDB_DATE_EXPR if "Tarih" in cols else "CAST(NULL AS DATE)"
        row = con.execute(
            f"""
            WITH base AS (
                SELECT *, {dt_expr} AS _dt
                FROM read_parquet('{_sql_path(p)}')
            )
            SELECT
                COUNT(*) AS toplam_haber,
                {url_expr} AS tekil_url,
                {src_expr} AS kaynak_sayisi,
                MIN(_dt) AS ilk_tarih,
                MAX(_dt) AS son_tarih,
                SUM(CASE WHEN _dt IS NULL THEN 1 ELSE 0 END) AS tarihsiz_haber,
                {resmi_expr} AS resmi_ilan_sayisi
            FROM base
            """
        ).fetchone()
    finally:
        con.close()

    ilk, son = row[3], row[4]
    span = (son - ilk).days + 1 if (ilk is not None and son is not None) else 0
    return {
        "mevcut": True,
        "toplam_haber": int(row[0] or 0),
        "tekil_url": int(row[1] or 0),
        "kaynak_sayisi": int(row[2] or 0),
        "ilk_tarih": ilk.isoformat() if ilk is not None else None,
        "son_tarih": son.isoformat() if son is not None else None,
        "kapsam_gun": int(span),
        "tarihsiz_haber": int(row[5] or 0),
        "resmi_ilan_sayisi": int(row[6] or 0),
    }


def source_health_summary(
    news_parquet: Path,
    days: int = 14,
    as_of: Optional[object] = None,
) -> pd.DataFrame:
    """Kaynak bazında haber hacmi, tazelik ve kesinti/uyarı sağlık özetini DuckDB ile hesaplar."""
    cols_out = [
        "Kaynak",
        "Toplam_Haber",
        "Son_Donem_Haber",
        "Aktif_Gun",
        "Gunluk_Ortalama",
        "Son_Haber_Tarihi",
        "Bayatlik_Gun",
        "Saglik_Durumu",
    ]
    p = Path(news_parquet)
    if not p.exists():
        return pd.DataFrame(columns=cols_out)

    ref_date = _normalize_ref_date(as_of)
    window = max(1, int(days))

    con = duckdb.connect(":memory:")
    try:
        cols = set(_parquet_columns(con, p))
        if "Kaynak" not in cols or "Tarih" not in cols:
            return pd.DataFrame(columns=cols_out)
        df = con.execute(
            f"""
            WITH base AS (
                SELECT
                    COALESCE(NULLIF(TRIM(CAST("Kaynak" AS VARCHAR)), ''), '(Bilinmiyor)') AS Kaynak,
                    {_DUCKDB_DATE_EXPR} AS _dt
                FROM read_parquet('{_sql_path(p)}')
            ),
            agg AS (
                SELECT
                    Kaynak,
                    COUNT(*)::BIGINT AS Toplam_Haber,
                    SUM(CASE WHEN _dt IS NOT NULL
                              AND _dt <= CAST(? AS DATE)
                              AND _dt > CAST(? AS DATE) - INTERVAL '{window} days'
                             THEN 1 ELSE 0 END)::BIGINT AS Son_Donem_Haber,
                    COUNT(DISTINCT CASE WHEN _dt IS NOT NULL
                                         AND _dt <= CAST(? AS DATE)
                                         AND _dt > CAST(? AS DATE) - INTERVAL '{window} days'
                                        THEN _dt END)::BIGINT AS Aktif_Gun,
                    MAX(_dt) AS Son_Haber_Tarihi
                FROM base
                GROUP BY Kaynak
            )
            SELECT
                Kaynak,
                Toplam_Haber,
                Son_Donem_Haber,
                Aktif_Gun,
                ROUND(CAST(Son_Donem_Haber AS DOUBLE) / {window}, 2) AS Gunluk_Ortalama,
                CAST(Son_Haber_Tarihi AS VARCHAR) AS Son_Haber_Tarihi,
                CASE
                    WHEN Son_Haber_Tarihi IS NULL THEN NULL
                    ELSE GREATEST(0, DATE_DIFF('day', Son_Haber_Tarihi, CAST(? AS DATE)))
                END AS Bayatlik_Gun,
                CASE
                    WHEN Son_Haber_Tarihi IS NULL THEN 'KESİNTİ'
                    WHEN DATE_DIFF('day', Son_Haber_Tarihi, CAST(? AS DATE)) > 7 THEN 'KESİNTİ'
                    WHEN DATE_DIFF('day', Son_Haber_Tarihi, CAST(? AS DATE)) > 2 THEN 'UYARI'
                    ELSE 'SAĞLIKLI'
                END AS Saglik_Durumu
            FROM agg
            ORDER BY
                CASE Saglik_Durumu WHEN 'KESİNTİ' THEN 0 WHEN 'UYARI' THEN 1 ELSE 2 END,
                Toplam_Haber DESC,
                Kaynak ASC
            """,
            [ref_date, ref_date, ref_date, ref_date, ref_date, ref_date, ref_date],
        ).df()
    finally:
        con.close()
    return df


def daily_source_matrix(
    news_parquet: Path,
    days: int = 14,
    as_of: Optional[object] = None,
) -> pd.DataFrame:
    """Son `days` gün için Kaynak × Tarih haber sayısı matrisini (pivot) DuckDB ile üretir."""
    p = Path(news_parquet)
    if not p.exists():
        return pd.DataFrame()
    ref_date = _normalize_ref_date(as_of)
    window = max(1, int(days))
    con = duckdb.connect(":memory:")
    try:
        cols = set(_parquet_columns(con, p))
        if "Kaynak" not in cols or "Tarih" not in cols:
            return pd.DataFrame()
        raw = con.execute(
            f"""
            WITH base AS (
                SELECT
                    COALESCE(NULLIF(TRIM(CAST("Kaynak" AS VARCHAR)), ''), '(Bilinmiyor)') AS Kaynak,
                    {_DUCKDB_DATE_EXPR} AS _dt
                FROM read_parquet('{_sql_path(p)}')
            )
            SELECT Kaynak, CAST(_dt AS VARCHAR) AS Gun, COUNT(*)::BIGINT AS Adet
            FROM base
            WHERE _dt IS NOT NULL
              AND _dt <= CAST(? AS DATE)
              AND _dt > CAST(? AS DATE) - INTERVAL '{window} days'
            GROUP BY Kaynak, _dt
            ORDER BY Kaynak, _dt
            """,
            [ref_date, ref_date],
        ).df()
    finally:
        con.close()
    if raw.empty:
        return pd.DataFrame()
    return raw.pivot(index="Kaynak", columns="Gun", values="Adet").fillna(0).astype(int)


def _pick_col(cols: List[str], candidates: List[str]) -> Optional[str]:
    lower_map = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None


def portfolio_financial_summary(structured_parquet: Path) -> Dict[str, Any]:
    """structured_data.parquet için hızlı DuckDB portföy ve sektör özetini üretir."""
    p = Path(structured_parquet)
    if not p.exists():
        return {"mevcut": False, "satir_sayisi": 0, "sektor_ozeti": pd.DataFrame()}

    con = duckdb.connect(":memory:")
    try:
        cols = _parquet_columns(con, p)
        id_col = _pick_col(cols, ["counterparty_id", "sirket", "şirket", "musteri_no", "cust_id"])
        bal_col = _pick_col(cols, ["toplam_bakiye_try", "bakiye_try", "total_exposure_try", "exposure_try", "bakiye"])
        dt_col = _pick_col(cols, ["finansal_tarih", "statement_date", "financial_date", "donem", "as_of_dt", "fintarih"])
        sec_col = _pick_col(cols, ["sektor", "sektör", "sector", "nace_desc", "sector_name"])

        id_expr = f'COUNT(DISTINCT "{id_col}")' if id_col else "COUNT(*)"
        bal_expr = f'SUM(TRY_CAST("{bal_col}" AS DOUBLE))' if bal_col else "NULL"
        dt_min = f'MIN(CAST("{dt_col}" AS VARCHAR))' if dt_col else "NULL"
        dt_max = f'MAX(CAST("{dt_col}" AS VARCHAR))' if dt_col else "NULL"
        dt_cnt = f'COUNT(DISTINCT CAST("{dt_col}" AS VARCHAR))' if dt_col else "0"

        row = con.execute(
            f"""
            SELECT
                COUNT(*) AS satir_sayisi,
                {id_expr} AS sirket_sayisi,
                {bal_expr} AS toplam_bakiye_try,
                {dt_min} AS ilk_donem,
                {dt_max} AS son_donem,
                {dt_cnt} AS donem_sayisi
            FROM read_parquet('{_sql_path(p)}')
            """
        ).fetchone()

        sektor_df = pd.DataFrame()
        if sec_col:
            s_bal = f', ROUND(SUM(TRY_CAST("{bal_col}" AS DOUBLE)), 2) AS Toplam_Bakiye_TRY' if bal_col else ""
            s_ord = "Toplam_Bakiye_TRY DESC NULLS LAST" if bal_col else "Sirket_Sayisi DESC"
            sektor_df = con.execute(
                f"""
                SELECT
                    COALESCE(NULLIF(TRIM(CAST("{sec_col}" AS VARCHAR)), ''), '(Belirsiz)') AS Sektor,
                    {id_expr}::BIGINT AS Sirket_Sayisi
                    {s_bal}
                FROM read_parquet('{_sql_path(p)}')
                GROUP BY 1
                ORDER BY {s_ord}
                """
            ).df()
    finally:
        con.close()

    return {
        "mevcut": True,
        "satir_sayisi": int(row[0] or 0),
        "sirket_sayisi": int(row[1] or 0),
        "toplam_bakiye_try": float(row[2]) if row[2] is not None else None,
        "ilk_donem": row[3],
        "son_donem": row[4],
        "donem_sayisi": int(row[5] or 0),
        "kolonlar": cols,
        "sektor_ozeti": sektor_df,
    }


def query_parquet(
    sql: str,
    news_parquet: Optional[Path] = None,
    structured_parquet: Optional[Path] = None,
) -> pd.DataFrame:
    """`news` ve/veya `structured` Parquet görünümleri üzerinde salt-okunur DuckDB SQL çalıştırır."""
    clean = (sql or "").strip().rstrip(";").strip()
    if not re.match(r"(?is)^(select|with)\b", clean):
        raise ValueError("Yalnızca SELECT veya WITH ile başlayan salt-okunur SQL çalıştırılabilir.")
    no_str = re.sub(r"'(?:''|[^'])*'", "''", re.sub(r"--[^\n]*|/\*.*?\*/", " ", clean, flags=re.S))
    if ";" in no_str:
        raise ValueError("Tek seferde tek SQL sorgusu çalıştırılabilir.")
    m = _READ_ONLY_FORBIDDEN.search(no_str)
    if m:
        raise ValueError(f"İzin verilmeyen SQL anahtar kelimesi: {m.group(0)}")

    con = duckdb.connect(":memory:")
    try:
        if news_parquet is not None and Path(news_parquet).exists():
            con.execute(f"CREATE VIEW news AS SELECT * FROM read_parquet('{_sql_path(Path(news_parquet))}')")
        if structured_parquet is not None and Path(structured_parquet).exists():
            con.execute(f"CREATE VIEW structured AS SELECT * FROM read_parquet('{_sql_path(Path(structured_parquet))}')")
        return con.execute(clean).df()
    finally:
        con.close()
