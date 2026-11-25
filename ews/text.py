"""EWS ortak metin yardımcıları.

Config üreticisi (2_sql_to_parquet.py) ve skorlama (3_scoring.py) metni bu modülle
normalize eder. Alias üretimi ile alias eşleştirmesi aynı kuralla yapılmazsa, config'te
görünen bir alias haberde bulunamaz ya da beklenmeyen yerde bulunur; tek kaynak bu
tutarsızlığı önler.

İçerik
  - Gevşek normalizasyon: Türkçe karakterleri ASCII'ye katlar, büyük/küçük harf farkını
    kaldırır ("İNŞAAT" -> "insaat") ve orijinal metindeki konumları korur.
  - Türkçe duyarlı küçük harf: ı/i ayrımını koruyan küçük harfe çevirme (config üreticisi
    tek kelimelik aday uzunluğunu hesaplarken kullanır).
  - LOOSE_SINGLE_WORDS: eski regex skorunun tek kelime istisnaları (artık kullanılmıyor; 2_sql_to_parquet
    unvanın ilk kelimesini kendisi ekliyor).
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import List, Optional, Tuple


# =============================================================================
# 1. Gevşek normalizasyon
# =============================================================================
# Türkçe harflerin ASCII karşılıkları; haberlerdeki "Gumushane", "GÜMÜŞHANE", "gümüşhane" yazımları
# aynı biçime iner.
_TURKISH_FOLD = str.maketrans({
    "ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
    "ç": "c", "Ç": "c", "ö": "o", "Ö": "o", "ü": "u", "Ü": "u",
})

# Hızlı 1-e-1 katlama tablosu: ASCII + Türkçe + sık kullanılan tipografik işaretler (uzunluk değişmez).
_FAST_EXTRA_CHARS = "çğıöşüÇĞİÖŞÜâîûÂÎÛ‘’“”–—•₺€£"
_FAST_TRANS = str.maketrans({
    **{chr(i): chr(i).lower() for i in range(128)},
    "ı": "i", "İ": "i", "I": "i",
    "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
    "ç": "c", "Ç": "c", "ö": "o", "Ö": "o", "ü": "u", "Ü": "u",
    "â": "a", "Â": "a", "î": "i", "Î": "i", "û": "u", "Û": "u",
})
_NON_FAST_RE = re.compile(r"[^\x00-\x7fçğıöşüÇĞİÖŞÜâîûÂÎÛ‘’“”–—•₺€£]")


@lru_cache(maxsize=8192)
def _normalize_char(ch: str) -> str:
    """Tek karakteri gevşek biçime çevirir (NFKC, Türkçe katlama, küçük harf, aksan temizliği)."""
    value = unicodedata.normalize("NFKC", ch)
    value = value.translate(_TURKISH_FOLD).casefold()
    return "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))


def fast_normalize_if_1to1(text: object) -> Optional[str]:
    """Metin yalnızca 1-e-1 güvenli karakterlerden oluşuyorsa C hızında normalize metin döner; aksi halde None."""
    if text is None:
        return ""
    value = str(text)
    if not value:
        return ""
    if _NON_FAST_RE.search(value) is None:
        return value.translate(_FAST_TRANS)
    return None


def normalize_with_offsets(text: object) -> Tuple[str, List[int], List[int]]:
    """Metni gevşek normalize eder ve konum eşlemesini döndürür.

    Normalize metindeki her karakter için orijinal metindeki [başlangıç, bitiş) konumu
    verilir. Normalize metnin uzunluğu orijinalden farklı olabildiği için ("…" -> "...")
    eşleşme konumları orijinal metne her zaman bu eşlemeyle çevrilmelidir; aksi hâlde
    snippet ve cümle sınırları kayar.
    """
    if text is None:
        return "", [], []

    value = str(text)
    fast = fast_normalize_if_1to1(value)
    if fast is not None:
        n = len(fast)
        return fast, list(range(n)), list(range(1, n + 1))

    out: List[str] = []
    starts: List[int] = []
    ends: List[int] = []

    for index, ch in enumerate(value):
        normalized = _normalize_char(ch)
        if not normalized:
            if ends:
                ends[-1] = index + 1
            continue
        for c in normalized:
            out.append(c)
            starts.append(index)
            ends.append(index + 1)

    return "".join(out), starts, ends


def normalize_text(text: object) -> str:
    """Gevşek normalize edilmiş metni döndürür ("Gümüşhane" -> "gumushane")."""
    fast = fast_normalize_if_1to1(text)
    if fast is not None:
        return fast
    return normalize_with_offsets(text)[0]


def to_original_span(
    starts: List[int], ends: List[int], norm_start: int, norm_end: int, original_length: int,
) -> Tuple[int, int]:
    """Normalize metindeki [norm_start, norm_end) aralığını orijinal metin konumlarına çevirir."""
    if norm_start >= len(starts):
        return original_length, original_length
    original_start = starts[norm_start]
    original_end = ends[norm_end - 1] if norm_end > norm_start else original_start
    return original_start, original_end


# =============================================================================
# 2. Türkçe duyarlı küçük harf
# =============================================================================
# Türkçe duyarlı küçük harfe çevirmede korunan harfler.
_TURKISH_LETTERS = set("çğıöşüÇĞİÖŞÜ")


def turkish_lower(text: object) -> str:
    """Türkçe kurallarıyla küçük harfe çevirir.

    Türkçe harfler korunur, diğer aksanlar atılır:
    "ASİL" -> "asil", "ASIL" -> "asıl", "Hâkim" -> "hakim".
    """
    if text is None:
        return ""
    value = unicodedata.normalize("NFKC", str(text))
    value = value.replace("I", "ı").replace("İ", "i")
    out = []
    for ch in value:
        if ch in _TURKISH_LETTERS:
            out.append(ch)
            continue
        decomposed = unicodedata.normalize("NFKD", ch)
        out.append("".join(c for c in decomposed if not unicodedata.combining(c)))
    return "".join(out).lower()


# =============================================================================
# 3. Tek kelimelik loose alias listesi
# =============================================================================
# Tek kelimelik alias'lar varsayılan olarak strict'tir: geçerli sayılması için aynı haberde
# şirketin bir loose alias'ı da eşleşmelidir (bkz. 3_scoring.py). Tek kelime, başka bir
# şirketin ya da sıradan bir kelimenin parçası olabileceği için bu varsayılan yanlış
# atfı önler.
# Bu listedeki ayırt edici adlar istisnadır: çapa aranmadan eşleşir. Alias'lar tam kelime
# olarak ve yazım farkı gözetilmeden eşleşir (3_scoring.py); sıradan kelime ya da kişi adı
# olmayan, sektörde tek anlamlı adlar eklenmelidir. Haberler 4_llm_dogrulama.py ile
# doğrulandığından kalan yanlış eşleşmeler (sponsorluk, kulüp adı) orada ayıklanır. Liste
# hem config üretiminde hem skorlamada kullanılır.
LOOSE_SINGLE_WORDS = {"ÖrnekMarka"}  # bankadaki liste paylaşılmadı; artık kullanılmıyor


def is_loose_single_word(word: object) -> bool:
    """Kelime LOOSE_SINGLE_WORDS listesinde mi? Karşılaştırma gevşek normalize biçimde yapılır."""
    key = normalize_text(word).strip()
    return bool(key) and key in {normalize_text(w).strip() for w in LOOSE_SINGLE_WORDS}