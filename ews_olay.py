"""Olay sözlüğü, alaka ve olay soruları, cevapların kayıtlara işlenmesi (4_llm_dogrulama.py kullanır).

Her (haber, şirket) eşleşmesi için LLM'e iki soru sorulur:
  A. Alaka  Eşleşen ifade bu şirket mi, haber şirket hakkında mı? odak / ikincil / alakasiz / baska_varlik
  B. Olay   Odak ve ikincil haberlerde: olay tipi (sabit listeden), durum, risk türü ve haberden birebir alıntı.
LLM seviye seçmez: tipin seviyesi OLAY_TIPLERI'nden gelir (CRITICAL -> Stage 3, HIGH -> Stage 2, diğerleri
Stage 1), alıntının haberde geçip geçmediğine kod bakar.
"""

from __future__ import annotations

import importlib
import re
from typing import Dict, Optional, Tuple

import pandas as pd

eslestirme = importlib.import_module("3_eslestirme")  # adı rakamla başladığı için "import" ile alınamaz

PROMPT_SURUMU = "5"          # önbellek anahtarının parçası: alaka ya da olay prompt'u değişince artırın

STAGE_LABEL = {3: "Kırmızı Alarm (Stage 3 Sinyali)", 2: "Yakın İzleme (Stage 2)", 1: "Düşük Risk (Stage 1)"}
STAGE_NO = {v: k for k, v in STAGE_LABEL.items()}    # stage etiketi -> 1/2/3
SEVIYE_NO = {"MODERATE": 1, "HIGH": 2, "CRITICAL": 3}  # olay seviyesi -> stage (seviyesiz tip de Stage 1)
STAGE2_ESIK, STAGE3_ESIK = 25, 65  # 0-100 skor -> stage; haber ve finansal skor için aynı


def score_stage(skor) -> Optional[int]:
    """0-100 skorun stage'i (1/2/3); skor yoksa None."""
    if skor is None or pd.isna(skor):
        return None
    return 3 if skor >= STAGE3_ESIK else 2 if skor >= STAGE2_ESIK else 1


# =============================================================================
# Olay sözlüğü
# =============================================================================
# Olay tipi -> (seviye, tanım, dahil olmayanlar). Seviyesi None olan tip risk sinyali değildir.
# "Dahil değil" listeleri banka koşularındaki aşırı alarmlardan çıkarıldı.
OLAY_TIPLERI: Dict[str, Tuple[Optional[str], str, str]] = {
    "konkordato_iflas":   ("CRITICAL", "Şirketin konkordato başvurusu/mühleti, iflası, tasfiyesi, borca batıklığı",
                           "başka şirketin ya da fonun tasfiyesi"),
    "temerrut":           ("CRITICAL", "Şirketin borcunu/kuponunu ödeyememesi, karşılıksız çek, protestolu senet",
                           "borsada şirketin payında takas temerrüdü ya da \"temerrüt satışı\" (payı alan yatırımcının "
                           "ya da aracı kurumun yükümlülüğüdür, şirketin borcu değildir; rutin seç)"),
    "haciz_el_koyma":     ("CRITICAL", "Şirkete ya da sahibine/hakim ortağına haciz, el koyma, mal varlığına tedbir, "
                                       "kayyum, TMSF'ye devir", "yalnızca başka kişi ya da şirketlere yönelik tedbir"),
    "borc_yapilandirma":  ("HIGH", "Kredi/borç yapılandırması, vade uzatımı talebi, finansal taahhüt ihlali, icra takibi",
                           "olağan tahvil/kredi ihracı ve refinansman (rutin seç)"),
    "dava_ceza":          ("HIGH", "Şirkete, sahibine ya da yöneticilerine ceza soruşturması, gözaltı, tutuklama, "
                                   "işlem yasağı, faaliyet durdurma; düzenleyici kurumun (Rekabet, SPK, BDDK) devam "
                                   "eden soruşturması",
                           "sonuçlanmış ve ödenmiş ya da uzlaşılmış idari para cezası (idari_ceza seç); çok sayıda "
                           "şirketi birlikte kapsayan sektör soruşturması (dis_baski seç)"),
    "idari_ceza":         ("MODERATE", "Sonuçlanmış ve ödenmiş ya da uzlaşmayla kapanmış idari para cezası, kurum uyarısı",
                           ""),
    "finansal_bozulma":   ("HIGH", "Net zarar, zarara geçiş, özkaynak/sermaye kaybı, süreklilik şüphesi, olumsuz "
                                   "denetim görüşü", "hâlâ kâr eden şirketin kârındaki düşüş (kar_dususu seç)"),
    "kar_dususu":         ("MODERATE", "Kâr, satış ya da FAVÖK'te belirgin düşüş, beklenti altı sonuç; şirket hâlâ kârlı",
                           ""),
    "faaliyet_sona_erme": ("CRITICAL", "Şirketin üretiminin ya da faaliyetinin tamamen ve süresiz durması, bütün "
                                       "tesislerinin kapatılması, faaliyetlerine son verme kararı",
                           "tek bir tesisin ya da hattın kapanması, geçici duruş (faaliyet_kesintisi seç)"),
    "faaliyet_kesintisi": ("HIGH", "Üretimin ya da hizmetin günlerce durması, bir tesisin ya da hattın kapanması, "
                                   "üretimi durduran büyük yangın/patlama, grev, toplu işten çıkarma, büyük müşteri/sözleşme kaybı",
                           "bütün faaliyetin süresiz durması (faaliyet_sona_erme seç); kısa süreli arıza, tek "
                           "seferlik iptal, tekil personel olayı, kontrol altına alınan ve üretimi durdurduğu belirtilmeyen yangın "
                           "(operasyonel_aksaklik seç); planlı bakım ya da "
                           "tatil duruşu (rutin seç); başka şirketin tesisindeki olay"),
    "operasyonel_aksaklik": ("MODERATE", "Kısa süreli arıza, tek seferlik sefer/üretim iptali, küçük kaza, tekil "
                                         "personel olayı; üretimi ya da satışı kalıcı etkilemeyen aksaklık", ""),
    "not_dusurme":        ("HIGH", "Şirketin kredi notunun ya da görünümünün düşürülmesi, izlemeye alınması", ""),
    "yonetim_krizi":      ("HIGH", "Üst yöneticinin ani ya da zorunlu ayrılışı, ortaklar arası anlaşmazlık, "
                                   "yönetime müdahale",
                           "olağan atama, görev değişikliği ya da emeklilik (rutin seç); ortak ya da iştirak "
                           "şirketteki yönetim değişikliği"),
    "dis_baski":          ("MODERATE", "Şirketi etkileyen dış baskı: talep düşüşü, maliyet/kur baskısı, tedarik sorunu, "
                                       "gümrük/anti-damping vergisi, sektör geneli soruşturma, şartlı denetim görüşü", ""),
    "olumlu":             (None, "Kâr artışı, yeni sözleşme/yatırım, sermaye artırımı, not artırımı, borç kapatma", ""),
    "rutin":              (None, "Olağan borçlanma ihracı, temettü, pay hareketi, olağan yönetici ataması, planlı "
                                 "bakım, rutin duyuru", ""),
    "hicbiri":            (None, "Şirket hakkında somut bir olay yok", ""),
}
ILGILI = ("odak", "ikincil")          # olay sorusu sorulan alaka düzeyleri
RED = ("alakasiz", "baska_varlik")    # alias raporunda "reddedildi" sayılanlar
DURUMLAR = ("gerceklesti", "iddia", "yalanlandi_cozuldu")
RISK_TURLERI = ("kredi", "hukuki_itibar")


def tip_seviyesi(tip: object) -> Optional[str]:
    """Olay tipinin seviyesi (CRITICAL / HIGH / MODERATE) ya da None."""
    return OLAY_TIPLERI.get(tip, (None,))[0]


# =============================================================================
# Promptlar
# =============================================================================
SYSTEM_ALAKA = (
    "Sen bir bankanın kredi riski analistisin. Bir haberde geçen ifadenin, bankanın müşterisi olan "
    "HEDEF ŞİRKETİ gösterip göstermediğine ve haberin bu şirket için ne kadar önemli olduğuna karar "
    "veriyorsun. Yalnızca JSON döndür; açıklama yazma."
)
ALAKA_PROMPT = """HEDEF ŞİRKET (tam unvan): {unvan}
HABERDE EŞLEŞEN İFADE: "{alias}"

BAŞLIK: {baslik}

İFADENİN GEÇTİĞİ BÖLÜM:
{snippet}

HABERİN BAŞI:
{bas}

---
Alaka düzeyini seç:
  baska_varlik: İfade bu şirketi göstermiyor: mahkeme, belediye, kurum, yer adı, kişi, başka bir şirket
                ya da sıradan bir kelime. Örnek: "Ankara 2. Asliye Ceza Mahkemesi" ANKARA 2
                GAYRİMENKUL şirketi değildir.
  alakasiz:     Şirket kastediliyor ama yalnızca adı geçiyor, kendisi hakkında bilgi yok: borsa bülteni,
                hisse listesi, sponsorluk, stadyum/salon adı, reklam.
  ikincil:      Şirket hakkında somut bir bilgi var ama haberin ana konusu başka.
  odak:         Haber esas olarak bu şirket hakkında.
Şirketin sahibi, hakim ortağı ya da üst yöneticisi hakkındaki haber şirket hakkında sayılır.
İfadenin şirketi gösterip göstermediğinden emin değilsen "ikincil" seç.

{{"alaka": "odak|ikincil|alakasiz|baska_varlik", "gerekce": "<tek kısa cümle>"}}
/no_think"""

SYSTEM_OLAY = (
    "Sen bir bankanın kredi riski analistisin. Haberden HEDEF ŞİRKETİN kredi riskini etkileyen somut "
    "olayı çıkarıyorsun. Haberde geçen başka şirketlerin olaylarını hedef şirkete yazma. Uydurma: "
    "alıntıyı haberden kelimesi kelimesine kopyala. Yalnızca JSON döndür; açıklama yazma."
)
OLAY_PROMPT = """HEDEF ŞİRKET: {unvan} (haberde "{alias}" olarak geçiyor)

BAŞLIK: {baslik}

HABER:
{metin}

---
Haberde HEDEF ŞİRKET hakkındaki olayı seç. Birden fazla olay varsa kredi riski açısından en ağırını seç.
Şirketin sahibi ya da hakim ortağı hakkındaki el koyma, gözaltı gibi olaylar şirketin olayıdır.
"DAHİL DEĞİL" yazan durumlarda o tipi seçme.

OLAY TİPLERİ:
{tipler}

DURUM:
  gerceklesti:        Olay oldu ya da resmi olarak açıklandı (ör. konkordato başvurusu yapıldı).
  iddia:              İddia, söylenti ya da beklenti; şirket henüz böyle bir adım atmadı.
  yalanlandi_cozuldu: Olay hiç olmadı, yetkili makam kararı kaldırdı ya da sorun çözüldü (dava düştü,
                      haciz kalktı, borç ödendi). Şirketin "faaliyetlerimiz sürüyor" gibi açıklaması
                      olayı yalanlamaz. Suçlanan şirketin ya da kişinin itirazı, iddiaları reddetmesi ya da
                      hukuki süreç başlatması kararı ortadan kaldırmaz; bu durumda "gerceklesti" seç.

RİSK TÜRÜ (olay_tipi olumlu, rutin ya da hicbiri ise boş bırak):
  kredi:          Olay şirketin ödeme gücünü doğrudan etkiliyor (zarar, borç sorunu, faaliyetin durması,
                  şirketin kendi varlıklarına haciz, TMSF'ye devir).
  hukuki_itibar:  Olay şirket, sahibi ya da yöneticileri hakkında hukuki bir süreç ya da itibar meselesi;
                  şirketin ödeme gücüne etkisi henüz belli değil (soruşturma, sahibine yönelik tedbir, dava).

{{"olay_tipi": "<{tip_adlari}>", "durum": "gerceklesti|iddia|yalanlandi_cozuldu",
 "risk_turu": "kredi|hukuki_itibar|",
 "alinti": "<olayı gösteren cümle, haberden kelimesi kelimesine, en fazla 250 karakter>",
 "ozet": "<tek cümle, en fazla 150 karakter>"}}
/no_think"""
_TIPLER = "\n".join(f"  {k}: {tanim}" + (f" | DAHİL DEĞİL: {haric}" if haric else "")
                    for k, (_, tanim, haric) in OLAY_TIPLERI.items())


# =============================================================================
# LLM'e sorma
# =============================================================================
def pair_keys(df: pd.DataFrame) -> pd.Series:
    """Önbellek anahtarı: haberin tekilleştirme anahtarı | şirket | prompt sürümü."""
    return eslestirme.build_dedup_keys(df) + "|" + df["Şirket"].astype(str) + "|" + PROMPT_SURUMU


def _pick(value: object, allowed, default: str) -> str:
    """LLM cevabını izin verilen değerlere çeker; tanımsız cevap varsayılana döner."""
    value = str(value or "").strip().lower()
    return value if value in allowed else default


def ask_pair(llm, rec: dict) -> dict:
    """Bir (haber, şirket) eşleşmesini sorar; önbelleğe yazılacak sonucu döndürür.

    Hata olursa {"hata": ...} döner; bu eşleşme önbelleğe yazılmaz, sonraki çalışmada tekrar sorulur.
    Tanımsız cevaplar güvenli tarafa çekilir: alaka -> ikincil (olay sorusuna devam), tip -> hicbiri,
    durum -> iddia, risk olayında risk türü -> kredi.
    """
    baslik, metin = str(rec.get("Başlık") or ""), str(rec.get("Metin") or "")
    unvan, alias = rec["Şirket_Adi"], rec["Eşleşen_Alias"]
    ortak = rec.get("Ortak")
    if isinstance(ortak, str) and ortak:  # ortak üzerinden eşleşme: prompt şablonu aynı, yalnızca unvan satırı uzar
        unvan = f"{unvan} (hakim ortağı: {ortak}; haberde ortağın adı geçiyor)"

    obj, status = llm.ask(SYSTEM_ALAKA, ALAKA_PROMPT.format(
        unvan=unvan, alias=alias, baslik=baslik, snippet=rec.get("Metin_Snippet") or "", bas=metin[:1000]))
    if obj is None:
        return {"hata": f"alaka: {status}"}
    result = {"alaka": _pick(obj.get("alaka"), ILGILI + RED, "ikincil"),
              "alaka_gerekce": str(obj.get("gerekce", ""))[:250]}
    if result["alaka"] not in ILGILI:
        return result

    obj, status = llm.ask(SYSTEM_OLAY, OLAY_PROMPT.format(
        unvan=unvan, alias=alias, baslik=baslik, metin=metin[:6000], tipler=_TIPLER, tip_adlari="|".join(OLAY_TIPLERI)))
    if obj is None:
        return {"hata": f"olay: {status}"}
    tip = _pick(obj.get("olay_tipi"), OLAY_TIPLERI, "hicbiri")
    result.update({
        "olay_tipi": tip,
        "durum": _pick(obj.get("durum"), DURUMLAR, "iddia"),
        "risk_turu": _pick(obj.get("risk_turu"), RISK_TURLERI, "kredi") if tip_seviyesi(tip) else "",
        "alinti": str(obj.get("alinti", ""))[:300],
        "ozet": str(obj.get("ozet", ""))[:200],
    })
    return result


# =============================================================================
# Cevapların kayıtlara işlenmesi
# =============================================================================
LLM_COLUMNS = ["LLM_Alaka", "LLM_Alaka_Gerekce", "LLM_Olay", "LLM_Durum", "LLM_Risk_Turu", "LLM_Olay_Seviyesi",
               "LLM_Seviye", "LLM_Alinti", "Alinti_Dogrulandi", "LLM_Ozet", "LLM_Hata"]


def fold(text: object) -> str:
    """Karşılaştırma biçimi: Türkçe küçük harf, harf ve rakam dışındaki her şey (boşluk dahil) atılır.

    Boşluk da atılır: kazınan metinde kelimeler bazen bitişik kalır ("AstorEnerjiYönetim").
    """
    return "".join(re.findall(r"\w+", str(text or "").replace("İ", "i").replace("I", "ı").lower()))


def quote_found(quote: str, haystack: str) -> bool:
    """Alıntı (fold edilmiş) haberde geçiyor mu? "..." ile kısaltılmışsa her parça ayrı aranır (kısa parça sayılmaz)."""
    parts = [p for p in (fold(x) for x in re.split(r"\.\.\.|…", str(quote))) if len(p) >= 15]
    return bool(parts) and all(p in haystack for p in parts)


def answered(df: pd.DataFrame) -> pd.Series:
    """LLM cevabı olan kayıtlar (hata vermemiş ve sorulmuş)."""
    return ~df["LLM_Alaka"].isin(["hata", "sorulmadi"])


def apply_results(df: pd.DataFrame, cache, errors: Dict[str, str]) -> pd.DataFrame:
    """LLM cevaplarını kolon olarak ekler ve kayıt bazlı LLM stage'ini hesaplar.

    Olay seviyesi yalnızca alaka odak/ikincil, alıntı haberde geçiyor ve olay yalanlanmamışsa geçerlidir.
    Önbellekteki ham cevaptan hesaplandığı için bu kurallar değişince LLM'e tekrar sorulmaz.
    """
    rows = []
    for rec in df.to_dict("records"):
        res = cache.get(rec["_Anahtar"])
        if res is None:
            error = errors.get(rec["_Anahtar"], "")
            rows.append({"LLM_Alaka": "hata" if error else "sorulmadi", "LLM_Hata": error,
                         "LLM_Seviye": STAGE_LABEL[1]})
            continue
        level = tip_seviyesi(res.get("olay_tipi", "")) if res["alaka"] in ILGILI else None
        quote_ok = None
        if level:
            quote_ok = quote_found(res.get("alinti", ""), fold(f"{rec.get('Başlık')} {rec.get('Metin')}"))
            if not quote_ok or res.get("durum") == "yalanlandi_cozuldu":
                level = None
        rows.append({
            "LLM_Alaka": res["alaka"], "LLM_Alaka_Gerekce": res.get("alaka_gerekce", ""),
            "LLM_Olay": res.get("olay_tipi", ""), "LLM_Durum": res.get("durum", ""),
            "LLM_Risk_Turu": res.get("risk_turu", ""), "LLM_Olay_Seviyesi": level or "",
            "LLM_Seviye": STAGE_LABEL[SEVIYE_NO.get(level, 1)],
            "LLM_Alinti": res.get("alinti", ""), "Alinti_Dogrulandi": quote_ok, "LLM_Ozet": res.get("ozet", ""),
        })
    return pd.concat([df, pd.DataFrame(rows, index=df.index).reindex(columns=LLM_COLUMNS)], axis=1)
