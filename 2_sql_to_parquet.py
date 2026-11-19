"""Portföy verisini Oracle'dan okur; structured_data.parquet ve portfolio_config.py dosyalarını üretir.

portfolio_config.py her şirket için haberde aranacak adları (alias) tutar. Eşleştirme bilerek geniştir
(3_eslestirme.py): bir haberin kaçması yanlış eşleşmeden pahalıdır, yanlış eşleşmeleri 4_llm_dogrulama.py'deki LLM
eler. Bu yüzden:
  - Bütün alias'lar aynı biçimde, tam kelime olarak eşleşir (strict / çapa yoktur).
  - Birden fazla şirketin ortak adı ("ABC GRUP") hepsine verilir; haberin hangisine ait olduğuna LLM karar verir.

Otomatik alias'lar: tam unvan, hukuki eki atılmış unvan, çekirdek isim ("ÖRNEK ÇELİK"), 2-3 kelimelik ön ekler
("ÖRNEK ÇELİK SANAYİ") ve unvanın ilk kelimesi ("ÖRNEK"). İlk kelime TEK_KELIME_YASAK'taysa ("TÜRKİYE", "AVRUPA") eklenmez.
Unvandan çıkmayan adlar (basının kullandığı kısaltma ya da marka, borsa kodu) ALIAS_RULES'ta elle yazılır.

Hakim ortaklar: payı ORTAK_ESIK'ten (%50) büyük ortağın adı da şirketin altına "partners" olarak yazılır; ortağın
haberi şirketin kaydı olarak eşleşir. Tüzel kişi ortak şirketler gibi (unvan, kısa ad, ilk kelime), gerçek kişi ortak
yalnızca tam adıyla aranır.

portfolio_config.py her çalışmada baştan yazılır ve elle düzenlenmez; kalıcı kararlar aşağıdaki ALIAS_RULES'tadır.
"""

from __future__ import annotations

import ast
import logging
import pprint
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

import ayarlar
from ews.text import normalize_text, turkish_lower

logger = logging.getLogger("ews_portfolio_config")
SCRIPT_DIR = Path(__file__).parent


# =============================================================================
# 1. Ayarlar (ayarlar.py üzerinden okunur)
# =============================================================================
ORACLE_USER = ayarlar.ORACLE_USER
ORACLE_PASSWORD = ayarlar.ORACLE_PASSWORD
ORACLE_HOST = ayarlar.ORACLE_HOST
ORACLE_PORT = ayarlar.ORACLE_PORT
ORACLE_SERVICE = ayarlar.ORACLE_SERVICE
ORACLE_SCHEMA = ayarlar.ORACLE_SCHEMA

# Portföy: toplam bakiyeye (nakit + gayrinakit) göre en büyük 100 karşı taraf ve finansal göstergeleri.
# temp8 = temp7 + trend etiketleri (yöneticinin sorgu.txt'si); son satır ews.financial'ın üç katmanlı modeli için.
# NVL, bakiyesi NULL olan müşterilerin sıralamada öne geçmesini engeller.
PORTFOLIO_QUERY = f"""SELECT part_id, counterparty_id, full_nm, cash_bal_bank, noncash_bal_bank,
                          cash_bal_bank_try, noncash_bal_bank_try, cash_bal_bank_usd, noncash_bal_bank_usd,
                          mmzc_cash_tl, mmzc_cash_yp, mmzc_noncash_tl, mmzc_noncash_yp,
                          bus_activity_field_code, bus_fld_code, statement_dt, equity_try, ebitda_try,
                          net_fx_pos_try, net_working_capital_try, external_sales_try, domestic_sales_try,
                          total_sales_try, yp_debt_try, total_debt_try, totalcurrentliabilities_try,
                          cash_in_banks_try, leverage_try, total_assets_try, interestexpense_try,
                          dscrebitda_try, liquid_assets_try, bank_fx_deposit,
                          prev_statement_dt, equity_trend, ebitda_trend
                   FROM {ORACLE_SCHEMA}.temp8
                   ORDER BY NVL(cash_bal_bank, 0) + NVL(noncash_bal_bank, 0) DESC NULLS LAST
                   FETCH FIRST 100 ROWS ONLY"""

# Portföy şirketlerinin ortakları; payı ORTAK_ESIK'ten büyük olanlar haber eşleştirmesine eklenir.
PARTNER_QUERY = f"""SELECT company_cust_id, prtnr_cust_id, prtnr_nm, prtnr_prcntg
                   FROM {ORACLE_SCHEMA}.company_partners
                   WHERE company_cust_id IN ({{ids}})"""
ORTAK_ESIK = 50.0                # yüzde; tablo pay oranını 0-1 arası tutuyorsa kod bunu fark edip çevirir
# Bu parçalardan biri geçen ortak tüzel kişi sayılır (yoksa gerçek kişi: yalnızca tam adıyla aranır).
TUZEL_IPUCU = {"holding", "bv", "nv", "ag", "sa", "spa", "srl", "sarl", "fund", "fonu", "kooperatifi", "vakfi",
               "bankasi", "bank", "group", "grup", "yatirim"}

STRUCTURED_PARQUET_PATH = ayarlar.STRUCTURED_PARQUET_PATH
CONFIG_PATH = ayarlar.PORTFOLIO_CONFIG_PATH
PREFIX_WORD_COUNTS = (2, 3)      # unvanın ilk 2 ve 3 kelimesi alias olur
MIN_SINGLE_WORD_LENGTH = 3       # daha kısa ilk kelime aday sayılmaz

# Unvan ekleri (normalize biçimde karşılaştırılır; "A.Ş." noktadan bölünür: "a", "s").
LEGAL_PARTS = {"anonim", "sirketi", "sirket", "as", "a", "s", "limited", "ltd", "sti", "tas", "t", "koll", "kollektif",
               "komandit", "kom", "inc", "llc", "gmbh", "plc", "corp", "co", "ortakligi", "ortaklik"}
COMMERCE_PARTS = {"sanayi", "sanayii", "san", "ticaret", "tic", "ve", "ith", "ihr", "ithalat", "ihracat",
                  "pazarlama", "paz", "dis", "ic", "hizmetleri", "hiz", "uretim", "imalat"}


# =============================================================================
# 2. Kalıcı alias kararları
# =============================================================================
# Unvanın ilk kelimesi otomatik aranır ("ABC", "ÖRNEK"). Bu listedekiler hariç: sıradan kelime ya da yer
# adıdırlar ve on binlerce yanlış eşleşme getirirler (3 aylık arşivde "TÜRKİYE" 14.739, "TÜRK" 5.206, "AVRUPA" 3.714).
# Bu şirketler unvanın ilk 2-3 kelimesiyle yine bulunur ("AVRUPA ÖRNEK OTOYOLU", "PETROL ÖRNEK"). Portföye sıradan bir
# kelimeyle başlayan şirket girerse buraya eklenir (config'in sonundaki "otomatik eklenen tek kelimeler" listesine bakın).
TEK_KELIME_YASAK = {"TÜRKİYE", "TÜRK", "AVRUPA", "PETROL", "YAPI", "ASİL", "KOÇ", "HAYAT", "SANAT", "ÇANAKKALE", "ORDU",
                    "KAYSERİ", "TEKİRDAĞ", "STAR", "İSTANBUL", "ANKARA", "İZMİR", "AK"}

# Unvandan çıkmayan, basının kullandığı adlar. Anahtar counterparty_id; şirket başına tek satır.
#   "add": eklenecek adlar · "remove": otomatik adlardan çıkarılacaklar · "tickers": borsa kodu (yalnızca büyük harfle)
# Yanlış eşleşmeyi LLM eler; bu yüzden "remove" ancak bir ad çok sayıda gereksiz soru getiriyorsa kullanılır
# (3_llm_dogrulama'nın Alias_Onerisi sayfası). Bankadaki gerçek liste paylaşılmadı; aşağıdakiler örnektir.
ALIAS_RULES: Dict[int, dict] = {
    100001: {"add": ["ÖRNEK MARKA"]},               # unvandan çıkmayan, basının kullandığı ad
    100002: {"tickers": ["ABCD"]},                  # borsa kodu
    100003: {"remove": ["ÖRNEK"]},                  # çok yanlış eşleşme getiren otomatik ad
}


# =============================================================================
# 3. Oracle
# =============================================================================
class CredentialsNotFoundError(RuntimeError):
    """Oracle şifresi tanımlı değil; bağlantı denenmeden durulur."""


def _check_temp8_freshness(connection, df: pd.DataFrame) -> None:
    """temp8 tablosunun ne zaman oluşturulduğunu (LAST_DDL_TIME) ve part_id güncelliğini kontrol edip kullanıcıyı uyarır."""
    try:
        ddl_query = """
        SELECT created, last_ddl_time
        FROM all_objects
        WHERE UPPER(owner) = UPPER(:owner) AND UPPER(object_name) = 'TEMP8' AND object_type = 'TABLE'
        """
        meta = pd.read_sql(ddl_query, con=connection, params={"owner": ORACLE_SCHEMA})
        if not meta.empty and pd.notna(meta.iloc[0].get("LAST_DDL_TIME")):
            ddl_dt = pd.to_datetime(meta.iloc[0]["LAST_DDL_TIME"])
            age_days = (datetime.now() - ddl_dt).days
            logger.info("Oracle %s.TEMP8 tablosu son güncelleme tarihi: %s (%d gün önce).",
                        ORACLE_SCHEMA, ddl_dt.strftime("%Y-%m-%d %H:%M"), age_days)
            if age_days > ayarlar.STALE_DATA_WARN_DAYS:
                logger.warning(
                    "DİKKAT: %s.TEMP8 tablosu %d gün önce oluşturulmuş! SQL Developer'da sorgu.txt çalıştırılmamış olabilir.",
                    ORACLE_SCHEMA, age_days,
                )
    except Exception as exc:
        logger.warning("Oracle %s.TEMP8 güncellik kontrolü yapılamadı: %s", ORACLE_SCHEMA, exc)
    cols = {c.lower(): c for c in df.columns}
    if "part_id" in cols and not df.empty:
        max_part = df[cols["part_id"]].dropna().max()
        logger.info("Okunan portföydeki en güncel dönem (part_id): %s", max_part)


def fetch_data() -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Portföy ve ortaklık sorgularını çalıştırır: (portföy, ortaklar).

    Şifre boşsa kullanıcıya sorar; yine boşsa bağlanmaz: boş şifreyle denemeler hesabı kilitleyebilir (ORA-28000).
    """
    password = ORACLE_PASSWORD or ayarlar.get_oracle_password(interactive=True)
    if not password:
        raise CredentialsNotFoundError("Oracle şifresi girilmedi; ayarlar.py dosyasını kontrol edin veya şifreyi girin.")
    if "<ORACLE_HOST>" in ORACLE_HOST:
        raise CredentialsNotFoundError("ayarlar.py içindeki ORACLE_HOST (<ORACLE_HOST>) henüz doldurulmamış.")
    import oracledb

    dsn = oracledb.makedsn(ORACLE_HOST, ORACLE_PORT, service_name=ORACLE_SERVICE)
    with oracledb.connect(user=ORACLE_USER, password=password, dsn=dsn) as connection:
        df = pd.read_sql(PORTFOLIO_QUERY, con=connection)
        _check_temp8_freshness(connection, df)
        ids = ", ".join(str(int(i)) for i in pd.to_numeric(df["COUNTERPARTY_ID" if "COUNTERPARTY_ID" in df else "counterparty_id"],
                                                            errors="coerce").dropna().unique())
        partners = pd.read_sql(PARTNER_QUERY.format(ids=ids), con=connection) if ids else pd.DataFrame()
    logger.info("Oracle: %s portföy satırı, %s ortaklık satırı okundu (kullanıcı %s).", len(df), len(partners), ORACLE_USER)
    return df, partners


def majority_partners(partners: Optional[pd.DataFrame]) -> Dict[object, List[dict]]:
    """Şirket -> payı ORTAK_ESIK'ten büyük ortaklar [{"name", "share"}] (pay yüzde olarak)."""
    if partners is None or partners.empty:
        return {}
    p = partners.rename(columns=str.lower)
    raw = pd.to_numeric(p["prtnr_prcntg"], errors="coerce")
    fraction = raw.max() <= 1  # tablo payı 0-1 arası tutuyor
    logger.info("Ortaklık tablosu: pay oranı %s okundu (en yüksek değer %s).", "0-1 arası" if fraction else "yüzde olarak", raw.max())
    p = p.assign(_share=raw * 100 if fraction else raw)
    p = p[(p["_share"] > ORTAK_ESIK) & p["prtnr_nm"].notna()
          & (pd.to_numeric(p["prtnr_cust_id"], errors="coerce") != pd.to_numeric(p["company_cust_id"], errors="coerce"))]
    out: Dict[object, List[dict]] = defaultdict(list)
    for r in p.sort_values("_share", ascending=False).drop_duplicates(["company_cust_id", "prtnr_cust_id"]).to_dict("records"):
        out[_cust_id(r["company_cust_id"])].append({"name": clean_alias(r["prtnr_nm"]), "share": round(float(r["_share"]), 1)})
    return out


# =============================================================================
# 4. Unvandan alias üretimi
# =============================================================================
_STRIP_CHARS_RE = re.compile(r"[\"“”‘’'«»()\[\]]")  # haber metinlerinde bulunmayan tırnak ve parantezler


_YASAK = {normalize_text(w).strip() for w in TEK_KELIME_YASAK}


def clean_alias(text: str) -> str:
    return re.sub(r"\s+", " ", _STRIP_CHARS_RE.sub(" ", str(text))).strip()


def _parts(word: str) -> List[str]:
    """Kelimeyi noktalamadan bölüp normalize parçalara ayırır ("TİC.A.Ş." -> ["tic", "a", "s"])."""
    return [p for p in re.split(r"[.,;:/]+", normalize_text(word)) if p]


def _is_suffix(word: str, legal_only: bool = False) -> bool:
    """Kelime yalnızca hukuki tür (legal_only) ya da hukuki/ticari ek parçalarından mı oluşuyor?"""
    allowed = LEGAL_PARTS if legal_only else LEGAL_PARTS | COMMERCE_PARTS
    parts = _parts(word)
    return bool(parts) and all(p in allowed for p in parts)


def strip_legal_form(name: str) -> str:
    """Sondaki hukuki türü atar ("... SANAYİ VE TİCARET ANONİM ŞİRKETİ" -> "... SANAYİ VE TİCARET")."""
    words = clean_alias(name).split()
    while words and (_is_suffix(words[-1], legal_only=True)
                     or (_is_suffix(words[-1]) and any(p in LEGAL_PARTS for p in _parts(words[-1])))):
        words.pop()
    return " ".join(words)


def core_name(short: str) -> str:
    """Sondaki ticari ekleri atarak en az iki kelimelik çekirdek isim ("ÖRNEK ÇELİK SANAYİ VE TİCARET" -> "ÖRNEK ÇELİK")."""
    words = short.split()
    while len(words) > 2 and _is_suffix(words[-1]):
        words.pop()
    return " ".join(words)


def first_word(short: str, core: str) -> Optional[str]:
    """Tek kelimelik ad adayı: unvanın ilk kelimesi ya da tireden önceki parça ("ABC-TÜRKİYE ..." -> "ABC")."""
    words = (core or short).split()
    if not words:
        return None
    word = next((p for p in re.split(r"[-–—]", words[0]) if p), "")
    ok = re.fullmatch(r"\w+", word) and len(turkish_lower(word)) >= MIN_SINGLE_WORD_LENGTH and not _is_suffix(word)
    return word if ok else None


def auto_aliases(full: str) -> Tuple[List[str], Optional[str]]:
    """Unvandan otomatik alias'lar ve eklenen tek kelime (yoksa ya da TEK_KELIME_YASAK'taysa None)."""
    short = strip_legal_form(full)
    core = core_name(short)
    aliases = [full] + [a for a in (short, core) if len(a.split()) >= 2 and not all(_is_suffix(w) for w in a.split())]
    words = short.split()
    aliases += [" ".join(words[:k]) for k in PREFIX_WORD_COUNTS if k < len(words) and not _is_suffix(words[k - 1])]
    word = first_word(short, core)
    if word and normalize_text(word).strip() not in _YASAK:
        return aliases + [word], word
    return aliases, None


def _unique(items: Sequence[str]) -> List[str]:
    """Normalize biçime göre tekrarları atar, ilk yazımı tutar."""
    seen, out = set(), []
    for item in items:
        key = normalize_text(item).strip()
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def is_legal_entity(name: str) -> bool:
    """Ortak tüzel kişi mi? Unvanında hukuki tür ya da TUZEL_IPUCU parçası geçiyorsa evet."""
    return any(p in LEGAL_PARTS or p in TUZEL_IPUCU for w in clean_alias(name).split() for p in _parts(w))


def partner_aliases(name: str) -> List[str]:
    """Hakim ortağın aranacak adları: tüzel kişide şirketlerle aynı kural, gerçek kişide yalnızca tam ad (en az 2 kelime)."""
    if is_legal_entity(name):
        return _unique(auto_aliases(name)[0])
    return [name] if len(name.split()) >= 2 else []


# =============================================================================
# 5. Config üretimi
# =============================================================================
def duplicate_rule_keys(source_path: Path = Path(__file__)) -> List[object]:
    """ALIAS_RULES'ta birden fazla satırda yazılmış anahtarlar (Python ilkini sessizce attığı için kaynaktan okunur)."""
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) else None)
        if getattr(target, "id", None) == "ALIAS_RULES" and isinstance(node.value, ast.Dict):
            keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
            return sorted({k for k in keys if keys.count(k) > 1}, key=str)
    return []


def _is_old_ticker(item) -> bool:
    return isinstance(item, (tuple, list)) and len(item) == 2 and item[1] == "ticker"


def _texts(rule: dict, field: str) -> List[str]:
    """Kuraldaki adlar. Eski biçimdeki ("ad", "loose" | "strict" | "ticker") çiftleri de kabul edilir."""
    items = list(rule.get(field, []))
    if field == "add":
        items = [i for i in items if not _is_old_ticker(i)]
    if field == "tickers":
        items += [i for i in rule.get("add", []) if _is_old_ticker(i)]
    names = [i[0] if isinstance(i, (tuple, list)) else i for i in items]
    return [clean_alias(n) for n in names if isinstance(n, str) and n.strip()]


def _cust_id(value) -> Optional[object]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return str(value).strip() or None


def build_companies(df: pd.DataFrame, rules: Optional[Dict[object, dict]] = None,
                    partners: Optional[pd.DataFrame] = None) -> Tuple[dict, List[str], List[str]]:
    """Portföy verisinden COMPANIES sözlüğü, denetim satırları ve otomatik tek kelime satırları üretir (dosya yazmaz).

    partners verilirse (ortaklık tablosu) payı ORTAK_ESIK'ten büyük ortaklar şirketin "partners" alanına yazılır.
    """
    rules = ALIAS_RULES if rules is None else rules
    owners_of = majority_partners(partners)
    audit = [f"UYARI ALIAS_RULES {k}: anahtar birden fazla satırda; yalnızca sonuncusu geçerli"
             for k in (duplicate_rule_keys() if rules is ALIAS_RULES else [])]
    cols = {c.lower(): c for c in df.columns}
    if "counterparty_id" not in cols or "full_nm" not in cols:
        raise ValueError(f"counterparty_id ve full_nm kolonları gerekli. Kolonlar: {list(df.columns)}")
    ticker_col = next((cols[c] for c in ("bist_ticker", "ticker", "bist_kodu", "hisse_kodu") if c in cols), None)
    vkn_col = next((cols[c] for c in ("tax_no", "vkn", "vergi_no", "tax_identity_no") if c in cols), None)

    companies, candidates = {}, []
    for row in df[df[cols["full_nm"]].notna()].to_dict("records"):
        key = _cust_id(row[cols["counterparty_id"]])
        name = clean_alias(row[cols["full_nm"]])
        if key is None or key in companies:
            audit.append(f"UYARI {name}: counterparty_id boş ya da tekrar ediyor; atlandı")
            continue
        aliases, word = auto_aliases(name)
        rule = rules.get(key) if isinstance(rules.get(key), dict) else {}
        removed = {normalize_text(a) for a in _texts(rule, "remove")}
        aliases = _unique([a for a in aliases if normalize_text(a) not in removed] + _texts(rule, "add"))
        ticker = str(row[ticker_col]).strip().upper() if ticker_col and pd.notna(row[ticker_col]) else ""
        tickers = list(dict.fromkeys(t.upper() for t in [ticker] + _texts(rule, "tickers") if t))
        entry = {"name": name, "aliases": aliases, "tickers": tickers}
        if vkn_col and pd.notna(row.get(vkn_col)):
            vkn_raw = re.sub(r"\D", "", str(row[vkn_col]).split(".")[0])
            if vkn_raw:
                entry["vkn"] = vkn_raw.zfill(10) if len(vkn_raw) <= 10 else vkn_raw
        companies[key] = entry
        own = {normalize_text(a) for a in aliases}
        for o in owners_of.get(key, []):
            o_aliases = [a for a in partner_aliases(o["name"]) if normalize_text(a) not in own]
            if o_aliases:
                companies[key].setdefault("partners", []).append({**o, "aliases": o_aliases})
        if word and normalize_text(word) not in removed:  # gözden geçirme listesi (config'in sonunda)
            candidates.append(f"{word:<16} {name}")

    audit += [f"UYARI ALIAS_RULES {k}: portföyde yok, kural uygulanmadı" for k in rules if _cust_id(k) not in companies]
    owners: Dict[str, List[str]] = defaultdict(list)  # ortak adlar: hepsine verilir, bilgi için listelenir
    for c in companies.values():
        for a in c["aliases"][1:]:
            owners[normalize_text(a)].append(c["name"])
    audit += [f"ORTAK '{a}': {', '.join(names)}" for a, names in sorted(owners.items()) if len(names) > 1]
    return companies, audit, candidates


def load_previous_config(path: Path = CONFIG_PATH) -> dict:
    """Önceki portfolio_config.py'deki COMPANIES sözlüğü (çalıştırmadan okunur); okunamazsa boş."""
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) == "COMPANIES")
        return ast.literal_eval(node.value)
    except (OSError, SyntaxError, ValueError, StopIteration):
        return {}


def config_diff(previous: dict, current: dict) -> List[str]:
    """Önceki config'e göre eklenen/çıkan şirket ve adlar (izleme kapsamı değiştiği için her çalışmada gözden geçirilir)."""
    lines = []
    for key in sorted(set(previous) | set(current), key=str):
        old, new = previous.get(key), current.get(key)
        if old is None or new is None:
            lines.append(f"{'+' if old is None else '-'} şirket: {(new or old).get('name')} ({key})")
            continue
        for field in ("aliases", "tickers"):
            before = set(old.get(field, [])) | (set(old.get("strict_aliases", [])) if field == "aliases" else set())
            after = set(new.get(field, []))
            lines += [f"+ {new['name']}: {a}" for a in sorted(after - before)]
            lines += [f"- {new['name']}: {a}" for a in sorted(before - after)]
    return lines


def render_config(companies: dict, audit: List[str], candidates: List[str], meta: Dict[str, object]) -> str:
    bar = "# " + "=" * 76
    lines = ['"""', "Kurumsal Kredi Riski Erken Uyarı Sistemi (EWS) - Portföy Konfigürasyonu", "",
             "OTOMATİK ÜRETİLMİŞTİR - ELLE DÜZENLEMEYİN. 2_sql_to_parquet.py her çalıştığında baştan yazılır;",
             "kalıcı alias kararları için 2_sql_to_parquet.py içindeki ALIAS_RULES'u kullanın.", ""]
    lines += [f"{k}: {v}" for k, v in meta.items()] + ['"""', "", "# fmt: off",
              "COMPANIES = " + pprint.pformat(companies, width=110, sort_dicts=False, indent=1), "# fmt: on", ""]
    lines += [bar, "# DENETİM", bar] + ([f"# {a}" for a in audit] or ["# (kayıt yok)"])
    if candidates:
        lines += ["", bar, "# OTOMATİK EKLENEN TEK KELİMELER (unvanın ilk kelimesi)", bar,
                  "# Sıradan bir kelime ya da yer adıysa (AVRUPA, BÜYÜK...) 2_sql_to_parquet.py'deki TEK_KELIME_YASAK'a",
                  "# ekleyin: her geçişi bir LLM sorusu demektir. Şirket yine unvanın ilk 2-3 kelimesiyle bulunur.", "#"]
        lines += [f"#   {c}" for c in candidates]
    return "\n".join(lines) + "\n"


def create_portfolio_config(df: pd.DataFrame, partners: Optional[pd.DataFrame] = None, config_path: Path = CONFIG_PATH) -> dict:
    """Config'i üretir, doğrular ve yazar; önceki config'e göre değişiklikleri denetim bölümüne ekler."""
    companies, audit, candidates = build_companies(df, partners=partners)
    changes = config_diff(load_previous_config(config_path), companies)
    meta = {"Üretim zamanı": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "Şirket sayısı": len(companies),
            "Önceki config'e göre değişiklik": len(changes), "Otomatik tek kelime": len(candidates),
            "Hakim ortağı eklenen şirket": sum(bool(c.get("partners")) for c in companies.values())}
    content = render_config(companies, audit + [f"DEĞİŞİKLİK {c}" for c in changes], candidates, meta)
    ast.literal_eval(content.split("COMPANIES = ", 1)[1].split("\n# fmt: on", 1)[0])  # bozuk config yazılmasın
    Path(config_path).write_text(content, encoding="utf-8")
    logger.info("Config yazıldı: %s (%s şirket, %s değişiklik).", config_path, len(companies), len(changes))
    for line in audit:
        if line.startswith("UYARI"):
            logger.warning(line)
    return companies


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    portfolio_df, partners_df = fetch_data()
    portfolio_df.to_parquet(STRUCTURED_PARQUET_PATH, index=False, engine="pyarrow")
    logger.info("Parquet yazıldı: %s", STRUCTURED_PARQUET_PATH)
    create_portfolio_config(portfolio_df, partners_df)
