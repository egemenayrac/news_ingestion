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
import io
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
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
ILAN_GOV_BASE = "https://www.ilan.gov.tr"
ILAN_GOV_SEARCH_API = f"{ILAN_GOV_BASE}/api/api/services/app/Ad/AdsByFilter"
ILAN_GOV_DETAIL_API = f"{ILAN_GOV_BASE}/api/api/services/app/AdDetail/GetAdDetail"
ILAN_GOV_PAGE_SIZE = 20  # sunucu maxResultCount'u yok sayıp sayfa başına 20 ilan döndürüyor (canlı API, 24 Kasım)
ILAN_GOV_AD_TYPE_TEBLIGAT = 4  # 2=İcra, 3=İhale, 4=Tebligat (Konkordato ve İflas Hukuku Davaları dahil), 5=Personel
ILAN_GOV_HEADERS = {
    "accept": "text/plain, application/json",
    "content-type": "application/json-patch+json",
    "origin": ILAN_GOV_BASE,
    "referer": f"{ILAN_GOV_BASE}/ilan/tum-ilanlar",
    "x-request-origin": "IGT-UI",
    "x-requested-with": "XMLHttpRequest",
}

# Resmi ilanlarda aranan risk anahtar kelimeleri (HTML ve PDF ilanlar için)
RISK_KEYWORDS_RE = re.compile(
    r"\b(konkordato|geçici\s+mühlet|kesin\s+mühlet|iflas|tasfiye|alacaklılara\s+çağrı|"
    r"ihale(?:lere|den)?\s+(?:katılmaktan\s+)?yasakla\w*|yasaklama\s+karar[ıi]|"
    r"kayy[ıu]m|mal\s*varlığına\s+(?:tedbir|el\s+koy\w*)|tmsf|tasarruf\s+mevduatı\s+sigorta\s+fonu|"
    r"yargı\s+ilan[ıi]|icra\s+ve\s+iflas)\b",
    flags=re.IGNORECASE | re.UNICODE,
)
VKN_PATTERNS = (
    re.compile(r"\b(?:VKN|Vergi\s*(?:Kimlik)?\s*No|Vergi\s*Numaras[ıi])\s*[:.]?\s*(\d{10})\b", re.IGNORECASE),
    re.compile(r"\b(\d{10})\s*(?:vergi\s*(?:kimlik\s*)?numaral[ıi]|vergi\s*(?:kimlik\s*)?no['’]?lu|VKN['’]?li)\b", re.IGNORECASE),
)
MERSIS_PATTERNS = (
    re.compile(r"\b(?:MERS[İI]S|Mersis)\s*(?:No|Numaras[ıi])?\s*[:.]?\s*(\d{16})\b", re.IGNORECASE),
    re.compile(r"\b(\d{16})\s*(?:MERS[İI]S|Mersis)\s*(?:numaral[ıi]|no['’]?lu)?\b", re.IGNORECASE),
)
TCKN_MASK_PATTERNS = (
    (re.compile(r"(\b(?:T\.?\s*C\.?\s*(?:Kimlik)?\s*(?:No|Numaras[ıi])?|TCKN)\s*[:.]?\s*)\d{11}\b", re.IGNORECASE), r"\1***"),
    (re.compile(r"\b\d{11}(\s*(?:T\.?\s*C\.?\s*(?:Kimlik)?\s*(?:numaral[ıi]|no['’]?lu)|TCKN['’]?li))\b", re.IGNORECASE), r"***\1"),
    # Etiketsiz 11 haneli sayılar da maskelenir (ilanlarda "Ad Soyad 12345678901," ve 11 haneli telefonlar görüldü).
    # 10 haneli VKN ve 16 haneli MERSİS rakam sınırları sayesinde etkilenmez.
    (re.compile(r"(?<!\d)\d{11}(?!\d)"), "***"),
)


# =============================================================================
# 2. Ağ Oturumu
# =============================================================================
def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
    })
    proxy_url = ayarlar.get_proxy_url(interactive=True)
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
        session.verify = False
    return session


def mask_tckn(text: str) -> str:
    """İlan metnindeki 11 haneli gerçek kişi TCKN'lerini maskeler; 10 haneli VKN ve 16 haneli MERSİS korunur."""
    masked = text or ""
    for pat, repl in TCKN_MASK_PATTERNS:
        masked = pat.sub(repl, masked)
    return masked


def extract_identifiers(text: str) -> str:
    """Metinde geçen VKN (10 hane) ve MERSİS (16 hane) numaralarını özet etiketi olarak çıkarır."""
    raw = text or ""
    vkns = sorted({m for pat in VKN_PATTERNS for m in pat.findall(raw)})
    mersis = sorted({m for pat in MERSIS_PATTERNS for m in pat.findall(raw)})
    parts = []
    if vkns:
        parts.append(f"VKN: {', '.join(vkns)}")
    if mersis:
        parts.append(f"MERSİS: {', '.join(mersis)}")
    return f" [{'; '.join(parts)}]" if parts else ""


def _parse_ilan_date(raw: object) -> Optional[datetime]:
    """İlan.gov.tr tarih biçimlerini (ISO veya DD.MM.YYYY) UTC datetime'a çevirir."""
    s = str(raw or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# =============================================================================
# 3. İlan.gov.tr Konkordato ve İflas İlanları
# =============================================================================
def fetch_ilan_gov_tr(session: requests.Session, existing_urls: Set[str], days: int = LOOKBACK_DAYS,
                      max_pages: int = 15, page_size: int = ILAN_GOV_PAGE_SIZE) -> List[dict]:
    """İlan.gov.tr AdsByFilter + GetAdDetail API üzerinden son `days` gündeki Konkordato/İflas tebligatlarını çeker."""
    results: List[dict] = []
    seen_ids: Set[str] = set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    ppdmin = cutoff.strftime("%d.%m.%Y")
    search_terms = ["konkordato", "geçici mühlet", "kesin mühlet", "iflas"]

    for term in search_terms:
        for page in range(max_pages):
            payload = {
                "keys": {
                    "q": [term],
                    "ats": [ILAN_GOV_AD_TYPE_TEBLIGAT],
                    "ppdmin": [ppdmin],
                },
                "skipCount": page * page_size,
                "maxResultCount": page_size,
            }
            if page > 0:
                payload["keys"]["currentPage"] = [page + 1]
            try:
                res = session.post(
                    ILAN_GOV_SEARCH_API, json=payload, headers=ILAN_GOV_HEADERS, timeout=25
                )
                if res.status_code != 200:
                    logger.warning(
                        "İlan.gov.tr '%s' (sayfa %d) sorgusu HTTP %d döndü (uç nokta/erişim kontrol edilmeli).",
                        term, page + 1, res.status_code,
                    )
                    break
                data = res.json()
                result_obj = data.get("result") or {}
                items = result_obj.get("ads") or []
                if not items:
                    break
                total = result_obj.get("numFound")

                for item in items:
                    ad_id = str(item.get("id") or "").strip()
                    if not ad_id or ad_id in seen_ids:
                        continue
                    url_str = str(item.get("urlStr") or f"/ilan/{ad_id}")
                    url = urljoin(ILAN_GOV_BASE, url_str)
                    if url in existing_urls:
                        seen_ids.add(ad_id)
                        continue

                    pub_dt = _parse_ilan_date(item.get("publishStartDate") or item.get("publishDate"))
                    if pub_dt is not None and pub_dt < cutoff:
                        continue
                    tarih = (pub_dt or datetime.now(timezone.utc)).strftime("%Y-%m-%d")

                    title = str(item.get("title") or "Resmi Tebligat / Konkordato İlanı").strip()
                    advertiser = str(item.get("advertiserName") or "").strip()
                    body = ""
                    try:
                        d_res = session.get(
                            ILAN_GOV_DETAIL_API, params={"id": ad_id}, headers=ILAN_GOV_HEADERS, timeout=20
                        )
                        if d_res.status_code == 200:
                            d_json = d_res.json()
                            d_result = d_json.get("result") or {}
                            raw_html = str(d_result.get("content") or "")
                            body = BeautifulSoup(raw_html, "html.parser").get_text(" ", strip=True)
                            if not advertiser:
                                advertiser = str(d_result.get("advertiserName") or "").strip()
                        else:
                            logger.warning("İlan.gov.tr detay (id=%s) HTTP %d döndü.", ad_id, d_res.status_code)
                    except requests.RequestException as exc:
                        logger.warning("İlan.gov.tr detay (id=%s) çekilemedi: %s", ad_id, exc)

                    prefix = f"{title} ({advertiser})." if advertiser else f"{title}."
                    full_text = mask_tckn(re.sub(r"\s+", " ", f"{prefix} {body}").strip())
                    seen_ids.add(ad_id)
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
                    time.sleep(0.15)

                if len(items) < page_size or (isinstance(total, int) and (page + 1) * page_size >= total):
                    break
            except Exception as exc:
                logger.warning("İlan.gov.tr '%s' (sayfa %d) sorgusunda uyarı: %s", term, page + 1, exc)
                break
    logger.info("İlan.gov.tr: %d yeni resmi tebligat/konkordato ilanı bulundu.", len(results))
    return results


# =============================================================================
# 4. Resmî Gazete Çeşitli İlanlar, İhale Yasaklıları, TMSF ve Yargı İlanları (HTML + PDF)
# =============================================================================
def extract_pdf_text(pdf_bytes: bytes, max_pages: int = 30) -> str:
    """Resmî Gazete PDF ilanından metni `pypdf` ile çıkarır ve 11 haneli TCKN'leri anında maskeler.

    10 haneli şirket VKN'leri ve 16 haneli MERSİS numaraları `mask_tckn` tarafından korunur.
    Bozuk/şifreli PDF'lerde akışı kırmadan boş string döner.
    """
    if not pdf_bytes:
        return ""
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf kurulu değil; Resmî Gazete PDF ilanları atlandı (pip install pypdf).")
        return ""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages_text: List[str] = []
        for page in reader.pages[:max_pages]:
            txt = page.extract_text() or ""
            if txt.strip():
                pages_text.append(txt)
        raw = re.sub(r"\s+", " ", "\n".join(pages_text)).strip()
        return mask_tckn(raw)
    except Exception as exc:
        logger.warning("Resmî Gazete PDF okunamadı: %s", exc)
        return ""


def _is_pdf_response(url: str, response: object) -> bool:
    clean_url = (url or "").split("?", 1)[0].split("#", 1)[0].lower()
    if clean_url.endswith(".pdf"):
        return True
    headers = getattr(response, "headers", None) or {}
    ctype = str(headers.get("Content-Type") or headers.get("content-type") or "").lower()
    if "application/pdf" in ctype:
        return True
    content = getattr(response, "content", b"") or b""
    return isinstance(content, (bytes, bytearray)) and content[:5] == b"%PDF-"


def _extract_resmi_gazete_links(soup: BeautifulSoup, index_url: str) -> List[Tuple[str, str]]:
    """Günlük indeks sayfasından HTML ve PDF ilan bağlantılarını `(url, link_metni)` olarak çıkarır."""
    links: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        clean_href = href.split("?", 1)[0].split("#", 1)[0].lower()
        if not clean_href.endswith((".htm", ".html", ".pdf")) or clean_href.endswith("-4.htm"):
            continue
        full_url = urljoin(index_url, href)
        if full_url in seen:
            continue
        seen.add(full_url)
        links.append((full_url, a.get_text(" ", strip=True)))
    return links


def fetch_resmi_gazete(session: requests.Session, existing_urls: Set[str], days: int = LOOKBACK_DAYS) -> List[dict]:
    """Son `days` günün Resmî Gazete 'Çeşitli İlanlar' sayfasındaki HTML ve PDF ilanlarını tarar.

    İhale yasaklama, TMSF ve yargı ilanları (PDF dahil) `pypdf` ile okunur; tüm metin ve başlıklarda
    11 haneli TCKN maskelenir, 10 haneli VKN ve 16 haneli MERSİS çıkarılır.
    """
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
            for link, anchor_text in _extract_resmi_gazete_links(soup, index_url):
                if link in existing_urls:
                    continue
                try:
                    sub_res = session.get(link, timeout=20)
                    if sub_res.status_code != 200:
                        continue
                    if _is_pdf_response(link, sub_res):
                        text = extract_pdf_text(sub_res.content)
                        if len(text) < 100 or not RISK_KEYWORDS_RE.search(text):
                            continue
                        raw_title = anchor_text or text[:160] or "Resmî Gazete PDF İlanı"
                        title = mask_tckn(re.sub(r"\s+", " ", raw_title).strip())[:160]
                    else:
                        sub_soup = BeautifulSoup(sub_res.content, "html.parser")
                        text = mask_tckn(re.sub(r"\s+", " ", sub_soup.get_text(" ", strip=True)))
                        if len(text) < 150 or not RISK_KEYWORDS_RE.search(text):
                            continue
                        title_el = sub_soup.find(["h1", "h2", "b", "strong"])
                        raw_title = title_el.get_text(strip=True) if title_el else (anchor_text or "Resmî Gazete İlanı")
                        title = mask_tckn(re.sub(r"\s+", " ", raw_title).strip())[:160]
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
