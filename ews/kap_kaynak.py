# -*- coding: utf-8 -*-
"""
kap_kaynak.py - KAP (kap.org.tr) adaptörü: kap_ingestion.py ile AYNI mekanizma, depolamasız (şirket inceleme ajanı için).

kap_ingestion.py'nin KAP katmanı (çerez, hız sınırı, liste ucu, __next_f ayrıştırması, detay sayfası) buraya
fonksiyonları değiştirilmeden alındı. Farklar yalnızca şunlar:
  1. Diske hiçbir şey yazılmaz (CSV/parquet/seen_ids/çerez dosyası yok). Çerez, istemcinin requests.Session'ında
     bellekte durur. ("Çekmek serbest, saklamak/kopyalamak yasak.")
  2. Şirket sabit değil: KAP_COMPANIES yerine hedef (BIST kodu / mkkMemberOid / unvan ipucu) verilir.
  3. İçe aktarılınca hiçbir şey çalışmaz (proxy kurma, input() yok); oturum dışarıdan verilir.
  4. Pencere ve detay sınırı çağırana bağlıdır (ajan için varsayılan 30 gün, 15 detay).

Uçlar (tarayıcıda gözlenen; kap_ingestion.py ile aynı):
  POST /tr/api/disclosure/list/main   {"fromDate","toDate","memberTypes":["IGS","DDK"]} -> o günün TÜM bildirimleri
  GET  /tr/Bildirim/{disclosureIndex} -> detay sayfası (Next.js, __next_f içinde disclosureBody)
  POST /tr/api/search/combined         {"keyword": unvan} -> şirket (memberOrFundOid, cmpOrFundCode)
"""
import json
import re
import time
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from bs4 import BeautifulSoup

KAP_TZ = ZoneInfo("Europe/Istanbul")
KAP_BASE = "https://kap.org.tr"   # tarayıcıda gözlenen alan adı (www YOK)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0 Safari/537.36",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
}

KAP_CLASS_LABELS = {
    "FR": "Finansal Rapor",
    "ODA": "Özel Durum Açıklaması",
    "DG": "Diğer Bildirim",
    "DUY": "Düzenleyici Kurum Bildirimi",
    "FON": "Fon Bildirimi",
    "CA": "Hak Kullanım",
}

KAPWEB_SLEEP = 4.0
KAPWEB_BACKOFF = 20.0
KAPWEB_RETRIES = 5
KAPWEB_RANGE_DAYS = 1                 # kap_ingestion ile aynı: gün gün (aralık büyürse kırpılabilir)
KAPWEB_MEMBER_TYPES = ["IGS", "DDK"]
KAPWEB_LIST_URL = "/tr/api/disclosure/list/main"
KAPWEB_SEARCH_URL = "/tr/api/search/combined"


# ============================================================
# Normalizasyon ve tarih (kap_ingestion.py'den aynen)
# ============================================================
def normalize_text(text):
    if text is None:
        return ""
    text = str(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_search_text(text):
    """Arama/eşleştirme normalizasyonu (İ/I -> i, casefold, NFKD, ı -> i)."""
    text = normalize_text(text)
    text = text.replace("İ", "i").replace("I", "i")
    text = text.casefold()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("ı", "i")
    return text


TR_DATE_RE = re.compile(r"^\s*\d{1,2}[./]\d{1,2}[./]\d{4}")


def parse_date(value):
    """gg.aa.yyyy dayfirst okunur; KAP saati Türkiye saatidir (KAP_TZ ile etiketlenir)."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, str) and TR_DATE_RE.match(value):
            dt = pd.to_datetime(value.strip(), errors="coerce", dayfirst=True)
        else:
            dt = pd.to_datetime(value, errors="coerce")
        if pd.isna(dt):
            return None
        dt = dt.to_pydatetime()
        return dt.replace(tzinfo=KAP_TZ) if dt.tzinfo is None else dt.astimezone(KAP_TZ)
    except Exception:
        return None


# ============================================================
# Next.js React Flight ayrıştırması (kap_ingestion.py'den aynen)
# ============================================================
NEXT_F_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
FLIGHT_ENTRY_RE = re.compile(rb'([0-9a-f]*):', re.I)
FLIGHT_TEXT_MARKER_RE = re.compile(rb'T([0-9a-f]+),', re.I)
FLIGHT_REF_RE = re.compile(r'^\$([0-9a-f]+)$')


def _parse_flight_chunk(buf, by_id):
    """"T<hex>," uzunluğu UTF-8 BAYT cinsindendir; buffer bytes olarak işlenir. Id'siz girdiler atlanır."""
    buf_bytes = buf.encode("utf-8")
    pos = 0
    n = len(buf_bytes)
    while pos < n:
        m = FLIGHT_ENTRY_RE.match(buf_bytes, pos)
        if not m:
            break
        node_id = m.group(1).decode("ascii")
        pos = m.end()
        text_m = FLIGHT_TEXT_MARKER_RE.match(buf_bytes, pos) if node_id else None
        if text_m:
            length = int(text_m.group(1), 16)
            start = text_m.end()
            by_id[node_id] = buf_bytes[start:start + length].decode("utf-8", errors="replace")
            pos = start + length
            if pos < n and buf_bytes[pos:pos + 1] == b"\n":
                pos += 1
            continue
        nl = buf_bytes.find(b"\n", pos)
        rest = (buf_bytes[pos:nl] if nl != -1 else buf_bytes[pos:]).decode("utf-8", errors="replace")
        if node_id:
            if rest.startswith(("{", "[", '"')):
                try:
                    by_id[node_id] = json.loads(rest)
                except Exception:
                    by_id[node_id] = rest
            else:
                by_id[node_id] = rest
        pos = nl + 1 if nl != -1 else n


def kapweb_next_data(html):
    """Sayfadaki bütün self.__next_f.push(...) gövdelerini birleştirip id -> değer sözlüğü döner."""
    if not html:
        return None
    pieces = []
    for m in NEXT_F_PUSH_RE.finditer(html):
        try:
            pieces.append(json.loads(m.group(1)))
        except Exception:
            continue
    by_id = {}
    _parse_flight_chunk("".join(pieces), by_id)
    return by_id or None


def kapweb_collect_records(node, out, depth=0):
    """Bilinmeyen şemalı JSON içinden disclosureIndex taşıyan kayıtları toplar."""
    if depth > 8:
        return
    if isinstance(node, dict):
        basic = node.get("disclosureBasic") if isinstance(node.get("disclosureBasic"), dict) else None
        if basic and "disclosureIndex" in basic:
            merged = dict(basic)
            for key, value in node.items():
                if key != "disclosureBasic" and isinstance(value, (str, int, float)):
                    merged.setdefault(key, value)
            out.append(merged)
            return
        if "disclosureIndex" in node:
            out.append(node)
            return
        for value in node.values():
            kapweb_collect_records(value, out, depth + 1)
    elif isinstance(node, list):
        for item in node:
            kapweb_collect_records(item, out, depth + 1)


def kapweb_find_disclosure_body_ref(node, depth=0):
    if depth > 8:
        return None
    if isinstance(node, dict):
        body = node.get("disclosureBody")
        if isinstance(body, list) and body and isinstance(body[0], str):
            return body[0]
        for value in node.values():
            found = kapweb_find_disclosure_body_ref(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = kapweb_find_disclosure_body_ref(item, depth + 1)
            if found:
                return found
    return None


def kapweb_resolve_body(by_id):
    """disclosureBody referansını ("$31" -> by_id["31"]) düz metne çevirir."""
    ref = kapweb_find_disclosure_body_ref(by_id)
    if not ref:
        return ""
    m = FLIGHT_REF_RE.match(ref)
    if not m:
        return ""
    raw = by_id.get(m.group(1))
    if not isinstance(raw, str) or not raw:
        return ""
    return normalize_text(BeautifulSoup(raw, "html.parser").get_text(" ", strip=True))


def kapweb_flat_text(record, limit=600):
    parts = []
    for key, value in record.items():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts += [str(v) for v in value if isinstance(v, (str, int))]
    return " ".join(parts)[:limit]


def deep_find_value(obj, key_candidates, max_depth=6):
    targets = [k.lower() for k in key_candidates]

    def walk(node, depth):
        if depth > max_depth:
            return None
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in targets and isinstance(value, (str, int, float)):
                    return value
            for value in node.values():
                found = walk(value, depth + 1)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = walk(item, depth + 1)
                if found is not None:
                    return found
        return None

    return walk(obj, 0)


KAP_KEYS = {
    "disclosure_id": ["disclosureIndex", "disclosureId", "id"],
    "published_at": ["time", "publishDate", "publishedDate", "date", "kapPublishDate"],
    "subject": ["subject", "title", "disclosureSubject"],
    "summary": ["summary"],
    "disclosure_class": ["disclosureClass"],
    "disclosure_type": ["disclosureType", "type"],
    "company": ["senderTitle", "companyTitle", "companyName", "kapTitle", "memberName"],
    "bist_code": ["senderExchCodes", "stockCode", "ticker", "code"],
    "related_stocks": ["relatedStocks", "relatedStockCodes"],
    "body": ["htmlMessage", "content", "body"],
}


def parse_kap_json_record(obj):
    body = normalize_text(deep_find_value(obj, KAP_KEYS["body"]))
    if body and "<" in body and ">" in body:
        body = normalize_text(BeautifulSoup(body, "html.parser").get_text(" ", strip=True))
    related_stocks = normalize_text(deep_find_value(obj, KAP_KEYS["related_stocks"]))
    bist_code_raw = normalize_text(deep_find_value(obj, KAP_KEYS["bist_code"]))
    return {
        "disclosure_id": deep_find_value(obj, KAP_KEYS["disclosure_id"]),
        "published_raw": deep_find_value(obj, KAP_KEYS["published_at"]),
        "subject": normalize_text(deep_find_value(obj, KAP_KEYS["subject"])),
        "summary_text": normalize_text(deep_find_value(obj, KAP_KEYS["summary"])),
        "disclosure_class": normalize_text(deep_find_value(obj, KAP_KEYS["disclosure_class"])),
        "disclosure_type": normalize_text(deep_find_value(obj, KAP_KEYS["disclosure_type"])),
        "sender_title": normalize_text(deep_find_value(obj, KAP_KEYS["company"])),
        "company_raw": normalize_text(deep_find_value(obj, KAP_KEYS["company"])),
        "bist_code_raw": bist_code_raw,
        "related_stocks": related_stocks or bist_code_raw,
        "body_text": body,
        "disclosure_link": normalize_text(deep_find_value(obj, ["link", "url"])),
        "fetch_method": "kapweb",
    }


# ============================================================
# Hedef şirket eşleşmesi (kapweb_match_ticker'ın tek şirketlik hali)
# ============================================================
def hedef(bist_codes=(), oid="", title_hint=""):
    return {"bist_codes": {str(c).upper() for c in bist_codes if c}, "oid": str(oid or ""),
            "title_hint": normalize_search_text(title_hint) if title_hint else ""}


def kayit_eslesir(record, h):
    """Liste kaydı hedef şirkete mi ait? Sıra: BIST kodu, mkkMemberOid, unvan ipucu (kap_ingestion ile aynı kurallar)."""
    text = kapweb_flat_text(record).upper()
    for code in h["bist_codes"]:
        if re.search(r"(?<![A-Z0-9])" + re.escape(code) + r"(?![A-Z0-9])", text):
            return True
    if h["oid"] and str(record.get("mkkMemberOid") or "") == h["oid"]:
        return True
    return bool(h["title_hint"]) and h["title_hint"] in normalize_search_text(text)


# ============================================================
# İstemci: oturum + çerez bellekte, hız sınırı kap_ingestion ile aynı
# ============================================================
class KapClient:
    def __init__(self, session=None, sleep=None):
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)
        self.sleep = KAPWEB_SLEEP if sleep is None else sleep
        self.last_call = 0.0
        self.calls = 0
        self._cookie_ok = False

    def cerez(self):
        """Ana sayfaya bir GET; çerezler oturumda (bellekte) kalır."""
        if self._cookie_ok:
            return True
        try:
            r = self.session.get(KAP_BASE + "/tr", timeout=30)
            self._cookie_ok = r.status_code == 200
        except requests.RequestException:
            self._cookie_ok = False
        return self._cookie_ok

    def istek(self, path, method="GET", payload=None, as_json=False):
        """kap_ingestion.kapweb_request ile aynı: aralık, 429 geri çekilme, durum metni. (veri|None, durum)."""
        url = KAP_BASE + path
        headers = {"Accept": "*/*", "Accept-Language": "tr", "Referer": KAP_BASE + "/tr", "Origin": KAP_BASE}
        if method == "POST":
            headers["Content-Type"] = "application/json"
        for attempt in range(KAPWEB_RETRIES):
            wait = self.sleep - (time.monotonic() - self.last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                if method == "POST":
                    response = self.session.post(url, json=payload, headers=headers, timeout=30)
                else:
                    response = self.session.get(url, headers=headers, timeout=30)
            except Exception as e:
                self.last_call = time.monotonic()
                if attempt == KAPWEB_RETRIES - 1:
                    return None, f"baglanti_hatasi: {repr(e)[:120]}"
                time.sleep(5 * (attempt + 1))
                continue
            self.last_call = time.monotonic()
            self.calls += 1
            if response.status_code == 429:
                time.sleep(KAPWEB_BACKOFF + attempt * 12)
                continue
            if response.status_code in (401, 403):
                return None, f"yetki_{response.status_code}"
            if response.status_code != 200:
                return None, f"http_{response.status_code}"
            if as_json:
                try:
                    return response.json(), "ok"
                except Exception:
                    return None, "json_degil"
            return response.text, "ok"
        return None, "429_tukendi"

    def sirket_ara(self, unvan):
        """POST /tr/api/search/combined -> [{'ad','oid','kod'}] (yalnızca şirketler)."""
        self.cerez()
        data, status = self.istek(KAPWEB_SEARCH_URL, method="POST", payload={"keyword": unvan}, as_json=True)
        if status != "ok" or not isinstance(data, list):
            return [], status
        out = []
        for grup in data:
            for r in (grup.get("results") or []) if isinstance(grup, dict) else []:
                if r.get("searchType") == "C" and r.get("memberOrFundOid"):
                    out.append({"ad": r.get("searchValue"), "oid": r.get("memberOrFundOid"),
                                "kod": str(r.get("cmpOrFundCode") or "").upper()})
        uniq = {o["oid"]: o for o in out}
        return list(uniq.values()), "ok"

    def liste_gun(self, day):
        """Bir günün TÜM bildirimleri (kap_ingestion.kapweb_list_day)."""
        stamp = day.strftime("%d.%m.%Y")
        payload = {"fromDate": stamp, "toDate": stamp, "memberTypes": KAPWEB_MEMBER_TYPES}
        data, status = self.istek(KAPWEB_LIST_URL, method="POST", payload=payload, as_json=True)
        if status != "ok":
            return [], status
        records = []
        kapweb_collect_records(data, records)
        return records, "ok"

    def detay(self, disclosure_index, hint_code=""):
        """/tr/Bildirim/{ix} (kap_ingestion.kapweb_detail; __next_f önce, HTML yedek)."""
        html, status = self.istek(f"/tr/Bildirim/{disclosure_index}")
        if status != "ok" or not html:
            return None
        data = kapweb_next_data(html)
        if data:
            found = []
            kapweb_collect_records(data, found)
            if found:
                rec = parse_kap_json_record(found[0])
                rec["disclosure_id"] = str(disclosure_index)
                rec["disclosure_link"] = f"{KAP_BASE}/tr/Bildirim/{disclosure_index}"
                rec["fetch_method"] = "kapweb_next"
                rec["bist_code_raw"] = rec.get("bist_code_raw") or hint_code
                body = kapweb_resolve_body(data)
                if body:
                    rec["body_text"] = body
                if rec.get("subject") and rec.get("published_raw"):
                    return rec
        soup = BeautifulSoup(html, "html.parser")
        visible = normalize_text(soup.get_text(" ", strip=True))
        if len(visible) < 200:
            return None
        return {"disclosure_id": str(disclosure_index), "published_raw": None, "subject": "", "summary_text": "",
                "disclosure_class": "", "body_text": visible[:20000], "bist_code_raw": hint_code,
                "disclosure_link": f"{KAP_BASE}/tr/Bildirim/{disclosure_index}", "fetch_method": "kapweb_html"}

    def bildirimler(self, h, gun=30, detay_maks=15, bugun=None, on_progress=None):
        """Son `gun` günü gün gün tarar, hedefin bildirimlerini bellekte süzer, en önemli `detay_maks` tanesinin
        metnini çeker. (kayıtlar, durum) döner; hiçbir şey diske yazılmaz."""
        self.cerez()
        today = bugun or datetime.now(KAP_TZ)
        day, end = today, today - timedelta(days=int(gun))
        bulunan, taranan, empty_streak, durum = [], 0, 0, "ok"
        while day >= end:
            records, status = self.liste_gun(day)
            if status.startswith("yetki") or status == "429_tukendi":
                durum = status
                break
            taranan += len(records)
            for rec in records:
                if kayit_eslesir(rec, h):
                    bulunan.append(rec)
            empty_streak = 0 if records else empty_streak + 1
            if on_progress:
                on_progress(day, len(records), len(bulunan))
            if empty_streak >= 7 and taranan == 0:
                durum = "bos_liste (çerez/erişim)"
                break
            day -= timedelta(days=KAPWEB_RANGE_DAYS)
        oncelik = {"ODA": 0, "FR": 1}
        bulunan.sort(key=lambda r: (oncelik.get(str(r.get("disclosureClass") or ""), 2),
                                    -(parse_date(r.get("publishDate")) or today).timestamp()))
        hint = next(iter(h["bist_codes"]), "")
        out = []
        for i, rec in enumerate(bulunan):
            temel = parse_kap_json_record(rec)
            # kap_ingestion ile aynı: bildirim numarası doğrudan disclosureIndex'ten alınır (deep_find, kayıttaki
            # disclosureId'yi -uzun kimlik- önce bulabiliyor; o numarayla /tr/Bildirim sayfası açılmaz).
            ix = str(rec.get("disclosureIndex") or "").strip()
            temel["disclosure_id"] = ix
            temel["disclosure_link"] = f"{KAP_BASE}/tr/Bildirim/{ix}" if ix else ""
            if i < detay_maks and ix:
                d = self.detay(ix, hint_code=hint)
                if d and d.get("body_text"):
                    temel["body_text"] = d["body_text"]
            temel["published_at"] = parse_date(temel.get("published_raw"))
            temel["class_label"] = KAP_CLASS_LABELS.get(temel.get("disclosure_class") or "", "")
            out.append(temel)
        out.sort(key=lambda r: r["published_at"] or today, reverse=True)
        return out, f"{durum}; taranan {taranan} bildirim, şirkete ait {len(out)}"
