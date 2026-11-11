"""Optional-at-runtime Danish-to-English translation using local Argos Translate models.

The Argos Python package is installed with handle-mcp. The da→en model is downloaded on the
first translated response and then runs locally/offline. If model setup or translation fails,
original store text is returned so translation can never make a grocery tool unavailable.
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from threading import Lock
from typing import Any

log = logging.getLogger(__name__)

FROM_CODE = "da"
TO_CODE = "en"

# Only translate user-facing store text. IDs, EANs, URLs, quantities, units, money, and
# canonical store identifiers are deliberately left untouched.
_TEXT_FIELDS = frozenset(
    {
        "name",
        "category",
        "campaign",
        "offer_text",
        "group",
        "description",
        "ingredients",
        "ingredient",
        "title",
        "steps",
        "tip",
        "note",
        "notes",
        "summary",
        "hint",
        "warnings",
        "message",
        "reason",
        "status",
        "delivery",
        "kind",
        "error",
    }
)
_FEE_FIELDS = frozenset({"fees", "other_fees"})
_setup_lock = Lock()
_translate_lock = Lock()
_ready = False
_failed = False


def enabled() -> bool:
    """Translation is on by default; set HANDLE_LANGUAGE=da to keep Danish output."""
    return os.environ.get("HANDLE_LANGUAGE", "en").strip().lower() not in {
        "da",
        "danish",
        "off",
        "false",
        "0",
    }


def _argos_translate(text: str) -> str:
    """Translate one string, lazily installing the Danish-English model exactly once."""
    global _ready, _failed
    if not text.strip():
        return text

    with _setup_lock:
        if not _ready and not _failed:
            try:
                import argostranslate.package
                import argostranslate.translate

                installed = argostranslate.translate.get_installed_languages()
                source = next((lang for lang in installed if lang.code == FROM_CODE), None)
                target = next((lang for lang in installed if lang.code == TO_CODE), None)
                if source is None or target is None or source.get_translation(target) is None:
                    installed_ok = argostranslate.package.install_package_for_language_pair(
                        FROM_CODE, TO_CODE
                    )
                    if not installed_ok:
                        raise RuntimeError("Argos has no Danish-to-English model available")
                _ready = True
            except Exception as exc:  # network/model errors must not break grocery tools
                _failed = True
                log.warning(
                    "Danish-to-English translation is unavailable; returning original text (%s)",
                    type(exc).__name__,
                )
                return text

    if _failed:
        return text

    try:
        import argostranslate.translate

        # Argos models are shared process-wide; serialize inference across MCP requests.
        with _translate_lock:
            return argostranslate.translate.translate(text, FROM_CODE, TO_CODE)
    except Exception as exc:
        log.warning("Argos translation failed; returning original text (%s)", type(exc).__name__)
        return text


@lru_cache(maxsize=4096)
def _cached_translate(text: str) -> str:
    return _argos_translate(text)


_STORE_NAME_RE = re.compile(r"(?i)\b(?:BilkaToGo|Bilka|REMA\s+1000|nemlig(?:\.com)?|Køb\s*&\s*Hent)\b")


def translate_text(text: str) -> str:
    """Translate user-facing text while preserving official store names verbatim."""
    if not enabled():
        return text
    parts = _STORE_NAME_RE.split(text)
    names = _STORE_NAME_RE.findall(text)
    if not names:
        return _cached_translate(text)
    output: list[str] = []
    for index, part in enumerate(parts):
        if part:
            output.append(_cached_translate(part))
        if index < len(names):
            output.append(names[index])
    return "".join(output)


def _translate_value(value: Any) -> Any:
    if isinstance(value, str):
        return translate_text(value)
    if isinstance(value, list):
        return [_translate_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_translate_value(item) for item in value)
    return value


def translate_payload(value: Any) -> Any:
    """Translate known display-text fields recursively without changing data identifiers."""
    if not enabled():
        return value
    if isinstance(value, list):
        return [translate_payload(item) for item in value]
    if not isinstance(value, dict):
        return value

    translated: dict[Any, Any] = {}
    for key, item in value.items():
        if key in _TEXT_FIELDS:
            translated[key] = _translate_value(item)
        elif key in _FEE_FIELDS and isinstance(item, dict):
            translated[key] = {
                translate_text(str(label)): amount for label, amount in item.items()
            }
        else:
            translated[key] = translate_payload(item) if isinstance(item, (dict, list)) else item
    return translated


def reset_for_tests() -> None:
    """Reset process state; intended for isolated unit tests only."""
    global _ready, _failed
    with _setup_lock:
        _ready = False
        _failed = False
        _cached_translate.cache_clear()
