# -*- coding: utf-8 -*-
"""
ews/inceleme.py - Şirket inceleme ajanı (tek şirket, istek üzerine)

Analist bir şirketi incelemek istediğinde çalışır (6_sirket_inceleme.py ya da baslat.py -> 6). LLM, araçları
kendisi seçip çağırır (tool calling), sonuçları kaynak numaralarıyla toplar ve kısa bir kredi izleme notu yazar.
Ajan seviye ya da skor VERMEZ (stage EWS kurallarıyla belirlenir); kanıtı toplar, çelişkileri ve eksik bilgiyi
gösterir.

Araçlar:
  ews_ozet, ews_haberler, ortaklar : EWS çıktıları (data/llm_dogrulama.xlsx, data/portfolio_config.py)
  ilan_ara                         : ilan.gov.tr (tüm ilan türleri; şirket adı ya da VKN ile)
  kap_ara                          : KAP bildirimleri (ews/kap_kaynak.py; kap_ingestion.py ile aynı mekanizma)
  google_haber                     : Google News RSS (başlık, kaynak, tarih, bağlantı)
  dwh_sorgu                        : Oracle, yalnızca ayarlar.INCELEME_SORGULARI'ndaki şablonlar (:cid bind)

Kurallar:
  - Çekmek serbest, saklamak yasak: dış kaynak sonuçları diske yazılmaz; yalnızca not kaydedilir.
  - Dış kaynaklara yalnızca kamuya açık kimlik gider (şirketin kısa adı, ortak adı, VKN). TCKN/bakiye gitmez.
  - Her kayıt bir kaynak numarası taşır ([K1], [K2] ...); not yalnızca bunlara atıf yapabilir. not_denetimi()
    uydurma numarayı, kaynakta geçmeyen alıntıyı ve kaynaksız maddeyi işaretler; ajan bir kez düzeltmeyi dener.
"""
from __future__ import annotations

import ast
import datetime as dt
import html
import json
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pandas as pd
import requests

import ayarlar
from ews import kap_kaynak
from ews.llm import resolve_ssl_verify

# =============================== AYARLAR ===============================
HABER_GUN = 180            # ews_haberler varsayılan penceresi
HABER_MAKS = 25
ILAN_GUN = 365
ILAN_MAKS_SAYFA = 3        # ilan.gov.tr sayfa başına 20 ilan döndürüyor
ILAN_DETAY_MAKS = 6        # metni okunacak en fazla ilan
KAP_GUN = getattr(ayarlar, "INCELEME_KAP_GUN", 30)     # gün gün, 4 sn aralık: 30 gün ~2 dk
KAP_DETAY = 15             # metni okunacak en fazla KAP bildirimi (önce ODA, sonra FR)
GOOGLE_GUN = 90
GOOGLE_MAKS = 25
DIS_BEKLEME = 1.0          # dış sitelere art arda istekler arasında saniye
# DWH sorgu şablonları (kuruma özel): {"ad": {"aciklama": "...", "sql": "SELECT ... WHERE ... = :cid"}}.
# :cid incelenen şirketin müşteri numarasıyla bind edilir; ajan SQL yazamaz. Boşsa banka verisi bölümü boş kalır.
INCELEME_SORGULARI = getattr(ayarlar, "INCELEME_SORGULARI", {}) or {}
DWH_MAKS_SATIR = 60
LLM_MAKS_TOKEN = 4096      # not için (platform sınırı); EWS'in soru başına 1024'ünden ayrı
LLM_SICAKLIK = 0.1
MAKS_ADIM = 20             # bir inceleme için en fazla LLM isteği
ARAC_CIKTI_KARAKTER = 9000 # araç sonucunun modele giden en fazla karakteri
# ======================================================================

ILGILI = ("odak", "ikincil")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
ILAN_BASE = "https://www.ilan.gov.tr"
ILAN_SEARCH = f"{ILAN_BASE}/api/api/services/app/Ad/AdsByFilter"
ILAN_DETAIL = f"{ILAN_BASE}/api/api/services/app/AdDetail/GetAdDetail"
ILAN_HEADERS = {"accept": "text/plain, application/json", "content-type": "application/json-patch+json",
                "origin": ILAN_BASE, "referer": f"{ILAN_BASE}/ilan/tum-ilanlar",
                "x-request-origin": "IGT-UI", "x-requested-with": "XMLHttpRequest"}
TCKN_RE = re.compile(r"(?<!\d)\d{11}(?!\d)")           # 11 haneli sayılar (TCKN, telefon) maskelenir
HUKUKI_EK_RE = re.compile(r"\b(A\.?\s?Ş\.?|ANONİM\s+ŞİRKETİ|ANONİM\s+ORTAKLIĞI|LTD\.?\s*ŞTİ\.?|LİMİTED\s+ŞİRKETİ|"
                          r"T\.?\s?A\.?\s?Ş\.?|A\.?\s?O\.?)\s*$", re.I)
_FOLD = str.maketrans({"İ": "i", "I": "i", "ı": "i", "Ş": "s", "ş": "s", "Ğ": "g", "ğ": "g",
                       "Ü": "u", "ü": "u", "Ö": "o", "ö": "o", "Ç": "c", "ç": "c"})
_YASAK_SQL = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|EXECUTE|BEGIN|"
                        r"DECLARE|CALL|COMMIT|ROLLBACK|LOCK)\b", re.I)


def fold(text) -> str:
    return "" if text is None else str(text).translate(_FOLD).lower()


def kisa(s, n) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def maskele(text) -> str:
    return TCKN_RE.sub("***", text or "")


def _tarih(v):
    t = pd.to_datetime(v, errors="coerce", dayfirst=isinstance(v, str) and bool(re.match(r"^\d{1,2}[./]", v)))
    return None if pd.isna(t) else t.tz_localize(None) if getattr(t, "tzinfo", None) else t


def _gun(v) -> str:
    t = _tarih(v)
    return t.strftime("%Y-%m-%d") if t is not None else str(v or "")[:10]


def sadece_select(sql: str) -> str:
    """Yalnızca tek bir SELECT/WITH sorgusuna izin verir (asıl koruma: kullanıcının salt okunur olması)."""
    s = (sql or "").strip().rstrip(";").strip()
    if not re.match(r"(?is)^(select|with)\b", s):
        raise ValueError("Sadece SELECT veya WITH ile başlayan sorgular çalıştırılabilir.")
    temiz = re.sub(r"'(?:''|[^'])*'", "''", re.sub(r"--[^\n]*|/\*.*?\*/", " ", s, flags=re.S))
    if ";" in temiz:
        raise ValueError("Tek seferde tek sorgu çalıştırılabilir.")
    m = _YASAK_SQL.search(temiz)
    if m:
        raise ValueError(f"Sorguda izin verilmeyen kelime var: {m.group(0)}")
    return s


def cekirdek_ad(unvan, aliases=()) -> str:
    """Dış arama için şirketin kısa adı: 2-4 kelimelik en uzun alias, yoksa hukuki eki atılmış unvan."""
    adaylar = [a for a in aliases if 2 <= len(str(a).split()) <= 4 and not HUKUKI_EK_RE.search(str(a))]
    if adaylar:
        return max(adaylar, key=len)
    ad = str(unvan or "").strip()
    for _ in range(3):
        ad = HUKUKI_EK_RE.sub("", ad).strip(" .,")
    return ad


def dis_oturum(interactive: bool = True) -> requests.Session:
    """Dış siteler için HTTP oturumu; proxy scraper'la aynı (ayarlar.get_proxy_url, şifre çalışırken sorulur)."""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "tr-TR,tr;q=0.9"})
    proxy = ayarlar.get_proxy_url(interactive=interactive)
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
        s.verify = False   # kurumsal SSL incelemesi (scraper ve KAP kodundaki uygulama)
    return s


# ------------------------------- EWS verisi -------------------------------
class EwsVerisi:
    """EWS çıktılarını (Excel ve portföy config'i) bir kez okur; salt okuma."""

    def __init__(self, excel_path=None, config_path=None):
        self.excel = Path(excel_path or ayarlar.EXCEL_OUTPUT_PATH)
        self.config = Path(config_path or ayarlar.PORTFOLIO_CONFIG_PATH)
        self._sheets: Dict[str, pd.DataFrame] = {}
        self._companies: Optional[dict] = None

    def sheet(self, name):
        if name not in self._sheets:
            if not self.excel.exists():
                raise FileNotFoundError(f"EWS çıktısı yok: {self.excel} (önce 3_llm_dogrulama.py çalıştırılmalı)")
            self._sheets[name] = pd.read_excel(self.excel, sheet_name=name)
        return self._sheets[name]

    def companies(self) -> dict:
        if self._companies is None:
            self._companies = {}
            if self.config.exists():
                src = self.config.read_text(encoding="utf-8")
                m = re.search(r"COMPANIES\s*=\s*", src)
                if m:
                    body = src[m.end():]
                    end = body.find("\n# fmt: on")
                    self._companies = ast.literal_eval(body[:end] if end != -1 else body)
        return self._companies


def _sirket_satiri(df, sirket):
    s = str(sirket).strip()
    ids = pd.to_numeric(df["Şirket"], errors="coerce")
    if s.isdigit():
        return df[ids == int(s)]
    return df[df["Şirket_Adi"].astype(str).map(fold).str.contains(re.escape(fold(s)), na=False)]


# ------------------------------- Araçlar -------------------------------
class Araclar:
    """İnceleme ajanının araçları; her kayıt bir kaynak numarası alır."""

    def __init__(self, sirket, ews: Optional[EwsVerisi] = None, http=None, conn=None):
        self.sirket = sirket
        self.ews = ews or EwsVerisi()
        self.http = http if http is not None else dis_oturum()
        self.conn = conn            # Oracle; dwh_sorgu ilk çağrıldığında açılır
        self.kaynaklar: Dict[str, dict] = {}
        self.denetim: List[str] = []
        self._son_istek = 0.0
        self._kap = None

    def _bekle(self):
        gecen = time.monotonic() - self._son_istek
        if gecen < DIS_BEKLEME:
            time.sleep(DIS_BEKLEME - gecen)
        self._son_istek = time.monotonic()

    def kaynak(self, tur, baslik, tarih="", url="", metin="") -> str:
        kid = f"K{len(self.kaynaklar) + 1}"
        self.kaynaklar[kid] = {"tur": tur, "baslik": str(baslik or "")[:300], "tarih": str(tarih or "")[:10],
                               "url": str(url or ""), "metin": str(metin or "")}
        return kid

    def _yok(self, tur, mesaj) -> str:
        """Sonuç yok / erişilemedi bulgusu da kaynak numarası alır; not bu bulguya da atıf yapabilir."""
        k = self.kaynak(tur, mesaj, dt.date.today().isoformat(), "", mesaj)
        return f"[{k}] {mesaj}."

    def _bul(self, sirket):
        sirket = sirket if sirket not in (None, "") else self.sirket
        rows = _sirket_satiri(self.ews.sheet("Birlesik"), sirket)
        if rows.empty:
            raise ValueError(f"'{sirket}' EWS portföyünde bulunamadı. Müşteri numarası ya da unvan dene.")
        if len(rows) > 1:
            adlar = "; ".join(f"{r['Şirket']} {r['Şirket_Adi']}" for _, r in rows.head(8).iterrows())
            raise ValueError(f"Birden çok şirket eşleşti, numarayla tekrar çağır: {adlar}")
        return rows.iloc[0]

    def _arama_adi(self, sirket, sorgu):
        if sorgu:
            return str(sorgu).strip()
        r = self._bul(sirket)
        comp = self.ews.companies().get(int(r["Şirket"])) or {}
        return cekirdek_ad(r["Şirket_Adi"], comp.get("aliases") or ())

    # ---- EWS ----
    def ews_ozet(self, sirket=""):
        r = self._bul(sirket)
        sid = int(r["Şirket"])
        alan = ["Birlesik_Seviye", "Birlesik_Skor", "Birlesik_Kural", "Risk_Matrisi", "Haber_Sinyali", "Haber_Skoru",
                "Finansal_Seviye", "Finansal_Skor", "Finansal_Veri_Kapsami", "Finansal_Tarih", "DSCR_Degeri",
                "Toplam_Bakiye_TRY", "Finansal_Puan_Detay", "Not"]
        ozet = "; ".join(f"{a}={r[a]}" for a in alan if a in r.index and pd.notna(r[a]) and str(r[a]) != "")
        k = self.kaynak("EWS", f"EWS Birlesik satırı: {r['Şirket_Adi']}", metin=ozet)
        out = [f"[{k}] {r['Şirket_Adi']} (no {sid}) — EWS özeti: {ozet}"]
        try:
            defter = self.ews.sheet("Olay_Defteri")
            olaylar = defter[pd.to_numeric(defter["Şirket"], errors="coerce") == sid]
        except (ValueError, KeyError):
            olaylar = pd.DataFrame()
        if olaylar.empty:
            out.append("Olay defterinde kayıt yok.")
        for _, o in olaylar.iterrows():
            metin = " | ".join(str(o.get(c, "")) for c in ("Tanim", "Zaman_Cizelgesi", "Kapanis_Alinti") if pd.notna(o.get(c)))
            k = self.kaynak("EWS-olay", f"{o.get('Olay')} {o.get('Olay_Tipi')} ({o.get('Seviye')}, {o.get('Durum')})",
                            _gun(o.get("Son_Haber") or o.get("Acilis")), o.get("Ilk_Haber_URL") or "", metin)
            out.append(f"[{k}] Olay {o.get('Olay')}: {o.get('Olay_Tipi')} · {o.get('Seviye')} · {o.get('Durum')} · "
                       f"açılış {_gun(o.get('Acilis'))} · {o.get('Haber_Sayisi')} haber · etki: {o.get('Sinyale_Etkisi')} "
                       f"· {kisa(o.get('Tanim'), 300)}")
        return "\n".join(out)

    def ews_haberler(self, sirket="", gun=HABER_GUN, sadece_ilgili=True):
        r = self._bul(sirket)
        sid = int(r["Şirket"])
        kay = self.ews.sheet("Kayıtlar")
        kay = kay[pd.to_numeric(kay["Şirket"], errors="coerce") == sid].copy()
        if sadece_ilgili and "LLM_Alaka" in kay:
            kay = kay[kay["LLM_Alaka"].isin(ILGILI)]
        kay["_t"] = kay["Tarih"].map(_tarih)
        sinir = pd.Timestamp.now() - pd.Timedelta(days=int(gun))
        kay = kay[kay["_t"].isna() | (kay["_t"] >= sinir)].sort_values("_t", ascending=False)
        if kay.empty:
            return self._yok("EWS", f"{r['Şirket_Adi']} için son {gun} günde {'ilgili ' if sadece_ilgili else ''}"
                                    f"EWS haberi yok")
        out = [f"{r['Şirket_Adi']}: son {gun} günde {len(kay)} haber (en yeni {min(len(kay), HABER_MAKS)} tanesi):"]
        for _, h in kay.head(HABER_MAKS).iterrows():
            alinti = h.get("LLM_Alinti") if pd.notna(h.get("LLM_Alinti")) else ""
            metin = " ".join(str(h.get(c)) for c in ("Başlık", "LLM_Ozet", "LLM_Alinti", "Metin_Snippet") if pd.notna(h.get(c)))
            k = self.kaynak("haber", h.get("Başlık"), _gun(h.get("Tarih")), h.get("URL") or "", metin)
            ortak = f" · ortak üzerinden: {h.get('Ortak')}" if pd.notna(h.get("Ortak")) and str(h.get("Ortak")) else ""
            out.append(f"[{k}] {_gun(h.get('Tarih'))} · {h.get('Kaynak')} · {h.get('Başlık')} · olay: {h.get('LLM_Olay')} "
                       f"({h.get('LLM_Durum')}){ortak}" + (f" · alıntı: \"{kisa(alinti, 250)}\"" if alinti else ""))
        return "\n".join(out)

    def ortaklar(self, sirket=""):
        r = self._bul(sirket)
        p = (self.ews.companies().get(int(r["Şirket"])) or {}).get("partners") or []
        if not p:
            return f"{r['Şirket_Adi']}: portföy config'inde %50'den büyük hakim ortak kaydı yok."
        return f"{r['Şirket_Adi']} hakim ortakları: " + "; ".join(f"{x.get('name')} (%{x.get('share')})" for x in p)

    # ---- ilan.gov.tr ----
    def ilan_ara(self, sirket="", sorgu="", vkn="", gun=ILAN_GUN):
        ad = self._arama_adi(sirket, sorgu)
        terimler = [t for t in (str(vkn).strip(), ad) if t]
        ppdmin = (dt.date.today() - dt.timedelta(days=int(gun))).strftime("%d.%m.%Y")
        ilanlar, goruldu, toplam = [], set(), {}
        for terim in terimler:
            for sayfa in range(ILAN_MAKS_SAYFA):
                self._bekle()
                body = {"keys": {"q": [terim], "ppdmin": [ppdmin]}, "skipCount": sayfa * 20, "maxResultCount": 20}
                if sayfa:
                    body["keys"]["currentPage"] = [sayfa + 1]
                res = self.http.post(ILAN_SEARCH, json=body, headers=ILAN_HEADERS, timeout=25)
                if res.status_code != 200:
                    return self._yok("ilan", f"ilan.gov.tr HTTP {res.status_code} döndü ('{terim}'); erişim/proxy kontrol edilmeli")
                sonuc = res.json().get("result") or {}
                toplam[terim] = sonuc.get("numFound")
                ads = sonuc.get("ads") or []
                for a in ads:
                    if str(a.get("id")) not in goruldu:
                        goruldu.add(str(a.get("id")))
                        ilanlar.append(a)
                if len(ads) < 20:
                    break
        if not ilanlar:
            return self._yok("ilan", f"ilan.gov.tr: '{' / '.join(terimler)}' için son {gun} günde ilan yok")
        out = [f"ilan.gov.tr: '{' / '.join(terimler)}' için {len(ilanlar)} ilan (son {gun} gün; bulunan: {toplam}). "
               f"Ad benzerliği nedeniyle başka taraflara ait ilanlar olabilir; unvanı kontrol et."]
        for i, a in enumerate(ilanlar):
            kategori = ", ".join(c.get("name", "") for c in (a.get("categories") or []) if isinstance(c, dict))
            metin = ""
            if i < ILAN_DETAY_MAKS:
                self._bekle()
                d = self.http.get(ILAN_DETAIL, params={"id": a.get("id")}, headers=ILAN_HEADERS, timeout=20)
                if d.status_code == 200:
                    icerik = str((d.json().get("result") or {}).get("content") or "")
                    metin = maskele(re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", icerik))).strip())
            url = ILAN_BASE + str(a.get("urlStr") or f"/ilan/{a.get('id')}")
            k = self.kaynak("ilan", a.get("title"), _gun(a.get("publishStartDate")), url, f"{a.get('title')} {metin}")
            out.append(f"[{k}] {_gun(a.get('publishStartDate'))} · {kategori or '-'} · {a.get('advertiserName') or ''} · "
                       f"{a.get('title')}" + (f" · metin: {kisa(metin, 500)}" if metin else ""))
        return "\n".join(out)

    # ---- KAP ----
    def _kap_istemci(self):
        if self._kap is None:
            s = requests.Session()               # KAP'ın kendi oturumu; proxy/SSL ayarı dış oturumla aynı
            s.proxies.update(getattr(self.http, "proxies", {}) or {})
            s.verify = getattr(self.http, "verify", True)
            self._kap = kap_kaynak.KapClient(s)
        return self._kap

    def kap_ara(self, sirket="", sorgu="", gun=KAP_GUN):
        r = self._bul(sirket)
        comp = self.ews.companies().get(int(r["Şirket"])) or {}
        ad = str(sorgu).strip() or cekirdek_ad(r["Şirket_Adi"], comp.get("aliases") or ())
        kodlar = [str(t).upper() for t in (comp.get("tickers") or []) if t]
        kap = self._kap_istemci()
        adaylar, durum = kap.sirket_ara(ad)
        if durum != "ok":
            return self._yok("KAP", f"KAP şirket araması başarısız ({durum}); erişim/proxy kontrol edilmeli")
        fa = fold(ad)
        secilen = next((a for a in adaylar if fold(a["ad"]).startswith(fa)), None) or \
            next((a for a in adaylar if fa in fold(a["ad"])), None)
        if secilen is None and not kodlar:
            liste = "; ".join(a["ad"] for a in adaylar[:5]) or "yok"
            return self._yok("KAP", f"KAP'ta '{ad}' adlı işlem gören şirket bulunamadı (halka açık olmayabilir); "
                                    f"adaylar: {liste}")
        h = kap_kaynak.hedef(kodlar + ([secilen["kod"]] if secilen and secilen["kod"] else []),
                             secilen["oid"] if secilen else "", secilen["ad"] if secilen else "")
        kayitlar, ozet = kap.bildirimler(h, gun=int(gun), detay_maks=KAP_DETAY)
        kim = f"{secilen['ad']} ({secilen['kod']})" if secilen else ", ".join(kodlar)
        if not kayitlar:
            return self._yok("KAP", f"KAP: {kim} için son {gun} günde bildirim yok ({ozet})")
        out = [f"KAP: {kim} — son {gun} günde {len(kayitlar)} bildirim ({ozet}). Metni okunanlar en fazla {KAP_DETAY}."]
        for b in kayitlar:
            tarih = b["published_at"].strftime("%Y-%m-%d") if b["published_at"] else ""
            metin = maskele(b.get("body_text") or b.get("summary_text") or "")
            baslik = b.get("subject") or b.get("summary_text") or "KAP bildirimi"
            k = self.kaynak("KAP", baslik, tarih, b.get("disclosure_link"), f"{baslik} {b.get('summary_text') or ''} {metin}")
            out.append(f"[{k}] {tarih} · {b.get('class_label') or b.get('disclosure_class') or '-'} · {baslik}"
                       + (f" · özet: {kisa(b.get('summary_text'), 200)}" if b.get("summary_text") else "")
                       + (f" · metin: {kisa(metin, 600)}" if metin and metin != b.get("summary_text") else ""))
        return "\n".join(out)

    # ---- Google News ----
    def google_haber(self, sirket="", sorgu="", gun=GOOGLE_GUN):
        ad = self._arama_adi(sirket, sorgu)
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": f"\"{ad}\" when:{int(gun)}d", "hl": "tr", "gl": "TR", "ceid": "TR:tr"})
        self._bekle()
        res = self.http.get(url, timeout=25)
        if res.status_code != 200:
            return self._yok("google", f"Google News HTTP {res.status_code} döndü; erişim/proxy kontrol edilmeli")
        items = ET.fromstring(res.content).findall("./channel/item")
        if not items:
            return self._yok("google", f"Google News: '{ad}' için son {gun} günde haber yok")
        out = [f"Google News: '{ad}' için {len(items)} haber (en yeni {min(len(items), GOOGLE_MAKS)}; yalnızca başlık). "
               f"Spor kulübü, sponsorluk ve reklam haberleri gürültüdür."]
        for it in items[:GOOGLE_MAKS]:
            baslik = html.unescape(it.findtext("title") or "")
            tarih = _gun(it.findtext("pubDate"))
            k = self.kaynak("google", baslik, tarih, it.findtext("link") or "", baslik)
            out.append(f"[{k}] {tarih} · {it.findtext('source') or ''} · {baslik}")
        return "\n".join(out)

    # ---- DWH (yalnızca onaylı şablonlar) ----
    def dwh_sorgu(self, ad="", sirket=""):
        if not INCELEME_SORGULARI:
            return self._yok("DWH", "DWH sorgu şablonu tanımlı değil (ayarlar.INCELEME_SORGULARI); banka verisi incelenmedi")
        if ad not in INCELEME_SORGULARI:
            return "Şablonlar: " + "; ".join(f"{k}: {v.get('aciklama', '')}" for k, v in INCELEME_SORGULARI.items())
        if self.conn is None:
            self.conn = _oracle_baglan()
            if self.conn is None:
                return self._yok("DWH", "Oracle bağlantısı kurulamadı; banka verisi alınamadı")
        cid = int(self._bul(sirket)["Şirket"])
        t = INCELEME_SORGULARI[ad]
        with self.conn.cursor() as cur:
            cur.execute(sadece_select(t["sql"]), {"cid": cid})
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(DWH_MAKS_SATIR + 1)
        df = pd.DataFrame(rows[:DWH_MAKS_SATIR], columns=cols)
        if df.empty:
            return self._yok("DWH", f"DWH '{ad}': kayıt yok")
        tablo = df.to_string(index=False, max_colwidth=40)
        k = self.kaynak("DWH", f"{ad}: {t.get('aciklama', '')}", dt.date.today().isoformat(), "", tablo)
        ek = f" (ilk {DWH_MAKS_SATIR} satır)" if len(rows) > DWH_MAKS_SATIR else ""
        return f"[{k}] DWH '{ad}' — {t.get('aciklama', '')}{ek}:\n{tablo}"


def _oracle_baglan():
    """Oracle bağlantısı (2_sql_to_parquet ile aynı ayarlar; şifre çalışırken sorulur, kaydedilmez)."""
    try:
        import oracledb
        sifre = ayarlar.get_oracle_password(interactive=True)
        if not sifre or "<ORACLE_HOST>" in ayarlar.ORACLE_HOST:
            return None
        dsn = oracledb.makedsn(ayarlar.ORACLE_HOST, ayarlar.ORACLE_PORT, service_name=ayarlar.ORACLE_SERVICE)
        conn = oracledb.connect(user=ayarlar.ORACLE_USER, password=sifre, dsn=dsn)
        conn.call_timeout = 180_000
        return conn
    except Exception as exc:
        print(f"[UYARI] Oracle'a bağlanılamadı: {str(exc)[:200]}")
        return None


# ------------------------------- Araç tanımları (tool calling) -------------------------------
def _fn(name, desc, props, required=()):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": list(required)}}}


S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
_SIRKET = {"sirket": {"type": "string", "description": "müşteri numarası ya da unvan; boşsa incelenen şirket"}}
ARACLAR = [
    _fn("ews_ozet", "EWS özeti: birleşik/haber/finansal seviye ve skor, risk matrisi, olay defteri. İlk adım budur.",
        dict(_SIRKET)),
    _fn("ews_haberler", "EWS haber havuzunda şirketle eşleşmiş haberler (tarih, kaynak, başlık, olay tipi, alıntı).",
        dict(_SIRKET, gun=I, sadece_ilgili=B)),
    _fn("ortaklar", "Şirketin %50'den büyük hakim ortakları.", dict(_SIRKET)),
    _fn("ilan_ara", "ilan.gov.tr'de ilanlar (tebligat: konkordato/iflas; icra satışları; ihaleler). sorgu boşsa "
        "şirketin kısa adı; ortak için ortağın adını sorgu olarak ver.", dict(_SIRKET, sorgu=S, vkn=S, gun=I)),
    _fn("kap_ara", "KAP'ta şirketin bildirimleri (özel durum açıklamaları, finansal raporlar). Yalnızca borsada işlem "
        "gören şirketler; 30 gün ~2-3 dakika sürer. sorgu boşsa şirketin kısa adı.", dict(_SIRKET, sorgu=S, gun=I)),
    _fn("google_haber", "Google News'te son haber başlıkları (EWS havuzunda olmayanlar için). sorgu boşsa şirketin "
        "kısa adı; ortak için ortağın adını ver.", dict(_SIRKET, sorgu=S, gun=I)),
    _fn("dwh_sorgu", "Banka verisi için ONAYLI sorgu şablonunu çalıştırır; ad boşsa şablon listesini döner. SQL "
        "yazamazsın, yalnızca şablon adı verirsin.", dict(_SIRKET, ad=S)),
    _fn("cevap_ver", "Not bitince çağrılır. 'cevap' alanına notun tamamı (şablondaki bölümlerle).", {"cevap": S}, ["cevap"]),
]

SISTEM = """Sen bir bankanın kredi riski izleme ekibinde çalışan bir analistsin. Görevin TEK bir şirket için
kaynakları toplayıp kısa bir kredi izleme notu yazmak. Bugünün tarihi: {today}.

ÇALIŞMA YÖNTEMİ
1. ews_ozet ile başla. Sonra ews_haberler, ortaklar, ilan_ara, kap_ara ve google_haber'i çağır. dwh_sorgu'yu ad
   vermeden çağırıp şablonları gör ve her şablonu bir kez çalıştır (banka verisi rakamları yalnızca buradan gelir).
   Hakim ortak varsa ilan_ara ve google_haber'i ortağın adıyla da çağır.
2. Araç sonuçlarındaki her kayıt bir kaynak numarası taşır ([K1], [K2] ...). Nottaki HER bilgi cümlesinin sonuna
   dayandığı kaynak numarasını yaz. Araç çıktısında olmayan bir bilgiyi, rakamı ya da kaynağı YAZMA; kendi bilgini ekleme.
3. Alıntı yapacaksan kaynaktaki ifadeyi kelimesi kelimesine ve tırnak içinde ver.
4. Seviye, stage ya da skor VERME ve değiştirme; EWS'in seviyesini olduğu gibi aktar. Görevin kanıtı özetlemek.
5. Ad benzerliğine dikkat: ilan ve Google sonuçlarında başka bir şirkete, spor kulübüne, sponsorluğa, reklama ya da
   kampanyaya ait kayıtları notta kullanma (ör. "Türk Hava Yolları 0-3 Fenerbahçe" bir voleybol maçıdır).
6. Bir araç hata verirse ya da sonuç yoksa bunu 5. bölümde, aracın verdiği kaynak numarasıyla yaz; tahmin yürütme.
7. Bitince cevap_ver'i çağır. 'cevap' alanına notun tamamını yaz (en fazla ~500 kelime).

NOT ŞABLONU
<Şirket unvanı> — Kredi İzleme Notu ({today}, ajan taslağı)
1. Özet (3-5 madde)
2. Olaylar ve haberler
3. Resmi kayıtlar (ilan.gov.tr, KAP)
4. Banka verisi ve EWS seviyesi (dwh_sorgu ve EWS özetinden; rakamları kaynaktaki gibi yaz)
5. Çelişkiler ve eksik bilgi
6. Analiste sorular / önerilen kontroller"""


# ------------------------------- LLM (tool calling) -------------------------------
_LLM = {"url": None, "key": None, "verify": None, "session": None}


def llm_chat(messages, tools=None, no_think=False) -> dict:
    """Bir LLM isteği (OpenAI uyumlu, araç çağırmalı); mesaj sözlüğü döner. Geçici hatalarda bekleyip tekrar dener."""
    if _LLM["url"] is None:
        _LLM["url"] = ayarlar.get_llm_url(interactive=True)
        _LLM["key"] = ayarlar.LLM_API_KEY or ayarlar.get_llm_api_key(interactive=True)
        _LLM["verify"] = resolve_ssl_verify(ayarlar.LLM_VERIFY_SSL)
        _LLM["session"] = requests.Session()
    msgs = messages + ([{"role": "user", "content": "Kısa ve öz devam et. /no_think"}] if no_think else [])
    payload = {"messages": msgs, "temperature": LLM_SICAKLIK, "max_tokens": LLM_MAKS_TOKEN, "stream": False}
    if ayarlar.LLM_MODEL:
        payload["model"] = ayarlar.LLM_MODEL
    if tools:
        payload["tools"], payload["tool_choice"] = tools, "auto"
    key = re.sub(r"^bearer\s+", "", str(_LLM["key"] or "").strip(), flags=re.I)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    son = ""
    for deneme in range(5):
        try:
            r = _LLM["session"].post(_LLM["url"], json=payload, headers=headers, timeout=ayarlar.LLM_TIMEOUT,
                                     verify=_LLM["verify"])
        except requests.RequestException as exc:
            son = f"{type(exc).__name__}: {str(exc)[:200]}"
            time.sleep(10 * (deneme + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            son = f"HTTP {r.status_code}: {r.text[:200]}"
            ra = r.headers.get("Retry-After", "")
            time.sleep(int(ra) if ra.isdigit() else 20 * (deneme + 1))
            continue
        if r.status_code in (401, 403):
            raise RuntimeError(f"LLM anahtarı reddedildi (HTTP {r.status_code}).")
        if not r.ok:
            raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:400]}")
        choice = (r.json().get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        msg["_finish"] = choice.get("finish_reason")
        return msg
    raise RuntimeError(f"LLM'e ulaşılamadı: {son}")


def _temizle(text) -> str:
    return re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.S).strip()


class Oturum:
    def __init__(self, sirket):
        self.sirket = sirket
        self.mesajlar = [{"role": "system", "content": SISTEM.format(today=dt.date.today().isoformat())},
                         {"role": "user", "content": f"İncelenecek şirket: {sirket}. Kaynakları topla ve notu yaz."}]
        self.adimlar: List[dict] = []
        self.not_metni = ""
        self.durum, self.hata, self.n_llm = "yeni", None, 0
        self.basla = time.time()


def calistir(oturum: Oturum, araclar: Araclar, on_step: Optional[Callable[[dict], None]] = None) -> str:
    """Ajan döngüsü: LLM araç çağırır, araçlar çalışır, cevap_ver ile biter. 'bitti' | 'hata' döner."""
    oturum.durum = "calisiyor"
    try:
        while oturum.n_llm < MAKS_ADIM:
            msg = llm_chat(oturum.mesajlar, ARACLAR)
            oturum.n_llm += 1
            if msg.get("_finish") == "length":   # çıktı sınırı: düşünme kısmı yeri bitirmiş olabilir
                msg = llm_chat(oturum.mesajlar, ARACLAR, no_think=True)
                oturum.n_llm += 1
            icerik = _temizle(msg.get("content"))
            cagrilar = []
            for i, c in enumerate(msg.get("tool_calls") or []):
                fn = c.get("function", {})
                ham = fn.get("arguments") or "{}"
                cagrilar.append({"id": c.get("id") or f"c{oturum.n_llm}_{i}", "type": "function",
                                 "function": {"name": fn.get("name"),
                                              "arguments": ham if isinstance(ham, str) else json.dumps(ham)}})
            if not cagrilar:                      # araç çağırmadan cevap yazdıysa onu not kabul et
                oturum.not_metni, oturum.durum = icerik, "bitti"
                return "bitti"
            oturum.mesajlar.append({"role": "assistant", "content": icerik, "tool_calls": cagrilar})
            for c in cagrilar:
                ad = c["function"]["name"]
                try:
                    args = json.loads(c["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args, cikti = None, "Argümanlar okunamadı; aracı daha kısa argümanlarla tekrar çağır."
                if ad == "cevap_ver" and args is not None:
                    metin = str(args.get("cevap") or "").strip()
                    if len(metin) < 15:
                        cikti = "cevap alanı boş geldi; notun tamamını 'cevap' alanına yazıp tekrar çağır."
                    else:
                        oturum.not_metni, oturum.durum = metin, "bitti"
                        oturum.mesajlar.append({"role": "tool", "tool_call_id": c["id"], "content": "Not kaydedildi."})
                        oturum.adimlar.append({"arac": ad, "girdi": "", "cikti": kisa(metin, 300)})
                        return "bitti"
                elif args is not None:
                    fn = getattr(araclar, ad, None) if ad in {a["function"]["name"] for a in ARACLAR} else None
                    if fn is None:
                        cikti = f"Bilinmeyen araç: {ad}"
                    else:
                        try:
                            cikti = str(fn(**args))
                        except TypeError as exc:
                            cikti = f"Hatalı parametre: {exc}"
                        except Exception as exc:
                            cikti = f"Araç hatası: {type(exc).__name__}: {str(exc)[:300]}"
                oturum.mesajlar.append({"role": "tool", "tool_call_id": c["id"], "content": kisa(cikti, ARAC_CIKTI_KARAKTER)})
                oturum.adimlar.append({"arac": ad, "girdi": json.dumps(args or {}, ensure_ascii=False), "cikti": kisa(cikti, 1500)})
                if on_step:
                    on_step(oturum.adimlar[-1])
        oturum.not_metni, oturum.durum = f"{MAKS_ADIM} adım sınırına ulaşıldı; not tamamlanamadı.", "bitti"
        return "bitti"
    except Exception as exc:
        oturum.durum, oturum.hata = "hata", f"{type(exc).__name__}: {exc}"
        return "hata"


# ------------------------------- Not denetimi -------------------------------
_ALINTI_RE = re.compile(r"[\"“«]([^\"”»]{15,400})[\"”»]")
_MADDE_RE = re.compile(r"^\s*(?:[-•*]|\d+[.)])\s+(.*)$")
_KAYNAKSIZ_SERBEST = ("eksik", "celiski", "soru", "kontrol", "kaynak")   # bu başlıkların altında kaynak şart değil


def not_denetimi(metin, kaynaklar) -> List[str]:
    """Notu kaynaklara karşı denetler; bulgu listesi döner (boş = temiz)."""
    bulgular = []
    for k in sorted(set(re.findall(r"\[(K\d+)\]", metin or "")) - set(kaynaklar), key=lambda x: int(x[1:])):
        bulgular.append(f"{k} diye bir kaynak yok (uydurma kaynak numarası).")
    havuz = fold(" ".join(f"{v['baslik']} {v['metin']}" for v in kaynaklar.values()))
    for a in _ALINTI_RE.findall(metin or ""):
        if fold(a).strip() and fold(a).strip() not in havuz:
            bulgular.append(f"Alıntı kaynaklarda birebir geçmiyor: \"{kisa(a, 120)}\"")
    bolum = ""
    for satir in (metin or "").splitlines():
        s = satir.strip()
        if not s:
            continue
        m = _MADDE_RE.match(s)
        if (re.match(r"^\d+\.\s+\S", s) and len(s) < 80 and "[K" not in s) or s.lower().startswith("kaynaklar"):
            bolum = fold(s)
            continue
        if any(w in bolum for w in _KAYNAKSIZ_SERBEST):
            continue
        if m and "[K" not in s and len(m.group(1)) > 25:
            bulgular.append(f"Kaynaksız madde: \"{kisa(m.group(1), 120)}\"")
    return bulgular


def incele(sirket, araclar: Optional[Araclar] = None, on_step=None):
    """Bir şirketi inceler: ajan çalışır, not denetlenir, bulgu varsa bir kez düzeltme istenir. (oturum, araclar)."""
    araclar = araclar or Araclar(sirket)
    oturum = Oturum(sirket)
    if calistir(oturum, araclar, on_step) != "bitti":
        return oturum, araclar
    araclar.denetim = not_denetimi(oturum.not_metni, araclar.kaynaklar)
    if araclar.denetim:
        ilk_not, ilk_denetim = oturum.not_metni, list(araclar.denetim)
        oturum.mesajlar.append({"role": "user", "content": (
            "Not denetimi şu sorunları buldu:\n- " + "\n- ".join(araclar.denetim) +
            "\nBu sorunları düzelt (kaynaksız cümleleri ya kaynakla destekle ya da çıkar) ve cevap_ver'i tekrar çağır.")})
        oturum.not_metni = ""
        if calistir(oturum, araclar, on_step) == "bitti" and oturum.not_metni.strip():
            araclar.denetim = not_denetimi(oturum.not_metni, araclar.kaynaklar)
        else:   # düzeltme turu başarısız: ilk not, uyarılarıyla geri gelir
            oturum.not_metni = ilk_not
            araclar.denetim = ilk_denetim + [f"Düzeltme turu tamamlanamadı ({oturum.hata or 'boş cevap'})."]
            oturum.durum, oturum.hata = "bitti", None
    return oturum, araclar


# ------------------------------- Çıktı -------------------------------
def not_markdown(oturum: Oturum, araclar: Araclar) -> str:
    satirlar = [oturum.not_metni.strip(), ""]
    if araclar.denetim:
        satirlar += ["> **Denetim uyarıları** (kontrol edilmeli):"] + [f"> - {d}" for d in araclar.denetim] + [""]
    satirlar.append("## Kaynaklar")
    for k, v in araclar.kaynaklar.items():
        satirlar.append(f"- [{k}] {v['tur']} · {v['tarih']} · {v['baslik']}" + (f" — {v['url']}" if v["url"] else ""))
    satirlar.append(f"\n_Ajan taslağıdır; seviye ve skor EWS kurallarındandır. {dt.datetime.now():%Y-%m-%d %H:%M}_")
    return "\n".join(satirlar)


def not_html(oturum: Oturum, araclar: Araclar) -> str:
    """Tarayıcıda açılacak tek dosyalık not: [K#] atıfları kaynak tablosuna bağlanır."""
    def atif(s):
        return re.sub(r"\[(K\d+)\]", lambda m: f'<a href="#{m.group(1)}">[{m.group(1)}]</a>', html.escape(s))
    govde = "\n".join(f"<p>{atif(s)}</p>" if s.strip() else "" for s in oturum.not_metni.splitlines())
    uyari = ("<div class='uyari'><b>Denetim uyarıları</b> (kontrol edilmeli)<ul>" +
             "".join(f"<li>{html.escape(d)}</li>" for d in araclar.denetim) + "</ul></div>") if araclar.denetim else ""
    satir = "".join(
        f"<tr id='{k}'><td>{k}</td><td>{html.escape(v['tur'])}</td><td>{html.escape(v['tarih'])}</td><td>"
        + (f"<a href='{html.escape(v['url'])}' target='_blank' rel='noopener'>{html.escape(v['baslik'])}</a>"
           if v["url"].startswith("http") else html.escape(v["baslik"])) + "</td></tr>"
        for k, v in araclar.kaynaklar.items())
    return f"""<!doctype html><html lang="tr"><head><meta charset="utf-8"><title>İnceleme {html.escape(str(oturum.sirket))}</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#222;line-height:1.5}}
.uyari{{background:#fff4ce;border:1px solid #e0c060;padding:8px 14px;margin:12px 0}}table{{border-collapse:collapse;width:100%;font-size:13px}}
td{{border-bottom:1px solid #ddd;padding:4px 6px;vertical-align:top}}tr:target{{background:#e8f0ff}}.alt{{color:#777;font-size:12px}}</style>
</head><body>{uyari}{govde}<h3>Kaynaklar</h3><table>{satir}</table>
<p class="alt">Ajan taslağıdır; seviye ve skor EWS kurallarındandır. {dt.datetime.now():%Y-%m-%d %H:%M} · {oturum.n_llm} LLM isteği,
{len(oturum.adimlar)} araç çağrısı, {time.time() - oturum.basla:.0f} sn.</p></body></html>"""


def kaydet(oturum: Oturum, araclar: Araclar, klasor=None):
    """Notu (.md ve .html) data/incelemeler/ altına yazar; dış kaynak ham sonuçları yazılmaz. (md_yolu, html_yolu)."""
    klasor = Path(klasor or Path(getattr(ayarlar, "DATA_DIR", ayarlar.ROOT_DIR)) / "incelemeler")
    klasor.mkdir(parents=True, exist_ok=True)
    ad = re.sub(r"[^\w-]+", "_", str(oturum.sirket))[:40]
    damga = f"{dt.datetime.now():%Y%m%d_%H%M}"
    md, ht = klasor / f"{ad}_{damga}.md", klasor / f"{ad}_{damga}.html"
    md.write_text(not_markdown(oturum, araclar), encoding="utf-8")
    ht.write_text(not_html(oturum, araclar), encoding="utf-8")
    return md, ht
