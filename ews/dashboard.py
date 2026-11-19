"""Dashboard: her koşunun sonunda tek dosyalık dashboard.html ve sinyal geçmişi (4_llm_dogrulama.py kullanır).

dashboard.html tarayıcıda çift tıklanarak açılır: sunucu, kurulum ya da internet gerekmez (dış kütüphane yok).
Müşteri adı ve bakiye içerir; Excel çıktısıyla aynı gizlilikte saklanmalıdır.

Geçmiş (dashboard_gecmis.jsonl): her gün için bir satır; aynı gün tekrar koşulursa o günün satırı yenilenir.
"Ne değişti" sayfası bugünü seçilen bir geçmiş koşuyla karşılaştırır (önceki koşu, 1 hafta, 1/3/6 ay ya da listeden bir
tarih; karşılaştırma tarayıcıda yapılır). Olaylar haberleri üzerinden eşleştirilir (iki koşuda ortak haberi olan olay aynı
olaydır), çünkü O1, O2 kodları haber penceresi kaydıkça değişebilir. Geçmiş GECMIS_SAKLA_GUN gün tutulur.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from ews.defter import Olay
from ews.olay import ILGILI, PROMPT_SURUMU, SEVIYE_NO, STAGE_NO, answered, eslestirme, tip_seviyesi

TIP_ETIKET = {
    "konkordato_iflas": "Konkordato / iflas", "temerrut": "Temerrüt", "haciz_el_koyma": "Haciz / el koyma",
    "faaliyet_sona_erme": "Faaliyetin sona ermesi", "borc_yapilandirma": "Borç yapılandırma",
    "dava_ceza": "Dava / soruşturma", "finansal_bozulma": "Finansal bozulma", "faaliyet_kesintisi": "Faaliyet kesintisi",
    "not_dusurme": "Not düşürme", "yonetim_krizi": "Yönetim krizi", "idari_ceza": "İdari ceza",
    "kar_dususu": "Kâr düşüşü", "operasyonel_aksaklik": "Operasyonel aksaklık", "dis_baski": "Dış baskı",
    "olumlu": "Olumlu", "rutin": "Rutin", "hicbiri": "Olay yok",
}
DIGER_MAKS = 25              # şirket detayında "Diğer haberler"de gösterilen en fazla haber (en ağırları önce)
TREND_MAKS = 200             # trend grafiğindeki en fazla gün
GECMIS_GOMULU_GUN = 200      # "Ne değişti" için dashboard'a gömülen geçmiş (6 aylık karşılaştırmaya yeter)
GECMIS_SAKLA_GUN = 400       # dashboard_gecmis.jsonl'da tutulan en eski gün


# =============================================================================
# Yardımcılar
# =============================================================================
def _v(x):
    """JSON'a yazılabilir değer: NaN -> None, numpy -> Python, tarih -> 'YYYY-MM-DD'."""
    if x is None:
        return None
    if hasattr(x, "item") and not isinstance(x, (str, bytes)):
        x = x.item()
    if isinstance(x, float) and math.isnan(x):
        return None
    if isinstance(x, (pd.Timestamp, datetime, date)):
        return str(x)[:10]
    return x


def _stage(label) -> Optional[int]:
    return STAGE_NO.get(label) if isinstance(label, str) else None


def _news_key(anahtar: str) -> str:
    """Önbellek anahtarından prompt sürümü atılır: sürüm değişince olaylar 'yeni' görünmesin."""
    return anahtar.rsplit("|", 1)[0]


# =============================================================================
# Sayfaların verisi
# =============================================================================
def _companies(combined: pd.DataFrame, companies: pd.DataFrame) -> List[dict]:
    """Portföydeki her şirket: birleşik, haber ve finansal seviye, bakiye, haber istatistikleri."""
    news = companies.set_index("Şirket")
    rows = []
    for r in combined.to_dict("records"):
        n = news.loc[r["Şirket"]] if r["Şirket"] in news.index else None
        g = (lambda c: _v(n[c])) if n is not None else (lambda c: None)
        rows.append({
            "k": str(r["Şirket"]), "ad": _v(r["Şirket_Adi"]) or str(r["Şirket"]),
            "b": _stage(r["Birlesik_Seviye"]), "h": _stage(r["Haber_Sinyali"]), "f": _stage(r["Finansal_Seviye"]),
            "kural": _v(r["Birlesik_Kural"]), "bakiye": _v(r["Toplam_Bakiye_TRY"]),
            "bs": _v(r["Birlesik_Skor"]), "hs": _v(r["Haber_Skoru"]), "hesap": _v(r["Skor_Hesabi"]),
            "olaylar": _v(r["Risk_Olaylari"]), "en_agir": _v(r["En_Agir_Olay"]), "en_agir_durum": _v(r["En_Agir_Durum"]),
            "risk_turu": _v(r["En_Agir_Risk_Turu"]), "skor": _v(r["Finansal_Skor"]), "dscr": _v(r["DSCR_Degeri"]),
            "puan": _v(r["Finansal_Puan_Detay"]), "ftarih": _v(r["Finansal_Tarih"]), "not": _v(r["Not"]),
            "eslesme": g("Eslesme"), "odak": g("Odak_Haber"),
            "red": g("Reddedilen"), "olumlu": g("Olumlu_Olay"), "acik_kritik": g("Acik_Kritik") or 0,
            "acik_yuksek": g("Acik_Yuksek") or 0,
        })
    return rows


def _events(df: pd.DataFrame, ledgers: Dict[object, List[Olay]]) -> Dict[str, List[dict]]:
    """Şirket başına olay defteri; her haberin başlığı ve alıntısıyla."""
    recs = df.set_index("_Anahtar")[["Başlık", "LLM_Alinti"]].to_dict("index")
    out = {}
    for key, events in ledgers.items():
        out[str(key)] = [{
            "kod": e.kod, "tip": e.tip, "seviye": e.seviye, "etki": e.stage, "durum": e.durum, "risk": e.risk_turu, "eski": e.eski,
            "tanim": _v(e.tanim), "acilis": e.acilis, "kapanis": e.kapanis or None,
            "haberler": [{"tarih": h["tarih"], "islem": h["islem"], "kaynak": _v(h["kaynak"]), "url": _v(h["url"]),
                          "baslik": _v(recs.get(h["anahtar"], {}).get("Başlık")),
                          "alinti": _v(recs.get(h["anahtar"], {}).get("LLM_Alinti"))} for h in e.haberler],
        } for e in sorted(events, key=lambda e: (-e.stage, e.acilis))]
    return out


def _other_news(df: pd.DataFrame, ledgers: Dict[object, List[Olay]]) -> Dict[str, List[dict]]:
    """Olay defterine girmeyen bütün ilgili haberler: odak ya da ikincil, olay tipi "hicbiri" değil.

    Defterde olanlar (odak kritik/yüksek, ikincil kritik) burada tekrar gösterilmez. Kalanlar: ikincil haberdeki yüksek
    olaylar, orta seviyeli olaylar, olumlu ve rutin haberler, alıntısı doğrulanamayan olaylar. En ağırları ve en yenileri önce.
    """
    in_ledger = {h["anahtar"] for evs in ledgers.values() for e in evs for h in e.haberler}
    mask = df["LLM_Alaka"].isin(ILGILI) & (df["LLM_Olay"] != "hicbiri") & ~df["_Anahtar"].isin(in_ledger)
    sub = df[mask].assign(_lv=df.loc[mask, "LLM_Olay"].map(lambda t: SEVIYE_NO.get(tip_seviyesi(t), 0)),
                          _t=eslestirme.to_naive_datetime(df.loc[mask, "Tarih"]))
    sub = sub.sort_values(["_lv", "_t"], ascending=False)
    out: Dict[str, List[dict]] = {}
    for r in sub.to_dict("records"):
        items = out.setdefault(str(r["Şirket"]), [])
        if len(items) < DIGER_MAKS:
            items.append({"tarih": _v(r["Tarih"]), "tip": r["LLM_Olay"], "baslik": _v(r["Başlık"]), "url": _v(r["URL"]),
                          "kaynak": _v(r["Kaynak"]), "ozet": _v(r["LLM_Ozet"]), "alaka": r["LLM_Alaka"],
                          "seviye": int(r["_lv"]), "dogru": _v(r["Alinti_Dogrulandi"]), "durum": _v(r["LLM_Durum"])})
    return out


def _themes(themes: pd.DataFrame, companies: List[dict]) -> List[dict]:
    keys = {c["ad"]: c["k"] for c in companies}
    return [{"tema": _v(t["Tema"]), "aciklama": _v(t["Aciklama"]), "ifade": _v(t.get("Ortak_Ifade")),
             "bakiye": _v(t["Toplam_Bakiye_TRY"]), "sinyal": _stage(t["En_Yuksek_Haber_Sinyali"]), "tipler": _v(t["Olay_Tipleri"]),
             "sirketler": [{"ad": ad, "k": keys.get(ad)} for ad in str(t["Sirketler"]).split("; ")],
             "maddeler": [{k: (str(v) if k == "k" else _v(v)) for k, v in m.items()} for m in (t.get("_maddeler") or [])]}
            for t in themes.to_dict("records")]


def _quality(df: pd.DataFrame, aliases: pd.DataFrame) -> dict:
    """Alaka dağılımı, ilgili haberlerdeki olay tipleri, alıntı doğrulama ve en çok reddedilen alias'lar."""
    asked = df[answered(df)]
    ilgili = asked[asked["LLM_Alaka"].isin(ILGILI)]
    checked = df["Alinti_Dogrulandi"].notna()
    top = aliases[aliases["Reddedilen"] > 0].sort_values(["Reddedilen", "Red_Orani"], ascending=False).head(15)
    return {
        "olaylar": [{"tip": t, "seviye": SEVIYE_NO.get(tip_seviyesi(t), 0), "n": int(n)}
                    for t, n in ilgili["LLM_Olay"].value_counts().items()],
        "alaka": {k: int(v) for k, v in df["LLM_Alaka"].value_counts().items()},
        "alinti_kontrol": int(checked.sum()), "alinti_dogru": int(df.loc[checked, "Alinti_Dogrulandi"].eq(True).sum()),
        "aliaslar": [{"alias": _v(a["Eşleşen_Alias"]), "ad": _v(a["Şirket_Adi"]), "eslesme": _v(a["Eslesme"]),
                      "red": _v(a["Reddedilen"]), "oran": _v(a["Red_Orani"]), "cikar": bool(a["Cikar_Oner"])}
                     for a in top.to_dict("records")],
    }


# =============================================================================
# Geçmiş ve "Ne değişti"
# =============================================================================
def _snapshot(day: str, companies: List[dict], events: Dict[str, List[dict]], ledgers: Dict[object, List[Olay]]) -> dict:
    """Bugünün özeti: Stage 2+ şirketler (diğerleri Stage 1 sayılır) ve olaylar, haber anahtarlarıyla."""
    news_keys = {(str(k), e.kod): [_news_key(h["anahtar"]) for h in e.haberler] for k, evs in ledgers.items() for e in evs}
    risky = [c for c in companies if (c["b"] or 1) > 1]
    return {
        "tarih": day,
        "sirketler": {c["k"]: [c["b"] or 1, c["h"] or 1] for c in companies if (c["b"] or 1) > 1 or (c["h"] or 1) > 1},
        "olaylar": [{"s": k, "kod": e["kod"], "tip": e["tip"], "seviye": e["seviye"], "durum": e["durum"], "eski": e["eski"],
                     "h": news_keys[(k, e["kod"])]} for k, evs in events.items() for e in evs],
        "ozet": {"tarih": day, "s3": sum(c["b"] == 3 for c in risky), "s2": sum(c["b"] == 2 for c in risky),
                 "h3": sum(c["h"] == 3 for c in companies), "h2": sum(c["h"] == 2 for c in companies),
                 "bakiye3": sum(c["bakiye"] or 0 for c in risky if c["b"] == 3),
                 "bakiye2": sum(c["bakiye"] or 0 for c in risky if c["b"] == 2)},
    }


def _load_history(path: Path) -> List[dict]:
    history = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                history.append(json.loads(line))
            except ValueError:
                continue
    return history


def _save_history(path: Path, history: List[dict]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(h, ensure_ascii=False) + "\n" for h in history), encoding="utf-8")
    tmp.replace(path)


def _compact(snap: dict) -> dict:
    """Dashboard'a gömülen geçmiş satırı: haber anahtarları 10 karakterlik özetlere çevrilir (dosya boyutu için)."""
    short = lambda key: hashlib.md5(key.encode("utf-8")).hexdigest()[:10]  # noqa: E731
    return {"tarih": snap["tarih"], "s": snap["sirketler"],
            "o": [{**{k: e.get(k) for k in ("s", "kod", "tip", "seviye", "durum", "eski")}, "h": [short(x) for x in e["h"]]}
                  for e in snap.get("olaylar", [])]}


# =============================================================================
# Yazma
# =============================================================================
def write_dashboard(df: pd.DataFrame, companies: pd.DataFrame, combined: pd.DataFrame,
                    ledgers: Dict[object, List[Olay]], themes: pd.DataFrame, aliases: pd.DataFrame,
                    errors: int, html_path: Path, history_path: Path) -> Path:
    """dashboard.html'i yazar ve geçmişe bugünün satırını ekler (aynı gün tekrar koşulursa yeniler)."""
    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    comp = _companies(combined, companies)
    events = _events(df, ledgers)
    snap = _snapshot(day, comp, events, ledgers)

    oldest = (now - timedelta(days=GECMIS_SAKLA_GUN)).strftime("%Y-%m-%d")
    history = [h for h in _load_history(history_path) if h.get("tarih") != day and h.get("tarih", "") >= oldest]
    history = sorted(history + [snap], key=lambda h: h["tarih"])
    _save_history(history_path, history)
    embed_from = (now - timedelta(days=GECMIS_GOMULU_GUN)).strftime("%Y-%m-%d")

    dates = df["Tarih"].dropna().astype(str)
    data = {
        "meta": {"zaman": now.strftime("%Y-%m-%d %H:%M"), "gun": day, "eslesme": len(df),
                 "cevap": int(answered(df).sum()), "hata": errors, "portfoy": len(comp),
                 "haberli": sum(c["h"] is not None for c in comp), "prompt": PROMPT_SURUMU,
                 "haber_ilk": dates.min()[:10] if len(dates) else None, "haber_son": dates.max()[:10] if len(dates) else None},
        "tip": TIP_ETIKET,
        "sirketler": comp,
        "olaylar": events,
        "diger": _other_news(df, ledgers),
        "temalar": _themes(themes, comp),
        "gecmis": [_compact(h) for h in history if h["tarih"] >= embed_from],  # sonuncusu bugün
        "trend": [h["ozet"] for h in history][-TREND_MAKS:],
        "kalite": _quality(df, aliases),
    }
    payload = json.dumps(data, ensure_ascii=False, allow_nan=False).replace("</", "<\\/")
    html_path.write_text(_TEMPLATE.replace("__VERI__", payload), encoding="utf-8")
    return html_path


# =============================================================================
# Sayfa şablonu (HTML + CSS + JS; veri __VERI__ yerine gömülür)
# =============================================================================
_TEMPLATE = r"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Erken Uyarı Paneli</title>
<style>
:root{--bg:#f5f6f8;--panel:#fff;--text:#1c2330;--muted:#667085;--line:#e3e6eb;--accent:#2f5bd3;--hover:#f0f3fa;
--s3:#c4302b;--s3bg:#fdecea;--s2:#a86200;--s2bg:#fff3dc;--s1:#2f7d3a;--s1bg:#e7f4e8;--shadow:0 1px 2px rgba(16,24,40,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#0f1217;--panel:#171b22;--text:#e5e7eb;--muted:#98a2b3;--line:#2a313c;--accent:#7aa2ff;
--hover:#1e2430;--s3:#ff6b63;--s3bg:#3b1d1c;--s2:#ffb44d;--s2bg:#3a2a12;--s1:#7fcf88;--s1bg:#1a2e1d;--shadow:none}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 "Segoe UI",system-ui,-apple-system,Roboto,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header{background:var(--panel);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
.hwrap{max-width:1280px;margin:0 auto;padding:14px 20px 0}
h1{font-size:19px;margin:0}h2{font-size:15px;margin:0 0 10px}h3{font-size:14px;margin:18px 0 8px}
.sub{color:var(--muted);font-size:12.5px;margin-top:3px}
nav{display:flex;gap:4px;margin-top:10px;overflow-x:auto}
nav button{border:0;background:none;color:var(--muted);font:inherit;font-weight:600;padding:9px 12px;border-bottom:2px solid transparent;cursor:pointer;white-space:nowrap}
nav button.on{color:var(--text);border-bottom-color:var(--accent)}
main{max-width:1280px;margin:0 auto;padding:18px 20px 40px}
.grid{display:grid;gap:12px}.k4{grid-template-columns:repeat(auto-fit,minmax(min(190px,100%),1fr))}.k2{grid-template-columns:repeat(auto-fit,minmax(min(380px,100%),1fr))}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;box-shadow:var(--shadow);min-width:0}
.kpi .n{font-size:26px;font-weight:700;line-height:1.1}.kpi .l{color:var(--muted);font-size:12.5px}.kpi .x{font-size:12.5px;margin-top:4px}
.kpi.s3{border-left:4px solid var(--s3)}.kpi.s2{border-left:4px solid var(--s2)}.kpi.s1{border-left:4px solid var(--s1)}
.chip{display:inline-block;font-size:11.5px;font-weight:700;padding:1px 7px;border-radius:999px;white-space:nowrap}
.chip.s3{color:var(--s3);background:var(--s3bg)}.chip.s2{color:var(--s2);background:var(--s2bg)}.chip.s1{color:var(--s1);background:var(--s1bg)}
.badge{display:inline-block;font-size:11.5px;padding:1px 7px;border-radius:6px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.badge.open{color:var(--s3);border-color:var(--s3)}
.muted{color:var(--muted)}.small{font-size:12.5px}.r{text-align:right}.nowrap{white-space:nowrap}
.banner{background:var(--s2bg);color:var(--s2);border-radius:8px;padding:8px 12px;margin-bottom:12px;font-weight:600}
.bar{display:flex;height:14px;border-radius:7px;overflow:hidden;background:var(--line);margin:8px 0}
.bar div{height:100%}.b3{background:var(--s3)}.b2{background:var(--s2)}.b1{background:var(--s1)}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12.5px;color:var(--muted)}.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.hbar{display:grid;grid-template-columns:minmax(120px,190px) 1fr 44px;gap:8px;align-items:center;margin:5px 0;font-size:12.5px}
.hbar .t{height:10px;border-radius:5px;background:var(--accent);opacity:.8}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;white-space:nowrap}
th.sort{cursor:pointer;user-select:none}th.sort:hover{color:var(--text)}
tbody tr[data-k]{cursor:pointer}tbody tr[data-k]:hover{background:var(--hover)}
.tw{overflow-x:auto}
.controls{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.controls input[type=search]{flex:1 1 240px;min-width:0;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--text);font:inherit}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{border:0;background:var(--panel);color:var(--text);font:inherit;padding:7px 11px;cursor:pointer;border-right:1px solid var(--line)}
.seg button:last-child{border-right:0}.seg button.on{background:var(--accent);color:#fff}
label.ck{display:inline-flex;gap:6px;align-items:center;color:var(--muted)}
.controls select{padding:7px 8px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--text);font:inherit}
.theme{margin-bottom:12px}.theme .names{margin-top:8px;display:flex;flex-wrap:wrap;gap:6px}
.theme .names a,.theme .names span{font-size:12.5px;padding:2px 8px;border:1px solid var(--line);border-radius:6px}
.ev{border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:10px 0;background:var(--panel)}
.ev.acik{border-left:4px solid var(--s3)}.ev.kapandi{border-left:4px solid var(--muted)}
.ev-h{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.ev-t{margin:6px 0 2px}
ol.tl{list-style:none;margin:8px 0 0;padding:0 0 0 14px;border-left:2px solid var(--line)}
ol.tl li{margin:0 0 10px;position:relative}
ol.tl li:before{content:"";position:absolute;left:-20px;top:6px;width:10px;height:10px;border-radius:50%;background:var(--panel);border:2px solid var(--muted)}
ol.tl li.yeni:before{border-color:var(--s3)}ol.tl li.kapat:before{border-color:var(--s1);background:var(--s1)}
blockquote{margin:4px 0 0;padding:4px 10px;border-left:3px solid var(--line);color:var(--muted);font-size:12.5px}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}
.facts div{background:var(--bg);border-radius:8px;padding:8px 10px}.facts b{display:block;font-size:12px;color:var(--muted);font-weight:600}
.overlay{position:fixed;inset:0;background:rgba(10,14,20,.45);z-index:20}
.drawer{position:fixed;top:0;right:0;bottom:0;width:min(760px,100%);background:var(--bg);z-index:21;overflow-y:auto;box-shadow:-8px 0 24px rgba(0,0,0,.18)}
.drawer .dh{position:sticky;top:0;background:var(--panel);border-bottom:1px solid var(--line);padding:14px 18px;display:flex;gap:12px;align-items:flex-start}
.drawer .dh h2{flex:1;margin:0;font-size:16px}.drawer .db{padding:4px 18px 30px}
.xbtn{border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:8px;width:32px;height:32px;cursor:pointer;font-size:18px;line-height:1}
svg text{fill:var(--muted);font-size:11px}
.empty{color:var(--muted);padding:18px;text-align:center}
footer{max-width:1280px;margin:0 auto;padding:0 20px 30px;color:var(--muted);font-size:12px}
body.noscroll{overflow:hidden}
@media (max-width:600px){.hwrap,main,footer{padding-left:16px;padding-right:16px}.k4{grid-template-columns:1fr 1fr}.kpi .n{font-size:22px}.hbar{grid-template-columns:110px 1fr 36px}}
</style>
</head>
<body>
<header><div class="hwrap">
  <h1>Erken Uyarı Paneli</h1>
  <div class="sub" id="meta"></div>
  <nav id="tabs">
    <button data-tab="ozet" class="on">Özet</button>
    <button data-tab="degisim">Ne değişti</button>
    <button data-tab="sirketler">Şirketler</button>
    <button data-tab="temalar">Temalar</button>
    <button data-tab="kalite">Sistem kalitesi</button>
  </nav>
</div></header>
<main>
  <section class="tab" id="ozet"></section>
  <section class="tab" id="degisim" hidden></section>
  <section class="tab" id="sirketler" hidden></section>
  <section class="tab" id="temalar" hidden></section>
  <section class="tab" id="kalite" hidden></section>
</main>
<footer>Bu dosya müşteri adı ve bakiye içerir; Excel çıktısıyla aynı gizlilikte saklayın. Seviyeler: Stage 3 kırmızı alarm, Stage 2 yakın izleme, Stage 1 düşük risk.</footer>
<div class="overlay" id="overlay" hidden></div>
<aside class="drawer" id="drawer" hidden aria-modal="true" role="dialog">
  <div class="dh"><h2 id="dtitle"></h2><button class="xbtn" id="dclose" aria-label="Kapat">×</button></div>
  <div class="db" id="dbody"></div>
</aside>
<script id="veri" type="application/json">__VERI__</script>
<script>
(function () {
"use strict";
var D = JSON.parse(document.getElementById("veri").textContent);
function $(s, el) { return (el || document).querySelector(s); }
function $$(s, el) { return Array.prototype.slice.call((el || document).querySelectorAll(s)); }
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]; }); }
function link(url, text) { return /^https?:\/\//i.test(url || "") ? '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(text || url) + "</a>" : esc(text); }
var ST = {3: ["Stage 3", "Kırmızı alarm"], 2: ["Stage 2", "Yakın izleme"], 1: ["Stage 1", "Düşük risk"]};
function chip(s) { return s ? '<span class="chip s' + s + '" title="' + ST[s][1] + '">' + ST[s][0] + "</span>" : '<span class="muted">–</span>'; }
var nf1 = new Intl.NumberFormat("tr-TR", {maximumFractionDigits: 1}), nf0 = new Intl.NumberFormat("tr-TR", {maximumFractionDigits: 0});
function tl(v) { return v == null ? "–" : Math.abs(v) >= 1e9 ? nf1.format(v / 1e9) + " mr TL" : nf0.format(v / 1e6) + " mn TL"; }
function pct(a, b) { return b ? "%" + nf0.format(100 * a / b) : "–"; }
function tip(t) { return D.tip[t] || t || ""; }
var RISK = {kredi: "kredi riski", hukuki_itibar: "hukuki / itibar"};
var ISLEM = {yeni: "açıldı", mevcut: "haber", kapat: "KAPANDI"};
var BY = {}; D.sirketler.forEach(function (s) { BY[s.k] = s; });
function name(k) { return BY[k] ? BY[k].ad : k; }
function sum(a) { return a.reduce(function (t, s) { return t + (s.bakiye || 0); }, 0); }
function nm(ad, k) { return esc(ad) + ' <span class="small muted nowrap">' + esc(k) + "</span>"; }  // ad + cust_id
function cmpRisk(a, b) { return b.b - a.b || (b.bs || 0) - (a.bs || 0) || (b.bakiye || 0) - (a.bakiye || 0); }
function skor(v) { return v == null ? "–" : nf1.format(v); }  // yuvarlamadan, en fazla bir ondalık
function sc(s, v) { return chip(s) + (v != null ? ' <span class="small">' + skor(v) + "</span>" : ""); }

// ---- üst bilgi
var M = D.meta;
$("#meta").innerHTML = "Koşu: " + esc(M.zaman) + " · Haberler: " + esc(M.haber_ilk) + " – " + esc(M.haber_son) +
  " · Portföy: " + M.portfoy + " şirket, " + M.haberli + " tanesinin haberi var · " + nf0.format(M.eslesme) + " eşleşme";

// ---- sekmeler
function show(tab) {
  $$("#tabs button").forEach(function (b) { b.classList.toggle("on", b.dataset.tab === tab); });
  $$(".tab").forEach(function (t) { t.hidden = t.id !== tab; });
  try { localStorage.setItem("ews_sekme", tab); } catch (e) {}
}
$("#tabs").addEventListener("click", function (e) { var b = e.target.closest("button"); if (b) show(b.dataset.tab); });
document.addEventListener("click", function (e) {
  var g = e.target.closest("[data-go]"); if (g) { e.preventDefault(); show(g.dataset.go); return; }
  var r = e.target.closest("[data-k]"); if (r && !e.target.closest("a[href^=http]")) { e.preventDefault(); openCompany(r.dataset.k); }
});

function companyTable(rows, cols) {
  if (!rows.length) return '<div class="empty">Kayıt yok.</div>';
  var C = {
    ad: ["Şirket", function (s) { return nm(s.ad, s.k); }],
    b: ["Birleşik", function (s) { return '<span class="nowrap">' + sc(s.b, s.bs) + "</span>"; }],
    h: ["Haber", function (s) { return chip(s.h); }],
    f: ["Finansal", function (s) { return chip(s.f); }],
    bakiye: ["Bakiye", function (s) { return '<span class="nowrap">' + tl(s.bakiye) + "</span>"; }, "r"],
    olay: ["En ağır olay", function (s) { return s.en_agir ? esc(s.en_agir) + ' <span class="muted small">(' + esc(s.en_agir_durum) + ")</span>" : '<span class="muted">' + esc(s.olaylar || "") + "</span>"; }],
    kural: ["Seviyenin kaynağı", function (s) { return '<span class="small">' + esc(s.kural) + "</span>"; }]
  };
  return '<div class="tw"><table><thead><tr>' + cols.map(function (c) { return '<th class="' + (C[c][2] || "") + '">' + C[c][0] + "</th>"; }).join("") +
    "</tr></thead><tbody>" + rows.map(function (s) {
      return '<tr data-k="' + esc(s.k) + '">' + cols.map(function (c) { return '<td class="' + (C[c][2] || "") + '">' + C[c][1](s) + "</td>"; }).join("") + "</tr>";
    }).join("") + "</tbody></table></div>";
}

// ---- Özet
function trendSvg(T) {
  var W = 640, H = 190, L = 34, R = 12, TP = 12, B = 26, n = T.length;
  var max = Math.max(1, Math.max.apply(null, T.map(function (t) { return Math.max(t.s2, t.s3); })));
  function x(i) { return L + (W - L - R) * (n === 1 ? 0.5 : i / (n - 1)); }
  function y(v) { return TP + (H - TP - B) * (1 - v / max); }
  function line(key, cls) {
    return '<polyline fill="none" stroke-width="2.2" stroke="var(--' + cls + ')" points="' + T.map(function (t, i) { return x(i) + "," + y(t[key]); }).join(" ") + '"/>' +
      T.map(function (t, i) { return '<circle r="3" fill="var(--' + cls + ')" cx="' + x(i) + '" cy="' + y(t[key]) + '"><title>' + esc(t.tarih) + ": " + t[key] + "</title></circle>"; }).join("");
  }
  var grid = [0, 0.5, 1].map(function (f) { var v = Math.round(max * f); return '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y(v) + '" y2="' + y(v) + '" stroke="var(--line)"/><text x="' + (L - 6) + '" y="' + (y(v) + 4) + '" text-anchor="end">' + v + "</text>"; }).join("");
  return '<svg viewBox="0 0 ' + W + " " + H + '" width="100%" role="img" aria-label="Stage 2 ve Stage 3 şirket sayısı">' + grid + line("s2", "s2") + line("s3", "s3") +
    '<text x="' + x(0) + '" y="' + (H - 6) + '" text-anchor="start">' + esc(T[0].tarih) + '</text><text x="' + x(n - 1) + '" y="' + (H - 6) + '" text-anchor="end">' + esc(T[n - 1].tarih) + "</text></svg>";
}

function renderOzet() {
  var S = D.sirketler, by = {1: [], 2: [], 3: []};
  S.forEach(function (s) { by[s.b || 1].push(s); });
  var total = sum(S) || 1, html = "";
  if (M.hata) html += '<div class="banner">Bu koşuda ' + M.hata + " eşleşme LLM hatası verdi; sinyalleri eksik olabilir. Sonraki koşuda yeniden sorulur.</div>";
  var acikKritik = S.reduce(function (t, s) { return t + (s.acik_kritik || 0); }, 0);
  html += '<div class="grid k4">' + [3, 2, 1].map(function (s) {
    return '<div class="card kpi s' + s + '"><div class="l">' + ST[s][0] + " · " + ST[s][1] + '</div><div class="n">' + by[s].length + '</div><div class="x">' + tl(sum(by[s])) + ' <span class="muted">· bakiyenin ' + pct(sum(by[s]), total) + "</span></div></div>";
  }).join("") + '<div class="card kpi"><div class="l">Açık kritik olay</div><div class="n">' + acikKritik + '</div><div class="x muted">' + M.haberli + " şirketin haberi var</div></div></div>";

  var kural = {}; S.filter(function (s) { return s.b > 1; }).forEach(function (s) { kural[s.kural] = (kural[s.kural] || 0) + 1; });
  var kmax = Math.max.apply(null, [1].concat(Object.keys(kural).map(function (k) { return kural[k]; })));
  html += '<div class="grid k2" style="margin-top:12px"><div class="card"><h2>Bakiyenin seviyelere dağılımı</h2><div class="bar">' +
    [3, 2, 1].map(function (s) { return '<div class="b' + s + '" style="width:' + (100 * sum(by[s]) / total) + '%" title="' + ST[s][0] + ": " + tl(sum(by[s])) + '"></div>'; }).join("") +
    '</div><div class="legend">' + [3, 2, 1].map(function (s) { return '<span><i class="b' + s + '"></i>' + ST[s][0] + " " + pct(sum(by[s]), total) + "</span>"; }).join("") + "</div>" +
    '<h3>Stage 2+ şirketlerde seviyenin kaynağı</h3>' + (Object.keys(kural).length ? Object.keys(kural).sort(function (a, b) { return kural[b] - kural[a]; }).map(function (k) {
      return '<div class="hbar"><span>' + esc(k) + '</span><div class="t" style="width:' + (100 * kural[k] / kmax) + '%"></div><span class="r">' + kural[k] + "</span></div>";
    }).join("") : '<div class="muted small">Stage 2+ şirket yok.</div>') + "</div>";
  html += '<div class="card"><h2>Stage 2 ve Stage 3 şirket sayısı (günlük)</h2>' + (D.trend.length >= 2 ? trendSvg(D.trend) +
    '<div class="legend"><span><i class="b3"></i>Stage 3</span><span><i class="b2"></i>Stage 2</span></div>' :
    '<div class="empty">Trend için en az iki günlük koşu gerekiyor. Bugün ilk kayıt alındı.</div>') + "</div></div>";

  var news = S.filter(function (s) { return s.h > 1; }).sort(cmpRisk);
  html += '<div class="card" style="margin-top:12px"><h2>Haberden alarm alan şirketler <span class="muted small">(' + news.length + ")</span></h2>" +
    companyTable(news.slice(0, 20), ["ad", "b", "h", "f", "bakiye", "olay"]) +
    (news.length > 20 ? '<p class="small"><a href="#" data-go="sirketler">Tümünü Şirketler sekmesinde gör</a></p>' : "") + "</div>";
  if (D.temalar.length) html += '<div class="card" style="margin-top:12px"><h2>Ortak temalar</h2>' + D.temalar.slice(0, 3).map(function (t) {
    return '<div style="margin:6px 0">' + chip(t.sinyal) + " <b>" + esc(t.tema) + '</b> <span class="muted small">' + t.sirketler.length + " şirket · " + tl(t.bakiye) + "</span></div>";
  }).join("") + '<p class="small"><a href="#" data-go="temalar">Temalar sekmesi</a></p></div>';
  $("#ozet").innerHTML = html;
}

// ---- Ne değişti: bugünkü koşu seçilen geçmiş koşuyla karşılaştırılır (karşılaştırma tarayıcıda yapılır)
var SEVIYE = {CRITICAL: 3, HIGH: 2, MODERATE: 1};
var G = D.gecmis || [], NOW = G.length ? G[G.length - 1] : null, PAST = G.slice(0, -1);
var MODLAR = [["1g", "Önceki koşu", 0], ["7g", "1 hafta", 7], ["30g", "1 ay", 30], ["91g", "3 ay", 91], ["182g", "6 ay", 182]];
var DS = {mod: "1g", tarih: null};
try { var kayit = localStorage.getItem("ews_degisim"); if (kayit && MODLAR.some(function (m) { return m[0] === kayit; })) DS.mod = kayit; } catch (e) {}
function gunEkle(iso, n) { var d = new Date(iso + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); }
function karsilastirilan() {
  // Seçime göre geçmiş koşu: hedef tarihte ya da ondan önceki en yakın koşu; o kadar eski kayıt yoksa en eski koşu (notla).
  if (!PAST.length) return null;
  if (DS.mod === "tarih") return {snap: PAST.find(function (h) { return h.tarih === DS.tarih; }) || PAST[PAST.length - 1], not: ""};
  var gun = (MODLAR.find(function (m) { return m[0] === DS.mod; }) || MODLAR[0])[2];
  if (!gun) return {snap: PAST[PAST.length - 1], not: ""};
  var hedef = gunEkle(NOW.tarih, -gun), aday = PAST.filter(function (h) { return h.tarih <= hedef; });
  if (aday.length) {
    var s = aday[aday.length - 1];
    return {snap: s, not: s.tarih !== hedef ? hedef + " tarihinde koşu yok; ondan önceki en yakın koşu kullanıldı." : ""};
  }
  return {snap: PAST[0], not: "Kayıtlı geçmiş " + hedef + " tarihine kadar uzanmıyor; en eski koşu kullanıldı."};
}
function farklar(prev, now) {
  // Bugünkü portföyde seviyesi değişen şirketler ve olay farkları. Olaylar ortak haberleri üzerinden eşleşir.
  var rows = D.sirketler.map(function (c) {
    var once = prev.s[c.k] || [1, 1], simdi = now.s[c.k] || [1, 1];
    if (once[0] === simdi[0] && once[1] === simdi[1]) return null;
    var yon = Math.sign(simdi[0] - once[0]) || Math.sign(simdi[1] - once[1]);
    return {k: c.k, ad: c.ad, bakiye: c.bakiye, once: once, simdi: simdi, yon: yon};
  }).filter(Boolean).sort(function (a, b) { return b.yon - a.yon || b.simdi[0] - a.simdi[0] || (b.bakiye || 0) - (a.bakiye || 0); });
  function ayni(a, b) { return a.s === b.s && a.h.some(function (x) { return b.h.indexOf(x) >= 0; }); }
  var R = {rows: rows, yeni: [], kapanan: [], agirlasan: [], eskiyen: [], cikan: []};
  now.o.forEach(function (e) {
    var m = prev.o.find(function (o) { return ayni(o, e); }), ref = {k: e.s, tip: e.tip, seviye: e.seviye, durum: e.durum};
    if (!m) R.yeni.push(ref);
    else if (m.durum === "acik" && e.durum === "kapandi") R.kapanan.push(ref);
    else if (SEVIYE[e.seviye] > SEVIYE[m.seviye]) R.agirlasan.push(ref);
    else if (e.eski && !m.eski) R.eskiyen.push(ref);
  });
  prev.o.forEach(function (o) {
    if (!now.o.some(function (e) { return ayni(o, e); })) R.cikan.push({k: o.s, tip: o.tip, seviye: o.seviye, durum: o.durum});
  });
  return R;
}
function evRef(e) {
  return '<tr data-k="' + esc(e.k) + '"><td>' + nm(name(e.k), e.k) + "</td><td>" + esc(tip(e.tip)) + "</td><td>" + chip(SEVIYE[e.seviye]) +
    "</td><td>" + (e.durum === "acik" ? '<span class="badge open">açık</span>' : '<span class="badge">kapandı</span>') + "</td></tr>";
}
function renderDegisim() {
  var el = $("#degisim");
  if (!NOW || !PAST.length) {
    el.innerHTML = '<div class="card empty">Karşılaştırılacak önceki koşu yok. Bu ilk koşu; sonraki koşulardan itibaren burada seçtiğiniz döneme göre değişenler görünecek.</div>';
    return;
  }
  var sec = karsilastirilan(), P = sec.snap, C = farklar(P, NOW);
  var up = C.rows.filter(function (r) { return r.yon > 0; }), down = C.rows.filter(function (r) { return r.yon < 0; });
  function rowsTable(rows) {
    if (!rows.length) return '<div class="empty">Yok.</div>';
    return '<div class="tw"><table><thead><tr><th>Şirket</th><th>Birleşik</th><th>Haber</th><th class="r">Bakiye</th><th>Yeni olay</th></tr></thead><tbody>' + rows.map(function (r) {
      var yeni = C.yeni.concat(C.agirlasan).filter(function (e) { return e.k === r.k; }).map(function (e) { return esc(tip(e.tip)); }).join(", ");
      return '<tr data-k="' + esc(r.k) + '"><td>' + nm(r.ad, r.k) + '</td><td class="nowrap">' + chip(r.once[0]) + " → " + chip(r.simdi[0]) +
        '</td><td class="nowrap">' + chip(r.once[1]) + " → " + chip(r.simdi[1]) + '</td><td class="r nowrap">' + tl(r.bakiye) + "</td><td>" +
        (yeni || '<span class="muted">–</span>') + "</td></tr>";
    }).join("") + "</tbody></table></div>";
  }
  function evTable(list) {
    return list.length ? '<div class="tw"><table><thead><tr><th>Şirket</th><th>Olay</th><th>Seviye</th><th>Durum</th></tr></thead><tbody>' +
      list.map(evRef).join("") + "</tbody></table></div>" : '<div class="empty">Yok.</div>';
  }
  var secenekler = PAST.slice().reverse().map(function (h) {
    return '<option value="' + esc(h.tarih) + '"' + (DS.mod === "tarih" && DS.tarih === h.tarih ? " selected" : "") + ">" + esc(h.tarih) + "</option>";
  }).join("");
  el.innerHTML = '<div class="controls"><div class="seg" id="dmod">' + MODLAR.map(function (m) {
      return '<button data-m="' + m[0] + '"' + (DS.mod === m[0] ? ' class="on"' : "") + ">" + m[1] + "</button>";
    }).join("") + "</div>" +
    '<label class="ck">Kayıtlı koşu: <select id="dtarih"><option value="">seçin…</option>' + secenekler + "</select></label>" +
    '<span class="muted small">' + PAST.length + " geçmiş koşu (" + esc(PAST[0].tarih) + " – " + esc(PAST[PAST.length - 1].tarih) + ")</span></div>" +
    '<p class="muted">Karşılaştırma: <b>' + esc(P.tarih) + "</b> → <b>" + esc(NOW.tarih) + "</b>" + (sec.not ? ' <span class="small">· ' + esc(sec.not) + "</span>" : "") + "</p>" +
    '<div class="grid k4">' + [["Seviyesi yükselen", up.length, "s3"], ["Seviyesi düşen", down.length, "s1"], ["Yeni olay", C.yeni.length, "s2"], ["Kapanan olay", C.kapanan.length, ""]].map(function (k) {
      return '<div class="card kpi ' + k[2] + '"><div class="l">' + k[0] + '</div><div class="n">' + k[1] + "</div></div>";
    }).join("") + "</div>" +
    '<div class="grid k2" style="margin-top:12px"><div class="card"><h2>Seviyesi yükselenler</h2>' + rowsTable(up) +
    '</div><div class="card"><h2>Seviyesi düşenler</h2>' + rowsTable(down) + "</div></div>" +
    '<div class="grid k2" style="margin-top:12px"><div class="card"><h2>Yeni olaylar</h2>' + evTable(C.yeni) +
    '</div><div class="card"><h2>Kapanan, ağırlaşan ve eskiyen olaylar</h2>' +
    "<h3>Kapanan</h3>" + evTable(C.kapanan) + "<h3>Ağırlaşan</h3>" + evTable(C.agirlasan) +
    "<h3>90 günü geçen (seviyeye etkisi bir kademe düştü)</h3>" + evTable(C.eskiyen) + "</div></div>" +
    '<div class="card" style="margin-top:12px"><h2>Defterden çıkan olaylar</h2><p class="small muted">Karşılaştırılan koşuda defterde olup bugün olmayan olaylar: ' +
    "haberleri 180 günlük pencereden çıkmış ya da şirket portföyden çıkmış olabilir.</p>" + evTable(C.cikan) + "</div>";
  $("#dmod").addEventListener("click", function (e) {
    var b = e.target.closest("button"); if (!b) return;
    DS.mod = b.dataset.m; DS.tarih = null;
    try { localStorage.setItem("ews_degisim", DS.mod); } catch (x) {}
    renderDegisim();
  });
  $("#dtarih").addEventListener("change", function (e) { if (!e.target.value) return; DS.mod = "tarih"; DS.tarih = e.target.value; renderDegisim(); });
}

// ---- Şirketler
var Q = {q: "", st: 0, haberli: false, sort: "risk", dir: 1};
function renderSirketler() {
  var el = $("#sirketler");
  if (!el.dataset.ready) {
    el.innerHTML = '<div class="controls"><input type="search" id="q" placeholder="Şirket adı ya da cust_id ara…" aria-label="Şirket ara">' +
      '<div class="seg" id="st"><button data-st="0" class="on">Tümü</button><button data-st="3">Stage 3</button><button data-st="2">Stage 2</button><button data-st="1">Stage 1</button></div>' +
      '<label class="ck"><input type="checkbox" id="haberli"> Yalnızca haberi olanlar</label><span class="muted small" id="say"></span></div><div class="card" id="liste"></div>';
    $("#q").addEventListener("input", function (e) { Q.q = e.target.value.toLocaleLowerCase("tr"); draw(); });
    $("#st").addEventListener("click", function (e) { var b = e.target.closest("button"); if (!b) return; Q.st = +b.dataset.st; $$("#st button").forEach(function (x) { x.classList.toggle("on", x === b); }); draw(); });
    $("#haberli").addEventListener("change", function (e) { Q.haberli = e.target.checked; draw(); });
    $("#liste").addEventListener("click", function (e) { var th = e.target.closest("th[data-s]"); if (!th) return; e.stopPropagation(); Q.dir = Q.sort === th.dataset.s ? -Q.dir : 1; Q.sort = th.dataset.s; draw(); });
    el.dataset.ready = "1";
  }
  draw();
}
function draw() {
  var SORT = {risk: cmpRisk, ad: function (a, b) { return a.ad.localeCompare(b.ad, "tr"); },
    h: function (a, b) { return (b.h || 0) - (a.h || 0) || cmpRisk(a, b); }, f: function (a, b) { return (b.f || 0) - (a.f || 0) || cmpRisk(a, b); },
    bakiye: function (a, b) { return (b.bakiye || 0) - (a.bakiye || 0); }};
  var rows = D.sirketler.filter(function (s) {
    return (!Q.st || s.b === Q.st) && (!Q.haberli || s.h != null) && (!Q.q || s.ad.toLocaleLowerCase("tr").indexOf(Q.q) >= 0 || s.k.indexOf(Q.q) >= 0);
  }).sort(function (a, b) { return Q.dir * SORT[Q.sort](a, b); });
  $("#say").textContent = rows.length + " / " + D.sirketler.length + " şirket";
  var heads = [["ad", "Şirket"], ["risk", "Birleşik"], ["h", "Haber"], ["f", "Finansal"], ["bakiye", "Bakiye", "r"], [null, "Olaylar"], [null, "Seviyenin kaynağı"]];
  $("#liste").innerHTML = rows.length ? '<div class="tw"><table><thead><tr>' + heads.map(function (h) {
    return "<th" + (h[0] ? ' class="sort ' + (h[2] || "") + '" data-s="' + h[0] + '"' : "") + ">" + h[1] + (Q.sort === h[0] ? (Q.dir > 0 ? " ▾" : " ▴") : "") + "</th>";
  }).join("") + "</tr></thead><tbody>" + rows.map(function (s) {
    return '<tr data-k="' + esc(s.k) + '"><td>' + nm(s.ad, s.k) + '</td><td class="nowrap">' + sc(s.b, s.bs) + '</td><td class="nowrap">' + sc(s.h, s.hs) + "</td><td>" + chip(s.f) + '</td><td class="r nowrap">' + tl(s.bakiye) +
      '</td><td class="small">' + esc(s.olaylar || "") + '</td><td class="small">' + esc(s.kural) + "</td></tr>";
  }).join("") + "</tbody></table></div>" : '<div class="empty">Filtreye uyan şirket yok.</div>';
}

// ---- Temalar
function renderTemalar() {
  $("#temalar").innerHTML = D.temalar.length ? D.temalar.map(function (t) {
    return '<div class="card theme"><div class="ev-h">' + chip(t.sinyal) + "<h2 style=\"margin:0\">" + esc(t.tema) + '</h2></div><p style="margin:6px 0">' + esc(t.aciklama) +
      '</p><div class="small muted">' + t.sirketler.length + " şirket · toplam bakiye " + tl(t.bakiye) + " · " + esc(t.tipler) + '</div><div class="names">' +
      t.sirketler.map(function (s) { return s.k ? '<a href="#" data-k="' + esc(s.k) + '">' + esc(s.ad) + "</a>" : "<span>" + esc(s.ad) + "</span>"; }).join("") + "</div>" +
      (t.ifade ? '<p class="small muted" style="margin:10px 0 0">Ortak ifade (her haberde arandı): <b>' + esc(t.ifade) + "</b></p>" : "") +
      (t.maddeler.length ? '<ol class="tl" style="margin-top:10px">' + t.maddeler.map(function (m) {
        return '<li><span class="small"><b>' + esc(m.tarih) + "</b> · " + esc(m.ad) + " · " + esc(tip(m.tip)) + "</span><br>" + link(m.url, m.baslik) +
          (m.alinti ? "<blockquote>" + esc(m.alinti) + "</blockquote>" : "") + "</li>";
      }).join("") + "</ol>" : "") + "</div>";
  }).join("") : '<div class="card empty">Bu koşuda birden çok müşteriyi etkileyen ortak tema bulunmadı.</div>';
}

// ---- Sistem kalitesi
function renderKalite() {
  var K = D.kalite, A = K.alaka, toplam = 0; Object.keys(A).forEach(function (k) { toplam += A[k]; });
  var red = (A.alakasiz || 0) + (A.baska_varlik || 0), cevap = toplam - (A.hata || 0) - (A.sorulmadi || 0);
  var riskli = K.olaylar.filter(function (o) { return o.seviye >= 2; }).reduce(function (t, o) { return t + o.n; }, 0);
  var html = '<div class="grid k4">' +
    '<div class="card kpi"><div class="l">LLM\'in elediği eşleşme</div><div class="n">' + pct(red, cevap) + '</div><div class="x muted">' + red + " / " + cevap + " (alakasız ya da başka varlık)</div></div>" +
    '<div class="card kpi"><div class="l">Haberde birebir bulunan alıntı</div><div class="n">' + pct(K.alinti_dogru, K.alinti_kontrol) + '</div><div class="x muted">' + K.alinti_dogru + " / " + K.alinti_kontrol + "</div></div>" +
    '<div class="card kpi"><div class="l">Kritik ya da yüksek olay içeren haber</div><div class="n">' + riskli + '</div><div class="x muted">' + nf0.format(cevap) + " cevaplı eşleşmeden</div></div>" +
    '<div class="card kpi ' + (M.hata ? "s2" : "") + '"><div class="l">LLM hatası</div><div class="n">' + M.hata + '</div><div class="x muted">prompt sürümü ' + esc(M.prompt) + "</div></div></div>";
  var omax = Math.max.apply(null, [1].concat(K.olaylar.map(function (o) { return o.n; })));
  html += '<div class="grid k2" style="margin-top:12px"><div class="card"><h2>İlgili haberlerde LLM\'in bulduğu olaylar</h2>' +
    K.olaylar.map(function (o) {
      return '<div class="hbar"><span>' + (o.seviye >= 2 ? chip(o.seviye) + " " : "") + esc(tip(o.tip)) + '</span><div class="t" style="width:' + (100 * o.n / omax) + '%"></div><span class="r">' + o.n + "</span></div>";
    }).join("") + '<p class="small muted">Odak ve ikincil haberler. Seviye, olay tipinden gelir; şirket sinyaline yalnızca alıntısı doğrulanmış olaylar girer. LLM kararlarının örnek kontrolü Excel\'deki Kontrol sayfasında.</p></div>' +
    '<div class="card"><h2>Alaka kararları</h2>' + ["odak", "ikincil", "alakasiz", "baska_varlik", "hata"].filter(function (k) { return A[k]; }).map(function (k) {
      var L = {odak: "Odak: haber şirket hakkında", ikincil: "İkincil: şirket haberde geçiyor", alakasiz: "Alakasız: yalnızca adı geçiyor", baska_varlik: "Başka varlık: yanlış eşleşme", hata: "Hata"};
      return '<div class="hbar"><span>' + L[k] + '</span><div class="t" style="width:' + (100 * A[k] / toplam) + '%"></div><span class="r">' + A[k] + "</span></div>";
    }).join("") + "</div></div>";
  html += '<div class="card" style="margin-top:12px"><h2>En çok reddedilen alias\'lar</h2>' + (K.aliaslar.length ? '<div class="tw"><table><thead><tr><th>Alias</th><th>Şirket</th><th class="r">Eşleşme</th><th class="r">Reddedilen</th><th class="r">Oran</th><th></th></tr></thead><tbody>' +
    K.aliaslar.map(function (a) { return "<tr><td><b>" + esc(a.alias) + "</b></td><td>" + esc(a.ad) + '</td><td class="r">' + a.eslesme + '</td><td class="r">' + a.red + '</td><td class="r">' + pct(a.red, a.eslesme) + "</td><td>" + (a.cikar ? '<span class="badge open">çıkarılması önerilir</span>' : "") + "</td></tr>"; }).join("") +
    "</tbody></table></div>" : '<div class="empty">Reddedilen alias yok.</div>') + '<p class="small muted">Çıkarılması önerilen alias\'ların ALIAS_RULES satırları Excel\'deki Alias_Onerisi sayfasında.</p></div>';
  $("#kalite").innerHTML = html;
}

// ---- Şirket detayı
function openCompany(k) {
  var s = BY[k]; if (!s) return;
  var ev = D.olaylar[k] || [], other = D.diger[k] || [];
  var html = '<div class="ev-h" style="margin-top:12px">Birleşik ' + chip(s.b) + " · Haber " + chip(s.h) + " · Finansal " + chip(s.f) + '</div><div class="facts">' +
    "<div><b>Birleşik skor</b>" + skor(s.bs) + " / 100</div><div><b>Haber skoru</b>" + skor(s.hs) + "</div>" +
    "<div><b>Bakiye</b>" + tl(s.bakiye) + "</div><div><b>Seviyenin kaynağı</b>" + esc(s.kural) + "</div>" +
    (s.eslesme != null ? "<div><b>Haber eşleşmesi</b>" + s.eslesme + " (odak " + s.odak + ", reddedilen " + s.red + ")</div>" : "<div><b>Haber</b>Haber penceresinde eşleşen haber yok</div>") +
    "</div>" + (s.hesap ? '<p class="small muted">Haber skoru hesabı: ' + esc(s.hesap) + "</p>" : "");
  html += "<h3>Olay defteri</h3>" + (ev.length ? ev.map(function (e) {
    return '<div class="ev ' + e.durum + '"><div class="ev-h"><b>[' + esc(e.kod) + "] " + esc(tip(e.tip)) + "</b>" + chip(e.etki) +
      (e.durum === "acik" ? '<span class="badge open">açık</span>' : '<span class="badge">kapandı ' + esc(e.kapanis) + "</span>") +
      '<span class="badge">' + esc(RISK[e.risk] || e.risk) + "</span>" + (e.eski ? '<span class="badge">son haber 90+ gün önce · bir kademe düştü</span>' : "") + '</div><div class="ev-t">' + esc(e.tanim) + '</div><div class="small muted">Açılış ' + esc(e.acilis) + " · " + e.haberler.length + " haber</div>" +
      '<ol class="tl">' + e.haberler.map(function (h) {
        return '<li class="' + h.islem + '"><span class="small"><b>' + esc(h.tarih) + "</b> " + ISLEM[h.islem] + " · " + esc(h.kaynak) + "</span><br>" + link(h.url, h.baslik || h.url) +
          (h.alinti ? "<blockquote>" + esc(h.alinti) + "</blockquote>" : "") + "</li>";
      }).join("") + "</ol></div>";
  }).join("") : '<div class="muted small">Kritik ya da yüksek seviyeli olay yok.</div>');
  if (other.length) html += "<h3>Diğer haberler</h3>" + '<p class="small muted">Olay defterine girmeyen ilgili haberler (ikincil haberdeki yüksek olaylar, orta seviyeli olaylar, olumlu ve rutin haberler). ' +
    "Şirketin seviyesine girmezler; odak haberdeki orta seviyeli olaylar skora puan katar.</p>" + '<div class="card"><table><tbody>' + other.map(function (o) {
    var notlar = [o.alaka === "ikincil" ? "ikincil haber" : "odak haber"];
    if (o.dogru === false) notlar.push("alıntı doğrulanamadı");
    if (o.durum === "yalanlandi_cozuldu") notlar.push("yalanlandı / çözüldü");
    return '<tr><td class="nowrap small">' + esc(o.tarih) + '</td><td class="nowrap">' + (o.seviye >= 2 ? chip(o.seviye) + " " : "") + '<span class="badge">' + esc(tip(o.tip)) + "</span>" +
      '<div class="small muted">' + esc(notlar.join(" · ")) + "</div></td><td>" + link(o.url, o.baslik) + '<div class="small muted">' + esc(o.ozet) + "</div></td></tr>";
  }).join("") + "</tbody></table></div>";
  html += "<h3>Finansal</h3>" + (s.skor != null ? '<div class="card"><div>Skor <b>' + nf1.format(s.skor) + "</b> · DSCR " + (s.dscr != null ? nf1.format(s.dscr) : "–") + " · " + esc(s.ftarih || "") +
    '</div><ul class="small">' + String(s.puan || "").split(" | ").map(function (p) { return "<li>" + esc(p) + "</li>"; }).join("") + "</ul></div>" : '<div class="muted small">Finansal veri yok.</div>');
  $("#dtitle").textContent = s.ad + "  ·  cust_id " + s.k;
  $("#dbody").innerHTML = html;
  $("#drawer").hidden = false; $("#overlay").hidden = false; document.body.classList.add("noscroll");
  $("#drawer").scrollTop = 0; $("#dclose").focus();
}
function closeCompany() { $("#drawer").hidden = true; $("#overlay").hidden = true; document.body.classList.remove("noscroll"); }
$("#dclose").addEventListener("click", closeCompany);
$("#overlay").addEventListener("click", closeCompany);
document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeCompany(); });

renderOzet(); renderDegisim(); renderSirketler(); renderTemalar(); renderKalite();
try { var t = localStorage.getItem("ews_sekme"); if (t && $("#" + t)) show(t); } catch (e) {}
})();
</script>
</body>
</html>
"""
