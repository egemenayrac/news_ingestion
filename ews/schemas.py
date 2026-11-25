"""Pydantic v2 şemaları: alaka, olay, defter ve tema LLM JSON yanıtlarının doğrulanması.

Kurum içi LLM (Qwen3 27B / IBThink) JSON çıktılarındaki ufak biçim sapmalarını
(büyük/küçük harf, boşluk, liste yerine tek string, sınır aşımı vb.) güvenli biçimde
normalleştirir ve kredi riski güvenlik değişmezlerini (tanımsız alaka -> ikincil,
tanımsız olay -> hicbiri, risk olayında varsayılan risk_turu -> kredi) korur.

Python 3.9+ uyumludur (`from __future__ import annotations` + `typing` tipleri).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ALAKA_VALUES: Tuple[str, ...] = ("odak", "ikincil", "alakasiz", "baska_varlik")
DURUM_VALUES: Tuple[str, ...] = ("gerceklesti", "iddia", "yalanlandi_cozuldu")
RISK_TURU_VALUES: Tuple[str, ...] = ("kredi", "hukuki_itibar")
DEFTER_ISLEM_VALUES: Tuple[str, ...] = ("yeni", "mevcut", "kapat")

# ews/olay.py ile döngüsel import oluşmaması için sabit izinli olay tipi listesi ve seviyeli tipler:
RISKLI_OLAY_TIPLERI = frozenset({
    "konkordato_iflas",
    "temerrut",
    "haciz_el_koyma",
    "borc_yapilandirma",
    "dava_ceza",
    "idari_ceza",
    "finansal_bozulma",
    "kar_dususu",
    "faaliyet_sona_erme",
    "faaliyet_kesintisi",
    "operasyonel_aksaklik",
    "not_dusurme",
    "yonetim_krizi",
    "dis_baski",
})
TUM_OLAY_TIPLERI = RISKLI_OLAY_TIPLERI | frozenset({"olumlu", "rutin", "hicbiri"})


class AlakaResponse(BaseModel):
    """LLM Alaka (odak / ikincil / alakasiz / baska_varlik) yanıt şeması."""

    model_config = ConfigDict(extra="ignore")

    alaka: Literal["odak", "ikincil", "alakasiz", "baska_varlik"] = "ikincil"
    gerekce: str = ""

    @field_validator("alaka", mode="before")
    @classmethod
    def _norm_alaka(cls, v: Any) -> str:
        val = str(v or "").strip().lower()
        return val if val in ALAKA_VALUES else "ikincil"

    @field_validator("gerekce", mode="before")
    @classmethod
    def _norm_gerekce(cls, v: Any) -> str:
        return str(v or "").strip()[:250]


class OlayResponse(BaseModel):
    """LLM Olay (olay_tipi, durum, risk_turu, alinti, ozet) yanıt şeması."""

    model_config = ConfigDict(extra="ignore")

    olay_tipi: str = "hicbiri"
    durum: Literal["gerceklesti", "iddia", "yalanlandi_cozuldu"] = "iddia"
    risk_turu: Literal["kredi", "hukuki_itibar", ""] = ""
    alinti: str = ""
    ozet: str = ""

    @field_validator("olay_tipi", mode="before")
    @classmethod
    def _norm_tip(cls, v: Any) -> str:
        val = str(v or "").strip().lower()
        return val if val in TUM_OLAY_TIPLERI else "hicbiri"

    @field_validator("durum", mode="before")
    @classmethod
    def _norm_durum(cls, v: Any) -> str:
        val = str(v or "").strip().lower()
        return val if val in DURUM_VALUES else "iddia"

    @field_validator("risk_turu", mode="before")
    @classmethod
    def _norm_risk_turu(cls, v: Any) -> str:
        val = str(v or "").strip().lower()
        return val if val in RISK_TURU_VALUES else ""

    @field_validator("alinti", mode="before")
    @classmethod
    def _norm_alinti(cls, v: Any) -> str:
        return str(v or "").strip()[:300]

    @field_validator("ozet", mode="before")
    @classmethod
    def _norm_ozet(cls, v: Any) -> str:
        return str(v or "").strip()[:200]

    @model_validator(mode="after")
    def _enforce_risk_turu(self) -> "OlayResponse":
        if self.olay_tipi in RISKLI_OLAY_TIPLERI:
            if not self.risk_turu:
                self.risk_turu = "kredi"
        else:
            self.risk_turu = ""
        return self


class DefterBagResponse(BaseModel):
    """LLM Olay Defteri bağlama (yeni / mevcut / kapat) yanıt şeması."""

    model_config = ConfigDict(extra="ignore")

    islem: Literal["yeni", "mevcut", "kapat"] = "yeni"
    olay: str = ""
    gerekce: str = ""

    @field_validator("islem", mode="before")
    @classmethod
    def _norm_islem(cls, v: Any) -> str:
        val = str(v or "").strip().lower()
        return val if val in DEFTER_ISLEM_VALUES else "yeni"

    @field_validator("olay", mode="before")
    @classmethod
    def _norm_olay(cls, v: Any) -> str:
        return str(v or "").strip(" []")

    @field_validator("gerekce", mode="before")
    @classmethod
    def _norm_gerekce(cls, v: Any) -> str:
        return str(v or "").strip()[:200]


class TemaItem(BaseModel):
    """Tek bir ortak risk teması öğesi."""

    model_config = ConfigDict(extra="ignore")

    tema: str = ""
    aciklama: str = ""
    kanit_kelimeler: List[str] = Field(default_factory=list)
    maddeler: List[int] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _legacy_ortak_ifade(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("kanit_kelimeler"):
            fallback = data.get("ortak_ifade") or data.get("tema") or ""
            copied = dict(data)
            copied["kanit_kelimeler"] = fallback
            return copied
        return data

    @field_validator("tema", mode="before")
    @classmethod
    def _norm_tema(cls, v: Any) -> str:
        return str(v or "").strip()[:120]

    @field_validator("aciklama", mode="before")
    @classmethod
    def _norm_aciklama(cls, v: Any) -> str:
        return str(v or "").strip()[:300]

    @field_validator("kanit_kelimeler", mode="before")
    @classmethod
    def _norm_kanit(cls, v: Any) -> List[str]:
        if isinstance(v, str):
            return [x.strip() for x in re.split(r"[,;|]+", v) if x.strip()]
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v if x is not None and str(x).strip()]
        return []

    @field_validator("maddeler", mode="before")
    @classmethod
    def _norm_maddeler(cls, v: Any) -> List[int]:
        if not isinstance(v, (list, tuple)):
            return []
        out: List[int] = []
        for item in v:
            s = str(item).strip()
            if s.isdigit():
                out.append(int(s))
        return out


class TemalarResponse(BaseModel):
    """LLM Temalar keşfi yanıt şeması."""

    model_config = ConfigDict(extra="ignore")

    temalar: List[TemaItem] = Field(default_factory=list)

    @field_validator("temalar", mode="before")
    @classmethod
    def _norm_temalar(cls, v: Any) -> List[Any]:
        if not isinstance(v, list):
            return []
        return [item for item in v if isinstance(item, dict)]


def validate_alaka(obj: Dict[str, Any]) -> AlakaResponse:
    return AlakaResponse.model_validate(obj or {})


def validate_olay(obj: Dict[str, Any]) -> OlayResponse:
    return OlayResponse.model_validate(obj or {})


def validate_defter_bag(obj: Dict[str, Any]) -> DefterBagResponse:
    return DefterBagResponse.model_validate(obj or {})


def validate_temalar(obj: Dict[str, Any]) -> TemalarResponse:
    return TemalarResponse.model_validate(obj or {})
