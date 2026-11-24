"""news_ingestion çekirdek birim ve regresyon testleri (ağ, Oracle ve canlı LLM olmadan çalışır)."""
from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ayarlar  # noqa: E402
from ews import llm as ews_llm  # noqa: E402
from ews import olay as ews_olay  # noqa: E402
from ews import portfoy as ews_portfoy  # noqa: E402
from ews import tablolar as ews_tablolar  # noqa: E402


def test_backtest_find_candidate_outcome_tables_dwh_schema(tmp_path):
    bt = importlib.import_module("5_backtest")
    kb_path = tmp_path / "kb.sqlite"
    with sqlite3.connect(str(kb_path)) as conn:
        conn.executescript("""
            CREATE TABLE objects(owner TEXT, name TEXT, num_rows INTEGER);
            CREATE TABLE columns(owner TEXT, table_name TEXT, column_name TEXT);
            CREATE TABLE table_cards(owner TEXT, table_name TEXT, card TEXT, status TEXT);
            INSERT INTO objects VALUES('RISK', 'TFRS9_STAGE_HIST', 500000);
            INSERT INTO columns VALUES('RISK', 'TFRS9_STAGE_HIST', 'COUNTERPARTY_ID');
            INSERT INTO columns VALUES('RISK', 'TFRS9_STAGE_HIST', 'STAGE_CODE');
            INSERT INTO columns VALUES('RISK', 'TFRS9_STAGE_HIST', 'DPD_DAYS');
            INSERT INTO table_cards VALUES('RISK', 'TFRS9_STAGE_HIST', '{"ozet": "Aylık TFRS 9 stage geçmişi"}', 'ok');
        """)

    df = bt.find_candidate_outcome_tables(kb_path)
    assert len(df) == 1
    assert df.iloc[0]["owner"] == "RISK"
    assert df.iloc[0]["table_name"] == "TFRS9_STAGE_HIST"
    assert df.iloc[0]["tablo_karti_ozeti"] == "Aylık TFRS 9 stage geçmişi"
    assert "STAGE_CODE" in df.iloc[0]["ilgili_kolonlar"]


def test_backtest_evaluate_static_vs_historical_financials_and_sinirlar():
    bt = importlib.import_module("5_backtest")
    snaps = pd.DataFrame([
        {
            "As_Of_Tarih": "2026-08-31", "Şirket": 101, "Şirket_Adi": "ABC A.Ş.",
            "Haber_Sinyali": ews_olay.STAGE_LABEL[1],
            "Finansal_Seviye": ews_olay.STAGE_LABEL[2],
            "Birlesik_Seviye": ews_olay.STAGE_LABEL[2],
            "En_Agir_Olay": None,
        },
        {
            "As_Of_Tarih": "2026-09-30", "Şirket": 101, "Şirket_Adi": "ABC A.Ş.",
            "Haber_Sinyali": ews_olay.STAGE_LABEL[2],
            "Finansal_Seviye": ews_olay.STAGE_LABEL[2],
            "Birlesik_Seviye": ews_olay.STAGE_LABEL[2],
            "En_Agir_Olay": "borc_yapilandirma",
        },
    ])
    outcomes = pd.DataFrame([{"counterparty_id": 101, "outcome_dt": "2026-10-31", "outcome_type": "Stage 2"}])

    # Tek anlık bilanço -> Finansal kurşun süresi None ve Sinirlar sayfası uyarılı
    static_fin = pd.DataFrame([{"Şirket": 101, "Finansal_Tarih": "2025-12-31"}])
    res_static = bt.evaluate_backtest(snaps, outcomes, fin_df=static_fin, news_span_days=105)
    m_static = res_static["Ozet_Metrikler"].set_index("Model_Katmani")
    assert pd.isna(m_static.loc["Yalnızca Finansal Skor (3 Katmanlı)", "Medyan_Kursun_Suresi_Gun"])
    assert m_static.loc["Yalnızca Haber Sinyali (Olay Defteri)", "Medyan_Kursun_Suresi_Gun"] == 31.0
    assert "Sinirlar" in res_static and len(res_static["Sinirlar"]) >= 4
    assert "TEK ANLIK GÖRÜNTÜ" in set(res_static["Sinirlar"]["Durum"])

    # Çok dönemli tarihsel finansal seri -> Finansal kurşun süresi hesaplanır
    hist_fin = pd.DataFrame([
        {"Şirket": 101, "Finansal_Tarih": "2024-12-31"},
        {"Şirket": 101, "Finansal_Tarih": "2025-12-31"},
    ])
    res_hist = bt.evaluate_backtest(snaps, outcomes, fin_df=hist_fin, news_span_days=200)
    m_hist = res_hist["Ozet_Metrikler"].set_index("Model_Katmani")
    assert m_hist.loc["Yalnızca Finansal Skor (3 Katmanlı)", "Medyan_Kursun_Suresi_Gun"] == 61.0


def test_run_snapshot_as_of_selects_latest_historical_statement_per_company(tmp_path):
    bt = importlib.import_module("5_backtest")
    cache = ews_llm.Cache(tmp_path / "cache.jsonl")
    llm = ews_llm.LLMClient(url="https://<SUNUCU>/v1/chat/completions")

    # Aynı şirket için iki farklı bilanço dönemi: 2024 (sağlam/Stage 1) ve 2025 (zayıf/Stage 2)
    prematched = pd.DataFrame([
        {
            "Tarih": "2025-05-15", "Kaynak": "Test", "Başlık": "ABC A.Ş. rutin açıklama",
            "URL": "http://ex/n1", "Metin": "ABC A.Ş. olağan genel kurul yaptı.",
            "Şirket": 101, "Şirket_Adi": "ABC A.Ş.", "Eşleşen_Alias": "ABC",
            "Ortak": None, "Metin_Snippet": "ABC A.Ş. olağan genel kurul yaptı.",
            "_Anahtar": "URL::ex/n1|101|5",
        },
        {
            "Tarih": "2026-05-15", "Kaynak": "Test", "Başlık": "ABC A.Ş. yeni açıklama",
            "URL": "http://ex/n2", "Metin": "ABC A.Ş. yönetim kurulu toplandı.",
            "Şirket": 101, "Şirket_Adi": "ABC A.Ş.", "Eşleşen_Alias": "ABC",
            "Ortak": None, "Metin_Snippet": "ABC A.Ş. yönetim kurulu toplandı.",
            "_Anahtar": "URL::ex/n2|101|5",
        },
    ])
    cache.put("URL::ex/n1|101|5", {"alaka": "alakasiz", "alaka_gerekce": "rutin"})
    cache.put("URL::ex/n2|101|5", {"alaka": "alakasiz", "alaka_gerekce": "rutin"})

    hist_fin = pd.DataFrame([
        {
            "Şirket": 101, "Finansal_Unvan": "ABC A.Ş.", "Finansal_Tarih": "2024-12-31",
            "Finansal_Skor": 20.0, "Finansal_Veri_Kapsami": 1.0, "DSCR_Degeri": 2.5,
            "Toplam_Bakiye_TRY": 100_000_000.0,
        },
        {
            "Şirket": 101, "Finansal_Unvan": "ABC A.Ş.", "Finansal_Tarih": "2025-12-31",
            "Finansal_Skor": 65.0, "Finansal_Veri_Kapsami": 1.0, "DSCR_Degeri": 0.9,
            "Toplam_Bakiye_TRY": 100_000_000.0,
        },
    ])

    snap_2025 = bt.run_snapshot_as_of("2025-06-30", llm, cache, hist_fin, workers=1, prematched_df=prematched)
    assert len(snap_2025) == 1
    assert snap_2025.iloc[0]["Finansal_Tarih"] == "2024-12-31"
    assert snap_2025.iloc[0]["Finansal_Seviye"] == ews_olay.STAGE_LABEL[1]

    snap_2026 = bt.run_snapshot_as_of("2026-06-30", llm, cache, hist_fin, workers=1, prematched_df=prematched)
    assert len(snap_2026) == 1
    assert snap_2026.iloc[0]["Finansal_Tarih"] == "2025-12-31"
    assert snap_2026.iloc[0]["Finansal_Seviye"] == ews_olay.STAGE_LABEL[2]


def test_build_themes_strict_stems_similarity_cache_and_nan_guard(tmp_path):
    cache = ews_llm.Cache(tmp_path / "cache.jsonl")
    calls = {"ask": 0, "sim": 0}

    class FakeLLM:
        def ask(self, system, user, max_tokens=0):
            calls["ask"] += 1
            return {
                "temalar": [
                    {
                        "tema": "Rekabet Kurumu Soruşturması",
                        "aciklama": "Sektör genelinde ortak risk ve kredi baskısı oluşturan süreç",
                        "kanit_kelimeler": ["rekabet", "soruşturma"],
                        "maddeler": [0, 1, 2],
                    }
                ]
            }, "ok"

        def similarity(self, text, categories, threshold=0.5):
            calls["sim"] += 1
            # Sadece 3. madde (halüsinasyon) için çağrılır; düşük skor döner
            return {categories[0]: 0.20}

    df = pd.DataFrame([
        {
            "Şirket": 1, "Şirket_Adi": "Firma A", "Tarih": "2026-10-01",
            "Başlık": "Rekabet Kurumu Firma A hakkında soruşturma açtı", "URL": "http://ex/1",
            "LLM_Alaka": "odak", "Alinti_Dogrulandi": True, "LLM_Olay": "dava_ceza",
            "LLM_Ozet": "Rekabet soruşturması açıldı", "LLM_Alinti": "Rekabet Kurumu soruşturma başlattı.",
        },
        {
            "Şirket": 2, "Şirket_Adi": "Firma B", "Tarih": "2026-10-02",
            "Başlık": "Firma B de rekabet soruşturması kapsamında", "URL": "http://ex/2",
            "LLM_Alaka": "odak", "Alinti_Dogrulandi": True, "LLM_Olay": "dava_ceza",
            "LLM_Ozet": "Rekabet incelemesi başlatıldı", "LLM_Alinti": "Rekabet Kurumu inceleme açtı.",
        },
        {
            "Şirket": 3, "Şirket_Adi": "Firma C", "Tarih": "2026-10-03",
            "Başlık": "Firma C depo yangını sonrası üretim durdu", "URL": "http://ex/3",
            "LLM_Alaka": "odak", "Alinti_Dogrulandi": True, "LLM_Olay": "faaliyet_kesintisi",
            "LLM_Ozet": "Depo yangını nedeniyle sektör riski", "LLM_Alinti": "Yangın sebebiyle hat durdu.",
        },
    ])
    # Bilerek Firma 2'yi companies'ten eksik bırakarak NaN max() korumasını da test et
    companies = pd.DataFrame([{"Şirket": 999, "LLM_Sinyali": ews_olay.STAGE_LABEL[2]}])
    fin = pd.DataFrame(columns=["Şirket", "Toplam_Bakiye_TRY"])

    llm = FakeLLM()
    t1 = ews_portfoy.build_themes(df, companies, fin, llm, cache)
    assert len(t1) == 1
    assert t1.iloc[0]["Sirket_Sayisi"] == 2
    assert "Firma C" not in t1.iloc[0]["Sirketler"]
    assert t1.iloc[0]["En_Yuksek_Haber_Sinyali"] == ews_olay.STAGE_LABEL[1]
    assert calls["sim"] == 1

    # İkinci koşuda similarity önbellekten okunmalı (yeni çağrı olmamalı)
    t2 = ews_portfoy.build_themes(df, companies, fin, llm, cache)
    assert len(t2) == 1
    assert calls["sim"] == 1


def test_scraper_fails_fast_on_404(monkeypatch):
    scraper = importlib.import_module("1_scraper")
    attempts = {"n": 0}

    class FakeSession:
        def get(self, url, timeout=None):
            attempts["n"] += 1
            return SimpleNamespace(status_code=404, content=b"Not Found")

    monkeypatch.setattr(scraper, "get_session", lambda: FakeSession())
    assert scraper.get_content_with_retry("https://example.com/missing.xml", retries=3) == ""
    assert attempts["n"] == 1
    assert scraper.get_soup_with_retry("https://example.com/missing.html", retries=3) is None
    assert attempts["n"] == 2


def test_llm_client_uses_thread_local_session(monkeypatch):
    import threading

    client = ews_llm.LLMClient(url="https://example.local/v1/chat/completions", key="Bearer test-key")
    s_main = client._get_session()
    assert s_main is client._get_session()

    other_ids = []
    t = threading.Thread(target=lambda: other_ids.append(id(client._get_session())))
    t.start()
    t.join()
    assert other_ids[0] != id(s_main)

    called = {"session_post": 0}

    class FakeResp:
        ok = True
        status_code = 200

        def json(self):
            return {"choices": [{"finish_reason": "stop", "message": {"content": '{"alaka": "odak"}'}}]}

    def fake_post(url, **kwargs):
        called["session_post"] += 1
        assert kwargs["headers"]["Authorization"] == "Bearer test-key"
        return FakeResp()

    monkeypatch.setattr(s_main, "post", fake_post)
    obj, status = client.ask("sys", "usr")
    assert status == "ok"
    assert obj == {"alaka": "odak"}
    assert called["session_post"] == 1


def test_3_llm_dogrulama_reads_key_even_when_todo_empty(tmp_path, monkeypatch):
    mod = importlib.import_module("3_llm_dogrulama")
    key_file = tmp_path / ".llm_key.txt"
    key_file.write_text("dosya-anahtari", encoding="utf-8")
    monkeypatch.setattr(ayarlar, "LLM_API_KEY", "")
    monkeypatch.setattr(ayarlar, "KEY_FILE_PATH", key_file)
    monkeypatch.delenv("EWS_LLM_KEY", raising=False)
    monkeypatch.delenv("IBTHINK_API_KEY", raising=False)

    assert ayarlar.get_llm_api_key(interactive=False) == "dosya-anahtari"

    captured = {}
    orig_cls = mod.LLMClient

    def spy_client(**kwargs):
        captured.update(kwargs)
        return orig_cls(**kwargs)

    monkeypatch.setattr(mod, "LLMClient", spy_client)
    monkeypatch.setattr(mod, "LLM", {"url": "https://example.local/v1/chat/completions", "key": "", "model": ""})
    monkeypatch.setattr(mod, "CACHE_PATH", tmp_path / "c.jsonl")
    monkeypatch.setattr(mod, "CIKTI_PATH", tmp_path / "out.xlsx")
    monkeypatch.setattr(mod, "DASHBOARD_PATH", tmp_path / "dash.html")
    monkeypatch.setattr(mod, "GECMIS_PATH", tmp_path / "hist.jsonl")
    monkeypatch.setattr(mod, "KONTROL_PATH", tmp_path / "chk.jsonl")
    monkeypatch.setattr(mod, "LLM_VERIFY_SSL", False)

    # Önbellekte zaten var olan tek bir eşleşme -> todo boş olacak ama key yine de okunmalı
    sample = pd.DataFrame([{
        "Tarih": "2026-10-01", "Kaynak": "Test", "Başlık": "Başlık", "URL": "http://ex/1", "Metin": "Metin",
        "Şirket": 1, "Şirket_Adi": "ABC", "Eşleşen_Alias": "ABC", "Ortak": None, "Metin_Snippet": "ABC",
        "_Anahtar": "URL::ex/1|1|5",
    }])
    cache = ews_llm.Cache(tmp_path / "c.jsonl")
    cache.put("URL::ex/1|1|5", {"alaka": "alakasiz", "alaka_gerekce": "test"})
    monkeypatch.setattr(mod, "load_matches", lambda: sample)
    monkeypatch.setattr(ews_portfoy, "load_financials", lambda: pd.DataFrame())

    mod.run()
    assert captured["key"] == "dosya-anahtari"


def test_write_excel_cleans_strings_only(tmp_path):
    out = tmp_path / "test.xlsx"
    df = pd.DataFrame({"Ad": ["A\x00B"], "Tutar": [123.45], "_gizli": ["x"]})
    written = ews_tablolar.write_excel({"Sayfa": df}, out)
    back = pd.read_excel(written, sheet_name="Sayfa")
    assert list(back.columns) == ["Ad", "Tutar"]
    assert back.iloc[0]["Ad"] == "AB"
    assert back.iloc[0]["Tutar"] == 123.45


def test_ilan_gov_tr_api_and_vkn_extraction(monkeypatch):
    from datetime import datetime, timezone

    mod = importlib.import_module("4_resmi_ilanlar")
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)

    # Hem önekli (VKN: ...) hem mahkeme üslubu (... vergi numaralı) VKN çıkarımı
    tag = mod.extract_identifiers(
        "Bursa Ticaret Sicil'de kayıtlı 3851381252 vergi numaralı FEY A.Ş. ve VKN: 1234567890 borçlu."
    )
    assert "3851381252" in tag
    assert "1234567890" in tag

    # Gerçek kişi TCKN'leri maskelenmeli, 10 haneli şirket VKN'si korunmalı
    masked = mod.mask_tckn(
        "3851381252 vergi numaralı FEY A.Ş., 11305933184 TC Kimlik numaralı Kişi ve komiser (TC. 14456809940)."
    )
    assert "3851381252" in masked
    assert "11305933184" not in masked
    assert "14456809940" not in masked
    # Etiketsiz TCKN ve 11 haneli telefon da maskelenir; 16 haneli MERSİS korunur
    bare = mod.mask_tckn("Hukukçu Ad Soyad 12345678901, Tel: 05321234567, MERSİS 0987654321098765")
    assert "12345678901" not in bare and "05321234567" not in bare
    assert "0987654321098765" in bare

    today_tr = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    seen_posts = []
    seen_gets = []

    class FakeResp:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

    class FakeSession:
        def post(self, url, json=None, headers=None, timeout=None, **kwargs):
            assert "verify" not in kwargs  # TLS ayarı session.verify'dan gelmeli
            seen_posts.append((url, json, headers))
            return FakeResp(200, {
                "success": True,
                "result": {
                    "numFound": 1,
                    "ads": [{
                        "id": 2291587,
                        "adNo": "ILN02597022",
                        "title": "Konkordato mühlet kararının ilanen tebliği",
                        "advertiserName": "BURSA 2. ASLİYE TİCARET MAHKEMESİ",
                        "publishStartDate": today_tr,
                        "urlStr": "/ilan/2291587/konkordato-muhlet-karari",
                    }],
                },
            })

        def get(self, url, params=None, headers=None, timeout=None, **kwargs):
            assert "verify" not in kwargs  # TLS ayarı session.verify'dan gelmeli
            seen_gets.append((url, params, headers))
            return FakeResp(200, {
                "success": True,
                "result": {
                    "id": 2291587,
                    "content": (
                        "<p>3851381252 vergi numaralı FEY KONFEKSİYON A.Ş. ve 11305933184 TC Kimlik numaralı "
                        "kişi hakkında üç ay süre ile GEÇİCİ MÜHLET kararı verilmesine ve konkordato komiseri "
                        "(TC. 14456809940) atanmasına karar verildi.</p>"
                    ),
                },
            })

    rows = mod.fetch_ilan_gov_tr(FakeSession(), existing_urls=set(), days=10, max_pages=1)
    assert len(rows) == 1  # 4 arama teriminde aynı ilan tekilleştirilmeli
    assert len(seen_gets) == 1
    assert seen_posts[0][0] == mod.ILAN_GOV_SEARCH_API
    assert seen_posts[0][1]["keys"]["ats"] == [mod.ILAN_GOV_AD_TYPE_TEBLIGAT]
    assert seen_posts[0][2]["x-request-origin"] == "IGT-UI"
    assert seen_gets[0][0] == mod.ILAN_GOV_DETAIL_API
    assert seen_gets[0][1] == {"id": "2291587"}
    assert "[VKN: 3851381252]" in rows[0]["Başlık"]
    assert rows[0]["URL"] == "https://www.ilan.gov.tr/ilan/2291587/konkordato-muhlet-karari"
    assert "GEÇİCİ MÜHLET" in rows[0]["Metin"]
    assert "11305933184" not in rows[0]["Metin"]
    assert "14456809940" not in rows[0]["Metin"]


def test_baslat_respects_enable_resmi_ilanlar(monkeypatch):
    baslat = importlib.import_module("baslat")
    scraper = importlib.import_module("1_scraper")
    resmi = importlib.import_module("4_resmi_ilanlar")
    called = {"scraper": 0, "resmi": 0}

    monkeypatch.setattr(scraper, "run_ingestion_pipeline", lambda: called.__setitem__("scraper", called["scraper"] + 1))
    monkeypatch.setattr(resmi, "run_official_gazette_pipeline", lambda: called.__setitem__("resmi", called["resmi"] + 1))

    monkeypatch.setattr(ayarlar, "ENABLE_RESMI_ILANLAR", False)
    baslat.run_step("3")
    assert called == {"scraper": 1, "resmi": 0}

    monkeypatch.setattr(ayarlar, "ENABLE_RESMI_ILANLAR", True)
    baslat.run_step("3")
    assert called == {"scraper": 2, "resmi": 1}




def test_dashboard_ajan_url_from_ayarlar(monkeypatch):
    from ews import dashboard as ews_dashboard

    monkeypatch.setattr(ayarlar, "AJAN_URL", "")
    assert ews_dashboard._ajan_url() == ""
    monkeypatch.setattr(ayarlar, "AJAN_URL", " http://localhost:8501 ")
    assert ews_dashboard._ajan_url() == "http://localhost:8501"
    assert "D.meta.ajan_url" in ews_dashboard._TEMPLATE and "?sirket=" in ews_dashboard._TEMPLATE
