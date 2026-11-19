"""Excel sayfaları: Şirketler, Olay_Defteri, Kayıtlar, Kontrol, Kontrol_Etiketleri, Alias, Alias_Onerisi (4_llm_dogrulama.py kullanır).

Birlesik ve Temalar sayfaları ews_portfoy.py'dedir.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd

from ews.defter import Olay, news_score
from ews.olay import ILGILI, LLM_COLUMNS, RED, STAGE_LABEL, STAGE_NO, answered, fold, tip_seviyesi

logger = logging.getLogger("ews_llm")

RECORD_COLUMNS = ["Tarih", "Kaynak", "Şirket", "Şirket_Adi", "Eşleşen_Alias", "Ortak", "Başlık", "URL",
                  *LLM_COLUMNS[:-1], "Olay_No", "Defter_Islem", "Metin_Snippet", "LLM_Hata"]
KONTROL_ORNEK = 15           # Kontrol sayfasında her gruptan en fazla kaç kayıt


def _sort_by_stage(table: pd.DataFrame, stage_col: str, by: List[str], ascending=False, **kwargs) -> pd.DataFrame:
    """Stage etiketi kolonunu sayıya çevirip (Stage 3 önce) diğer kolonlarla sıralar."""
    return (table.assign(_ord=table[stage_col].map(STAGE_NO)).sort_values(["_ord", *by], ascending=ascending, **kwargs)
            .drop(columns="_ord").reset_index(drop=True))


def build_company_table(df: pd.DataFrame, ledgers: Dict[object, List[Olay]], bugun=None) -> pd.DataFrame:
    """Şirket başına tek satır: olay defterinden LLM sinyali ve haber skoru, olay sayıları, en ağır olayın kanıtı.

    Haber_Skoru (0-100) ews_defter.news_score ile hesaplanır, Skor_Hesabi nasıl hesaplandığını yazar; bugun
    (tazelik için referans gün) verilmezse bugünün tarihi kullanılır.

    Risk_Olaylari defterdeki olayları durumlarıyla, ardından "odak" haberlerdeki orta seviyeli olay tiplerini
    listeler; Olumlu_Olay, olumlu olay içeren odak/ikincil haber sayısıdır.
    """
    bugun = pd.Timestamp(bugun).normalize() if bugun is not None else pd.Timestamp.now().normalize()
    rows = []
    for key, g in df.groupby("Şirket", sort=False):
        events = ledgers.get(key, [])
        top = max(events, key=lambda e: (e.stage, e.acilis), default=None)
        first = g[g["_Anahtar"] == top.haberler[0]["anahtar"]].iloc[0] if top else {}
        orta = g.loc[(g["LLM_Alaka"] == "odak") & (g["LLM_Olay_Seviyesi"] == "MODERATE"), "LLM_Olay"].value_counts()
        risk = [f"[{e.kod}] {e.tip} ({e.durum_metni()})" for e in events]
        risk += [f"orta: {', '.join(f'{t} ({n})' for t, n in orta.items())}"] if len(orta) else []
        orta_son = g.loc[(g["LLM_Alaka"] == "odak") & (g["LLM_Olay_Seviyesi"] == "MODERATE")].groupby("LLM_Olay")["Tarih"].max()
        skor, hesap = news_score(events, {t: str(d) for t, d in orta_son.items()}, bugun)
        rows.append({
            "Şirket": key, "Şirket_Adi": g["Şirket_Adi"].iloc[0],
            "LLM_Sinyali": STAGE_LABEL[max((e.stage for e in events), default=1)],
            "Haber_Skoru": skor, "Skor_Hesabi": hesap,
            "Acik_Kritik": sum(e.seviye == "CRITICAL" and e.acik for e in events),
            "Acik_Yuksek": sum(e.seviye == "HIGH" and e.acik for e in events),
            "Kapanan_Olay": sum(not e.acik for e in events),
            "Eslesme": len(g), "Odak_Haber": int((g["LLM_Alaka"] == "odak").sum()),
            "Reddedilen": int(g["LLM_Alaka"].isin(RED).sum()),
            "Risk_Olaylari": "; ".join(risk),
            "Olumlu_Olay": int((g["LLM_Alaka"].isin(ILGILI) & (g["LLM_Olay"] == "olumlu")).sum()),
            "En_Agir_Olay": f"[{top.kod}] {top.tanim}" if top else None,
            "En_Agir_Durum": top.durum_metni() if top else None,
            "En_Agir_Risk_Turu": top.risk_turu if top else None,
            "En_Agir_Haber": first.get("Başlık"), "En_Agir_Tarih": first.get("Tarih"),
            "En_Agir_URL": first.get("URL"), "En_Agir_Alinti": first.get("LLM_Alinti"),
        })
    return _sort_by_stage(pd.DataFrame(rows), "LLM_Sinyali", ["Haber_Skoru", "Acik_Kritik", "Odak_Haber"])


def build_ledger_table(df: pd.DataFrame, ledgers: Dict[object, List[Olay]]) -> pd.DataFrame:
    """Olay_Defteri sayfası: olay başına bir satır, zaman çizelgesi ve kanıtlarıyla."""
    names = df.drop_duplicates("Şirket").set_index("Şirket")["Şirket_Adi"]
    etiket = {"yeni": "açıldı", "mevcut": "haber", "kapat": "KAPANDI"}
    rows = []
    for key, events in ledgers.items():
        for e in events:
            hs = e.haberler
            rows.append({
                "Şirket": key, "Şirket_Adi": names.get(key), "Olay": e.kod, "Olay_Tipi": e.tip,
                "Seviye": e.seviye, "Durum": e.durum_metni(), "Sinyale_Etkisi": STAGE_LABEL[e.stage], "Son_Haber": hs[-1]["tarih"],
                "Risk_Turu": e.risk_turu, "Tanim": e.tanim, "Acilis": e.acilis, "Kapanis": e.kapanis or None,
                "Haber_Sayisi": len(hs), "Kaynak_Sayisi": len({h["kaynak"] for h in hs}),
                "Zaman_Cizelgesi": " → ".join(f"{h['tarih']} {etiket[h['islem']]} ({h['kaynak']})" for h in hs),
                "Ilk_Haber_URL": hs[0]["url"], "Kapanis_URL": e.kapanis_url or None,
                "Kapanis_Alinti": e.kapanis_alinti or None,
            })
    cols = ["Şirket", "Şirket_Adi", "Olay", "Olay_Tipi", "Seviye", "Durum", "Sinyale_Etkisi", "Son_Haber", "Risk_Turu", "Tanim", "Acilis",
            "Kapanis", "Haber_Sayisi", "Kaynak_Sayisi", "Zaman_Cizelgesi", "Ilk_Haber_URL", "Kapanis_URL", "Kapanis_Alinti"]
    return _sort_by_stage(pd.DataFrame(rows, columns=cols), "Sinyale_Etkisi", ["Şirket_Adi", "Olay"],
                          ascending=[False, True, True])


def build_record_table(df: pd.DataFrame) -> pd.DataFrame:
    """Kayıtlar sayfası: her (haber, şirket) eşleşmesi ve LLM'in kararları (en yeni haber önce)."""
    return df[[c for c in RECORD_COLUMNS if c in df.columns]]


def _label_key(sirket: object, url: object) -> str:
    return f"{sirket}|{url}"


def label_result(note: object) -> str:
    """Analist notunun sonucu: "doğru", "yanlış" ya da "diğer" (notun başına bakılır; "d" / "y" de olur)."""
    f = fold(note)
    return "doğru" if f.startswith(("doğru", "dogru")) or f == "d" else (
        "yanlış" if f.startswith(("yanlış", "yanlis")) or f == "y" else "diğer")


def harvest_labels(excel_path: Path, labels_path: Path) -> Dict[str, dict]:
    """Önceki Excel'lerdeki dolu Analist_Notu'larını etiket dosyasına ekler; bütün etiketleri (Şirket|URL -> satır) döndürür.

    Excel her koşuda baştan yazılır; notlar o yazılmadan önce buradan kalıcı dosyaya alınır. Kontrol ve
    Kontrol_Etiketleri sayfaları okunur (eski not ikincisinde düzeltilebilir); Excel açıkken zaman damgalı adla
    yazılmış çıktılar da okunur, aynı kayıtta en son kaydedilen dosyanın notu geçerlidir. Değişmeyen not tekrar
    yazılmaz; etiket dosyasında aynı kayıt birden çok kez varsa sonuncusu geçerlidir.
    """
    labels: Dict[str, dict] = {}
    if labels_path.exists():
        for line in labels_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                labels[_label_key(row["Şirket"], row["URL"])] = row
            except (ValueError, KeyError, TypeError):
                continue
    found: Dict[str, dict] = {}
    for path in sorted(excel_path.parent.glob(f"{excel_path.stem}*.xlsx"), key=lambda p: p.stat().st_mtime):
        try:
            with pd.ExcelFile(path) as book:
                tables = [book.parse(s, dtype=str) for s in ("Kontrol", "Kontrol_Etiketleri") if s in book.sheet_names]
        except Exception as exc:  # bozuk ya da başka biçimde dosya koşuyu durdurmasın
            logger.warning("Kontrol notları okunamadı: %s (%s)", path.name, exc)
            continue
        for table in tables:
            if not {"Şirket", "URL", "Analist_Notu"} <= set(table.columns):
                continue
            for row in table[table["Analist_Notu"].fillna("").str.strip() != ""].to_dict("records"):
                row = {k: v for k, v in row.items() if pd.notna(v) and k not in ("Sonuc", "Etiket_Tarihi")}
                found[_label_key(row["Şirket"], row["URL"])] = row | {"Analist_Notu": row["Analist_Notu"].strip()}
    bugun = datetime.now().strftime("%Y-%m-%d")
    new = [row | {"Etiket_Tarihi": bugun} for key, row in found.items()
           if labels.get(key, {}).get("Analist_Notu") != row["Analist_Notu"]]
    if new:
        with labels_path.open("a", encoding="utf-8") as f:
            f.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in new)
        labels.update((_label_key(row["Şirket"], row["URL"]), row) for row in new)
        logger.info("Kontrol: %s yeni ya da düzeltilmiş analist notu %s dosyasına eklendi.", len(new), labels_path.name)
    return labels


def build_label_table(labels: Dict[str, dict]) -> pd.DataFrame:
    """Kontrol_Etiketleri sayfası: biriken bütün analist notları, en yeni önce (not burada düzeltilebilir)."""
    first = ["Grup", "Sonuc", "Analist_Notu", "Etiket_Tarihi"]
    if not labels:
        return pd.DataFrame(columns=first)
    table = pd.DataFrame(list(labels.values()))
    table["Sonuc"] = table["Analist_Notu"].map(label_result)
    return (table[first + [c for c in table.columns if c not in first]]
            .sort_values("Etiket_Tarihi", ascending=False, kind="stable"))


def label_summary(labels: Dict[str, dict]) -> str:
    """Grup başına isabet: "risk olayı 14/15 doğru; ..." ("diğer" notlar sayılmaz)."""
    table = build_label_table(labels)
    table = table[table["Sonuc"].isin(["doğru", "yanlış"])]
    return "; ".join(f"{grup} {int((g['Sonuc'] == 'doğru').sum())}/{len(g)} doğru"
                     for grup, g in table.groupby("Grup", sort=False)) or "henüz not yok"


def build_check_table(df: pd.DataFrame, labels: Dict[str, dict]) -> pd.DataFrame:
    """Kontrol sayfası: analistin LLM kararlarını doğrulaması için her gruptan en fazla KONTROL_ORNEK kayıt.

    Gruplar: risk olayı (kritik / yüksek), orta seviyeli olay, LLM'in elediği eşleşme (alakasız / başka varlık)
    ve ilgili ama olaysız haber. Analist Analist_Notu kolonuna "doğru" ya da "yanlış: ..." yazar; notlar sonraki
    koşuda etiket dosyasına alınır (harvest_labels) ve etiketlenmiş kayıt bir daha örneklenmez. Hedef değişken
    olmadan LLM'in isabetini ölçmenin yolu budur. Seçim rastgele ama tekrarlanabilir (sabit tohum).
    """
    asked = df[answered(df)]
    asked = asked[~(asked["Şirket"].astype(str) + "|" + asked["URL"].astype(str)).isin(labels)]
    level = asked["LLM_Olay"].map(tip_seviyesi)
    groups = [("risk olayı", asked["LLM_Alaka"].isin(ILGILI) & level.isin(["CRITICAL", "HIGH"])),
              ("orta seviye", asked["LLM_Alaka"].isin(ILGILI) & level.eq("MODERATE")),
              ("elenen eşleşme", asked["LLM_Alaka"].isin(RED)),
              ("olay yok", asked["LLM_Alaka"].isin(ILGILI) & level.isna())]
    parts = [build_record_table(asked[mask]).sample(min(KONTROL_ORNEK, int(mask.sum())), random_state=0)
             .assign(Grup=name) for name, mask in groups]
    table = pd.concat(parts) if parts else pd.DataFrame()
    return table[["Grup"] + [c for c in table.columns if c != "Grup"]].assign(Analist_Notu="")


def build_alias_tables(df: pd.DataFrame, min_eslesme: int) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Alias başına eşleşme ve LLM red sayısı + çıkarılması önerilenler için ALIAS_RULES satırları.

    Yalnızca en az `min_eslesme` eşleşmesi olup hiçbiri kabul edilmeyen alias önerilir: arada gerçek haber
    getiren bir marka adı çıkarılırsa o haberler kaçar. Tam unvan önerilmez. Satırlar şirket başına tektir:
    aynı anahtar ALIAS_RULES'ta iki kez yazılırsa ilki kaybolur (şirketin satırı zaten varsa alias'lar o
    satırın "remove" listesine eklenmelidir).
    """
    asked = df[answered(df)]
    table = (asked.assign(_red=asked["LLM_Alaka"].isin(RED))
             .groupby(["Şirket", "Şirket_Adi", "Eşleşen_Alias"], as_index=False)
             .agg(Eslesme=("_red", "size"), Reddedilen=("_red", "sum")))
    table["Red_Orani"] = (table["Reddedilen"] / table["Eslesme"]).round(2)
    table["Cikar_Oner"] = ((table["Eslesme"] >= min_eslesme) & (table["Reddedilen"] == table["Eslesme"])
                           & (table["Eşleşen_Alias"].map(fold) != table["Şirket_Adi"].map(fold)))
    table = table.sort_values(["Red_Orani", "Eslesme"], ascending=False).reset_index(drop=True)

    rules = []
    for (key, name), g in table[table["Cikar_Oner"].astype(bool)].groupby(["Şirket", "Şirket_Adi"], sort=False):
        removes = ", ".join(json.dumps(a, ensure_ascii=False) for a in g["Eşleşen_Alias"])
        stats = "; ".join(f"{a}: {r}/{n} reddedildi" for a, r, n in zip(g["Eşleşen_Alias"], g["Reddedilen"], g["Eslesme"]))
        rules.append({"Şirket": key, "Şirket_Adi": name,
                      "ALIAS_RULES_Satiri": f'{key}: {{"remove": [{removes}]}},  # {name} ({stats})'})
    return table, pd.DataFrame(rules, columns=["Şirket", "Şirket_Adi", "ALIAS_RULES_Satiri"])


_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")  # Excel'in kabul etmediği kontrol karakterleri


def write_excel(sheets: Dict[str, pd.DataFrame], path: Path) -> Path:
    """Sayfaları tek Excel dosyasına yazar; dosya Excel'de açıksa zaman damgalı yeni adla yazar."""
    stamped = path.with_name(f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
    for target in (path, stamped):
        try:
            with pd.ExcelWriter(target) as writer:
                for name, table in sheets.items():
                    table = table[[c for c in table.columns if not str(c).startswith("_")]].copy()  # iç kolonlar yazılmaz
                    for col in table.select_dtypes(include=["object", "string"]).columns:
                        table[col] = table[col].replace(_ILLEGAL_RE, "", regex=True)
                    table.to_excel(writer, sheet_name=name, index=False)
            return target
        except PermissionError:
            logger.warning("%s yazılamadı (Excel'de açık olabilir); yeni adla deneniyor.", target)
    raise PermissionError(f"Çıktı yazılamadı: {path}")
