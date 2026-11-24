# -*- coding: utf-8 -*-
"""ews/inceleme.py testleri (Oracle, LLM ve internet olmadan)."""
import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ews import inceleme, kap_kaynak  # noqa: E402

SID = 4242


def _ews(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    birlesik = pd.DataFrame([
        {"Şirket": SID, "Şirket_Adi": "ÖRNEK ÇELİK SANAYİ VE TİCARET A.Ş.", "Birlesik_Seviye": "Yakın İzleme (Stage 2)",
         "Birlesik_Skor": 52.0, "Risk_Matrisi": "Bilanço İzlemede × Yüksek Haber Sinyali"},
        {"Şirket": 7, "Şirket_Adi": "BAŞKA A.Ş.", "Birlesik_Seviye": "Düşük Risk (Stage 1)", "Birlesik_Skor": 10.0},
    ])
    defter = pd.DataFrame([{"Şirket": SID, "Olay": "O1", "Olay_Tipi": "dava_ceza", "Seviye": "HIGH", "Durum": "açık",
                            "Acilis": "2026-11-01", "Son_Haber": "2026-11-10", "Haber_Sayisi": 2,
                            "Tanim": "Rekabet Kurumu soruşturma açtı", "Ilk_Haber_URL": "https://ornek.com/h1"}])
    kayit = pd.DataFrame([
        {"Tarih": "2026-11-10", "Kaynak": "Dünya", "Şirket": SID, "Başlık": "Örnek Çelik'e soruşturma",
         "URL": "https://ornek.com/h1", "LLM_Alaka": "odak", "LLM_Olay": "dava_ceza", "LLM_Durum": "gerceklesti",
         "LLM_Alinti": "Rekabet Kurumu Örnek Çelik hakkında soruşturma açtı", "LLM_Ozet": "Soruşturma"},
        {"Tarih": "2026-11-12", "Kaynak": "Ekonomim", "Şirket": SID, "Başlık": "Alakasız", "URL": "https://ornek.com/h2",
         "LLM_Alaka": "alakasiz"},
    ])
    with pd.ExcelWriter(d / "llm_dogrulama.xlsx") as w:
        birlesik.to_excel(w, sheet_name="Birlesik", index=False)
        defter.to_excel(w, sheet_name="Olay_Defteri", index=False)
        kayit.to_excel(w, sheet_name="Kayıtlar", index=False)
    comp = {SID: {"name": "ÖRNEK ÇELİK SANAYİ VE TİCARET A.Ş.",
                  "aliases": ["ÖRNEK ÇELİK SANAYİ VE TİCARET A.Ş.", "ÖRNEK ÇELİK SANAYİ", "ÖRNEK ÇELİK", "ÖRNEK"],
                  "tickers": [], "partners": [{"name": "ÖRNEK HOLDİNG A.Ş.", "share": 62.0}]}}
    (d / "portfolio_config.py").write_text("COMPANIES = " + repr(comp) + "\n", encoding="utf-8")
    return inceleme.EwsVerisi(d / "llm_dogrulama.xlsx", d / "portfolio_config.py")


class R:
    def __init__(self, status=200, body=None, content=b"", text=""):
        self.status_code, self._b, self.content, self.text = status, body, content, text

    def json(self):
        return self._b


class FakeHttp:
    def __init__(self):
        self.calls, self.proxies, self.verify, self.headers = [], {}, True, {}

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json))
        return R(200, {"result": {"numFound": 1, "ads": [{
            "id": 99, "title": "Konkordato geçici mühlet kararının ilanen tebliği", "advertiserName": "X ASLİYE TİCARET",
            "publishStartDate": "20.11.2026", "urlStr": "/ilan/99/konkordato"}]}})

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params))
        if "news.google.com" in url:
            return R(200, content=("<rss><channel><item><title>Örnek Çelik ihracatını artırdı</title>"
                                   "<link>https://news.google.com/a1</link><pubDate>Fri, 20 Nov 2026 08:00:00 GMT</pubDate>"
                                   "<source>Dünya</source></item></channel></rss>").encode("utf-8"))
        return R(200, {"result": {"content": "<p>ÖRNEK ÇELİK A.Ş. ve 12345678901 TC kimlik numaralı ortak hakkında "
                                             "geçici mühlet kararı verildi.</p>"}})


def _araclar(tmp_path, http=None):
    inceleme.DIS_BEKLEME = 0
    return inceleme.Araclar(SID, ews=_ews(tmp_path), http=http or FakeHttp())


def _hata(fn):
    try:
        fn()
    except ValueError as e:
        return str(e)
    return ""


def test_ews_araclari_kaynak_numarasi(tmp_path):
    a = _araclar(tmp_path)
    assert "[K1]" in a.ews_ozet() and "Yakın İzleme" in a.kaynaklar["K1"]["metin"]
    h = a.ews_haberler()
    assert "soruşturma" in h and "Alakasız" not in h
    assert "ÖRNEK HOLDİNG" in a.ortaklar()
    assert "Birden çok" in _hata(lambda: a.ews_ozet("A.Ş."))


def test_dis_kaynaklar_yalnizca_kisa_ad_ve_tckn_maskeli(tmp_path):
    http = FakeHttp()
    a = _araclar(tmp_path, http)
    ilan = a.ilan_ara()
    assert "Konkordato geçici mühlet" in ilan and "12345678901" not in ilan and "***" in ilan
    assert [c[2]["keys"]["q"] for c in http.calls if c[0] == "POST"] == [["ÖRNEK ÇELİK SANAYİ"]]
    assert "ihracatını artırdı" in a.google_haber()
    gurl = [c[1] for c in http.calls if "news.google.com" in c[1]][0]
    assert str(SID) not in gurl


def test_not_denetimi():
    k = {"K1": {"baslik": "Örnek Çelik'e soruşturma", "metin": "Rekabet Kurumu Örnek Çelik hakkında soruşturma açtı"}}
    temiz = "1. Özet\n- Rekabet Kurumu soruşturma açtı ve dosya sürüyor [K1]\n5. Eksik bilgi\n- KAP aracı sonuç vermedi bu yüzden bakılamadı"
    assert inceleme.not_denetimi(temiz, k) == []
    kirli = ("1. Özet\n- Şirket konkordato başvurusu yaptı ve mühlet aldı [K9]\n- Şirketin satışları yüzde otuz düştü geçen yıla göre\n"
             "2. Olaylar\n- Haberde \"şirket iflasın eşiğinde olduğunu açıkladı\" deniyor [K1]")
    b = inceleme.not_denetimi(kirli, k)
    assert any("K9" in x for x in b) and any("Kaynaksız" in x for x in b) and any("birebir" in x for x in b)


def _tc(name, args, i):
    return {"id": f"c{i}", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def test_ajan_dongusu_araclari_cagirir_duzeltir_ve_kaydeder(tmp_path, monkeypatch):
    a = _araclar(tmp_path)
    plan = iter([
        {"tool_calls": [_tc("ews_ozet", {}, 1), _tc("ews_haberler", {}, 2)]},
        {"tool_calls": [_tc("cevap_ver", {"cevap": "1. Özet\n- Rekabet Kurumu soruşturması açık ve sürüyor [K2]\n"
                                                   "- Şirket konkordatoya başvurdu ve mühlet aldı [K7]"}, 3)]},
        {"tool_calls": [_tc("cevap_ver", {"cevap": "1. Özet\n- Rekabet Kurumu soruşturması açık ve sürüyor [K2]\n"
                                                   "5. Eksik bilgi\n- Konkordato bilgisi kaynaklarda yok"}, 4)]},
    ])
    gorulen = []

    def fake(messages, tools=None, no_think=False):
        gorulen.append(sorted(t["function"]["name"] for t in tools))
        m = dict(next(plan))
        m.setdefault("content", "")
        return m

    monkeypatch.setattr(inceleme, "llm_chat", fake)
    oturum, a = inceleme.incele(SID, araclar=a)
    assert oturum.durum == "bitti" and a.denetim == [] and "[K7]" not in oturum.not_metni
    assert "kap_ara" in gorulen[0] and "dwh_sorgu" in gorulen[0]
    assert any("K7" in m["content"] for m in oturum.mesajlar if m["role"] == "user")      # denetim geri gönderdi
    md, ht = inceleme.kaydet(oturum, a, klasor=tmp_path / "incelemeler")
    assert md.exists() and ht.exists() and 'href="#K2"' in ht.read_text(encoding="utf-8")


def test_duzeltme_basarisizsa_ilk_not_korunur_ve_bos_sonuc_kaynak_alir(tmp_path, monkeypatch):
    a = _araclar(tmp_path)
    monkeypatch.setattr(inceleme, "INCELEME_SORGULARI", {})
    assert a.dwh_sorgu().startswith("[K1]")
    plan = iter([{"tool_calls": [_tc("cevap_ver", {"cevap": "1. Özet\n- Şirket hakkında uydurma bir iddia burada [K9]"}, 1)]}])

    def fake(messages, tools=None, no_think=False):
        m = dict(next(plan))
        m.setdefault("content", "")
        return m

    monkeypatch.setattr(inceleme, "llm_chat", fake)
    oturum, a = inceleme.incele(SID, araclar=a)
    assert oturum.durum == "bitti" and "[K9]" in oturum.not_metni
    assert any("Düzeltme turu" in d for d in a.denetim)


class FakeCur:
    def __init__(self, log):
        self.log, self.description = log, [("PART_ID",), ("NAKIT",)]

    def __enter__(self):
        return self

    def __exit__(self, *x):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def fetchmany(self, n):
        return [(202609, 1.5e9)]


class FakeConn:
    def __init__(self):
        self.log = []

    def cursor(self):
        return FakeCur(self.log)


def test_dwh_sorgu_sablon_ve_bind(tmp_path, monkeypatch):
    a = _araclar(tmp_path)
    s = {"bakiye": {"aciklama": "aylık bakiye", "sql": "SELECT part_id, nakit FROM S.T WHERE counterparty_id = :cid"},
         "kotu": {"aciklama": "x", "sql": "DELETE FROM S.T WHERE counterparty_id = :cid"}}
    monkeypatch.setattr(inceleme, "INCELEME_SORGULARI", s)
    a.conn = FakeConn()
    assert "bakiye: aylık bakiye" in a.dwh_sorgu("")
    out = a.dwh_sorgu("bakiye")
    assert re.match(r"^\[K\d+\] DWH 'bakiye'", out) and a.conn.log[0][1] == {"cid": SID} and str(SID) not in a.conn.log[0][0]
    assert "SELECT" in _hata(lambda: a.dwh_sorgu("kotu"))


_GOVDE = "<p>Şirketimiz konkordato başvurusunda bulunmuştur.</p>"


def _flight(ix, kod):
    temel = {"disclosureBasic": {"disclosureIndex": ix, "publishDate": "20.11.2026 10:00:00", "title": "Özel Durum Açıklaması",
                                 "stockCode": kod, "disclosureClass": "ODA"}, "disclosureBody": ["$2"]}
    chunk = "1:" + json.dumps(temel, ensure_ascii=False) + "\n2:T" + format(len(_GOVDE.encode("utf-8")), "x") + "," + _GOVDE
    return "<html><script>self.__next_f.push([1," + json.dumps(chunk, ensure_ascii=False) + "])</script></html>"


class FakeKap:
    def __init__(self):
        self.headers, self.proxies, self.verify, self.calls = {}, {}, True, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url))
        r = R(200, content=b"ok", text=_flight(555, "ORNEK"))
        return r

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json))
        if url.endswith("/search/combined"):
            return R(200, [{"results": [{"searchValue": "ÖRNEK ÇELİK SANAYİ VE TİCARET A.Ş.", "searchType": "C",
                                         "memberOrFundOid": "oid1", "cmpOrFundCode": "ornek"}]}])
        gun = json["fromDate"]
        k = [{"disclosureBasic": {"disclosureIndex": 1, "stockCode": "BASKA", "title": "Rutin", "publishDate": gun + " 09:00:00"}}]
        if gun == FakeKap.hedef:
            k.append({"disclosureBasic": {"disclosureId": "4028abc", "disclosureIndex": 555, "stockCode": "ORNEK",
                                          "mkkMemberOid": "oid1", "title": "Özel Durum Açıklaması", "disclosureClass": "ODA",
                                          "summary": "Konkordato başvurusu", "publishDate": gun + " 10:00:00"}})
        return R(200, k)


def test_kap_ara_bellekte_suzer_ve_diske_yazmaz(tmp_path, monkeypatch):
    FakeKap.hedef = (_dt.date.today() - _dt.timedelta(days=1)).strftime("%d.%m.%Y")
    monkeypatch.setattr(kap_kaynak, "KAPWEB_SLEEP", 0)
    a = _araclar(tmp_path)
    a._kap = kap_kaynak.KapClient(FakeKap())
    once = sorted(p.name for p in tmp_path.rglob("*"))
    out = a.kap_ara(gun=3)
    assert "ÖRNEK ÇELİK" in out and "Rutin" not in out and "konkordato başvurusunda" in out
    k = [v for v in a.kaynaklar.values() if v["tur"] == "KAP"][0]
    assert k["url"] == "https://kap.org.tr/tr/Bildirim/555"
    assert sorted(p.name for p in tmp_path.rglob("*")) == once
