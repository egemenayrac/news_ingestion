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

from ews_financial import (FIN_STAGE2_ESIK, FIN_STAGE3_ESIK, FINANCIAL_API_VERSION, build_financial_scores,
                           describe_points, financial_stage)
from ews_olay import ILGILI, STAGE2_ESIK, STAGE3_ESIK, SEVIYE_NO, STAGE_LABEL, STAGE_NO, fold, tip_seviyesi

logger = logging.getLogger("ews_llm")


# =============================================================================
# Birlesik: haber sinyali + finansal skor + bakiye
# =============================================================================
STRUCTURED_PARQUET_PATH = Path(__file__).resolve().parent / "structured_data.parquet"
REQUIRED_FINANCIAL_API = 5   # beklenen ews_financial.py sürümü (üç katmanlı model)
KAPSAM_UYARI_ESIK = 0.5      # finansal veri kapsamı bunun altındaysa skor bir alt sınırdır; Not'ta işaretlenir
if FINANCIAL_API_VERSION < REQUIRED_FINANCIAL_API:
    raise ImportError(f"ews_financial.py eski sürüm (API {FINANCIAL_API_VERSION}, beklenen en az {REQUIRED_FINANCIAL_API}).")


def load_financials(path: Path = STRUCTURED_PARQUET_PATH) -> pd.DataFrame:
    """structured_data.parquet'ten ews_financial ile finansal skor tablosu (borçlu başına bir satır); dosya yoksa boş."""
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


def build_combined_table(df: pd.DataFrame, companies: pd.DataFrame, fin: pd.DataFrame, zayif_dscr: float) -> pd.DataFrame:
    """Birlesik sayfası: portföydeki her şirket için tek satır, birleşik skor ve bakiyeye göre sıralı.

    Birleşik seviye haber ve finansal seviyenin, Birlesik_Skor haber skoru ile haber ölçeğine çevrilmiş finansal skorun
    yükseğidir (max kuralı; haber tarafı olay defterinden). Ölçek çevrimi sayesinde Birlesik_Skor birleşik seviyeyle
    tutarlıdır. Ek kural:
    "odak" haberde kâr düşüşü varken finansallar zayıfsa haber sinyali Stage 2'ye (skoru en az STAGE2_ESIK'e) çıkar: kâr düşüşünün ağırlığına bilanço karar verir (kaldıraçlı şirkette önemlidir, nakdi bol
    şirkette değildir).
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
            "Birlesik_Seviye": STAGE_LABEL[birlesik], "Birlesik_Skor": birlesik_skor, "Birlesik_Kural": _rule(haber, finansal or 1, birlesik, kar_kurali),
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
# Temalar: birden çok müşteriyi etkileyen ortak gelişmeler
# =============================================================================
TEMA_SURUMU = "2"            # önbellek anahtarının parçası: tema prompt'u değişince artırın
TEMA_MAKS_MADDE = 300        # LLM'e gönderilecek en fazla olay (en ağırları önce)
SYSTEM_TEMA = ("Sen bir bankanın kredi riski analistisin. Portföydeki şirketlerin olaylarından birden fazla "
               "şirketi etkileyen ortak temaları buluyorsun. Yalnızca JSON döndür; açıklama yazma.")
TEMA_PROMPT = """Aşağıda bankanın müşterisi olan şirketlerin son dönemdeki olayları var.
Her satır: [no] şirket | olay tipi | özet | haberden alıntı

{maddeler}

---
Birden fazla şirketi etkileyen ORTAK bir gelişme varsa grupla. Ortak gelişme somut olmalı: aynı soruşturma ya da
dava, aynı kurum kararı, aynı vergi ya da düzenleme, aynı fiyat ya da talep şoku. Kurallar:
- Yalnızca satırlarda YAZAN bilgiyi kullan. Satırlarda geçmeyen bir olayı, kurumu, vergiyi ya da kavramı tema adına
  ya da açıklamaya koyma; kendi bilgini ekleme.
- Temadaki her madde ortak gelişmeyi kendi özetinde ya da alıntısında açıkça anmalı. Yalnızca aynı sektörde olmak ya
  da aynı olay tipine sahip olmak tema değildir.
- "ortak_ifade": temadaki bütün maddelerde geçen, ortak gelişmeyi gösteren kısa ifade; maddelerdeki kelimelerle yaz.
- Bir tema en az 2 FARKLI şirketi kapsamalı; her madde en fazla bir temaya girer. Emin değilsen maddeyi dışarıda
  bırak. Ortak gelişme yoksa boş liste döndür.

{{"temalar": [{{"tema": "<kısa ad>", "aciklama": "<tek cümle>", "ortak_ifade": "<kısa ifade>", "maddeler": [<no>, <no>]}}]}}
/no_think"""
TEMA_COLUMNS = ["Tema", "Aciklama", "Ortak_Ifade", "Sirket_Sayisi", "Toplam_Bakiye_TRY", "En_Yuksek_Haber_Sinyali",
                "Sirketler", "Olay_Tipleri", "Kanit", "_maddeler"]   # "_" ile başlayan kolon Excel'e yazılmaz (dashboard için)
_DOLGU = {"icin", "olan", "ile", "veya", "gibi", "daha", "kadar", "sonra", "once", "uzere", "nedeniyle"}


def _mentions(ifade: str, text: str) -> bool:
    """Ortak ifadenin anlamlı kelimelerinden en az biri (ilk 5 harfiyle, ek farkı yok sayılır) metinde geçiyor mu?"""
    words = [w for w in (fold(x) for x in re.findall(r"[\w-]+", str(ifade))) if len(w) >= 4 and w not in _DOLGU]
    return bool(words) and any(w[:5] in text for w in words)


def _theme_items(df: pd.DataFrame) -> pd.DataFrame:
    """Temaya aday olaylar: odak/ikincil haberde, alıntısı doğrulanmış, seviyeli (kapanış haberleri dahil).
    Şirket ve olay tipi başına bir madde; en ağırları ve en eskileri önce, en fazla TEMA_MAKS_MADDE."""
    level = df["LLM_Olay"].map(lambda t: SEVIYE_NO.get(tip_seviyesi(t), 0))
    return (df[df["LLM_Alaka"].isin(ILGILI) & df["Alinti_Dogrulandi"].eq(True) & (level > 0)]
            .assign(_lv=level).sort_values(["_lv", "Tarih"], ascending=[False, True], kind="stable")
            .drop_duplicates(["Şirket", "LLM_Olay"]).head(TEMA_MAKS_MADDE).reset_index(drop=True))


def build_themes(df: pd.DataFrame, companies: pd.DataFrame, fin: pd.DataFrame, llm, cache) -> pd.DataFrame:
    """Temalar sayfası: tek LLM sorusuyla olaylar temalara ayrılır.

    LLM maddeleri numaralarıyla gruplar ve her tema için bir "ortak ifade" yazar. Kod numaraları doğrular, her maddeyi
    tek temaya koyar, ortak ifadeyi özetinde ya da alıntısında anmayan maddeyi temadan çıkarır (LLM'in maddelerde
    olmayan bir konu uydurmasına karşı) ve 2'den az farklı şirketi kalan temayı atar. Tema başına şirket sayısı, toplam bakiye ve en yüksek haber sinyali
    hesaplanır. LLM hata verirse sayfa boş kalır (cevap önbelleğe yazılmaz, sonraki koşuda tekrar sorulur).
    """
    items = _theme_items(df)
    if items["Şirket"].nunique() < 2:
        return pd.DataFrame(columns=TEMA_COLUMNS)
    lines = "\n".join(f"[{i}] {r['Şirket_Adi'][:45]} | {r['LLM_Olay']} | {r['LLM_Ozet']} | \"{str(r['LLM_Alinti'])[:200]}\""
                      for i, r in items.iterrows())
    key = f"TEMA|{TEMA_SURUMU}|{hashlib.md5(lines.encode('utf-8')).hexdigest()[:12]}"
    if key not in cache:
        obj, status = llm.ask(SYSTEM_TEMA, TEMA_PROMPT.format(maddeler=lines), max_tokens=4000)
        if obj is None:
            logger.warning("Tema sorusu hata verdi, Temalar sayfası boş: %s", status)
            return pd.DataFrame(columns=TEMA_COLUMNS)
        cache.put(key, obj)

    bakiye = fin.set_index("Şirket")["Toplam_Bakiye_TRY"] if "Toplam_Bakiye_TRY" in fin else pd.Series(dtype=float)
    sinyal = companies.set_index("Şirket")["LLM_Sinyali"].map(STAGE_NO)
    used, rows = set(), []
    for t in cache.get(key).get("temalar") or []:
        ifade = str(t.get("ortak_ifade") or t.get("tema") or "")
        nos = [int(i) for i in t.get("maddeler") or [] if str(i).strip().isdigit() and int(i) < len(items) and int(i) not in used]
        nos = [i for i in nos if _mentions(ifade, fold(f"{items.at[i, 'LLM_Ozet']} {items.at[i, 'LLM_Alinti']}"))]
        members = items.loc[nos]
        if members["Şirket"].nunique() < 2:
            continue
        used.update(nos)
        sirketler = members.drop_duplicates("Şirket")
        rows.append({
            "Tema": str(t.get("tema", ""))[:120], "Aciklama": str(t.get("aciklama", ""))[:300], "Ortak_Ifade": ifade[:120],
            "Sirket_Sayisi": len(sirketler), "Toplam_Bakiye_TRY": bakiye.reindex(sirketler["Şirket"]).sum(min_count=1),
            "En_Yuksek_Haber_Sinyali": STAGE_LABEL[int(sinyal.reindex(sirketler["Şirket"]).max())],
            "Sirketler": "; ".join(sirketler["Şirket_Adi"]),
            "Olay_Tipleri": ", ".join(f"{k} ({v})" for k, v in members["LLM_Olay"].value_counts().items()),
            "Kanit": " | ".join(f"{m['Şirket_Adi']}: {m['Başlık']} ({m['Tarih']})" for _, m in members.iterrows()),
            "_maddeler": [{"k": m["Şirket"], "ad": m["Şirket_Adi"], "tip": m["LLM_Olay"], "tarih": m["Tarih"],
                           "baslik": m["Başlık"], "url": m["URL"], "alinti": m["LLM_Alinti"]} for _, m in members.iterrows()],
        })
    return pd.DataFrame(rows, columns=TEMA_COLUMNS).sort_values("Sirket_Sayisi", ascending=False).reset_index(drop=True)
