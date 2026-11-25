"""Portföy görünümleri: finansal skor, Birlesik (haber + finansal + bakiye) ve Temalar sayfaları (4_llm_dogrulama.py kullanır).

Finansal skor yöneticinin ews_financial.py modeliyle structured_data.parquet'ten (2_sql_to_parquet çıktısı) hesaplanır
ve kendi eşikleriyle (ews_financial.FIN_STAGE2_ESIK / FIN_STAGE3_ESIK: 50 / 80) stage'e çevrilir. Haber skorunun eşikleri
farklıdır (ews_olay.STAGE2_ESIK / STAGE3_ESIK: 25 / 65); Birlesik_Skor'da finansal skor haber ölçeğine çevrilir.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Optional

import pandas as pd

from ews.financial import (FIN_STAGE2_ESIK, FIN_STAGE3_ESIK, FINANCIAL_API_VERSION, build_financial_scores,
                           describe_points, financial_stage)
from ews.olay import ILGILI, STAGE2_ESIK, STAGE3_ESIK, SEVIYE_NO, STAGE_LABEL, STAGE_NO, fold, tip_seviyesi

logger = logging.getLogger("ews_llm")


# =============================================================================
# Birlesik: haber sinyali + finansal skor + bakiye
# =============================================================================
try:
    import ayarlar as _ayarlar
    STRUCTURED_PARQUET_PATH = _ayarlar.STRUCTURED_PARQUET_PATH
except ImportError:
    STRUCTURED_PARQUET_PATH = Path(__file__).resolve().parent.parent / "structured_data.parquet"
REQUIRED_FINANCIAL_API = 5   # beklenen ews/financial.py sürümü (üç katmanlı model)
KAPSAM_UYARI_ESIK = 0.5      # finansal veri kapsamı bunun altındaysa skor bir alt sınırdır; Not'ta işaretlenir
if FINANCIAL_API_VERSION < REQUIRED_FINANCIAL_API:
    raise ImportError(f"ews/financial.py eski sürüm (API {FINANCIAL_API_VERSION}, beklenen en az {REQUIRED_FINANCIAL_API}).")


def load_financials(path: Path = STRUCTURED_PARQUET_PATH) -> pd.DataFrame:
    """structured_data.parquet'ten ews.financial ile finansal skor tablosu (borçlu başına bir satır); dosya yoksa boş."""
    if not Path(path).exists():
        logger.warning("Finansal veri yok (%s); Birlesik sayfası yalnızca haber sinyaliyle üretilir.", path)
        return pd.DataFrame()
    return build_financial_scores(path)


def _haber_olcegi(fin_skor: float) -> float:
    """Finansal skoru haber skorunun ölçeğine çevirir: aynı stage bandında doğrusal (50-80 -> 25-65 gibi).

    Birlesik_Skor = max(haber, bu değer); böylece birleşik skor her zaman birleşik seviyenin bandında kalır.
    """
    stage = financial_stage(fin_skor)
    flo, fhi = {1: (0, FIN_STAGE2_ESIK), 2: (FIN_STAGE2_ESIK, FIN_STAGE3_ESIK), 3: (FIN_STAGE3_ESIK, 100)}[stage]
    nlo, nhi = {1: (0, STAGE2_ESIK), 2: (STAGE2_ESIK, STAGE3_ESIK), 3: (STAGE3_ESIK, 100)}[stage]
    value = round(nlo + (float(fin_skor) - flo) / (fhi - flo) * (nhi - nlo), 1)
    return min(value, nhi - 0.1) if stage < 3 else min(value, 100.0)


def _get(row, col: str):
    """Satır (Series ya da dict) yoksa None, varsa kolon değeri."""
    return None if row is None else row.get(col)


def _is_weak(f: Optional[dict], zayif_dscr: float) -> bool:
    """Zayıf finansal: DSCR eşiğin altında ya da özkaynak veya FAVÖK negatif."""
    if not f:
        return False
    dscr = f.get("DSCR_Degeri")
    return ((dscr is not None and pd.notna(dscr) and dscr < zayif_dscr)
            or "negatif" in (f.get("Özkaynak_Durumu"), f.get("FAVÖK_Durumu")))


def _rule(haber: int, finansal: int, birlesik: int, kar_kurali: bool) -> str:
    """Birleşik seviyenin nereden geldiği."""
    if birlesik == 1:
        return "risk sinyali yok"
    if kar_kurali:
        return "kâr düşüşü + zayıf finansal"
    if haber == birlesik == finansal:
        return "haber skoru ve finansal skor"
    return "haber skoru" if haber == birlesik else "finansal skor"


def _risk_matrisi(haber: int, finansal: Optional[int]) -> str:
    """2 Boyutlu Risk Matrisi etiketi (Finansal Dayanıklılık x Haber Şoku)."""
    f_etiket = {3: "Bilanço Kırılgan", 2: "Bilanço İzlemede", 1: "Bilanço Sağlam"}.get(finansal or 0, "Bilanço Yok")
    h_etiket = {3: "Kritik Haber Şoku", 2: "Yüksek Haber Sinyali", 1: "Haber Sakin"}.get(haber, "Haber Sakin")
    return f"{f_etiket} × {h_etiket}"


def build_combined_table(df: pd.DataFrame, companies: pd.DataFrame, fin: pd.DataFrame, zayif_dscr: float) -> pd.DataFrame:
    """Birlesik sayfası: portföydeki her şirket için tek satır, birleşik skor ve bakiyeye göre sıralı.

    Birleşik seviye haber ve finansal seviyenin, Birlesik_Skor haber skoru ile haber ölçeğine çevrilmiş finansal skorun
    yükseğidir (max kuralı; haber tarafı olay defterinden). Ayrıca 2 boyutlu Risk_Matrisi (Bilanço x Haber) üretilir.
    """
    news = companies.set_index("Şirket")
    finx = fin.set_index("Şirket") if len(fin) else pd.DataFrame()
    kar_dususu = set(df.loc[(df["LLM_Alaka"] == "odak") & (df["LLM_Olay_Seviyesi"] == "MODERATE")
                            & (df["LLM_Olay"] == "kar_dususu"), "Şirket"])
    rows = []
    for key in dict.fromkeys(list(finx.index) + list(news.index)):  # önce finansalı olanlar, sırası korunur
        n = news.loc[key] if key in news.index else None
        f = finx.loc[key].to_dict() if key in finx.index else None
        haber = STAGE_NO[n["LLM_Sinyali"]] if n is not None else 1
        finansal = financial_stage(_get(f, "Finansal_Skor"))
        kar_kurali = key in kar_dususu and haber < 2 and _is_weak(f, zayif_dscr)
        haber = 2 if kar_kurali else haber
        haber_skor, hesap = _get(n, "Haber_Skoru"), _get(n, "Skor_Hesabi")
        if kar_kurali:  # hesap metni de kuralın etkisini göstersin
            dscr = _get(f, "DSCR_Degeri")
            neden = f"DSCR {dscr:.2f}" if dscr is not None and pd.notna(dscr) and dscr < zayif_dscr else "negatif özkaynak/FAVÖK"
            haber_skor = max(haber_skor or 0, STAGE2_ESIK)
            hesap = f"{hesap}; kâr düşüşü + zayıf finansal ({neden}): haber Stage 2, skor en az {STAGE2_ESIK} → {haber_skor:g}"
        fin_skor = _get(f, "Finansal_Skor")
        birlesik = max(haber, finansal or 1)
        birlesik_skor = max(haber_skor or 0, 0 if fin_skor is None or pd.isna(fin_skor) else _haber_olcegi(fin_skor))
        rows.append({
            "Şirket": key, "Şirket_Adi": n["Şirket_Adi"] if n is not None else f.get("Finansal_Unvan"),
            "Birlesik_Seviye": STAGE_LABEL[birlesik], "Birlesik_Skor": birlesik_skor,
            "Birlesik_Kural": _rule(haber, finansal or 1, birlesik, kar_kurali),
            "Risk_Matrisi": _risk_matrisi(haber, finansal),
            "Toplam_Bakiye_TRY": _get(f, "Toplam_Bakiye_TRY"),
            "Haber_Sinyali": STAGE_LABEL[haber] if n is not None else None, "Haber_Skoru": haber_skor,
            "Skor_Hesabi": hesap,
            **{c: _get(n, c) for c in ("Risk_Olaylari", "En_Agir_Olay", "En_Agir_Durum", "En_Agir_Risk_Turu")},
            "Finansal_Skor": fin_skor, "Finansal_Seviye": STAGE_LABEL[finansal] if finansal else None,
            "Finansal_Veri_Kapsami": _get(f, "Finansal_Veri_Kapsami"),
            "Finansal_Puan_Detay": describe_points(f) if f else None, "DSCR_Degeri": _get(f, "DSCR_Degeri"),
            "Finansal_Tarih": _get(f, "Finansal_Tarih"),
            "Not": "; ".join((["haberi yok"] if n is None else []) + (["finansal veri yok"] if f is None else [])
                             + ([f"finansal kapsam %{100 * f['Finansal_Veri_Kapsami']:.0f}: skor alt sınır"]
                                if f and fin_skor is not None and pd.notna(fin_skor)
                                and f["Finansal_Veri_Kapsami"] < KAPSAM_UYARI_ESIK else [])),
        })
    return (pd.DataFrame(rows).sort_values(["Birlesik_Skor", "Toplam_Bakiye_TRY"], ascending=False, na_position="last")
            .reset_index(drop=True))


# =============================================================================
# Temalar: birden çok müşteriyi etkileyen ortak gelişmeler (Açık Uçlu Keşif + İkili Doğrulama)
# =============================================================================
TEMA_SURUMU = "3"            # önbellek anahtarının parçası: tema prompt'u değişince artırın
TEMA_MAKS_MADDE = 250        # LLM'e gönderilecek en fazla olay (en ağırları ve en yenileri önce)
TEMA_EMBED_ESIK = 0.55       # /v1/moderations (paraphrase-multilingual-mpnet-base-v2) anlamsal benzerlik eşiği
SYSTEM_TEMA = (
    "Sen bir bankanın kredi riski analistisin. Portföydeki şirketlerin haberlerinden birden fazla "
    "şirketi etkileyen ortak gelişmeleri, sektörel/makro şokları, düzenleyici kararları ya da ortak soruşturmaları "
    "keşfediyorsun. Yalnızca JSON döndür; açıklama yazma."
)
TEMA_PROMPT = """Aşağıda bankanın müşterisi olan şirketlerin son dönemdeki risk olayları yer alıyor.
Her satır: [no] şirket | olay tipi | özet | haberden alıntı

{maddeler}

---
Birden fazla şirketi (en az 2 FARKLI şirketi) etkileyen ORTAK gelişmeleri veya risk temalarını bul ve grupla.
Ortak gelişme şunlardan biri olabilir:
  1. Aynı kurum soruşturması, yargı süreci veya düzenleyici karar (Rekabet Kurumu, SPK, BDDK, MASAK, TMSF, vergi incelemesi vb.)
  2. Aynı sektörel/makro şok, maliyet/kur/talep baskısı, ihracat/gümrük engeli, tedarik veya enerji krizi
  3. Aynı ticari ağdaki zincirleme kriz, grev/sendika uyuşmazlığı veya ortak finansal darboğaz

Kurallar:
- Önceden tanımlı bir şablona bağlı kalma; yalnızca yukarıdaki satırlarda açıkça geçen somut ortak gelişmeleri çıkar, satırlarda olmayanı ekleme ve kendi bilgini koyma.
- Farklı haberler aynı konuyu farklı kelimelerle anlatabilir (ör. biri "soruşturma", diğeri "inceleme" veya "ceza"; biri "kur baskısı", diğeri "döviz artışı"). Bu yüzden her tema için maddelerde geçen anahtar kavramları "kanit_kelimeler" listesine (2-5 kelime/ifade) yaz.
- Bir tema en az 2 FARKLI şirketi kapsamalı; her madde en uygun tek bir temaya girmeli.

{{"temalar": [{{"tema": "<kısa ve net tema başlığı>", "aciklama": "<ortak gelişmeyi açıklayan tek cümle>", "kanit_kelimeler": ["<kelime1>", "<kelime2>", "<kelime3>"], "maddeler": [<no>, <no>]}}]}}
/no_think"""
TEMA_COLUMNS = ["Tema", "Aciklama", "Ortak_Ifade", "Sirket_Sayisi", "Toplam_Bakiye_TRY", "En_Yuksek_Haber_Sinyali",
                "Sirketler", "Olay_Tipleri", "Kanit", "_maddeler"]   # "_" ile başlayan kolon Excel'e yazılmaz (dashboard için)
_DOLGU = {"icin", "olan", "ile", "veya", "gibi", "daha", "kadar", "sonra", "once", "uzere", "nedeniyle", "sirket", "sirketi", "haber"}


def _extract_stems(ifade_list) -> list:
    """Kanıt kelimelerinden ya da ortak ifadeden anlamlı kelime köklerini (en az 3 harf, ilk 4-5 harf) çıkarır."""
    if isinstance(ifade_list, str):
        raw = ifade_list
    elif isinstance(ifade_list, (list, tuple)):
        raw = " ".join(str(x) for x in ifade_list)
    else:
        raw = str(ifade_list or "")
    stems = []
    for x in re.findall(r"[\w]+", raw):
        w = fold(x)
        if len(w) >= 3 and w not in _DOLGU:
            stems.append(w[:5] if len(w) >= 5 else w)
    return list(dict.fromkeys(stems))


def _mentions_any(stems: list, text_fold: str) -> bool:
    """Tema kanıt köklerinden en az biri maddenin başlık, özet veya alıntısında geçiyor mu?"""
    if not stems:
        return False
    return any(s in text_fold for s in stems)


def _theme_items(df: pd.DataFrame) -> pd.DataFrame:
    """Temaya aday olaylar: odak/ikincil haberde, alıntısı doğrulanmış, seviyeli (CRITICAL/HIGH/MODERATE).
    Eski sürümdeki drop_duplicates(['Şirket', 'LLM_Olay']) hatası giderildi: aynı şirketin aynı olay tipindeki
    farklı tarihli/konulu haberleri korunur; yalnızca aynı şirketin aynı özete sahip kopya haberleri tekilleştirilir."""
    level = df["LLM_Olay"].map(lambda t: SEVIYE_NO.get(tip_seviyesi(t), 0))
    sub = df[df["LLM_Alaka"].isin(ILGILI) & df["Alinti_Dogrulandi"].eq(True) & (level > 0)].copy()
    if sub.empty:
        return sub
    sub["_lv"] = level.loc[sub.index]
    sub["_ozet_k"] = sub["LLM_Ozet"].fillna("").map(lambda s: fold(s)[:35])
    return (sub.sort_values(["_lv", "Tarih"], ascending=[False, False], kind="stable")
            .drop_duplicates(["Şirket", "LLM_Olay", "_ozet_k"])
            .head(TEMA_MAKS_MADDE).reset_index(drop=True))


def build_themes(df: pd.DataFrame, companies: pd.DataFrame, fin: pd.DataFrame, llm, cache) -> pd.DataFrame:
    """Temalar sayfası: açık uçlu LLM keşfi + kelime kökü kümesi ve (varsa) /v1/moderations anlamsal benzerlik doğrulaması.

    1. LLM aday olaylardan beklenmedik ortak gelişmeleri gruplar ve her tema için `kanit_kelimeler` üretir.
    2. Kod her maddenin başlık/özet/alıntısında bu kanıt kelimelerden en az birinin kökünü arar.
    3. Kök eşleşmesi bulamazsa ve sunucuda `/v1/moderations` (`paraphrase-multilingual-mpnet-base-v2`) açıksa,
       maddenin tema açıklamasına anlamsal benzerliğini ölçer (>= TEMA_EMBED_ESIK ise kabul eder).
    4. En az 2 farklı şirketi kalan temalar toplam bakiye ve en yüksek haber sinyaliyle raporlanır.
    """
    items = _theme_items(df)
    if items.empty or items["Şirket"].nunique() < 2:
        return pd.DataFrame(columns=TEMA_COLUMNS)
    lines = "\n".join(
        f"[{i}] {r['Şirket_Adi'][:45]} | {r['LLM_Olay']} | {r['LLM_Ozet']} | \"{str(r['LLM_Alinti'])[:180]}\""
        for i, r in items.iterrows()
    )
    key = f"TEMA|{TEMA_SURUMU}|{hashlib.md5(lines.encode('utf-8')).hexdigest()[:12]}"
    from ews.schemas import validate_temalar

    if key not in cache:
        obj, status = llm.ask(SYSTEM_TEMA, TEMA_PROMPT.format(maddeler=lines), max_tokens=4000)
        if obj is None:
            logger.warning("Tema sorusu hata verdi, Temalar sayfası boş: %s", status)
            return pd.DataFrame(columns=TEMA_COLUMNS)
        cache.put(key, validate_temalar(obj).model_dump())

    bakiye = fin.set_index("Şirket")["Toplam_Bakiye_TRY"] if "Toplam_Bakiye_TRY" in fin else pd.Series(dtype=float)
    sinyal = companies.set_index("Şirket")["LLM_Sinyali"].map(STAGE_NO)
    used, rows = set(), []
    validated_themes = validate_temalar(cache.get(key) or {})
    for t in validated_themes.temalar:
        tema_adi = t.tema
        aciklama = t.aciklama
        kanit_list = t.kanit_kelimeler or ([tema_adi] if tema_adi else [])
        ortak_ifade_str = ", ".join(kanit_list)
        stems = _extract_stems(kanit_list)

        raw_nos = [i for i in t.maddeler if 0 <= i < len(items) and i not in used]
        verified_nos = []
        cat_desc = f"{tema_adi}: {aciklama} ({ortak_ifade_str})"
        for i in raw_nos:
            item_text = f"{items.at[i, 'Başlık']} {items.at[i, 'LLM_Ozet']} {items.at[i, 'LLM_Alinti']}"
            if _mentions_any(stems, fold(item_text)):
                verified_nos.append(i)
            elif hasattr(llm, "similarity"):
                sim_key = f"SIM|{hashlib.md5((item_text[:500] + '|' + cat_desc[:300]).encode('utf-8')).hexdigest()[:16]}"
                cached_sim = cache.get(sim_key) if cache is not None else None
                if isinstance(cached_sim, dict) and "score" in cached_sim:
                    score = float(cached_sim["score"])
                else:
                    scores = llm.similarity(item_text, [cat_desc], threshold=TEMA_EMBED_ESIK)
                    score = float(scores.get(cat_desc, 0.0)) if scores else -1.0
                    if scores is not None and cache is not None:
                        cache.put(sim_key, {"score": score})
                if score >= TEMA_EMBED_ESIK:
                    verified_nos.append(i)

        members = items.loc[verified_nos]
        if members["Şirket"].nunique() < 2:
            continue
        used.update(verified_nos)
        sirketler = members.drop_duplicates("Şirket")
        max_stage = sinyal.reindex(sirketler["Şirket"]).max()
        stage_no = int(max_stage) if pd.notna(max_stage) else 1
        rows.append({
            "Tema": tema_adi[:120],
            "Aciklama": aciklama[:300],
            "Ortak_Ifade": ortak_ifade_str[:120],
            "Sirket_Sayisi": len(sirketler),
            "Toplam_Bakiye_TRY": bakiye.reindex(sirketler["Şirket"]).sum(min_count=1),
            "En_Yuksek_Haber_Sinyali": STAGE_LABEL[stage_no],
            "Sirketler": "; ".join(sirketler["Şirket_Adi"]),
            "Olay_Tipleri": ", ".join(f"{k} ({v})" for k, v in members["LLM_Olay"].value_counts().items()),
            "Kanit": " | ".join(f"{m['Şirket_Adi']}: {m['Başlık']} ({m['Tarih']})" for _, m in members.iterrows()),
            "_maddeler": [{"k": m["Şirket"], "ad": m["Şirket_Adi"], "tip": m["LLM_Olay"], "tarih": m["Tarih"],
                           "baslik": m["Başlık"], "url": m["URL"], "alinti": m["LLM_Alinti"]} for _, m in members.iterrows()],
        })
    return pd.DataFrame(rows, columns=TEMA_COLUMNS).sort_values("Sirket_Sayisi", ascending=False).reset_index(drop=True)
