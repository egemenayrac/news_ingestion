"""
1_scraper.py
Kurumsal Kredi Riski Erken Uyarı Sistemi (EWS) - Haber ve Bildirim Ingestion Katmanı
"""

import os
import pathlib
import io
import re
import json
import time
import gzip
import email.utils
import warnings
import getpass
import concurrent.futures
from urllib.parse import urljoin, quote
from datetime import datetime, timedelta, timezone

import requests
import urllib3
import pandas as pd
from bs4 import BeautifulSoup

# Sistem uyarılarının konsolda bastırılması
warnings.filterwarnings("ignore", category=UserWarning, module='bs4')
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import ayarlar

# =============================================================================
# 1. YAPILANDIRMA VE DOSYA YOLU AYARLARI (ayarlar.py üzerinden okunur)
# =============================================================================

NETWORK_MODE = ayarlar.NETWORK_MODE
PROXY_HOST = ayarlar.PROXY_HOST
LOOKBACK_DAYS = ayarlar.LOOKBACK_DAYS
ENABLE_KAP_CRAWLING = ayarlar.ENABLE_KAP_CRAWLING
MAX_WORKERS = ayarlar.SCRAPER_MAX_WORKERS
TIME_LIMIT = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

PARQUET_DIR = str(ayarlar.NEWS_PARQUET_PATH.parent)
os.makedirs(PARQUET_DIR, exist_ok=True)
PARQUET_PATH = str(ayarlar.NEWS_PARQUET_PATH)

# Haber Kaynakları Konfigürasyonu
SOURCES = {
    "cnbce": {"name": "CNBC-e", "sitemaps": ["https://www.cnbce.com/newsmap.xml", "https://www.cnbce.com/posts-sitemap.xml"], "rss": ["https://www.cnbce.com/rss"], "enabled": True},
    "bloomberght": {"name": "Bloomberg HT", "sitemaps": ["https://www.bloomberght.com/sitemap.xml"], "rss": [], "enabled": True},
    "dunya": {"name": "Dünya Gazetesi", "sitemaps": ["https://www.dunya.com/sitemap.xml"], "rss": ["https://www.dunya.com/rss"], "enabled": True},
    "ekonomim": {"name": "Ekonomim", "sitemaps": ["https://www.ekonomim.com/sitemap_index.xml", "https://www.ekonomim.com/sitemap.xml"], "rss": ["https://www.ekonomim.com/rss"], "enabled": True},
    "patronlardunyasi": {"name": "Patronlar Dünyası", "sitemaps": ["https://www.patronlardunyasi.com/sitemap.xml"], "rss": ["https://www.patronlardunyasi.com/rss"], "enabled": True}
}

# =============================================================================
# 2. AĞ VE İLETİŞİM YÖNETİMİ
# =============================================================================

class HybridSession(requests.Session):
    def __init__(self, proxy_url=None, bypass_domains=None):
        super().__init__()
        self.custom_proxy_url = proxy_url
        self.bypass_domains = bypass_domains or []
        self.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate", "Connection": "keep-alive"
        })

    def request(self, method, url, **kwargs):
        if any(domain in url for domain in self.bypass_domains):
            kwargs['proxies'] = {"http": None, "https": None}
            kwargs['verify'] = True
        else:
            if self.custom_proxy_url:
                kwargs['proxies'] = {"http": self.custom_proxy_url, "https": self.custom_proxy_url}
            kwargs['verify'] = False if self.custom_proxy_url else True
        return super().request(method, url, **kwargs)

def create_configured_session() -> HybridSession:
    proxy_url = ayarlar.get_proxy_url(interactive=True)
    return HybridSession(proxy_url=proxy_url, bypass_domains=ayarlar.PROXY_BYPASS_DOMAINS)

_SESSION: HybridSession | None = None

def get_session() -> HybridSession:
    global _SESSION
    if _SESSION is None:
        _SESSION = create_configured_session()
    return _SESSION

# =============================================================================
# 3. YARDIMCI VERİ VE KAP DECODER METOTLARI
# =============================================================================

def parse_date(date_str: str) -> datetime:
    if not date_str: return None
    try:
        parsed_tuple = email.utils.parsedate_tz(date_str)
        if parsed_tuple: return email.utils.to_datetime(parsed_tuple).astimezone(timezone.utc)
        date_str = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(date_str)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except:
        return None

def get_soup_with_retry(url: str, retries: int = 3) -> BeautifulSoup:
    session = get_session()
    for _ in range(retries):
        try:
            res = session.get(url, timeout=(5, 15)) 
            if res.status_code == 200: return BeautifulSoup(res.content, 'html.parser')
        except: time.sleep(1) 
    return None

def get_content_with_retry(url: str, retries: int = 3) -> str:
    session = get_session()
    for _ in range(retries):
        try:
            res = session.get(url, timeout=(5, 20))
            if res.status_code == 200:
                content = res.content
                if content.startswith(b'\x1f\x8b'):  
                    content = gzip.GzipFile(fileobj=io.BytesIO(content)).read()
                return content.decode('utf-8', errors='ignore')
        except: time.sleep(1)
    return ""

def determine_source(url: str) -> str:
    if "kap.org.tr" in url: return "KAP"
    if "cnbce.com" in url: return "CNBC-e"
    if "bloomberght.com" in url: return "Bloomberg HT"
    if "dunya.com" in url: return "Dünya Gazetesi"
    if "ekonomim.com" in url: return "Ekonomim"
    if "patronlardunyasi.com" in url: return "Patronlar Dünyası"
    return "Unknown"

def remove_financial_disclaimers(text: str) -> str:
    if not text: return ""
    disclaimer_patterns = [
        r"Bu sayfada yer alan piyasa verileri.*?kesinlikle yasaktır\.?",
        r"Burada yer alan yatırım bilgi, yorum ve tavsiyeleri yatırım danışmanlığı kapsamında değildir.*?(olmayabilir|beklentilerinize uygun sonuçlar doğurmayabilir)\.?",
        r"BİST isim ve logosu.*?koruma altındadır.*?izinsiz kullanılamaz.*?bilgilerin telif hakları tamamen BİST'e aittir\.?",
        r"Veriler (Foreks|Matriks|İdeal|TradingView) tarafından sağlanmaktadır\.?"
    ]
    cleaned_text = text
    for pattern in disclaimer_patterns:
        cleaned_text = re.sub(pattern, '', cleaned_text, flags=re.IGNORECASE | re.DOTALL)
    return cleaned_text

# KAP Next.js Payload Decoder
NEXT_F_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
FLIGHT_ENTRY_RE = re.compile(rb'([0-9a-f]*):', re.I)
FLIGHT_TEXT_MARKER_RE = re.compile(rb'T([0-9a-f]+),', re.I)
FLIGHT_REF_RE = re.compile(r'^\$([0-9a-f]+)$')

def _parse_flight_chunk(buf: str, by_id: dict):
    buf_bytes = buf.encode("utf-8")
    pos, n = 0, len(buf_bytes)
    while pos < n:
        m = FLIGHT_ENTRY_RE.match(buf_bytes, pos)
        if not m: break
        node_id = m.group(1).decode("ascii")
        pos = m.end()
        text_m = FLIGHT_TEXT_MARKER_RE.match(buf_bytes, pos) if node_id else None
        if text_m:
            length = int(text_m.group(1), 16)
            start = text_m.end()
            by_id[node_id] = buf_bytes[start:start + length].decode("utf-8", errors="replace")
            pos = start + length
            if pos < n and buf_bytes[pos:pos + 1] == b"\n": pos += 1
            continue
        nl = buf_bytes.find(b"\n", pos)
        rest = (buf_bytes[pos:nl] if nl != -1 else buf_bytes[pos:]).decode("utf-8", errors="replace")
        if node_id:
            try: by_id[node_id] = json.loads(rest) if rest.startswith(("{", "[", '"')) else rest
            except: by_id[node_id] = rest
        pos = nl + 1 if nl != -1 else n

def kapweb_next_data(html: str) -> dict:
    if not html: return None
    pieces = [json.loads(m.group(1)) for m in NEXT_F_PUSH_RE.finditer(html) if True]
    by_id = {}
    _parse_flight_chunk("".join(pieces), by_id)
    return by_id or None

def kapweb_find_disclosure_body_ref(node, depth: int = 0) -> str:
    if depth > 8: return None 
    if isinstance(node, dict):
        body = node.get("disclosureBody")
        if isinstance(body, list) and body and isinstance(body[0], str): return body[0]
        for value in node.values():
            found = kapweb_find_disclosure_body_ref(value, depth + 1)
            if found: return found
    elif isinstance(node, list):
        for item in node:
            found = kapweb_find_disclosure_body_ref(item, depth + 1)
            if found: return found
    return None

def kapweb_resolve_body(by_id: dict) -> str:
    ref = kapweb_find_disclosure_body_ref(by_id)
    if not ref: return ""
    m = FLIGHT_REF_RE.match(ref)
    if not m: return ""
    raw = by_id.get(m.group(1))
    if not isinstance(raw, str) or not raw: return ""
    return BeautifulSoup(raw, "html.parser").get_text("\n", strip=True)

# =============================================================================
# 4. TOPLAMA VE ANALİZ METOTLARI
# =============================================================================

def fetch_kap_disclosures(existing_urls: set) -> list:
    print(f"[BİLGİ] KAP Resmi Bildirimleri Sorgulanıyor (Son {LOOKBACK_DAYS} Gün)...")
    kap_results = []
    seen_indexes = set()
    today = datetime.now(timezone.utc)
    start_date = today - timedelta(days=LOOKBACK_DAYS)
    
    session = get_session()
    try:
        session.get("https://www.kap.org.tr/tr/", timeout=15)
        current_date = start_date
        while current_date <= today:
            date_str = current_date.strftime("%d.%m.%Y")
            print(f"    -> KAP Tarih: {date_str}...", end=" ", flush=True)
            payload = {"fromDate": date_str, "toDate": date_str, "memberTypes": ["IGS", "DDK"]}
            try:
                res = session.post("https://www.kap.org.tr/tr/api/disclosure/list/main", json=payload, timeout=15)
                if res.status_code == 200:
                    data = res.json()
                    print(f"Kayıt: {len(data)}")
                    for item in data:
                        basic = item.get("disclosureBasic", item)
                        disc_index = basic.get("disclosureIndex")
                        if not disc_index or disc_index in seen_indexes: continue
                        seen_indexes.add(disc_index)
                        
                        kap_url = f"https://www.kap.org.tr/tr/Bildirim/{disc_index}"
                        if kap_url in existing_urls: continue
                        
                        subject = basic.get("subject", "KAP Bildirimi")
                        summary = basic.get("summary", "")
                        pub_date_str = basic.get("publishDate", "")
                        pub_date_dt = parse_date(pub_date_str) or today
                        
                        tam_metin = (summary if summary else f"Resmi Bildirim: {subject}").strip()
                        try:
                            detail_res = session.get(kap_url, timeout=15)
                            if detail_res.status_code == 200:
                                flight_data = kapweb_next_data(detail_res.text)
                                if flight_data:
                                    resolved = kapweb_resolve_body(flight_data)
                                    if resolved and len(resolved) > 50: tam_metin = resolved
                        except: pass
                        
                        kap_results.append({
                            "Tarih": pub_date_dt.strftime("%Y-%m-%d"),
                            "Kaynak": "KAP",
                            "Başlık": subject,
                            "URL": kap_url,
                            "Metin": tam_metin
                        })
            except: print("Bağlantı Hatası")
            current_date += timedelta(days=1)
            time.sleep(1)
    except Exception as e:
        print(f"[HATA] KAP Modülü Hatası: {e}")
    return kap_results

def parse_sitemap(url: str, existing_urls: set) -> dict:
    urls_dict = {}
    content = get_content_with_retry(url)
    if not content: return urls_dict
    
    sitemaps = re.findall(r'<[a-z0-9:]*sitemap[^>]*>(.*?)</[a-z0-9:]*sitemap>', content, re.DOTALL | re.IGNORECASE)
    if sitemaps:
        child_sitemaps = []
        for sm in sitemaps:
            loc_m = re.search(r'<[a-z0-9:]*loc[^>]*>(.*?)</', sm, re.DOTALL | re.IGNORECASE)
            lastmod_m = re.search(r'<[a-z0-9:]*lastmod[^>]*>(.*?)</', sm, re.DOTALL | re.IGNORECASE)
            if not loc_m: continue
            loc = loc_m.group(1).replace('<![CDATA[', '').replace(']]>', '').strip()
            if any(j in loc.lower() for j in ['author', 'tag', 'category', 'etiket', 'yazar', 'kategori']): continue
            if lastmod_m:
                sm_date = parse_date(lastmod_m.group(1).replace('<![CDATA[', '').replace(']]>', '').strip())
                if sm_date and sm_date < TIME_LIMIT: continue
            child_sitemaps.append(loc)
            
        if child_sitemaps:
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [executor.submit(parse_sitemap, cu, existing_urls) for cu in child_sitemaps]
                for f in concurrent.futures.as_completed(futures):
                    res = f.result()
                    if res: urls_dict.update(res)
        return urls_dict
        
    url_tags = re.findall(r'<[a-z0-9:]*url[^>]*>(.*?)</[a-z0-9:]*url>', content, re.DOTALL | re.IGNORECASE)
    for u in url_tags:
        loc_m = re.search(r'<[a-z0-9:]*loc[^>]*>(.*?)</', u, re.DOTALL | re.IGNORECASE)
        if not loc_m: continue
        loc = loc_m.group(1).replace('<![CDATA[', '').replace(']]>', '').strip()
        if loc.endswith('.xml') or loc in existing_urls: continue
        
        lastmod_m = re.search(r'<[a-z0-9:]*lastmod[^>]*>(.*?)</', u, re.DOTALL | re.IGNORECASE)
        pub_date = parse_date(lastmod_m.group(1).replace('<![CDATA[', '').replace(']]>', '').strip() if lastmod_m else None)
        if pub_date and pub_date < TIME_LIMIT: continue
        urls_dict[loc] = pub_date
    return urls_dict

def parse_rss(url: str, existing_urls: set) -> dict:
    urls_dict = {}
    content = get_content_with_retry(url)
    if not content: return urls_dict
    
    items = re.findall(r'<[a-z0-9:]*item[^>]*>(.*?)</[a-z0-9:]*item>', content, re.DOTALL | re.IGNORECASE)
    for item in items:
        link_m = re.search(r'<[a-z0-9:]*link[^>]*>(.*?)</', item, re.DOTALL | re.IGNORECASE)
        pub_date_m = re.search(r'<[a-z0-9:]*pubDate[^>]*>(.*?)</', item, re.DOTALL | re.IGNORECASE)
        if link_m:
            link = link_m.group(1).replace('<![CDATA[', '').replace(']]>', '').strip()
            if link in existing_urls: continue
            pub_date = parse_date(pub_date_m.group(1).replace('<![CDATA[', '').replace(']]>', '').strip() if pub_date_m else None)
            if pub_date and pub_date < TIME_LIMIT: continue
            urls_dict[link] = pub_date
    return urls_dict

def generate_hidden_sitemaps(base_url: str) -> list:
    generated, today = [], datetime.now(timezone.utc)
    current = (today - timedelta(days=LOOKBACK_DAYS)).replace(day=1)
    while current <= today:
        y, m = current.year, current.month
        generated.extend([f"{base_url}/sitemap-{y}-{m:02d}.xml", f"{base_url}/sitemap_news_{y}-{m:02d}.xml"])
        for p in range(1, 10):
            if p == 1: generated.extend([f"{base_url}/sitemap/{y}/{m}", f"{base_url}/sitemap/{y}/{m:02d}"])
            else: generated.extend([f"{base_url}/sitemap/{y}/{m}?page={p}", f"{base_url}/sitemap/{y}/{m}/{p}"])
        current = current.replace(year=y+1, month=1) if m == 12 else current.replace(month=m+1)
    return generated

def analyze_article(url: str, sitemap_date: datetime) -> dict:
    invalid_paths = ["/spor/", "/spor-ekonomisi/", "/magazin/", "/yasam/", "/kultur-sanat/", "/astroloji/", "/emtia/", "/piyasa/", "/pariteler/"]
    if any(p in url.lower() for p in invalid_paths): return None
    
    soup = get_soup_with_retry(url)
    if not soup: return None
    
    raw_html = str(soup)
    html_date = None
    date_patterns = [r'article:published_time"\s*content="([^"]+)"', r'datePublished"\s*:\s*"([^"]+)"', r'datetime="([^"]+)"']
    for dp in date_patterns:
        m = re.search(dp, raw_html, re.IGNORECASE)
        if m:
            html_date = parse_date(m.group(1))
            if html_date: break
            
    final_date = html_date if html_date else sitemap_date
    if not final_date or final_date < TIME_LIMIT: return None
        
    title = soup.find('h1').get_text(strip=True) if soup.find('h1') else "No Title"
    for el in soup(["nav", "footer", "aside", "header", "script", "style", "meta", "noscript", "form"]): el.decompose()
    for bd in soup.find_all('div', class_=re.compile(r'(widget|sidebar|banner|reklam|related|popular|menu)', re.I)): bd.decompose()

    article_container = soup.find('article') or soup.find(class_=re.compile(r'(article-body|content-detail|haber-detay|news-detail)', re.I))
    if article_container:
        paragraphs = article_container.find_all('p')
        body_text = ' '.join([p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 20]) if paragraphs else article_container.get_text(separator=' ', strip=True)
    else:
        paragraphs = soup.find_all('p')
        body_text = ' '.join([p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 30])

    body_text = remove_financial_disclaimers(re.sub(r'\s+', ' ', body_text).strip())
    if len(body_text) < 150: return None 

    return {"Tarih": final_date.strftime("%Y-%m-%d"), "Kaynak": determine_source(url), "Başlık": title, "URL": url, "Metin": body_text}

# =============================================================================
# 5. ORKESTRASYON VE ARTIMLI PARQUET UPSERT SÜRECİ
# =============================================================================

def run_ingestion_pipeline() -> pd.DataFrame:
    print(f"[BİLGİ] EWS News Ingestion Süreci Başlatıldı. Zaman Ufku: Son {LOOKBACK_DAYS} Gün.")
    
    existing_df = pd.DataFrame()
    existing_urls = set()
    if os.path.exists(PARQUET_PATH):
        try:
            existing_df = pd.read_parquet(PARQUET_PATH)
            if 'URL' in existing_df.columns:
                existing_urls = set(existing_df['URL'].dropna())
            print(f"[BİLGİ] Mevcut Parquet havuzunda {len(existing_urls)} kayıt bulundu. Sadece yeni URL'ler taranacak.")
        except Exception as e:
            print(f"[UYARI] Parquet okunamadı, sıfırdan oluşturulacak: {e}")

    new_news = []
    
    # 1. KAP Bildirimlerinin Artımlı Toplanması
    if ENABLE_KAP_CRAWLING:
        kap_items = fetch_kap_disclosures(existing_urls)
        new_news.extend(kap_items)
        
    # 2. Medya Kaynaklarından Bağlantı Toplanması (Sadece yeni URL'ler)
    all_new_urls = {}
    for source_id, source_info in SOURCES.items():
        if not source_info.get("enabled", False): continue
        print(f"[BİLGİ] Medya taranıyor: {source_info['name']}...")
        for rss in source_info.get("rss", []):
            all_new_urls.update(parse_rss(rss, existing_urls))
        for sm in source_info.get("sitemaps", []):
            all_new_urls.update(parse_sitemap(sm, existing_urls))
        if source_id in ["ekonomim", "dunya"]:
            base_site = "https://www.ekonomim.com" if source_id == "ekonomim" else "https://www.dunya.com"
            for h_url in generate_hidden_sitemaps(base_site):
                all_new_urls.update(parse_sitemap(h_url, existing_urls))

    # 3. Yeni URL'lerin Asenkron İşlenmesi
    if all_new_urls:
        print(f"\n[BİLGİ] {len(all_new_urls)} adet yeni URL asenkron olarak indiriliyor...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(analyze_article, u, d): u for u, d in all_new_urls.items()}
            for f in concurrent.futures.as_completed(futures):
                try:
                    res = f.result()
                    if res: new_news.append(res)
                except: pass

    # 4. Artımlı Güncelleme (Incremental Upsert)
    if new_news:
        df_new = pd.DataFrame(new_news)
        if not existing_df.empty:
            df_combined = pd.concat([existing_df, df_new], ignore_index=True)
        else:
            df_combined = df_new
    else:
        print("\n[BİLGİ] Yeni eklenecek içerik bulunamadı. Mevcut havuz korundu.")
        df_combined = existing_df.copy()

    if df_combined.empty:
        print("[UYARI] İşlenecek veri havuzu boş.")
        return pd.DataFrame()

    df_combined.drop_duplicates(subset=['URL'], inplace=True)
    df_combined.sort_values(by='Tarih', ascending=False, inplace=True)
    df_combined.reset_index(drop=True, inplace=True)
    
    # 5. Belirtilen Dizin Altına Parquet Olarak Kayıt
    df_combined.to_parquet(PARQUET_PATH, index=False)
    print(f"\n[BAŞARILI] Toplam {len(df_combined)} kayıt ile '{PARQUET_PATH}' veri havuzu güncellendi.")
    
    return df_combined

if __name__ == "__main__":
    df_ham_veri = run_ingestion_pipeline()