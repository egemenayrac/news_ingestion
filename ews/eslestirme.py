"""Kurumsal Kredi Riski Erken Uyarı Sistemi (EWS) - haber ve şirket eşleştirme.

Son AGGREGATION_WINDOW_DAYS (180) gündeki haberlerde portföy şirketlerini bulur; her (haber, şirket)
eşleşmesi için şirket adının çevresinden bir metin kesiti çıkarır. Skor üretmez: haberin şirketle
ilgisini ve kredi riskini 4_llm_dogrulama.py LLM'e sordurur.

Eşleştirme kasıtlı olarak geniştir: bir haberin kaçması, yanlış bir eşleşmeden daha pahalıdır, çünkü
yanlış eşleşmeleri LLM eler. Bu yüzden bütün alias'lar (config'teki "aliases" ve "strict_aliases",
tek kelimeliler dahil) aynı biçimde eşleşir:
  alias   Tam kelime olarak: önünde ve arkasında harf ya da rakam olmamalıdır, kesme işaretli ek sayılır
          ("ABC'nin" eşleşir; "taşıma" TAŞ ile eşleşmez). Büyük/küçük harf ve Türkçe karakter farkı
          gözetilmez. Aynı konumda birden fazla alias eşleşirse en uzunu seçilir.
  ticker  Borsa kodu ("tickers", "bist_ticker"); tam kelime ve tam büyük harfle eşleşir, büyük harfli
          başlık içinde sayılmaz.
  ortak   Şirketin hakim ortağının adları ("partners"; 2_sql_to_parquet %50'den büyük payı yazar). Ortağın haberi
          şirketin kaydı olur, Ortak kolonunda "ABC HOLDİNG A.Ş. (%62)" yazar. Şirketin kendi adı da geçiyorsa
          kendi adıyla eşleşme tutulur.
Kesit, başka bir portföy şirketine gelince durur; böylece bir haberdeki diğer borçlunun olayı yanlış
şirkete yazılmaz. Şirket başlıkta da geçiyorsa başlıktaki kesit başa eklenir.

Girdi:  PARQUET_PATH (haberler; zorunlu kolonlar Başlık, Metin, Tarih), portfolio_config.py (2_sql_to_parquet üretir).
Çıktı:  run_matching() -> DataFrame: haberin kolonları + Şirket, Şirket_Adi, Eşleşen_Alias, Ortak, Metin_Snippet.
        En yeni haber önce; aynı şirket + aynı haber (URL, yoksa başlık + gün) bir kez yer alır.
"""

from __future__ import annotations

import bisect
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
import ayarlar  # noqa: E402
from ews.text import fast_normalize_if_1to1, normalize_text, normalize_with_offsets, to_original_span  # noqa: E402

try:
    from portfolio_config import COMPANIES  # noqa: E402
except ImportError:  # config yoksa örnek iki şirketle çalışılır
    COMPANIES = {
        "COMP_01": {"aliases": ["ABC Holding", "ABC"], "name": "ABC Holding"},
        "COMP_02": {"aliases": ["XYZ Lojistik", "XYZ"], "name": "XYZ Lojistik"},
    }

logger = logging.getLogger("ews_eslestirme")


# =============================================================================
# 1. Ayarlar (ayarlar.py üzerinden okunur)
# =============================================================================
PARQUET_PATH = str(ayarlar.NEWS_PARQUET_PATH)
AGGREGATION_WINDOW_DAYS = ayarlar.AGGREGATION_WINDOW_DAYS
MIN_HAVUZ_GUN = 30              # haber havuzu bundan az günü kapsıyorsa uyarı yazılır
SCORE_UNDATED_NEWS = False       # tarihi okunamayan haber: False ise atlanır (sayısı uyarı olarak yazılır)
SNIPPET_WORDS_BEFORE = 25        # şirket adından önce alınan kelime
SNIPPET_WORDS_AFTER = 60         # şirket adından sonra alınan kelime
MAX_SNIPPETS_PER_ARTICLE = 3     # aynı haberde şirketin birden çok geçişi varsa birleştirilecek en fazla kesit
REPORTING_TZ = "Europe/Istanbul"  # saat dilimli tarihler bu saate çevrilir
DATE_DAYFIRST = True             # "05.09.2026" gün-ay-yıl okunur; ISO tarihler etkilenmez
ESLESTIRME_SURUMU = "1.2"
REQUIRED_COLUMNS = {"Başlık", "Metin", "Tarih"}


# =============================================================================
# 2. Alias indeksi
# =============================================================================
def _canonical_name(comp_key, comp_data: dict, alias: str) -> str:
    """Raporda gösterilecek şirket adı: name, yoksa ilk alias, yoksa anahtar."""
    return comp_data.get("name") or (comp_data.get("aliases") or [alias])[0] or str(comp_key)


def _word_pattern(term: str) -> str:
    """Alias'ı baştan sona eşleyen desen (tam kelime)."""
    sep = r"\s+"
    return rf"(?<!\w){sep.join(re.escape(part) for part in normalize_text(term).split())}(?!\w)"


def _alias_trie_regex(aliases: Sequence[str]) -> str:
    """Normalize alias'lardan karakter ağacı (trie) regex'i: aynı konumda en uzun alias seçilir.

    Düz bir alternation ile aynı eşleşmeleri verir, ancak yüzlerce alias'ta yaklaşık 35 kat daha hızlıdır.
    """
    root: dict = {}
    for alias in aliases:
        node = root
        for ch in alias:
            node = node.setdefault(ch, {})
        node[""] = True

    def emit(node: dict) -> str:
        alternatives = [(r"\s+" if ch == " " else re.escape(ch)) + emit(node[ch]) for ch in sorted(k for k in node if k != "")]
        if not alternatives:
            return ""
        body = alternatives[0] if len(alternatives) == 1 else "(?:" + "|".join(alternatives) + ")"
        return f"(?:{body})?" if "" in node else body

    return emit(root)


def _compile_ticker_index(tickers: List[Tuple[re.Pattern, dict]]) -> Tuple[Dict[str, List[dict]], Optional[re.Pattern]]:
    """Tüm borsa kodlarını (ticker) tek birleşik regex ve sözlükte toplar (tek taramada eşleşme)."""
    tmap: Dict[str, List[dict]] = {}
    for _, item in tickers:
        code = str(item["matched_alias"]).strip().upper()
        if code:
            tmap.setdefault(code, []).append(item)
    if not tmap:
        return {}, None
    ordered = sorted(tmap, key=len, reverse=True)
    pat = re.compile(rf"(?<!\w)(?:{'|'.join(re.escape(c) for c in ordered)})(?!\w)")
    return tmap, pat


def build_alias_index(companies: dict):
    """Normalize alias -> şirketler eşlemesi, tek birleşik regex ve ticker listesi.

    "aliases" ile "strict_aliases" aynı biçimde eşleşir (eski config'lerdeki strict ayrımı yok sayılır).
    """
    alias_map: Dict[str, List[dict]] = {}
    tickers: List[Tuple[re.Pattern, dict]] = []
    for comp_key, comp_data in companies.items():
        names = [(a, None) for a in list(comp_data.get("aliases", [])) + list(comp_data.get("strict_aliases", []))]
        names += [(a, f"{p['name']} (%{p['share']:g})") for p in comp_data.get("partners", []) for a in p.get("aliases", [])]
        for alias, ortak in names:  # önce şirketin kendi adları: aynı ad ortakta da varsa kendi adı sayılır
            alias_norm = normalize_text(alias).strip()
            items = alias_map.setdefault(alias_norm, []) if alias_norm else None
            if items is not None and all(i["company_key"] != comp_key for i in items):
                items.append({"company_key": comp_key, "matched_alias": alias, "ortak": ortak,
                              "canonical_name": _canonical_name(comp_key, comp_data, alias)})
        codes = list(comp_data.get("tickers", [])) + [comp_data.get("bist_ticker") or ""]
        for code in dict.fromkeys(str(t).strip().upper() for t in codes if str(t).strip()):
            tickers.append((re.compile(rf"(?<!\w){re.escape(code)}(?!\w)"),
                            {"company_key": comp_key, "matched_alias": code, "ortak": None,
                             "canonical_name": _canonical_name(comp_key, comp_data, code)}))
    pattern = (re.compile(rf"(?<!\w)(?:{_alias_trie_regex(sorted(alias_map))})(?!\w)", flags=re.IGNORECASE | re.UNICODE)
               if alias_map else None)
    return alias_map, pattern, tickers


def set_companies(companies: dict) -> None:
    """Portföyü değiştirir ve alias indeksini yeniden kurar (testler ve dinamik yükleme için)."""
    alias_map, pattern, tickers = build_alias_index(companies)
    ticker_map, ticker_pattern = _compile_ticker_index(tickers)
    globals().update(
        COMPANIES=companies,
        ALIAS_MAP=alias_map,
        ALIAS_PATTERN=pattern,
        TICKERS=tickers,
        TICKER_MAP=ticker_map,
        TICKER_PATTERN=ticker_pattern,
    )


ALIAS_MAP, ALIAS_PATTERN, TICKERS = build_alias_index(COMPANIES)
TICKER_MAP, TICKER_PATTERN = _compile_ticker_index(TICKERS)


# =============================================================================
# 3. Metin yardımcıları
# =============================================================================
_SENTENCE_END_RE = re.compile(r"[.!?;]+(?=\s|$)|\n+")
_PRECEDING_TOKEN_RE = re.compile(r"([\w.]+)$")
_ABBREVIATIONS = {"a.s", "t.a.s", "ltd", "sti", "vb", "vs", "dr", "prof", "doc", "no",
                  "mah", "cad", "sok", "bkz", "sn", "inc", "corp", "co"}
# Ortak özne: iki şirket arasında yalnızca bağlaç ya da ek varsa ("ABC ve XYZ") birbirlerinin kesit sınırı sayılmaz.
_COORDINATION_RE = re.compile(r"^\s*(?:['\u2019]\w+)?\s*(?:,|&|/|\bve\b|\bile\b|\bveya\b|\bile\s+birlikte\b)\s*$")


def sentence_breaks(text_norm: str) -> List[Tuple[int, int]]:
    """Cümle sonu işaretlerinin konumları; kısaltma ("A.Ş.", "Ltd.") ve tek harf noktaları hariç."""
    breaks = []
    for match in _SENTENCE_END_RE.finditer(text_norm):
        if match.group(0) == ".":
            preceding = _PRECEDING_TOKEN_RE.search(text_norm[max(0, match.start() - 12):match.start()])
            token = preceding.group(1).strip(".") if preceding else ""
            if token in _ABBREVIATIONS or len(token) == 1:
                continue
        breaks.append((match.start(), match.end()))
    return breaks


def _inside_all_caps_run(text: str, start: int, end: int, window: int = 30) -> bool:
    """Eşleşme, büyük harfle yazılmış bir bölümün (başlık gibi) içinde mi?"""
    letters = [c for c in text[max(0, start - window):start] + text[end:end + window] if c.isalpha()]
    return len(letters) >= 6 and sum(c.isupper() for c in letters) / len(letters) > 0.8


def _is_coordinated(text: str, left_end: int, right_start: int) -> bool:
    return bool(_COORDINATION_RE.match(normalize_text(text[left_end:right_start])))


_WORD_RE = re.compile(r"\b\w+\b")
_WS_RE = re.compile(r"\s+")


def extract_context(text: str, start: int, end: int, left_boundary: Optional[int] = None,
                    right_boundary: Optional[int] = None) -> str:
    """Eşleşmenin öncesinden SNIPPET_WORDS_BEFORE, sonrasından SNIPPET_WORDS_AFTER kelimelik kesit.

    Sınırlar (başka şirketin geçişi) verilirse kesit onları aşmaz; sınırlar eşleşmenin içine taşamaz.
    """
    if not text or start < 0 or start >= len(text) or end < start:
        return ""
    left_limit = 0 if left_boundary is None else max(0, min(left_boundary, start))
    right_limit = len(text) if right_boundary is None else min(len(text), max(right_boundary, end))
    before = list(_WORD_RE.finditer(text[left_limit:start]))
    left = left_limit + before[-SNIPPET_WORDS_BEFORE].start() if len(before) > SNIPPET_WORDS_BEFORE else left_limit
    after = list(_WORD_RE.finditer(text[end:right_limit]))
    right = end + after[SNIPPET_WORDS_AFTER - 1].end() if len(after) > SNIPPET_WORDS_AFTER else right_limit
    return text[min(left, start):max(right, end)].strip()


# =============================================================================
# 4. Şirket eşleştirme (fast-path hızlandırmalı)
# =============================================================================
def match_companies(text: str, headline_end: Optional[int] = None) -> List[dict]:
    """Metindeki portföy şirketlerini bulur; her geçiş için bir eşleşme ve kesit döndürür.

    Fast-path: metin 1-e-1 güvenli karakterlerden oluşuyorsa offset listesi üretmeden C hızında
    normalleştirilir, tek geçişte birleşik alias + birleşik ticker regex'i taranır; eşleşme yoksa
    cümle sınırı (sentence_breaks) hiç hesaplanmadan anında dönülür.
    """
    if not text or (ALIAS_PATTERN is None and TICKER_PATTERN is None and not TICKERS):
        return []

    fast_norm = fast_normalize_if_1to1(text)
    if fast_norm is not None:
        # Hızlı eleme: ne alias ne ticker geçmiyorsa offset/cümle hesabı yapmadan çık
        has_alias = ALIAS_PATTERN is not None and ALIAS_PATTERN.search(fast_norm) is not None
        has_ticker = TICKER_PATTERN is not None and TICKER_PATTERN.search(text) is not None
        if not has_alias and not has_ticker:
            return []
        text_norm = fast_norm
        starts: Optional[List[int]] = None
        ends: Optional[List[int]] = None
    else:
        text_norm, starts, ends = normalize_with_offsets(text)
        has_alias = ALIAS_PATTERN is not None and ALIAS_PATTERN.search(text_norm) is not None
        has_ticker = TICKER_PATTERN is not None and TICKER_PATTERN.search(text) is not None
        if not has_alias and not has_ticker:
            return []

    hits = []
    if has_alias and ALIAS_PATTERN is not None:
        text_len = len(text)
        for m in ALIAS_PATTERN.finditer(text_norm):
            raw_m = m.group(0)
            matched = _WS_RE.sub(" ", raw_m).strip() if ("  " in raw_m or "\n" in raw_m or "\t" in raw_m) else raw_m.strip()
            candidates = ALIAS_MAP.get(matched)
            if candidates is None:
                candidates = [i for a, items in ALIAS_MAP.items()
                              if re.fullmatch(_word_pattern(a), matched) for i in items]
            if starts is None or ends is None:
                o_start, o_end = m.start(), m.end()
            else:
                o_start, o_end = to_original_span(starts, ends, m.start(), m.end(), text_len)
            hits += [{**item, "start": o_start, "end": o_end} for item in candidates]

    if has_ticker and TICKER_PATTERN is not None:
        for m in TICKER_PATTERN.finditer(text):
            if not _inside_all_caps_run(text, m.start(), m.end()):
                for item in TICKER_MAP.get(m.group(0), ()):
                    hits.append({**item, "start": m.start(), "end": m.end()})
    if not hits:
        return []

    for h in hits:
        h["is_headline"] = headline_end is not None and h["start"] < headline_end
    direct = {h["company_key"] for h in hits if not h["ortak"]}  # kendi adı geçen şirkette ortak eşleşmesi atılır
    hits = [h for h in hits if not h["ortak"] or h["company_key"] not in direct]
    in_headline = {h["company_key"] for h in hits if h["is_headline"]}
    in_body = {h["company_key"] for h in hits if not h["is_headline"]}
    hits = [h for h in hits if not h["is_headline"] or h["company_key"] not in in_body]

    # Aynı şirketin çakışan alias'larından uzun olan tutulur.
    hits.sort(key=lambda h: (h["start"], -(h["end"] - h["start"])))
    kept: List[dict] = []
    for h in hits:
        clash = next((i for i, k in enumerate(kept) if k["company_key"] == h["company_key"]
                      and not (h["end"] <= k["start"] or h["start"] >= k["end"])), None)
        if clash is None:
            kept.append(h)
        elif h["end"] - h["start"] > kept[clash]["end"] - kept[clash]["start"]:
            kept[clash] = h
    hits = sorted(kept, key=lambda h: (h["start"], h["end"]))

    # Cümle sınırları yalnızca birden fazla farklı şirket geçişi olduğunda (tembel) hesaplanır.
    has_multiple_companies = len({h["company_key"] for h in hits}) > 1
    if has_multiple_companies:
        if ends is None:
            sentence_ends = [b_end for _, b_end in sentence_breaks(text_norm)]
        else:
            sentence_ends = [ends[b_end - 1] for _, b_end in sentence_breaks(text_norm)]
    else:
        sentence_ends = []

    results = []
    for i, h in enumerate(hits):
        # Kesit sınırları: önceki ve sonraki başka şirket (bağlaçla bağlı ortak özneler atlanır).
        left_boundary = None
        if has_multiple_companies:
            for prev in reversed(hits[:i]):
                if (prev["start"], prev["end"]) == (h["start"], h["end"]):
                    continue
                if prev["company_key"] != h["company_key"]:
                    if prev["end"] <= h["start"]:
                        left_boundary = prev["end"]
                        pos = bisect.bisect_right(sentence_ends, h["start"]) - 1
                        if pos >= 0 and sentence_ends[pos] > prev["end"]:  # önceki şirket başka cümlede
                            left_boundary = sentence_ends[pos]
                    break
        right_boundary, group_end = None, h["end"]
        if has_multiple_companies:
            for nxt in hits[i + 1:]:
                if (nxt["start"], nxt["end"]) == (h["start"], h["end"]) or nxt["company_key"] == h["company_key"]:
                    continue
                if nxt["start"] < group_end:
                    continue
                if _is_coordinated(text, group_end, nxt["start"]):
                    group_end = max(group_end, nxt["end"])
                    continue
                right_boundary = nxt["start"]
                break
        results.append({"company_key": h["company_key"], "company_name": h["canonical_name"],
                        "matched_alias": h["matched_alias"], "ortak": h["ortak"], "in_headline": h["company_key"] in in_headline,
                        "snippet": extract_context(text, h["start"], h["end"], left_boundary, right_boundary)})
    return results


def match_record(baslik: str, metin: str) -> List[dict]:
    """Tek haberin eşleşmeleri; şirket başlıkta da geçiyorsa başlıktaki kesit gövde kesitinin başına eklenir.
    Aynı şirket gövdede farklı paragraflarda birden çok kez geçiyorsa ilk MAX_SNIPPETS_PER_ARTICLE kesit birleştirilir."""
    baslik, metin = baslik.strip(), metin.strip()
    text = f"{baslik}\n{metin}" if baslik and metin else (baslik or metin)
    matches = match_companies(text, headline_end=len(baslik) if baslik else 0)
    if not matches:
        return []
    # Başlık kesiti yalnızca başlıkta eşleşen şirket varsa hesaplanır (gereksiz çift tarama önlenir).
    headline = {r["company_key"]: r for r in match_companies(baslik)} if (baslik and any(r["in_headline"] for r in matches)) else {}
    by_company: Dict[object, dict] = {}
    snippets_by_company: Dict[object, List[str]] = {}
    for r in matches:
        ck = r["company_key"]
        if ck not in by_company:
            by_company[ck] = dict(r)
            head = headline.get(ck)
            init_snips = []
            if r["in_headline"] and head and head["snippet"] and head["snippet"] not in r["snippet"]:
                init_snips.append(head["snippet"])
            if r["snippet"]:
                init_snips.append(r["snippet"])
            snippets_by_company[ck] = init_snips
        else:
            snips = snippets_by_company[ck]
            if r["snippet"] and len(snips) < MAX_SNIPPETS_PER_ARTICLE and all(r["snippet"] not in s and s not in r["snippet"] for s in snips):
                snips.append(r["snippet"])
    results = []
    for ck, rec in by_company.items():
        rec["snippet"] = "\n...\n".join(snippets_by_company.get(ck, []))
        results.append(rec)
    return results


# =============================================================================
# 5. Tarih penceresi, borsa bülteni ön filtresi ve ajans tekilleştirme
# =============================================================================
_DAYFIRST_RE = re.compile(r"^\s*\d{1,2}[./-]\d{1,2}[./-]\d{4}")
_UTM_RE = re.compile(r"(?<=[?&])(?:utm_[^=&#]*|fbclid|gclid)=[^&#]*&?")

_OFFICIAL_SOURCES = {"ilan.gov.tr", "resmi gazete", "kap"}

# Borsa bülteni / hisse listesi başlık kalıpları (normalize edilmiş metin üzerinde)
_BORSA_BULTEN_RE = re.compile(
    r"(?:"
    r"\ben\s+cok\s+(?:kazandiran|kaybettiren|yukselen|dusen|islem\s+goren)\b"
    r"|\bborsa(?:da|\s+istanbul)?\s+(?:gunun\s+ozeti|gune\s+nasil\s+basladi|gune\s+.*basladi|gunu\s+.*tamamladi|gunu\s+.*kapatti|haftayi\s+.*kapatti)\b"
    r"|\bbist\s*100\s+(?:endeksi\s+)?(?:gune|gunu|haftayi|rekor|yukselisle|dususle)\b"
    r"|\bgunun\s+(?:one\s+cikan|en\s+cok\s+.*)\s+hisseleri\b"
    r"|\bhaftanin\s+en\s+cok\s+(?:kazandiran|kaybettiren|yukselen|dusen)\b"
    r"|\bgunluk\s+borsa\s+bulteni\b"
    r"|\bmodel\s+portfoy(?:une|unden|unu|unde)?\b"
    r"|\bhedef\s+fiyat(?:ini|larini)?\s+(?:guncelledi|yukseltti|acikladi|korudu)\b"
    r"|\btemettu\s+(?:takvimi|dagitacak\s+sirketler|verecek\s+hisseler)\b"
    r"|\bhalka\s+arz\s+takvimi\b"
    r"|\baraci\s+kurumlardan\s+.*hisse\s+onerisi\b"
    r"|\b(?:gunluk|haftalik)\s+hisse\s+onerileri\b"
    r")",
    flags=re.IGNORECASE,
)

# Kredi riski güvenlik koruması: bu kelimelerden herhangi biri geçiyorsa haber ASLA ön filtreden elenmez.
_RISK_GUARD_RE = re.compile(
    r"\b(?:"
    r"konkordato|iflas|tasfiye|haciz|tedbir|kayyum|tmsf|temerrut|yapilandirma|"
    r"sorusturma|dava|ceza|tutukl\w*|gozalti|zarar|grev|yangin|patlama|"
    r"uretim\w*\s+dur\w*|faaliyet\w*\s+dur\w*|not\w*\s+(?:dusur\w*|indir\w*)|"
    r"kredi\s+notu|borc\w*|icra|yasaklama|suc\s+duyurusu|ifsa|el\s+koy\w*"
    r")\b",
    flags=re.IGNORECASE,
)

_DEDUP_STOPWORDS = frozenset({
    "bir", "ve", "ile", "icin", "olan", "olarak", "daha", "cok", "son", "yeni",
    "buyuk", "kadar", "sonra", "once", "uzere", "gore", "karsi", "yuzde", "tl",
    "milyon", "milyar", "turkiye", "istanbul", "ankara", "son", "dakika", "haber",
})


def is_borsa_bulteni(baslik: object, metin: object = "", kaynak: object = "") -> bool:
    """Haber yalnızca borsa bülteni / en çok kazandıranlar listesi mi?

    Güvenlik ilkesi: Resmi ilan kaynakları (İlan.gov.tr, Resmî Gazete, KAP) ve
    başlıkta/özette kredi riski anahtar kelimesi geçen haberler ASLA elenmez.
    """
    src_norm = normalize_text(kaynak).strip() if kaynak is not None else ""
    if src_norm in _OFFICIAL_SOURCES:
        return False
    title_norm = normalize_text(baslik).strip() if baslik is not None else ""
    if not title_norm:
        return False
    if _RISK_GUARD_RE.search(title_norm):
        return False
    if not _BORSA_BULTEN_RE.search(title_norm):
        return False
    # Başlık borsa bülteni kalıbına uyuyor; gövde başlangıcında somut risk kelimesi varsa yine koru
    lead = normalize_text(str(metin or "")[:400])
    if lead and _RISK_GUARD_RE.search(lead):
        return False
    return True


def filter_borsa_bulletins(df: pd.DataFrame) -> Tuple[pd.DataFrame, int]:
    """Haber tablosundan risksiz borsa bülteni / liste haberlerini çıkarır; `(temiz_df, elenen_sayisi)` döner."""
    if df.empty or "Başlık" not in df.columns:
        return df, 0
    has_src = "Kaynak" in df.columns
    has_body = "Metin" in df.columns
    keep_mask = []
    for row in df.to_dict("records"):
        baslik = "" if pd.isna(row.get("Başlık")) else row.get("Başlık")
        metin = "" if (not has_body or pd.isna(row.get("Metin"))) else row.get("Metin")
        kaynak = "" if (not has_src or pd.isna(row.get("Kaynak"))) else row.get("Kaynak")
        keep_mask.append(not is_borsa_bulteni(baslik, metin, kaynak))
    kept = df.loc[keep_mask]
    return kept, len(df) - len(kept)


def _token_set(text: str, max_chars: int = 350) -> Set[str]:
    norm = normalize_text(text[:max_chars])
    return {w for w in _WORD_RE.findall(norm) if len(w) >= 3 and w not in _DEDUP_STOPWORDS}


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / (len(a) + len(b) - inter)


def dedup_agency_news(
    df: pd.DataFrame,
    title_threshold: float = 0.78,
    body_threshold: float = 0.75,
    day_window: int = 2,
) -> Tuple[pd.DataFrame, int]:
    """Farklı sitelerde yayımlanan aynı ajans haberlerini (AA/İHA/DHA vb.) tekilleştirir.

    Aynı gün (±`day_window` gün) içinde başlık token Jaccard >= `title_threshold` ya da
    gövde başlangıcı Jaccard >= `body_threshold` (+ başlık >= 0.35) olan kopyalar kümelenir ve
    en uzun metne sahip kayıt korunur. Resmi ilan kaynakları asla elenmez.
    """
    if len(df) < 2 or "Başlık" not in df.columns:
        return df, 0

    dates = to_naive_datetime(df["Tarih"]) if "Tarih" in df.columns else pd.Series(pd.NaT, index=df.index)
    # En uzun metinli haber önce işlenir: kopyası geldiğinde daha zengin metin korunur.
    body_lens = df["Metin"].fillna("").astype(str).str.len() if "Metin" in df.columns else pd.Series(0, index=df.index)
    order = body_lens.sort_values(ascending=False, kind="mergesort").index.tolist()

    has_src = "Kaynak" in df.columns
    # Gün -> [(exact_title, title_tokens, body_tokens)]
    buckets: Dict[int, List[Tuple[str, Set[str], Set[str]]]] = {}
    keep_indices: Set[object] = set()

    for idx in order:
        src_norm = normalize_text(df.at[idx, "Kaynak"]).strip() if has_src and pd.notna(df.at[idx, "Kaynak"]) else ""
        if src_norm in _OFFICIAL_SOURCES:
            keep_indices.add(idx)
            continue

        raw_title = "" if pd.isna(df.at[idx, "Başlık"]) else str(df.at[idx, "Başlık"])
        raw_body = "" if ("Metin" not in df.columns or pd.isna(df.at[idx, "Metin"])) else str(df.at[idx, "Metin"])
        title_clean = _WS_RE.sub(" ", normalize_text(raw_title)).strip()
        t_tokens = _token_set(raw_title, max_chars=250)
        b_tokens = _token_set(raw_body, max_chars=360)

        dt_val = dates.loc[idx]
        day_ord = int(dt_val.toordinal()) if pd.notna(dt_val) else -1

        is_dup = False
        if day_ord >= 0 and (len(title_clean) >= 20 or len(t_tokens) >= 4):
            for d in range(day_ord - day_window, day_ord + day_window + 1):
                for prev_title, prev_t, prev_b in buckets.get(d, ()):
                    if len(title_clean) >= 20 and title_clean == prev_title:
                        is_dup = True
                        break
                    t_sim = _jaccard(t_tokens, prev_t) if (len(t_tokens) >= 4 and len(prev_t) >= 4) else 0.0
                    if t_sim >= title_threshold:
                        is_dup = True
                        break
                    if t_sim >= 0.35 and len(b_tokens) >= 12 and len(prev_b) >= 12:
                        if _jaccard(b_tokens, prev_b) >= body_threshold:
                            is_dup = True
                            break
                if is_dup:
                    break

        if not is_dup:
            keep_indices.add(idx)
            if day_ord >= 0:
                buckets.setdefault(day_ord, []).append((title_clean, t_tokens, b_tokens))

    kept = df.loc[[i for i in df.index if i in keep_indices]]
    return kept, len(df) - len(kept)


def to_naive_datetime(series: pd.Series) -> pd.Series:
    """Tarihleri eleman bazında çözer; saat dilimli değerler REPORTING_TZ'ye çevrilir, çözülemeyenler NaT olur."""
    if pd.api.types.is_datetime64_any_dtype(series):
        return series.dt.tz_convert(REPORTING_TZ).dt.tz_localize(None) if getattr(series.dt, "tz", None) else series

    def convert(value):
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return pd.NaT
        try:
            ts = (pd.to_datetime(value, dayfirst=True) if DATE_DAYFIRST and isinstance(value, str) and _DAYFIRST_RE.match(value)
                  else pd.Timestamp(value))
        except (ValueError, TypeError, OverflowError):
            return pd.NaT
        if pd.isna(ts):
            return pd.NaT
        return ts.tz_convert(REPORTING_TZ).tz_localize(None) if ts.tzinfo is not None else ts

    return pd.to_datetime(series.map(convert), errors="coerce")


def news_window(window_days: int = AGGREGATION_WINDOW_DAYS, as_of: Optional[object] = None) -> Tuple[pd.Timestamp, pd.Timestamp]:
    """as_of gününü (verilmezse bugünü) içeren son window_days günün [başlangıç, bitiş] sınırları."""
    end = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp.now()
    if end.tzinfo is not None:
        end = end.tz_convert(REPORTING_TZ).tz_localize(None)
    end = end.normalize() + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    return (end - pd.Timedelta(days=window_days)).normalize() + pd.Timedelta(days=1), end


def normalize_url(url: object) -> str:
    """URL'yi karşılaştırma için sadeleştirir (şema, www, izleme parametreleri, sondaki '/' atılır); boşsa ''."""
    if url is None or (isinstance(url, float) and pd.isna(url)):
        return ""
    value = str(url).strip()
    if not value or value.lower() in {"nan", "none", "null"}:
        return ""
    value = re.sub(r"[?&]+$", "", _UTM_RE.sub("", value.split("#")[0]))
    return re.sub(r"^https?://(?:www\.)?", "", value, flags=re.IGNORECASE).rstrip("/").lower()


def build_dedup_keys(df: pd.DataFrame) -> pd.Series:
    """Tekilleştirme anahtarı: normalize URL; yoksa normalize başlık + gün; ikisi de yoksa satıra özgü."""
    empty = pd.Series("", index=df.index)
    urls = df["URL"] if "URL" in df.columns else empty
    titles = df["Başlık"] if "Başlık" in df.columns else empty
    dates = to_naive_datetime(df["Tarih"]).dt.strftime("%Y-%m-%d") if "Tarih" in df.columns else empty
    keys = []
    for index in df.index:
        url = normalize_url(urls.get(index))
        if url:
            keys.append("URL::" + url)
            continue
        title = titles.get(index)
        title = re.sub(r"\W+", " ", normalize_text("" if title is None or (isinstance(title, float) and pd.isna(title)) else title)).strip()
        date = dates.get(index)
        date = "" if date is None or (isinstance(date, float) and pd.isna(date)) else date
        keys.append(f"TITLE::{title}::{date}" if title else f"ROW::{index}")
    return pd.Series(keys, index=df.index)


# =============================================================================
# 6. Akış
# =============================================================================
def run_matching(
    as_of: Optional[object] = None,
    window_days: int = AGGREGATION_WINDOW_DAYS,
    filter_bulletins: Optional[bool] = None,
    dedup_agency: Optional[bool] = None,
) -> pd.DataFrame:
    """Penceredeki haberleri okur, ön filtre + ajans tekilleştirme uygular ve eşleştirir (en yeni önce)."""
    if not os.path.exists(PARQUET_PATH):
        logger.error("Haber dosyası bulunamadı: %s", PARQUET_PATH)
        return pd.DataFrame()
    df = pd.read_parquet(PARQUET_PATH)
    missing = REQUIRED_COLUMNS - set(df.columns)
    if df.empty or missing:
        logger.error("Haber dosyası boş ya da zorunlu kolon eksik: %s", sorted(missing))
        return pd.DataFrame()

    start, end = news_window(window_days, as_of)
    dates = to_naive_datetime(df["Tarih"])
    if dates.notna().any():
        span = (dates.max().normalize() - dates.min().normalize()).days + 1
        logger.info("Haber havuzu: %s haber, %s - %s (%s gün).", len(df), dates.min().date(), dates.max().date(), span)
        if span < MIN_HAVUZ_GUN:
            logger.warning("UYARI: haber havuzu yalnızca %s günü kapsıyor (pencere %s gün); çok az eşleşme ve olay çıkar. "
                           "1_scaper.py'nin yazdığı dosyayla PARQUET_PATH'in aynı olduğunu kontrol edin.", span, window_days)
    undated = dates.isna()
    keep = ((dates >= start) & (dates <= end)) | (undated if SCORE_UNDATED_NEWS else False)
    if undated.any() and not SCORE_UNDATED_NEWS:
        logger.warning("%s haber tarihsiz olduğu için atlandı; tarih kaynağını kontrol edin.", int(undated.sum()))
    df = df.loc[keep]

    use_bulletin_filter = getattr(ayarlar, "ENABLE_BORSA_ON_FILTRE", True) if filter_bulletins is None else filter_bulletins
    if use_bulletin_filter:
        df, n_bulten = filter_borsa_bulletins(df)
        if n_bulten:
            logger.info("Borsa bülteni ön filtresi: %s liste/bülten haberi LLM'den önce elendi.", n_bulten)

    use_agency_dedup = getattr(ayarlar, "ENABLE_AJANS_TEKILLESTIRME", True) if dedup_agency is None else dedup_agency
    if use_agency_dedup:
        df, n_dup = dedup_agency_news(df)
        if n_dup:
            logger.info("Ajans haber tekilleştirme: %s kopya haber elendi.", n_dup)

    logger.info("Haber penceresi %s - %s: %s haber eşleştiriliyor.", start.date(), end.date(), len(df))

    rows = []
    for row in df.to_dict("records"):
        baslik = "" if pd.isna(row.get("Başlık")) else str(row["Başlık"])
        metin = "" if pd.isna(row.get("Metin")) else str(row["Metin"])
        for r in match_record(baslik, metin):
            rows.append({**row, "Şirket": r["company_key"], "Şirket_Adi": r["company_name"],
                         "Eşleşen_Alias": r["matched_alias"], "Ortak": r["ortak"], "Metin_Snippet": r["snippet"]})
    if not rows:
        logger.warning("Portföy şirketleriyle eşleşen haber bulunamadı.")
        return pd.DataFrame()

    out = pd.DataFrame(rows)
    out = (out.assign(_t=to_naive_datetime(out["Tarih"])).sort_values("_t", ascending=False, na_position="last", kind="stable")
           .assign(_k=lambda d: build_dedup_keys(d)).drop_duplicates(["Şirket", "_k"])
           .drop(columns=["_t", "_k"]).reset_index(drop=True))
    logger.info("%s eşleşme (%s şirket); aynı şirket + aynı haber tekrarları %s satır.",
                len(out), out["Şirket"].nunique(), len(rows) - len(out))
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    df_eslesmeler = run_matching()
    if len(df_eslesmeler):
        print(df_eslesmeler.groupby("Şirket_Adi").size().sort_values(ascending=False).head(20).to_string())
