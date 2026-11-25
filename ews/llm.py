"""LLM istemcisi ve önbellek (3_llm_dogrulama.py kullanır).

LLMClient  OpenAI SDK + httpx (thread-safe, Windows SSL destekli) + tenacity (exponential backoff + jitter)
           üzerinden bankanın OpenAI uyumlu endpoint'ine (/v1/chat/completions) tek soru sorar,
           JSON cevabı (isteğe bağlı Pydantic v2 doğrulamasıyla) döndürür.
Cache      llm_cache.jsonl: her cevap anında dosyaya eklenir; aynı soru bir daha sorulmaz, yarıda kesilen
           koşu kaldığı yerden devam eder.
"""

from __future__ import annotations

import json
import re
import ssl
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Type

import httpx
import openai
import urllib3
from pydantic import BaseModel, ValidationError
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter


def resolve_ssl_verify(setting: object) -> object:
    """LLM_VERIFY_SSL ayarını SSL doğrulama değerine çevirir.

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


def build_httpx_verify(verify_setting: object) -> Any:
    """httpx 0.28+ için SSL doğrulama nesnesi (bool veya ssl.SSLContext) üretir."""
    v = verify_setting
    if v == "windows":
        v = resolve_ssl_verify("windows")
    if isinstance(v, bool) or isinstance(v, ssl.SSLContext):
        return v
    if isinstance(v, (str, Path)) and str(v):
        return ssl.create_default_context(cafile=str(v))
    return True


def openai_base_url(url: str) -> str:
    """Tam /v1/chat/completions adresini OpenAI SDK base_url (/v1) biçimine çevirir."""
    clean = str(url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if clean.endswith(suffix):
            clean = clean[:-len(suffix)]
            break
    return clean or "https://localhost/v1"


def _is_ssl_error(exc: BaseException) -> bool:
    """İstisna zincirinde ssl.SSLError var mı kontrol eder (SSL hatası tekrar denenmez)."""
    cur: Optional[BaseException] = exc
    seen = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLError):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


class _RetryableHTTPStatusError(Exception):
    """429 / 5xx geçici HTTP hatalarını tenacity ile yeniden denemek için sarmalayıcı."""

    def __init__(self, message: str, retry_after: Optional[int] = None):
        super().__init__(message)
        self.retry_after = retry_after


def _is_retryable_error(exc: BaseException) -> bool:
    if _is_ssl_error(exc):
        return False
    if isinstance(exc, (_RetryableHTTPStatusError, ValueError,
                        openai.RateLimitError, openai.APITimeoutError,
                        openai.InternalServerError, openai.APIConnectionError,
                        httpx.TimeoutException, httpx.ConnectError)):
        return True
    if isinstance(exc, openai.APIStatusError):
        return getattr(exc, "status_code", 0) in (429, 500, 502, 503, 504)
    return False


class _WaitWithRetryAfter(wait_exponential_jitter):
    """429 yanıtında Retry-After başlığına uyar; yoksa exponential backoff + jitter uygular."""

    def __call__(self, retry_state: Any) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, _RetryableHTTPStatusError) and exc.retry_after is not None:
            return min(float(exc.retry_after), 120.0)
        resp = getattr(exc, "response", None)
        if resp is not None:
            ra = (resp.headers.get("retry-after") or resp.headers.get("Retry-After") or "").strip()
            if ra.isdigit():
                return min(float(int(ra)), 120.0)
        return super().__call__(retry_state)


@dataclass
class LLMClient:
    url: str
    key: str = ""
    model: str = ""             # kurumsal endpoint "model" alanını reddettiği için boşsa gönderilmez
    verify: object = True       # resolve_ssl_verify çıktısı
    timeout: int = 300
    retries: int = 3
    max_tokens: int = 1024
    _local: threading.local = field(default_factory=threading.local, init=False, repr=False)

    def _clean_key(self) -> str:
        return re.sub(r"^bearer\s+", "", str(self.key or "").strip(), flags=re.I)

    def _get_session(self) -> httpx.Client:
        """Her iş parçacığı (thread) için ayrı kalıcı httpx.Client döner (thread-safe Keep-Alive, Windows SSL uyumlu)."""
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = httpx.Client(
                verify=build_httpx_verify(self.verify),
                timeout=httpx.Timeout(self.timeout, connect=30.0),
            )
            self._local.session = sess
            self._local.openai_client = None
        return sess

    def _get_openai_client(self) -> openai.OpenAI:
        """Her iş parçacığı için ayrı OpenAI SDK istemcisi (retry katmanı tenacity tarafından yönetilir)."""
        sess = self._get_session()
        client = getattr(self._local, "openai_client", None)
        sig = (self.url, self._clean_key(), self.timeout, id(sess))
        if client is None or getattr(self._local, "openai_sig", None) != sig:
            client = openai.OpenAI(
                base_url=openai_base_url(self.url),
                api_key=self._clean_key() or "not-required",
                http_client=sess,
                max_retries=0,
                timeout=self.timeout,
            )
            self._local.openai_client = client
            self._local.openai_sig = sig
        return client

    def _ask_via_custom_post(self, sess: Any, system: str, user: str, max_tokens: int) -> Tuple[Optional[dict], str]:
        """Geriye dönük uyumluluk: `_get_session().post` üzerine monkeypatch uygulanan test/çağrılar için."""
        key = self._clean_key()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        payload: Dict[str, Any] = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.0,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": False,
        }
        if self.model:
            payload["model"] = self.model
        r = sess.post(self.url, json=payload, headers=headers, timeout=self.timeout, verify=self.verify)
        status_code = getattr(r, "status_code", 200)
        is_ok = getattr(r, "ok", getattr(r, "is_success", 200 <= status_code < 300))
        if status_code in (429, 500, 502, 503, 504):
            ra_raw = (getattr(r, "headers", {}) or {}).get("Retry-After", "")
            ra = int(ra_raw) if str(ra_raw).isdigit() else None
            raise _RetryableHTTPStatusError(
                f"HTTP {status_code}: {str(getattr(r, 'text', ''))[:150]}{_correlation(r)}", retry_after=ra
            )
        if not is_ok:
            hint = " (API anahtarı boş ya da hatalı)" if status_code in (401, 403) else ""
            return None, f"HTTP {status_code}{hint}: {str(getattr(r, 'text', ''))[:200]}{_correlation(r)}"
        return _parse(r.json())

    def _ask_once(
        self,
        system: str,
        user: str,
        max_tokens: int,
        response_model: Optional[Type[BaseModel]] = None,
    ) -> Tuple[Optional[dict], str]:
        sess = self._get_session()
        if "post" in getattr(sess, "__dict__", {}):
            parsed, status = self._ask_via_custom_post(sess, system, user, max_tokens)
        else:
            client = self._get_openai_client()
            resp = client.chat.completions.create(
                model=self.model if self.model else openai.NOT_GIVEN,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0.0,
                max_tokens=max_tokens or self.max_tokens,
                stream=False,
            )
            parsed, status = _parse_completion(resp)
        if parsed is not None and response_model is not None:
            try:
                parsed = response_model.model_validate(parsed).model_dump()
            except ValidationError as exc:
                raise ValueError(f"şema doğrulama hatası: {exc.errors()[0].get('msg', exc)}") from exc
        return parsed, status

    def ask(
        self,
        system: str,
        user: str,
        max_tokens: int = 0,
        response_model: Optional[Type[BaseModel]] = None,
    ) -> Tuple[Optional[dict], str]:
        """Tek soru sorar; (JSON sözlük ya da None, durum) döndürür.

        Cevaptaki <think> bloğu atılır, ilk { ile son } arası okunur. Kesilen cevap tekrar denenmez.
        Geçici hatalarda (429, 5xx, bağlantı, bozuk JSON) tenacity ile exponential backoff + jitter
        uygulayarak `retries` kez dener; 429'da Retry-After başlığına uyar.
        """
        retrying = Retrying(
            stop=stop_after_attempt(max(1, int(self.retries))),
            wait=_WaitWithRetryAfter(multiplier=2.0, max=60.0, jitter=3.0),
            retry=retry_if_exception(_is_retryable_error),
            reraise=True,
        )
        try:
            return retrying(lambda: self._ask_once(system, user, max_tokens, response_model))
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            code = getattr(exc, "status_code", 401)
            return None, f"HTTP {code} (API anahtarı boş ya da hatalı): {str(exc)[:200]}{_correlation(getattr(exc, 'response', None))}"
        except openai.APIStatusError as exc:
            code = getattr(exc, "status_code", "?")
            hint = " (API anahtarı boş ya da hatalı)" if code in (401, 403) else ""
            return None, f"HTTP {code}{hint}: {str(exc)[:200]}{_correlation(getattr(exc, 'response', None))}"
        except _RetryableHTTPStatusError as exc:
            return None, str(exc)
        except Exception as exc:
            if _is_ssl_error(exc):
                return None, f'SSL hatası (LLM_VERIFY_SSL = "windows" ya da False deneyin): ...{str(exc)[-250:]}'
            return None, f"{type(exc).__name__}: {str(exc)[:150]}"

    def similarity(self, text: str, categories: list, threshold: float = 0.5) -> Optional[Dict[str, float]]:
        """Kurumsal /v1/moderations (paraphrase-multilingual-mpnet-base-v2) uç noktasından anlamsal benzerlik skorlarını alır.
        Endpoint kapalıysa ya da hata verirse None döner (akışı kırmaz)."""
        if not text or not categories or "<SUNUCU>" in self.url:
            return None
        mod_url = self.url.replace("/chat/completions", "/moderations")
        key = self._clean_key()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        payload = {
            "input": str(text)[:500],
            "categories": [str(c)[:300] for c in categories],
            "flag_threshold": threshold,
            "moderations_disabled": False,
        }
        try:
            r = self._get_session().post(mod_url, json=payload, headers=headers, timeout=min(self.timeout, 30))
            if getattr(r, "is_success", getattr(r, "ok", False)):
                results = r.json().get("results") or []
                if results and isinstance(results[0].get("category_scores"), dict):
                    return {str(k): float(v) for k, v in results[0]["category_scores"].items()}
        except Exception:
            return None
        return None


def _extract_json_object(content: str, finish_reason: Optional[str]) -> Tuple[Optional[dict], str]:
    if finish_reason == "length":
        return None, "cevap LLM_MAX_TOKENS sınırında kesildi"
    text = (content or "").split("</think>")[-1]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"cevapta JSON yok: {text[:100]!r}")
    return json.loads(text[start:end + 1]), "ok"


def _parse_completion(resp: Any) -> Tuple[Optional[dict], str]:
    choices = getattr(resp, "choices", None) or []
    if not choices:
        raise ValueError("cevapta choices boş")
    choice = choices[0]
    msg = getattr(choice, "message", None)
    content = getattr(msg, "content", None) or ""
    return _extract_json_object(content, getattr(choice, "finish_reason", None))


def _parse(body: dict) -> Tuple[Optional[dict], str]:
    """Chat cevabından JSON sözlüğü çıkarır. JSON bozuksa ValueError (tekrar denenir)."""
    choice = (body.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    return _extract_json_object(content, choice.get("finish_reason"))


def _correlation(r: Any) -> str:
    """Sunucunun hata için verdiği correlation_id (sorun bildirirken kayıtta bulunması için)."""
    if r is None:
        return ""
    try:
        cid = r.json().get("correlation_id")
    except Exception:
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
