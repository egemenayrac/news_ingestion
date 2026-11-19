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


def test_backtest_evaluate_computes_financial_lead_time():
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

    res = bt.evaluate_backtest(snaps, outcomes)
    metrics = res["Ozet_Metrikler"].set_index("Model_Katmani")
    assert metrics.loc["Yalnızca Finansal Skor (3 Katmanlı)", "Medyan_Kursun_Suresi_Gun"] == 61.0
    assert metrics.loc["Yalnızca Haber Sinyali (Olay Defteri)", "Medyan_Kursun_Suresi_Gun"] == 31.0


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


def test_llm_client_uses_persistent_session(monkeypatch):
    client = ews_llm.LLMClient(url="https://example.local/v1/chat/completions", key="Bearer test-key")
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

    monkeypatch.setattr(client._session, "post", fake_post)
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
