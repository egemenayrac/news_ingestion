"""4_resmi_ilanlar.py - Resmi İlan ve Tebligat Tarayıcısı (İlan.gov.tr Konkordato/İflas + Resmî Gazete).

Ekonomi basınında haberi hiç çıkmayan orta/büyük ölçekli şirketlerin konkordato mühlet kararları,
iflas/tasfiye tebligatları, ihaleden yasaklama ve kayyum/malvarlığı tedbir kararları kanunen ilan edilir.
Bu betik:
  1. İlan.gov.tr (Basın İlan Kurumu) üzerindeki son günlerin Konkordato ve İflas/Tasfiye tebligatlarını,
  2. Resmî Gazete (resmigazete.gov.tr) Çeşitli İlanlar bölümündeki İhale Yasaklıları ve mahkeme kararlarını
çeker, metin içindeki Vergi Kimlik Numarası (VKN - 10 hane) ve MERSİS (16 hane) numaralarını ayıklar ve
sonuçları `news.parquet` havuzuna ("Kaynak": "İlan.gov.tr" / "Resmî Gazete") ekler.

Böylece `3_llm_dogrulama.py` çalıştırıldığında resmi ilanlar da aynı eşleştirme, alıntı doğrulama ve
olay defteri (ews/defter.py) akışına girer.
"""

from __future__ import annotations

import getpass
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import quote, urljoin

import pandas as pd
import requests
import urllib3
from bs4 import BeautifulSoup

import ayarlar

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger("ews_resmi_ilan")

# =============================================================================
# 1. Ayarlar (ayarlar.py üzerinden okunur)
# =============================================================================
ROOT_DIR = ayarlar.ROOT_DIR
PARQUET_PATH = ayarlar.NEWS_PARQUET_PATH

NETWORK_MODE = ayarlar.NETWORK_MODE
PROXY_HOST = ayarlar.PROXY_HOST
LOOKBACK_DAYS = ayarlar.LOOKBACK_DAYS

# Resmi Gazete ve İlan.gov.tr uç noktaları
RESMI_GAZETE_BASE = "https://www.resmigazete.gov.tr"
ILAN_GOV_SEARCH_API = "https://www.ilan.gov.tr/api/api/services/app/Ad/GetAdsByFilter"
ILAN_GOV_DETAIL_BASE = "https://www.ilan.gov.tr/ilan/"

# Resmi ilanlarda aranan risk anahtar kelimeleri
RISK_KEYWORDS_RE = re.compile(
    r"\b(konkordato|geçici\s+mühlet|kesin\s+mühlet|iflas|tasfiye|alacaklılara\s+çağrı|"
    r"ihale(?:lere)?\s+katılmaktan\s+yasaklama|kayy[ıu]m|mal\s*varlığına\s+tedbir)\b",
    flags=re.IGNORECASE | re.UNICODE,
)
VKN_RE = re.compile(r"\b(?:VKN|Vergi\s*(?:Kimlik)?\s*No|Vergi\s*Numarası)\s*[:.]?\s*(\d{10})\b", re.IGNORECASE)
MERSIS_RE = re.compile(r"\b(?:MERSİS|Mersis)\s*(?:No|Numarası)?\s*[:.]?\s*(\d{16})\b", re.IGNORECASE)


# =============================================================================
# 2. Ağ Oturumu
# =============================================================================
def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
    })
    proxy_url = ayarlar.get_proxy_url(interactive=True)
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
        session.verify = False
    return session


def extract_identifiers(text: str) -> str:
    """Metinde geçen VKN (10 hane) ve MERSİS (16 hane) numaralarını özet etiketi olarak çıkarır."""
    vkns = sorted(set(VKN_RE.findall(text or "")))
    mersis = sorted(set(MERSIS_RE.findall(text or "")))
    parts = []
    if vkns:
        parts.append(f"VKN: {', '.join(vkns)}")
    if mersis:
        parts.append(f"MERSİS: {', '.join(mersis)}")
    return f" [{'; '.join(parts)}]" if parts else ""


# =============================================================================
# 3. İlan.gov.tr Konkordato ve İflas İlanları
# =============================================================================
def fetch_ilan_gov_tr(session: requests.Session, existing_urls: Set[str], days: int = LOOKBACK_DAYS) -> List[dict]:
    """İlan.gov.tr üzerinde son `days` gündeki Konkordato ve İflas/Tasfiye tebligatlarını arar."""
    results: List[dict] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    search_terms = ["konkordato", "geçici mühlet", "kesin mühlet", "iflas"]

    for term in search_terms:
        payload = {
            "searchText": term,
            "skipCount": 0,
            "maxResultCount": 50,
            "sorting": "PublishDate DESC",
        }
        try:
            res = session.post(ILAN_GOV_SEARCH_API, json=payload, timeout=20)
            if res.status_code != 200:
                continue
            data = res.json()
            items = ((data.get("result") or {}).get("items")) or data.get("items") or []
            for item in items:
                ad_id = item.get("id") or item.get("adNo")
                slug = item.get("urlSlug") or item.get("seoUrl") or str(ad_id)
                url = urljoin(ILAN_GOV_DETAIL_BASE, str(slug))
                if not ad_id or url in existing_urls:
                    continue

                pub_str = str(item.get("publishDate") or item.get("startDate") or "")[:10]
                try:
                    pub_dt = datetime.fromisoformat(pub_str).replace(tzinfo=timezone.utc)
                    if pub_dt < cutoff:
                        continue
                    tarih = pub_dt.strftime("%Y-%m-%d")
                except ValueError:
                    tarih = datetime.now(timezone.utc).strftime("%Y-%m-%d")

                title = str(item.get("title") or "Resmi Tebligat / Konkordato İlanı").strip()
                raw_body = str(item.get("content") or item.get("summary") or item.get("description") or "")
                body = BeautifulSoup(raw_body, "html.parser").get_text(" ", strip=True)

                # İçerik özet halindeyse ilan detay sayfasından tam metni çek
                if len(body) < 200 and url.startswith("http"):
                    try:
                        d_res = session.get(url, timeout=15)
                        if d_res.status_code == 200:
                            soup = BeautifulSoup(d_res.text, "html.parser")
                            detail_box = soup.find("div", class_=re.compile(r"(ad-detail|ilan-detay|content)", re.I))
                            if detail_box:
                                body = detail_box.get_text(" ", strip=True)
                    except requests.RequestException:
                        pass

                full_text = re.sub(r"\s+", " ", f"{title}. {body}").strip()
                if len(full_text) < 80 or not RISK_KEYWORDS_RE.search(full_text):
                    continue

                id_tag = extract_identifiers(full_text)
                existing_urls.add(url)
                results.append({
                    "Tarih": tarih,
                    "Kaynak": "İlan.gov.tr",
                    "Başlık": f"{title}{id_tag}",
                    "URL": url,
                    "Metin": full_text,
                })
        except Exception as exc:
            logger.warning("İlan.gov.tr '%s' sorgusunda uyarı: %s", term, exc)
    logger.info("İlan.gov.tr: %d yeni resmi tebligat/konkordato ilanı bulundu.", len(results))
    return results


# =============================================================================
# 4. Resmî Gazete Çeşitli İlanlar ve İhale Yasaklıları
# =============================================================================
def fetch_resmi_gazete(session: requests.Session, existing_urls: Set[str], days: int = LOOKBACK_DAYS) -> List[dict]:
    """Son `days` günün Resmî Gazete 'Çeşitli İlanlar' sayfasındaki ihale yasaklıları ve yargı ilanlarını tarar."""
    results: List[dict] = []
    today = datetime.now(timezone.utc)

    for offset in range(days + 1):
        dt = today - timedelta(days=offset)
        tarih_iso = dt.strftime("%Y-%m-%d")
        # Resmî Gazete günlük çeşitli ilanlar indeksi: /ilanlar/eskiilanlar/YYYY/MM/YYYYMMDD-4.htm
        index_url = f"{RESMI_GAZETE_BASE}/ilanlar/eskiilanlar/{dt.year}/{dt.month:02d}/{dt:%Y%m%d}-4.htm"
        if index_url in existing_urls:
            continue
        try:
            res = session.get(index_url, timeout=15)
            if res.status_code != 200:
                continue
            soup = BeautifulSoup(res.content, "html.parser")
            links = [urljoin(index_url, a["href"]) for a in soup.find_all("a", href=True)
                     if a["href"].endswith((".htm", ".html")) and not a["href"].endswith("-4.htm")]
            for link in links:
                if link in existing_urls:
                    continue
                try:
                    sub_res = session.get(link, timeout=15)
                    if sub_res.status_code != 200:
                        continue
                    sub_soup = BeautifulSoup(sub_res.content, "html.parser")
                    text = re.sub(r"\s+", " ", sub_soup.get_text(" ", strip=True))
                    if len(text) < 150 or not RISK_KEYWORDS_RE.search(text):
                        continue
                    title_el = sub_soup.find(["h1", "h2", "b", "strong"])
                    title = title_el.get_text(strip=True)[:160] if title_el else "Resmî Gazete İlanı"
                    id_tag = extract_identifiers(text)
                    existing_urls.add(link)
                    results.append({
                        "Tarih": tarih_iso,
                        "Kaynak": "Resmî Gazete",
                        "Başlık": f"{title}{id_tag}",
                        "URL": link,
                        "Metin": text,
                    })
                except requests.RequestException:
                    continue
            time.sleep(0.5)
        except Exception as exc:
            logger.warning("Resmî Gazete (%s) taramasında uyarı: %s", tarih_iso, exc)
    logger.info("Resmî Gazete: %d yeni karar/ilan bulundu.", len(results))
    return results


# =============================================================================
# 5. Parquet Havuzuna Artımlı Kayıt
# =============================================================================
def run_official_gazette_pipeline(days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    existing_df = pd.DataFrame()
    existing_urls: Set[str] = set()
    if PARQUET_PATH.exists():
        try:
            existing_df = pd.read_parquet(PARQUET_PATH)
            if "URL" in existing_df.columns:
                existing_urls = set(existing_df["URL"].dropna().astype(str))
        except Exception as exc:
            logger.warning("Mevcut parquet okunamadı: %s", exc)

    session = create_session()
    new_items = []
    new_items.extend(fetch_ilan_gov_tr(session, existing_urls, days=days))
    new_items.extend(fetch_resmi_gazete(session, existing_urls, days=days))

    if not new_items:
        logger.info("Yeni resmi ilan bulunamadı; mevcut havuz korundu (%d kayıt).", len(existing_df))
        return existing_df

    df_new = pd.DataFrame(new_items)
    df_combined = pd.concat([existing_df, df_new], ignore_index=True) if not existing_df.empty else df_new
    df_combined.drop_duplicates(subset=["URL"], inplace=True)
    df_combined.sort_values(by="Tarih", ascending=False, inplace=True)
    df_combined.reset_index(drop=True, inplace=True)
    df_combined.to_parquet(PARQUET_PATH, index=False)
    logger.info("Resmi ilanlar eklendi: +%d yeni ilan (toplam %d kayıt -> %s).", len(df_new), len(df_combined), PARQUET_PATH)
    return df_combined


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    run_official_gazette_pipeline()
