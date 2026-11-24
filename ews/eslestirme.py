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
import importlib.util
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
import ayarlar  # noqa: E402
from ews.text import normalize_text, normalize_with_offsets, to_original_span  # noqa: E402

def _load_config(path) -> Optional[dict]:
    """portfolio_config.py'yi dosya yolundan okur (data/ klasörüne taşınabilsin diye modül araması kullanılmaz)."""
    try:
        spec = importlib.util.spec_from_file_location("portfolio_config", str(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.COMPANIES
    except FileNotFoundError:
        return None
    except Exception as exc:  # bozuk config: sessizce örnek şirketlere düşmesin
        raise RuntimeError(f"portfolio_config okunamadı ({path}): {exc}") from exc


COMPANIES = _load_config(ayarlar.PORTFOLIO_CONFIG_PATH)
if COMPANIES is None:  # config yoksa örnek iki şirketle çalışılır
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
ESLESTIRME_SURUMU = "1.1"
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
    globals().update(COMPANIES=companies, ALIAS_MAP=alias_map, ALIAS_PATTERN=pattern, TICKERS=tickers)


ALIAS_MAP, ALIAS_PATTERN, TICKERS = build_alias_index(COMPANIES)


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


def extract_context(text: str, start: int, end: int, left_boundary: Optional[int] = None,
                    right_boundary: Optional[int] = None) -> str:
    """Eşleşmenin öncesinden SNIPPET_WORDS_BEFORE, sonrasından SNIPPET_WORDS_AFTER kelimelik kesit.

    Sınırlar (başka şirketin geçişi) verilirse kesit onları aşmaz; sınırlar eşleşmenin içine taşamaz.
    """
    if not text or start < 0 or start >= len(text) or end < start:
        return ""
    left_limit = 0 if left_boundary is None else max(0, min(left_boundary, start))
    right_limit = len(text) if right_boundary is None else min(len(text), max(right_boundary, end))
    before = list(re.finditer(r"\b\w+\b", text[left_limit:start]))
    left = left_limit + before[-SNIPPET_WORDS_BEFORE].start() if len(before) > SNIPPET_WORDS_BEFORE else left_limit
    after = list(re.finditer(r"\b\w+\b", text[end:right_limit]))
    right = end + after[SNIPPET_WORDS_AFTER - 1].end() if len(after) > SNIPPET_WORDS_AFTER else right_limit
    return text[min(left, start):max(right, end)].strip()


# =============================================================================
# 4. Şirket eşleştirme
# =============================================================================
def match_companies(text: str, headline_end: Optional[int] = None) -> List[dict]:
    """Metindeki portföy şirketlerini bulur; her geçiş için bir eşleşme ve kesit döndürür.

    Aynı şirket hem başlıkta hem gövdede geçiyorsa gövde eşleşmeleri kullanılır (in_headline işaretlenir).
    Şirket haberde birden çok kez geçiyorsa run_matching ilk geçişi tutar.
    headline_end, metindeki başlık sonudur.
    """
    if not text or (ALIAS_PATTERN is None and not TICKERS):
        return []
    text_norm, starts, ends = normalize_with_offsets(text)
    sentence_ends = [ends[b_end - 1] for _, b_end in sentence_breaks(text_norm)]

    hits = []
    for m in (ALIAS_PATTERN.finditer(text_norm) if ALIAS_PATTERN is not None else ()):
        matched = re.sub(r"\s+", " ", m.group(0)).strip()
        candidates = ALIAS_MAP.get(matched) or [i for a, items in ALIAS_MAP.items()
                                                 if re.fullmatch(_word_pattern(a), matched) for i in items]
        o_start, o_end = to_original_span(starts, ends, m.start(), m.end(), len(text))
        hits += [{**item, "start": o_start, "end": o_end} for item in candidates]
    for pattern, item in TICKERS:  # orijinal metinde, tam büyük harfle
        hits += [{**item, "start": m.start(), "end": m.end()} for m in pattern.finditer(text)
                 if not _inside_all_caps_run(text, m.start(), m.end())]
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

    results = []
    for i, h in enumerate(hits):
        # Kesit sınırları: önceki ve sonraki başka şirket (bağlaçla bağlı ortak özneler atlanır).
        left_boundary = None
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
    headline = {r["company_key"]: r for r in match_companies(baslik)} if baslik else {}
    by_company: Dict[object, dict] = {}
    snippets_by_company: Dict[object, List[str]] = {}
    for r in match_companies(text, headline_end=len(baslik) if baslik else 0):
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
# 5. Tarih penceresi ve tekilleştirme
# =============================================================================
_DAYFIRST_RE = re.compile(r"^\s*\d{1,2}[./-]\d{1,2}[./-]\d{4}")
_UTM_RE = re.compile(r"(?<=[?&])(?:utm_[^=&#]*|fbclid|gclid)=[^&#]*&?")


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
def run_matching(as_of: Optional[object] = None, window_days: int = AGGREGATION_WINDOW_DAYS) -> pd.DataFrame:
    """Penceredeki haberleri okur ve eşleştirir; her (haber, şirket) için bir satır döndürür (en yeni önce)."""
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
