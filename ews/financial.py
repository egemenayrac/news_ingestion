"""
EWS finansal (kural bazlı) skor.

Portföydeki borçluların son iki finansal tablosundan 0-100 arası bir finansal risk skoru üretir
(yüksek skor = yüksek risk; haber skoruyla aynı yön). Skor, üç katmandaki alt skorların toplamıdır;
her alt skor kendi puan tavanına kadar risk puanı verir (puan = tavan x risk oranı).

  Katman 1 · Seviye (60): neredesin
    Özkaynak (15)  negatif = borca batık, sıfır = sermaye erimiş, pozitif = sorun yok.
    FAVÖK (10)     negatif = operasyonlar nakit üretmiyor. FAVÖK ICR'ın paydasında ve FAVÖK
                   trendinde de var; üç kez tam ağırlıkla sayılmasın diye tavanı düşük.
    Kur (15)       bankadaki döviz mevduatı bankadaki döviz kredisini (tamponla) karşılıyorsa dirençli;
                   değilse net döviz pozisyonu ve ihracat payıyla (doğal koruma) karar verilir.
    DSCR (20)      Borç servis karşılama oranı (dscrebitda_try; paydası doğrulanmadı).
  Katman 2 · Yapısal dayanıklılık (20): şok gelirse ne kadar dayanırsın
    Likidite (15)  Likit aktif / kısa vadeli borç.
    ICR (5)        FAVÖK / faiz gideri. Ayrıca DSCR'ın veri kontrolüdür (bkz. DSCR_ICR_Ayrisma).
  Katman 3 · Trend (20): nereye gidiyorsun
    Özkaynak Trend (10), FAVÖK Trend (10): portföy sorgusunun ürettiği durum etiketleri (önceki tabloya
                   göre işaret geçişi ve yön). Yalnızca önceki tablo 11-13 ay önceyse ve para birimi
                   aynıysa kullanılır.

Sınırlar (analist notu)
  - Alt skorlar sınıflandırmadır (negatif/sıfır/pozitif gibi); büyüklük hesaba katılmaz. Küçük
    pozitif bir özkaynak ile güçlü bir özkaynak aynı puanı alır.
  - Eşikler ve puanlar BAŞLANGIÇ değerleridir; portföy verisiyle kalibre edilmelidir.
  - Finansal tablo tarihine (statement_dt) bakılır ama eskilik skoru etkilemez; eski bir tablo
    güncel durumu yansıtmayabilir.
  - Sektör ayrımı yoktur (ör. sermaye yoğun sektörlerde düşük DSCR normal olabilir).
  - Eksik alt skor 0 puan alır: skor, hesaplanabilen alt skorların toplamıdır ve ölçeklenmez. Veri
    eksikliği skoru yükseltmez; kapsam %100'ün altındaysa skor bir alt sınırdır (eksik alt skor
    kötü çıksaydı yüksek olurdu). Bu yüzden veri kapsamı ve eksik alt skorlar ayrıca raporlanır.
    Hiçbir alt skor hesaplanamıyorsa skor üretilmez (0 değil): verisi olmayan borçlu düşük riskli
    görünmemelidir. Hesaplanamayan alt skor varsayılan bir duruma (ör. "nötr") da çekilmez.
  - Trendler TL'ye çevrilmiş nominal tutarları karşılaştırır: yüksek enflasyonda ve döviz bazlı
    tablolarda büyüme abartılı görünür.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Dict, List, Mapping, Optional

import pandas as pd

logger = logging.getLogger("ews_financial")

# Çıktı kolonları ya da anlamları değiştiğinde artırılır; 3_scoring.py bu sürümü kontrol eder ve
# eski bir ews_financial.py ile sessizce yanlış çalışmak yerine anlaşılır bir hata verir.
FINANCIAL_API_VERSION = 5


# =============================================================================
# 1. Ayarlar (başlangıç değerleri)
# =============================================================================
# Alt skor puan tavanları. Toplam 100 olmalıdır; alt skor eklemek için buraya girdi eklenir
# ve ilgili sınıflandırıcı RISK_TABLES, COMPONENT_NAMES ve calculate_financial_score'a bağlanır.
FINANCIAL_MAX_POINTS: Dict[str, float] = {
    "equity": 15.0, "ebitda": 10.0, "fx": 15.0, "dscr": 20.0,   # katman 1 · seviye -> 60
    "liquidity": 15.0, "icr": 5.0,                              # katman 2 · yapısal dayanıklılık -> 20
    "equity_trend": 10.0, "ebitda_trend": 10.0,                 # katman 3 · trend -> 20
}

# Finansal seviye eşikleri (haber skorununkinden farklı: 25 / 65): 50 ve üstü Stage 2, 80 ve üstü Stage 3.
FIN_STAGE2_ESIK = 50.0
FIN_STAGE3_ESIK = 80.0

# Trend durum etiketi (portföy sorgusundaki case ifadesi) -> risk oranı; iki trendde ortak.
# "sabit" 0 değil 0,3: değişimin olmaması iyileşme kanıtı değildir.
TREND_RISK: Dict[str, float] = {
    "pozitife_donus": 0.0, "buyuyen_ozkaynak": 0.0, "artan_ebitda": 0.0,
    "sabit": 0.3,
    "kapanan_acik": 0.4, "azalan_negatif_ebitda": 0.4,
    "eriyen_ozkaynak": 0.7, "azalan_ebitda": 0.7,
    "buyuyen_acik": 0.85, "artan_negatif_ebitda": 0.85,
    "negatife_gecis": 1.0,
}

# Durum -> risk oranı (0 = risksiz, 1 = azami risk). Alt skor puanı = tavan x risk oranı.
# Üç düzeyli alt skorlarda eşit aralık kullanılır: en kötü 1.0, orta 0.5, en iyi 0.0.
_THREE_LEVEL_SIGN = {"negatif": 1.0, "sıfır": 0.5, "pozitif": 0.0}
RISK_TABLES: Dict[str, Dict[str, float]] = {
    "equity": dict(_THREE_LEVEL_SIGN),
    "ebitda": dict(_THREE_LEVEL_SIGN),
    "fx": {"dayanıksız": 1.0, "nötr": 0.5, "dirençli": 0.0},
    # DSCR bantları (aşağıya bakın). "standart üstü" bandı (1,50-2,00) kaynak tabloda tanımlı
    # değildir; standart ile çok güçlü arasında bir ara değer verilmiştir.
    "dscr": {
        "negatif nakit akışı": 1.0,
        "başabaş": 0.9,
        "düşük güvenlik rezervi": 0.7,
        "standart (sağlıklı)": 0.3,
        "standart üstü": 0.1,
        "çok güçlü": 0.0,
    },
    "liquidity": {"kritik": 1.0, "zayıf": 0.7, "marjinal": 0.5, "yeterli": 0.2, "güçlü": 0.0},
    "icr": {"operasyonel zarar": 1.0, "faiz ödenemiyor": 1.0, "çok düşük": 0.8, "düşük": 0.5,
            "yeterli": 0.2, "güçlü": 0.0},
    "equity_trend": TREND_RISK,
    "ebitda_trend": TREND_RISK,
}

# Kur riski: ihracat payı eşikleri (ihracat geliri / toplam satış geliri).
FX_EXPORT_SHARE_LOW = 0.40     # bunun altında + net pozisyon negatif -> dayanıksız
FX_EXPORT_SHARE_HIGH = 0.80    # bunun üstünde + net pozisyon pozitif -> dirençli
# Bankadaki döviz kapsaması: bank_fx_deposit + tampon >= cash_bal_bank_usd + noncash_bal_bank_usd ise dirençli.
# Tampon bu üç kolonla aynı para biriminde olmalıdır (doğrulanmadı; bkz. SKORLAMA_KURALLARI).
FX_COV_BUFFER = 1_000_000

# Likidite (likit aktif / KV borç) ve ICR (FAVÖK / faiz) bant üst sınırları; sınır değeri alt banda girer.
LIQUIDITY_BANDS = ((0.50, "kritik"), (0.80, "zayıf"), (1.00, "marjinal"), (1.50, "yeterli"))   # üstü "güçlü"
ICR_BANDS = ((1.50, "çok düşük"), (2.50, "düşük"), (4.00, "yeterli"))                           # üstü "güçlü"
ICR_MIN = 1.0                  # bunun altı "faiz ödenemiyor"
DSCR_ICR_SAGLIKLI_DSCR = 1.20  # DSCR bunun üstünde ama ICR ICR_AYRISMA_ESIK'in altındaysa DSCR kolonu şüpheli
ICR_AYRISMA_ESIK = 1.50

# Trend yalnızca önceki tablo bu kadar ay önceyse kullanılır (yıllık tablolar; 24 ay arayla "değişim" yanıltır).
TREND_GAP_MONTHS = (11, 13)

# Portföyde bir alt skor borçluların bu oranından fazlasında hesaplanamıyorsa kolon/kaynak sorunu olarak uyarılır.
COMPONENT_MISSING_WARN_SHARE = 0.5

# DSCR bant sınırları: (1,00 - 1,20], (1,20 - 1,50], (1,50 - 2,00]; 1,00 tam eşitlik başabaştır.
DSCR_BREAKEVEN = 1.0
DSCR_LOW_RESERVE_MAX = 1.20
DSCR_STANDARD_MAX = 1.50
DSCR_ADEQUATE_MAX = 2.00

# Parquet kolonları (portföy sorgusunun çıktısı; adlar küçük harfe çevrilerek okunur).
KEY_COLUMN = "counterparty_id"
METRIC_COLUMNS = ("equity_try", "ebitda_try", "net_fx_pos_try", "external_sales_try",
                  "total_sales_try", "dscrebitda_try", "liquid_assets_try", "cash_in_banks_try",
                  "totalcurrentliabilities_try", "interestexpense_try",
                  "bank_fx_deposit", "cash_bal_bank_usd", "noncash_bal_bank_usd")
TREND_COLUMNS = ("equity_trend", "ebitda_trend")          # metin: durum etiketi
DATE_COLUMNS = ("statement_dt", "prev_statement_dt")
CCY_COLUMNS = ("ccy_code", "prev_ccy_code")               # varsa trend için para birimi kontrolü yapılır
# Bakiye kolonları: bankanın karşı taraftaki nakit ve gayrinakit bakiyesi. Toplam bakiyenin TL karşılığı
# cash_bal_bank + noncash_bal_bank'dir (portföy sorgusu da bu toplama göre sıralar). Skoru etkilemez,
# önceliklendirme için şirket tablosunda gösterilir. *_try ve *_usd kolonları para birimi kalemleri
# olabilir (yabancı para ağırlıklı borçluda TL kalemi 0 çıkar); anlamları doğrulanana kadar bakiye
# hesabında kullanılmaz.
BALANCE_COLUMNS = ("cash_bal_bank", "noncash_bal_bank")
# Şirket tablosuna eklenen bakiye çıktı kolonları (TL karşılığı).
BALANCE_OUTPUT_COLUMNS = ["Toplam_Bakiye_TRY", "Nakit_Bakiye_TRY", "Gayrinakit_Bakiye_TRY"]
# Borçluların bu oranından fazlasında toplam bakiye 0 ya da boşsa kolonların anlamı kontrol edilmelidir.
BALANCE_EMPTY_WARN_SHARE = 0.5
STRUCTURED_PARQUET_PATH = Path(__file__).resolve().parent.parent / "structured_data.parquet"


# =============================================================================
# 2. Alt skor sınıflandırıcıları
# =============================================================================
def _number(value: object) -> Optional[float]:
    """Değeri sayıya çevirir; boş, geçersiz ya da sonsuz değerler için None döner."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def classify_sign(value: object) -> Optional[str]:
    """Özkaynak ve FAVÖK için: "negatif", "sıfır" ya da "pozitif"; veri yoksa None."""
    number = _number(value)
    if number is None:
        return None
    if number < 0:
        return "negatif"
    return "sıfır" if number == 0 else "pozitif"


def export_share(external_sales: object, total_sales: object) -> Optional[float]:
    """İhracat payı = ihracat geliri / toplam satış geliri; hesaplanamıyorsa None.

    Toplam satış sıfır, negatif ya da boşsa ya da ihracat geliri boş/negatifse pay
    hesaplanamaz; kur riski bu durumda "nötr" kalır.
    """
    external, total = _number(external_sales), _number(total_sales)
    if external is None or total is None or total <= 0 or external < 0:
        return None
    return external / total


def fx_covered(deposit: object, cash_fx: object, noncash_fx: object) -> Optional[bool]:
    """Bankadaki döviz mevduatı (+ FX_COV_BUFFER) bankadaki döviz kredi bakiyesini karşılıyor mu.

    Döviz kredi bakiyesi = cash_bal_bank_usd + noncash_bal_bank_usd. Bakiye boş ya da <= 0 ise ya da mevduat
    boşsa None: bankada döviz kredisi olmaması "karşılanıyor" demek değildir, bilgi yokluğudur.
    """
    dep = _number(deposit)
    parts = [n for n in (_number(cash_fx), _number(noncash_fx)) if n is not None]
    exposure = sum(parts) if parts else None
    if dep is None or exposure is None or exposure <= 0:
        return None
    return dep + FX_COV_BUFFER >= exposure


def classify_fx(net_fx_position: object, share: Optional[float], covered: Optional[bool] = None) -> Optional[str]:
    """Kur şokuna dayanıklılık: "dayanıksız", "nötr" ya da "dirençli"; hiç kur bilgisi yoksa None.

    Yukarıdan ilk uyan kural:
      1. covered (bankadaki mevduat bankadaki döviz kredisini karşılıyor)                    -> dirençli
      2. net döviz pozisyonu > 0 ve ihracat payı > %80                             -> dirençli
      3. net döviz pozisyonu < 0 ve ihracat payı < %40                             -> dayanıksız
      4. diğer her durum (eşikteki değerler, pay bilinmeyen, covered None olanlar) -> nötr
    Net pozisyon da banka kapsaması da bilinmiyorsa alt skor hesaplanmaz (None); varsayılan "nötr"e çekilmez.
    """
    if covered:
        return "dirençli"
    position = _number(net_fx_position)
    if position is None and covered is None:
        return None
    if position is not None and share is not None:
        if position > 0 and share > FX_EXPORT_SHARE_HIGH:
            return "dirençli"
        if position < 0 and share < FX_EXPORT_SHARE_LOW:
            return "dayanıksız"
    return "nötr"


def _ratio(numerator: object, denominator: object) -> Optional[float]:
    """Oran; pay boşsa ya da payda boş, sıfır ya da negatifse None (negatif yükümlülük veri hatasıdır)."""
    num, den = _number(numerator), _number(denominator)
    if num is None or den is None or den <= 0:
        return None
    return num / den


def _band(value: float, bands, top: str) -> str:
    """Değerin bandı: bands (üst sınır, etiket) artan sırada; sınır değeri alt banda girer."""
    for limit, label in bands:
        if value <= limit:
            return label
    return top


def classify_liquidity(ratio: Optional[float]) -> Optional[str]:
    """Likit aktif / KV borç bandı: kritik (<=0,50), zayıf (<=0,80), marjinal (<=1,00), yeterli (<=1,50), güçlü."""
    if ratio is None or ratio < 0:
        return None
    return _band(ratio, LIQUIDITY_BANDS, "güçlü")


def classify_icr(ebitda: object, interest: object) -> Optional[str]:
    """FAVÖK / faiz gideri bandı; faiz gideri boş, sıfır ya da negatifse None (faizsiz şirket "riskli" sayılmaz).

    FAVÖK negatifse "operasyonel zarar" (oran negatif çıkar, ayrı adlandırılır); < 1,0 "faiz ödenemiyor";
    sonra çok düşük (<=1,5), düşük (<=2,5), yeterli (<=4,0), güçlü.
    """
    icr = _ratio(ebitda, interest)
    if icr is None:
        return None
    if icr < 0:
        return "operasyonel zarar"
    if icr < ICR_MIN:
        return "faiz ödenemiyor"
    return _band(icr, ICR_BANDS, "güçlü")


def trend_unusable_reason(row: Mapping[str, object]) -> Optional[str]:
    """Trend kullanılamıyorsa nedeni (önceki tablo yok / dönem arası / para birimi değişti), kullanılabiliyorsa None."""
    current, previous = row.get("statement_dt"), row.get("prev_statement_dt")
    if previous is None or pd.isna(previous) or current is None or pd.isna(current):
        return "önceki tablo yok"
    gap = (current.year - previous.year) * 12 + current.month - previous.month
    if not TREND_GAP_MONTHS[0] <= gap <= TREND_GAP_MONTHS[1]:
        return f"tablolar arası {gap} ay"
    ccy, prev_ccy = row.get("ccy_code"), row.get("prev_ccy_code")
    if isinstance(ccy, str) and isinstance(prev_ccy, str) and ccy.strip() != prev_ccy.strip():
        return f"para birimi {prev_ccy.strip()} -> {ccy.strip()}"
    return None


def classify_trend(label: object, usable: bool) -> Optional[str]:
    """Trend durum etiketi; kullanılamıyorsa ya da TREND_RISK'te olmayan bir etiketse None (uydurma duruma puan sızmaz)."""
    if not usable or not isinstance(label, str):
        return None
    label = label.strip().lower()
    return label if label in TREND_RISK else None


def classify_dscr(value: object) -> Optional[str]:
    """DSCR bandı; değer yoksa None.

      < 1,00          negatif nakit akışı   borç servisi nakit akışıyla karşılanamıyor
      = 1,00          başabaş               güvenlik marjı sıfır
      1,00 - 1,20     düşük güvenlik rezervi  bankalar ek teminat/covenant ister
      1,20 - 1,50     standart (sağlıklı)   ticari kredide aranan ideal taban
      1,50 - 2,00     standart üstü         (kaynak tabloda tanımsız aralık)
      > 2,00          çok güçlü             borç yükümlülüğünün iki katından fazla kazanç
    Aralıklar üstten kapalıdır; sınır değerleri alt banda girer (1,20 -> düşük rezerv).
    """
    number = _number(value)
    if number is None:
        return None
    if number < DSCR_BREAKEVEN:
        return "negatif nakit akışı"
    if number == DSCR_BREAKEVEN:
        return "başabaş"
    if number <= DSCR_LOW_RESERVE_MAX:
        return "düşük güvenlik rezervi"
    if number <= DSCR_STANDARD_MAX:
        return "standart (sağlıklı)"
    if number <= DSCR_ADEQUATE_MAX:
        return "standart üstü"
    return "çok güçlü"


# =============================================================================
# 3. Finansal skor
# =============================================================================
# Rapor kolonlarında kullanılan Türkçe alt skor adları.
COMPONENT_NAMES = {"equity": "Özkaynak", "ebitda": "FAVÖK", "fx": "Kur", "dscr": "DSCR",
                   "liquidity": "Likidite", "icr": "ICR",
                   "equity_trend": "Özkaynak_Trend", "ebitda_trend": "FAVÖK_Trend"}


def calculate_financial_score(row: Mapping[str, object]) -> Dict[str, object]:
    """Bir borçlunun finansal göstergelerinden alt skorları ve toplam finansal skoru üretir.

    Dönen sözlük her alt skor için durum ve puan (<Ad>_Durumu, <Ad>_Puan), ihracat payı, bankadaki döviz
    kapsaması, DSCR / likidite / ICR değerleri, trendin kullanılamama nedeni, DSCR-ICR ayrışması,
    Finansal_Skor (0-100) ve Finansal_Veri_Kapsami (0-1) içerir. Hesaplanamayan alt skor 0 puan alır:
    skor hesaplanabilen alt skorların toplamıdır (tavanların toplamına göre 0-100) ve eksik veri
    nedeniyle ölçeklenmez. Hiçbiri hesaplanamazsa Finansal_Skor None olur.
    """
    share = export_share(row.get("external_sales_try"), row.get("total_sales_try"))
    covered = fx_covered(row.get("bank_fx_deposit"), row.get("cash_bal_bank_usd"), row.get("noncash_bal_bank_usd"))
    liquidity = _ratio(row.get("liquid_assets_try"), row.get("totalcurrentliabilities_try"))
    icr = _ratio(row.get("ebitda_try"), row.get("interestexpense_try"))
    dscr = _number(row.get("dscrebitda_try"))
    trend_reason = trend_unusable_reason(row)
    labels = {
        "equity": classify_sign(row.get("equity_try")),
        "ebitda": classify_sign(row.get("ebitda_try")),
        "fx": classify_fx(row.get("net_fx_pos_try"), share, covered),
        "dscr": classify_dscr(dscr),
        "liquidity": classify_liquidity(liquidity),
        "icr": classify_icr(row.get("ebitda_try"), row.get("interestexpense_try")),
        "equity_trend": classify_trend(row.get("equity_trend"), trend_reason is None),
        "ebitda_trend": classify_trend(row.get("ebitda_trend"), trend_reason is None),
    }
    points: Dict[str, Optional[float]] = {
        key: (FINANCIAL_MAX_POINTS[key] * RISK_TABLES[key][label] if label is not None else None)
        for key, label in labels.items()
    }
    available = [key for key, value in points.items() if value is not None]
    total_max = sum(FINANCIAL_MAX_POINTS.values())
    available_max = sum(FINANCIAL_MAX_POINTS[key] for key in available)

    result: Dict[str, object] = {}
    for key, name in COMPONENT_NAMES.items():
        result[f"{name}_Durumu"] = labels[key]
        result[f"{name}_Puan"] = points[key]
    result["Ihracat_Payi"] = share
    result["BANKA_FX_Kapsama"] = covered
    result["DSCR_Degeri"] = dscr
    result["Likidite_Degeri"] = liquidity
    # Sert likidite (nakit / KV borç) yalnızca raporlanır, skora girmez.
    result["Likidite_Sert"] = _ratio(row.get("cash_in_banks_try"), row.get("totalcurrentliabilities_try"))
    result["ICR_Degeri"] = icr
    result["Trend_Kullanilamaz"] = trend_reason
    # DSCR "sağlıklı" ama faiz bile zor karşılanıyorsa DSCR kolonu anaparayı dışarıda bırakıyor olabilir.
    result["DSCR_ICR_Ayrisma"] = bool(dscr is not None and icr is not None
                                      and dscr > DSCR_ICR_SAGLIKLI_DSCR and icr < ICR_AYRISMA_ESIK)
    result["Finansal_Skor"] = (
        100.0 * sum(points[key] for key in available) / total_max if available and total_max else None  # type: ignore[misc]
    )
    result["Finansal_Veri_Kapsami"] = available_max / total_max if total_max else 0.0
    return result


def financial_stage(score: object) -> Optional[int]:
    """Finansal skorun stage'i (1/2/3): FIN_STAGE2_ESIK ve FIN_STAGE3_ESIK; skor yoksa None."""
    number = _number(score)
    if number is None:
        return None
    return 3 if number >= FIN_STAGE3_ESIK else 2 if number >= FIN_STAGE2_ESIK else 1


def describe_points(row: Mapping[str, object]) -> str:
    """Alt skor puan dökümünü tek satırda yazar: "Özkaynak 25/25 (negatif) | FAVÖK 0/25 (pozitif) | ...".

    Her alt skor için alınan puan, puan tavanı ve durum gösterilir; hesaplanamayan alt skor
    "veri yok" yazılır (trendde nedeniyle). row, score_financial_table çıktısındaki bir satırdır.
    """
    parts = []
    for key, name in COMPONENT_NAMES.items():
        points, status = row.get(f"{name}_Puan"), row.get(f"{name}_Durumu")
        label = name.replace("_", " ")
        if status is None or points is None or pd.isna(points):
            reason = row.get("Trend_Kullanilamaz") if key.endswith("_trend") else None
            parts.append(f"{label} veri yok" + (f" ({reason})" if isinstance(reason, str) else ""))
        else:
            parts.append(f"{label} {float(points):g}/{FINANCIAL_MAX_POINTS[key]:g} ({status})")
    return " | ".join(parts)


# =============================================================================
# 4. Veri yükleme
# =============================================================================
def load_financial_data(path: Path = STRUCTURED_PARQUET_PATH) -> pd.DataFrame:
    """Portföyün yapısal verisini (2_sql_to_parquet.py çıktısı) okur ve skorlamaya hazırlar.

    Kolon adları küçük harfe çevrilir, sayısal kolonlar sayıya çevrilir (okunamayan değer
    boş kalır) ve her borçlu için en güncel finansal tablo satırı tutulur. Dosya ya da
    counterparty_id kolonu yoksa hata loglanır ve boş tablo döner; eksik bir metrik kolonu
    uyarı olarak loglanır ve o alt skor tüm borçlular için hesaplanamaz.
    """
    path = Path(path)
    if not path.exists():
        logger.error("Yapısal veri dosyası bulunamadı: %s", path)
        return pd.DataFrame()
    df = pd.read_parquet(path)
    df.columns = [str(c).lower() for c in df.columns]
    if KEY_COLUMN not in df.columns:
        logger.error("Yapısal veride '%s' kolonu yok; finansal skor hesaplanamaz.", KEY_COLUMN)
        return pd.DataFrame()

    missing = [c for c in METRIC_COLUMNS if c not in df.columns]
    if missing:
        logger.warning("Yapısal veride kolon eksik, ilgili alt skorlar hesaplanamaz: %s", ", ".join(missing))
        for column in missing:
            df[column] = float("nan")
    for column in METRIC_COLUMNS:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    missing_trend = [c for c in TREND_COLUMNS + ("prev_statement_dt",) if c not in df.columns]
    if missing_trend:
        logger.warning("Yapısal veride trend kolonu eksik, trend alt skorları hesaplanamaz: %s", ", ".join(missing_trend))
    for column in TREND_COLUMNS + CCY_COLUMNS:
        if column not in df.columns:
            df[column] = None
    missing_balance = [c for c in BALANCE_COLUMNS if c not in df.columns]
    if missing_balance:
        logger.warning("Yapısal veride bakiye kolonu eksik, ilgili bakiyeler boş kalır: %s", ", ".join(missing_balance))
    for column in BALANCE_COLUMNS:
        df[column] = pd.to_numeric(df[column], errors="coerce") if column in df.columns else float("nan")
    if not missing_balance and len(df):
        total = df[list(BALANCE_COLUMNS)].sum(axis=1, min_count=1)
        empty_share = float((total.isna() | (total <= 0)).mean())
        if empty_share > BALANCE_EMPTY_WARN_SHARE:
            logger.warning("Borçluların %%%.0f'inde toplam bakiye 0 ya da boş; bakiye kolonlarının anlamını kontrol edin: %s",
                           empty_share * 100, ", ".join(BALANCE_COLUMNS))

    df[KEY_COLUMN] = pd.to_numeric(df[KEY_COLUMN], errors="coerce")
    invalid_keys = int(df[KEY_COLUMN].isna().sum())
    if invalid_keys:
        logger.warning("%d satırda counterparty_id okunamadı; atlandı.", invalid_keys)
    df = df.dropna(subset=[KEY_COLUMN]).copy()
    df[KEY_COLUMN] = df[KEY_COLUMN].astype("int64")

    for column in DATE_COLUMNS:  # Oracle'dan metin gelirse "31/12/2025" biçimindedir (gün önce)
        df[column] = _to_date(df[column]) if column in df.columns else pd.NaT
    duplicated = int(df.duplicated(KEY_COLUMN).sum())
    if duplicated:
        logger.warning("%d tekrarlayan counterparty_id satırı var; en güncel finansal tablo tutuldu.", duplicated)
        df = df.sort_values("statement_dt", na_position="first", kind="stable").drop_duplicates(KEY_COLUMN, keep="last")
    return df.reset_index(drop=True)


def _to_date(series: pd.Series) -> pd.Series:
    """Tarih kolonu; tarih tipindeyse olduğu gibi, metinse gün önce ("31/12/2025") okunur."""
    if pd.api.types.is_datetime64_any_dtype(series):
        return series
    return pd.to_datetime(series, errors="coerce", dayfirst=True)


def _sum_available(*values: object) -> Optional[float]:
    """Boş olmayan değerlerin toplamı; hepsi boşsa None (bir bakiye türü eksikse diğeri korunur)."""
    numbers = [n for n in (_number(v) for v in values) if n is not None]
    return sum(numbers) if numbers else None


def score_financial_table(df: pd.DataFrame) -> pd.DataFrame:
    """Yüklenmiş yapısal veriden borçlu başına finansal skor tablosu üretir.

    Çıktı "Şirket" (counterparty_id), "Finansal_Unvan", "Finansal_Tarih",
    calculate_financial_score kolonları, skorda kullanılan ham göstergeler (özkaynak, FAVÖK,
    net döviz pozisyonu, ihracat ve toplam satış geliri) ve bakiyeleri (nakit, gayrinakit,
    toplam) içerir. Boş girdi için boş tablo döner.
    """
    columns: List[str] = ["Şirket", "Finansal_Unvan", "Finansal_Tarih"]
    if df is None or df.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for record in df.to_dict("records"):
        scored = calculate_financial_score(record)
        rows.append({
            "Şirket": int(record[KEY_COLUMN]),
            "Finansal_Unvan": record.get("full_nm"),
            "Finansal_Tarih": record.get("statement_dt"),
            **scored,
            "Özkaynak_TRY": _number(record.get("equity_try")),
            "FAVÖK_TRY": _number(record.get("ebitda_try")),
            "Net_FX_Pozisyonu_TRY": _number(record.get("net_fx_pos_try")),
            "Ihracat_Geliri_TRY": _number(record.get("external_sales_try")),
            "Toplam_Satis_TRY": _number(record.get("total_sales_try")),
            "BANKA_FX_Mevduat": _number(record.get("bank_fx_deposit")),
            "BANKA_FX_Kredi": _sum_available(record.get("cash_bal_bank_usd"), record.get("noncash_bal_bank_usd")),
            "Onceki_Tablo_Tarihi": record.get("prev_statement_dt"),
            "Toplam_Bakiye_TRY": _sum_available(record.get("cash_bal_bank"), record.get("noncash_bal_bank")),
            "Nakit_Bakiye_TRY": _number(record.get("cash_bal_bank")),
            "Gayrinakit_Bakiye_TRY": _number(record.get("noncash_bal_bank")),
        })
    table = pd.DataFrame(rows)
    _log_portfolio_checks(table)
    return table


def _log_portfolio_checks(table: pd.DataFrame) -> None:
    """Portföy geneli kontroller: çoğu borçluda hesaplanamayan alt skor (kolon/kaynak sorunu), skor dağılımı,
    DSCR-ICR ayrışması ve banka kapsamasıyla dirençli sayılanlar (yanlış direnç riski; ayrıca izlenmeli)."""
    if table.empty:
        return
    for key, name in COMPONENT_NAMES.items():
        missing = float(table[f"{name}_Durumu"].isna().mean())
        if missing > COMPONENT_MISSING_WARN_SHARE:
            extra = (f" En sık neden: {table['Trend_Kullanilamaz'].value_counts().idxmax()}."
                     if key.endswith("_trend") and table["Trend_Kullanilamaz"].notna().any() else "")
            logger.warning("%s alt skoru borçluların %%%.0f'inde hesaplanamadı; kolonu ve kaynağını kontrol edin.%s",
                           name.replace("_", " "), missing * 100, extra)
    scores = pd.to_numeric(table["Finansal_Skor"], errors="coerce").dropna()
    if len(scores):
        q = scores.quantile([0.25, 0.5, 0.75, 0.9])
        logger.info("Finansal skor (%s borçlu): p25 %.1f, medyan %.1f, p75 %.1f, p90 %.1f; Stage 2 (>= %g) %s, Stage 3 (>= %g) %s. "
                    "Ortalama veri kapsamı %%%.0f.", len(scores), *q, FIN_STAGE2_ESIK, int((scores >= FIN_STAGE2_ESIK).sum()),
                    FIN_STAGE3_ESIK, int((scores >= FIN_STAGE3_ESIK).sum()), 100 * float(table["Finansal_Veri_Kapsami"].mean()))
    ayrisma = int(table["DSCR_ICR_Ayrisma"].sum())
    if ayrisma:
        logger.info("%s borçluda DSCR sağlıklı (> %.1f) ama ICR < %.1f: DSCR kolonu anaparayı dışarıda bırakıyor olabilir.",
                    ayrisma, DSCR_ICR_SAGLIKLI_DSCR, ICR_AYRISMA_ESIK)
    covered = int((table["BANKA_FX_Kapsama"] == True).sum())  # noqa: E712 (None ve False ayrı)
    if covered:
        logger.info("%s borçlu bankadaki döviz kapsamasıyla kur riskinde dirençli sayıldı (diğer bankalardaki döviz borcu görünmez).",
                    covered)


def build_financial_scores(path: Path = STRUCTURED_PARQUET_PATH) -> pd.DataFrame:
    """Yapısal veriyi okuyup finansal skor tablosunu üretir (yükleme + skorlama)."""
    return score_financial_table(load_financial_data(path))