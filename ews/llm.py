"""LLM istemcisi ve önbellek (4_llm_dogrulama.py kullanır).

LLMClient  Bankanın OpenAI uyumlu endpoint'ine (/v1/chat/completions) tek soru sorar, JSON cevabı döndürür.
Cache      llm_cache.jsonl: her cevap anında dosyaya eklenir; aynı soru bir daha sorulmaz, yarıda kesilen
           koşu kaldığı yerden devam eder.
"""

from __future__ import annotations

import json
import random
import re
import ssl
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

import requests
import urllib3


def resolve_ssl_verify(setting: object) -> object:
    """LLM_VERIFY_SSL ayarını requests'in verify değerine çevirir.

    "windows": Windows sertifika deposundaki kök ve ara sertifikalar (bankanın kendi kök sertifikası
    dahil) geçici bir PEM dosyasına yazılır ve doğrulama onunla yapılır.
    """
    if setting is False:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # her istekteki uyarıyı sustur
        return False
    if setting != "windows":
        return setting
    if not hasattr(ssl, "enum_certificates"):
        raise SystemExit('LLM_VERIFY_SSL = "windows" yalnızca Windows\'ta çalışır.')
    pems = {ssl.DER_cert_to_PEM_cert(cert) for store in ("ROOT", "CA")
            for cert, encoding, _ in ssl.enum_certificates(store) if encoding == "x509_asn"}
    path = Path(tempfile.gettempdir()) / "ews_windows_ca.pem"
    path.write_text("".join(sorted(pems)), encoding="ascii")
    return str(path)


@dataclass
class LLMClient:
    url: str
    key: str = ""
    model: str = ""             # kurumsal endpoint "model" alanını reddettiği için boşsa gönderilmez
    verify: object = True       # resolve_ssl_verify çıktısı
    timeout: int = 300
    retries: int = 3
    max_tokens: int = 1024

    def ask(self, system: str, user: str, max_tokens: int = 0) -> Tuple[Optional[dict], str]:
        """Tek soru sorar; (JSON sözlük ya da None, durum) döndürür.

        Cevaptaki <think> bloğu atılır, ilk { ile son } arası okunur. Kesilen cevap tekrar denenmez.
        Geçici hatalarda (429, 5xx, bağlantı) `retries` kez dener; 429'da sunucunun Retry-After süresi,
        yoksa 30/60 sn beklenir.
        """
        key = re.sub(r"^bearer\s+", "", str(self.key or "").strip(), flags=re.I)  # kopyalama artıkları
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        payload = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                   "temperature": 0.0, "max_tokens": max_tokens or self.max_tokens, "stream": False}
        if self.model:
            payload["model"] = self.model
        last = ""
        for attempt in range(1, self.retries + 1):
            wait = 5 * attempt
            try:
                r = requests.post(self.url, json=payload, headers=headers, timeout=self.timeout, verify=self.verify)
                if r.status_code in (429, 500, 502, 503, 504):
                    last = f"HTTP {r.status_code}: {r.text[:150]}{_correlation(r)}"
                    if r.status_code == 429:  # "çok fazla istek": sunucunun söylediği kadar, yoksa 30/60 sn
                        retry_after = r.headers.get("Retry-After", "")
                        wait = int(retry_after) if retry_after.isdigit() else 30 * attempt
                elif not r.ok:
                    hint = " (API anahtarı boş ya da hatalı)" if r.status_code in (401, 403) else ""
                    return None, f"HTTP {r.status_code}{hint}: {r.text[:200]}{_correlation(r)}"
                else:
                    return _parse(r.json())
            except requests.exceptions.SSLError as exc:  # sebep mesajın sonundadır
                return None, f'SSL hatası (LLM_VERIFY_SSL = "windows" ya da False deneyin): ...{str(exc)[-250:]}'
            except (requests.RequestException, ValueError) as exc:  # ValueError: bozuk JSON
                last = f"{type(exc).__name__}: {str(exc)[:100]}"
            if attempt < self.retries:
                time.sleep(wait + random.uniform(0, 3))  # rastgele pay: iki iş parçacığı aynı anda tekrar denemesin
        return None, last

    def similarity(self, text: str, categories: list, threshold: float = 0.5) -> Optional[Dict[str, float]]:
        """Kurumsal /v1/moderations (paraphrase-multilingual-mpnet-base-v2) uç noktasından anlamsal benzerlik skorlarını alır.
        Endpoint kapalıysa ya da hata verirse None döner (akışı kırmaz)."""
        if not text or not categories or "<SUNUCU>" in self.url:
            return None
        mod_url = self.url.replace("/chat/completions", "/moderations")
        key = re.sub(r"^bearer\s+", "", str(self.key or "").strip(), flags=re.I)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        payload = {
            "input": str(text)[:500],
            "categories": [str(c)[:300] for c in categories],
            "flag_threshold": threshold,
            "moderations_disabled": False,
        }
        try:
            r = requests.post(mod_url, json=payload, headers=headers, timeout=min(self.timeout, 30), verify=self.verify)
            if r.ok:
                results = r.json().get("results") or []
                if results and isinstance(results[0].get("category_scores"), dict):
                    return {str(k): float(v) for k, v in results[0]["category_scores"].items()}
        except Exception:
            return None
        return None


def _parse(body: dict) -> Tuple[Optional[dict], str]:
    """Chat cevabından JSON sözlüğü çıkarır. JSON bozuksa ValueError (tekrar denenir)."""
    choice = (body.get("choices") or [{}])[0]
    if choice.get("finish_reason") == "length":
        return None, "cevap LLM_MAX_TOKENS sınırında kesildi"
    text = ((choice.get("message") or {}).get("content") or "").split("</think>")[-1]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"cevapta JSON yok: {text[:100]!r}")
    return json.loads(text[start:end + 1]), "ok"


def _correlation(r: requests.Response) -> str:
    """Sunucunun hata için verdiği correlation_id (sorun bildirirken kayıtta bulunması için)."""
    try:
        cid = r.json().get("correlation_id")
    except ValueError:
        cid = None
    return f" [correlation_id={cid}]" if cid else ""


class Cache:
    """Anahtar -> LLM cevabı. Dosya satır başına {"anahtar", "sonuc"} JSON'udur; bozuk satırlar atlanır."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: Dict[str, dict] = {}
        self._lock = threading.Lock()
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                    self._data[item["anahtar"]] = item["sonuc"]
                except (ValueError, KeyError, TypeError):
                    continue

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def get(self, key: str, default: Optional[dict] = None) -> Optional[dict]:
        return self._data.get(key, default)

    def put(self, key: str, value: dict) -> None:
        """Cevabı hemen dosyaya ekler: betik yarıda kesilirse yapılan iş kaybolmaz."""
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"anahtar": key, "sonuc": value}, ensure_ascii=False) + "\n")
            self._data[key] = value
