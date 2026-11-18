"""Olay defteri: şirket başına olaylar, açık / kapandı durumlarıyla (4_llm_dogrulama.py kullanır).

Haber tek başına değerlendirilince, kapanan bir olay ("tedbir kaldırıldı") önceki haberi ("tedbir konuldu")
etkilemez ve kritik sinyal 90 gün kalır. Defter her şirketin riskli haberlerini tarih sırasıyla işler: LLM'e
defterdeki olaylar gösterilir ve yeni haberin yeni bir olay mı açtığı, mevcut bir olayı mı anlattığı, yoksa
açık bir olayı mı kapattığı sorulur. Şirket sinyali olaylardan hesaplanır: açık CRITICAL -> Stage 3, kapanmış
CRITICAL ya da açık HIGH -> Stage 2 (kapanmış kritik olay geçmişteki sıkıntının işaretidir), kapanmış HIGH -> 1.
Son haberi ESKI_OLAY_GUN'den (90) eski olan olay bir kademe daha düşer (en az Stage 1).
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import pandas as pd

from ews_olay import ILGILI, SEVIYE_NO, STAGE2_ESIK, STAGE3_ESIK, eslestirme, tip_seviyesi

DEFTER_SURUMU = "1"          # önbellek anahtarının parçası: defter prompt'u değişince artırın

SYSTEM_BAG = ("Sen bir bankanın kredi riski analistisin. Bir şirket için tutulan olay defterini yeni bir "
              "haberle güncelliyorsun. Yalnızca JSON döndür; açıklama yazma.")
BAG_PROMPT = """HEDEF ŞİRKET: {unvan}

DEFTERDEKİ OLAYLAR:
{olaylar}

YENİ HABER ({tarih}): {baslik}
Haberdeki olay: {olay_tipi} ({durum})
Alıntı: "{alinti}"

---
Yeni haber deftere nasıl işlenmeli?
  yeni:   Defterde olmayan ayrı bir olay (farklı konu, farklı özne ya da farklı karar).
  mevcut: Defterdeki bir olayın aynısını, devamını ya da geçmişini anlatıyor.
  kapat:  Defterdeki AÇIK bir olayın yetkili makamca kaldırıldığını ya da çözüldüğünü bildiriyor.
Özne önemlidir: şirkete konan tedbir ile şirketin sahibine konan tedbir ayrı olaylardır; bir kararın
kaldırılması yalnızca o kararı kapatır. Suçlananın itirazı ya da açıklaması olay kapatmaz.

{{"islem": "yeni|mevcut|kapat", "olay": "<defterdeki olay kodu; yeni ise boş>", "gerekce": "<tek cümle>"}}
/no_think"""
_YENI = {"islem": "yeni", "olay": ""}


@dataclass
class Olay:
    kod: str                  # şirket içinde O1, O2...
    tip: str
    seviye: str               # CRITICAL / HIGH
    risk_turu: str            # kredi / hukuki_itibar
    tanim: str                # olayı açan haberin LLM özeti
    acilis: str
    durum: str = "acik"       # acik / kapandi
    kapanis: str = ""
    kapanis_url: str = ""
    kapanis_alinti: str = ""
    haberler: List[dict] = field(default_factory=list)  # {tarih, kaynak, url, anahtar, islem}
    eski: bool = False        # son haberi ESKI_OLAY_GUN'den eski (build_ledgers işaretler)

    @property
    def acik(self) -> bool:
        return self.durum == "acik"

    @property
    def stage(self) -> int:
        """Olayın şirket sinyaline etkisi: açık CRITICAL 3, HIGH 2; kapanan ve eskiyen olay birer kademe düşer (en az 1)."""
        return max(1, SEVIYE_NO[self.seviye] - (0 if self.acik else 1) - (1 if self.eski else 0))

    def durum_metni(self) -> str:
        durum = "açık" if self.acik else f"kapandı {self.kapanis}"
        return durum + (f" · son haber {ESKI_OLAY_GUN}+ gün önce" if self.eski else "")

    def satir(self) -> str:
        """LLM'e gösterilen satır (önbellek anahtarına girer; biçimi değişirse DEFTER_SURUMU artırılmalı)."""
        durum = "açık" if self.acik else f"kapandı {self.kapanis}"
        return f"  [{self.kod}] {self.tip} | {durum} | açılış {self.acilis} | {self.tanim}"

    def kapat(self, haber: dict, alinti: str) -> None:
        self.durum, self.kapanis, self.kapanis_url, self.kapanis_alinti = "kapandi", haber["tarih"], haber["url"], alinti


def _link(rec: dict, events: List[Olay], llm, cache, errors: Dict[str, str]) -> dict:
    """Haberin defterdeki karşılığını sorar: {"islem": yeni|mevcut|kapat, "olay": kod}.

    Defter boşken sorulmaz. Önbellek anahtarı defterin o anki halini içerir. LLM hata verirse haber yeni
    olay sayılır ve cevap önbelleğe yazılmaz (sonraki koşuda tekrar sorulur).
    """
    if not events:
        return _YENI
    shown = "\n".join(e.satir() for e in events)
    key = f"BAG|{rec['_Anahtar']}|{DEFTER_SURUMU}|{hashlib.md5(shown.encode('utf-8')).hexdigest()[:12]}"
    if key not in cache:
        obj, status = llm.ask(SYSTEM_BAG, BAG_PROMPT.format(
            unvan=rec["Şirket_Adi"] + (f" (hakim ortağı: {rec['Ortak']}; haber ortak hakkında)" if isinstance(rec.get("Ortak"), str) and rec.get("Ortak") else ""),
            olaylar=shown, tarih=rec["Tarih"], baslik=rec.get("Başlık"),
            olay_tipi=rec["LLM_Olay"], durum=rec["LLM_Durum"], alinti=rec["LLM_Alinti"]))
        if obj is None:
            errors[key] = f"defter: {status}"
            return _YENI
        cache.put(key, {"islem": str(obj.get("islem", "")).strip().lower(),
                        "olay": str(obj.get("olay") or "").strip(" []"), "gerekce": str(obj.get("gerekce", ""))[:200]})
    return cache.get(key)


def build_ledger(rows: List[dict], llm, cache, errors: Dict[str, str]) -> List[Olay]:
    """Bir şirketin riskli haberlerini (tarih sıralı) deftere işler.

    Kapatma için haberin kendi durumu "yalanlandi_cozuldu" olmalıdır (alıntısı zaten doğrulanmıştır); LLM
    "kapat" dese bile bu şart yoksa olay açık kalır. Aynı olaya bağlanan daha ağır haber olayın seviyesini
    yükseltir; kredi riski türündeki bir haber olayı "kredi" yapar. Kapanış haberi bu ikisini yapmaz
    ("konkordato iddiası yalanlandı" açık bir davayı konkordatoya çevirmemeli).
    """
    events: List[Olay] = []
    for rec in rows:
        closing = rec["LLM_Durum"] == "yalanlandi_cozuldu"
        ans = _link(rec, events, llm, cache, errors)
        haber = {"tarih": str(rec["Tarih"]), "kaynak": rec.get("Kaynak"), "url": rec.get("URL"), "anahtar": rec["_Anahtar"]}
        target = next((e for e in events if e.kod == ans.get("olay")), None)

        if target is None or ans["islem"] not in ("mevcut", "kapat"):
            # Yeni olay. Kapanış haberiyle açılan olay (açılışı pencere dışında kalmış) kapalı başlar.
            haber["islem"] = "yeni"
            olay = Olay(kod=f"O{len(events) + 1}", tip=rec["LLM_Olay"], seviye=rec["_TipSeviye"],
                        risk_turu=rec["LLM_Risk_Turu"] or "kredi", tanim=rec["LLM_Ozet"], acilis=haber["tarih"],
                        haberler=[haber])
            if closing:
                olay.kapat(haber, rec["LLM_Alinti"])
            events.append(olay)
            continue

        if ans["islem"] == "kapat" and closing and target.acik:
            target.kapat(haber, rec["LLM_Alinti"])
            haber["islem"] = "kapat"
        else:
            haber["islem"] = "mevcut"
        if not closing:  # kapanış / yalanlama haberi olayı ağırlaştırmaz
            if SEVIYE_NO[rec["_TipSeviye"]] > SEVIYE_NO[target.seviye]:  # ağırlaşan olay (dava -> haciz)
                target.seviye, target.tip = rec["_TipSeviye"], rec["LLM_Olay"]
            if rec["LLM_Risk_Turu"] == "kredi":
                target.risk_turu = "kredi"
        target.haberler.append(haber)
    return events


def build_ledgers(df: pd.DataFrame, llm, cache, workers: int, bugun=None) -> Tuple[Dict[object, List[Olay]], Dict[str, str]]:
    """Defter adaylarını seçer ve şirket başına defteri kurar (şirketler paralel, şirket içi sıralı).

    Aday: alıntısı doğrulanmış, tipi CRITICAL ya da HIGH olan haber (yalanlandı/çözüldü olanlar dahil);
    şirket sinyali kuralı gereği "odak" haberler ve "ikincil" haberlerdeki CRITICAL olaylar.
    (defterler, hata veren defter soruları) döndürür.
    """
    seviye = df["LLM_Olay"].map(tip_seviyesi)
    mask = (df["Alinti_Dogrulandi"].eq(True) & seviye.isin(["HIGH", "CRITICAL"])
            & ((df["LLM_Alaka"] == "odak") | ((seviye == "CRITICAL") & df["LLM_Alaka"].isin(ILGILI))))
    # Haberlerin yalnızca tarihi var (saati yok): aynı gün içinde açılış haberleri kapanış haberlerinden önce işlenir.
    cand = (df[mask].assign(_TipSeviye=seviye[mask], _Tarih_DT=eslestirme.to_naive_datetime(df.loc[mask, "Tarih"]),
                            _Kapanis=df.loc[mask, "LLM_Durum"].eq("yalanlandi_cozuldu"))
            .sort_values(["_Tarih_DT", "_Kapanis", "_Anahtar"], kind="stable"))
    groups = {key: g.to_dict("records") for key, g in cand.groupby("Şirket", sort=False)}
    errors: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {key: pool.submit(build_ledger, rows, llm, cache, errors) for key, rows in groups.items()}
    ledgers = {key: f.result() for key, f in futures.items()}
    bugun = pd.Timestamp(bugun).normalize() if bugun is not None else pd.Timestamp.now().normalize()
    for events in ledgers.values():  # eskiyen olay: seviyeye bir kademe düşük katkı
        for e in events:
            e.eski = _age_days(e.haberler[-1]["tarih"], bugun) > ESKI_OLAY_GUN
    return ledgers, errors


def annotate(df: pd.DataFrame, ledgers: Dict[object, List[Olay]]) -> pd.DataFrame:
    """Kayıtlara haberin bağlandığı olayı (Olay_No) ve defter işlemini (yeni / mevcut / kapat) ekler."""
    olay = {h["anahtar"]: (e.kod, h["islem"]) for evs in ledgers.values() for e in evs for h in e.haberler}
    return df.assign(Olay_No=df["_Anahtar"].map(lambda k: olay.get(k, (None, None))[0]),
                     Defter_Islem=df["_Anahtar"].map(lambda k: olay.get(k, (None, None))[1]))


# =============================================================================
# Haber skoru (0-100)
# =============================================================================
# Seviyeyi defter kuralı belirler (Olay.stage); skor, o seviyenin bandı içinde şirketleri sıralar. Bantlar finansal
# skorla aynı eşiklerden gelir, bu yüzden birleşik skor = max(haber, finansal) birleşik seviyeyle hep tutarlıdır.
# Değerler ayardır: kesinlik iddiası yoktur, sıralamayı makul kılmak için seçildi.
OLAY_PUANI = {("CRITICAL", True): 80, ("CRITICAL", False): 45,   # (seviye, açık mı) -> puan
              ("HIGH", True): 45, ("HIGH", False): 15}
ORTA_OLAY_PUANI = 8          # odak haberdeki her farklı orta seviyeli olay tipi (kâr düşüşü, dış baskı...)
HUKUKI_ITIBAR_CARPANI = 0.8  # ödeme gücüne etkisi henüz belirsiz olay
TAZELIK_TAM_GUN = 90         # son haberi bu kadar gün içinde olan olay puanının tamamını alır
TAZELIK_SIFIR_GUN = 180      # bu yaşta puan 0 olur; arası doğrusal azalır (haber penceresi de 180 gün)
EK_OLAY_ORANI = 0.25         # en ağır olay dışındaki olayların puanının skora eklenen payı
ESKI_OLAY_GUN = 90           # son haberi bundan eski olay seviyeye bir kademe düşük katkı yapar (Olay.stage)


def _age_days(tarih: str, bugun: pd.Timestamp) -> int:
    ts = eslestirme.to_naive_datetime(pd.Series([tarih])).iloc[0]
    return 0 if pd.isna(ts) else max(0, (bugun - ts.normalize()).days)


def freshness(age_days: int) -> float:
    """Tazelik çarpanı: TAZELIK_TAM_GUN'e kadar 1, sonra TAZELIK_SIFIR_GUN'de 0 olacak şekilde doğrusal."""
    if age_days <= TAZELIK_TAM_GUN:
        return 1.0
    return max(0.0, (TAZELIK_SIFIR_GUN - age_days) / (TAZELIK_SIFIR_GUN - TAZELIK_TAM_GUN))


def _n(value: float) -> str:
    return f"{round(value, 1):g}"


def news_score(events: List[Olay], orta: Dict[str, str], bugun: pd.Timestamp) -> Tuple[float, str]:
    """Şirketin haber skoru ve hesabın metni. orta: odak haberdeki orta seviyeli olay tipi -> son haber tarihi.

    Her olayın puanı: OLAY_PUANI (orta seviyede ORTA_OLAY_PUANI) x tazelik (son haberin yaşına göre) x
    (hukuki/itibar ise HUKUKI_ITIBAR_CARPANI). Skor = en yüksek puan +
    EK_OLAY_ORANI x diğerlerinin toplamı; sonuç şirketin seviye bandına (Stage 1: 0-24,9, 2: 25-64,9, 3: 65-100)
    sığdırılır ve bir ondalığa yuvarlanır.
    """
    items = []
    for e in events:
        tazelik = freshness(_age_days(e.haberler[-1]["tarih"], bugun))
        risk = HUKUKI_ITIBAR_CARPANI if e.risk_turu == "hukuki_itibar" else 1.0
        items.append((OLAY_PUANI[(e.seviye, e.acik)] * tazelik * risk, f"[{e.kod}] {e.tip} {'açık' if e.acik else 'kapandı'}"))
    items += [(ORTA_OLAY_PUANI * freshness(_age_days(tarih, bugun)), f"orta: {t}") for t, tarih in orta.items()]
    if not items:
        return 0, ""
    items.sort(key=lambda i: -i[0])
    raw = items[0][0] + EK_OLAY_ORANI * sum(v for v, _ in items[1:])
    stage = max((e.stage for e in events), default=1)
    low, high = {1: (0, STAGE2_ESIK - 0.1), 2: (STAGE2_ESIK, STAGE3_ESIK - 0.1), 3: (STAGE3_ESIK, 100)}[stage]
    skor = round(min(max(raw, low), high), 1)
    text = f"{items[0][1]} {_n(items[0][0])}"
    if len(items) > 1:
        shown = ", ".join(f"{n} {_n(v)}" for v, n in items[1:5]) + (f", +{len(items) - 5} diğer" if len(items) > 5 else "")
        text += f" + %{EK_OLAY_ORANI * 100:g} x ({shown}; toplam {_n(sum(v for v, _ in items[1:]))})"
    clamped = f" (ham {_n(raw)}, Stage {stage} bandı {_n(low)}-{_n(high)})" if round(raw, 1) != skor else ""
    return skor, f"{text} = {_n(skor)}{clamped}"
