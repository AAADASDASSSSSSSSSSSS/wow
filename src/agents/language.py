"""Reply-language selection shared by every agent.

The service serves a mixed Chinese/English audience, so a reply must follow the
language the user actually wrote in, not the language the system prompt happens
to be written in. Without this the model drifts: an English system prompt pulls
answers into English even when the request was Chinese, and a hard-coded Chinese
string does the opposite.

Detection is deterministic (a Unicode script census) rather than a model call so
that the same conversation always resolves to the same reply language, costs
nothing, and cannot fail at a provider boundary. It is script-based rather than
word-based on purpose: a hardware request is full of Latin identifiers
("帮我做一个 STM32F103C8T6 最小系统板"), and a word-frequency detector would call
that English.

Typical use inside a node::

    directive = reply_language_directive(state["messages"], config)
    system = SystemMessage(content=f"{INSTRUCTIONS}\\n\\n{directive}")
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

__all__ = [
    "DEFAULT_LANGUAGE",
    "LANGUAGE_NAMES",
    "detect_language",
    "conversation_language",
    "language_directive",
    "localized",
    "reply_language",
    "reply_language_directive",
    "with_reply_language",
]

DEFAULT_LANGUAGE = "en"

# Language code -> the name used when instructing the model. The name is written
# in English because the surrounding system prompts are, and providers follow an
# English meta-instruction more reliably than a translated one.
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "ru": "Russian",
    "ar": "Arabic",
    "he": "Hebrew",
    "th": "Thai",
    "hi": "Hindi",
    "el": "Greek",
}

# Ranges are deliberately coarse: we only need to tell scripts apart, not
# identify a language inside one script.
_SCRIPT_RANGES: tuple[tuple[str, int, int], ...] = (
    # Japanese kana are checked before the shared Han block so that Japanese
    # text is not reported as Chinese.
    ("ja", 0x3040, 0x30FF),  # Hiragana + Katakana
    ("ja", 0x31F0, 0x31FF),  # Katakana phonetic extensions
    ("ko", 0x1100, 0x11FF),  # Hangul Jamo
    ("ko", 0x3130, 0x318F),  # Hangul compatibility Jamo
    ("ko", 0xAC00, 0xD7AF),  # Hangul syllables
    ("zh", 0x3400, 0x4DBF),  # CJK extension A
    ("zh", 0x4E00, 0x9FFF),  # CJK unified ideographs
    ("zh", 0xF900, 0xFAFF),  # CJK compatibility ideographs
    ("zh", 0x20000, 0x2A6DF),  # CJK extension B
    ("el", 0x0370, 0x03FF),
    ("ru", 0x0400, 0x04FF),  # Cyrillic; Russian is the pragmatic default
    ("he", 0x0590, 0x05FF),
    ("ar", 0x0600, 0x06FF),
    ("ar", 0x0750, 0x077F),
    ("hi", 0x0900, 0x097F),  # Devanagari
    ("th", 0x0E00, 0x0E7F),
)

# A non-Latin script wins once it holds this share of the letters. It is low
# because Latin identifiers (part numbers, net names, file paths) inflate the
# Latin count in an otherwise fully Chinese sentence.
_SCRIPT_SHARE_THRESHOLD = 0.15

# Latin text this short carries no signal ("ok", "yes", a bare file path), so we
# keep the language already established by earlier turns instead of flipping.
_MIN_LATIN_LETTERS = 4

_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`]*`")
_URL = re.compile(r"https?://\S+|www\.\S+")
_WINDOWS_PATH = re.compile(r"[A-Za-z]:\\[^\s\"']*")
_POSIX_PATH = re.compile(r"(?<!\w)/(?:[\w.-]+/)+[\w.-]*")


def _strip_non_prose(text: str) -> str:
    """Remove spans that say nothing about the user's language.

    Code blocks, URLs and file paths are Latin no matter what language the
    surrounding question is in, and a pasted English log is often the reason a
    Chinese question gets answered in English.
    """
    for pattern in (_FENCED_CODE, _INLINE_CODE, _URL, _WINDOWS_PATH, _POSIX_PATH):
        text = pattern.sub(" ", text)
    return text


def _script_of(char: str) -> str | None:
    code = ord(char)
    for language, start, end in _SCRIPT_RANGES:
        if start <= code <= end:
            return language
    return None


def detect_language(text: str) -> str | None:
    """Return a language code for ``text``, or ``None`` when undecidable.

    ``None`` means "no signal" (empty, punctuation only, a bare path, a two
    letter acknowledgement) and lets the caller fall back to an earlier turn
    rather than guess.
    """
    if not text:
        return None
    prose = _strip_non_prose(text)

    counts: dict[str, int] = {}
    latin = 0
    for char in prose:
        script = _script_of(char)
        if script is not None:
            counts[script] = counts.get(script, 0) + 1
        elif char.isalpha() and char.isascii():
            latin += 1

    total_letters = latin + sum(counts.values())
    if total_letters == 0:
        return None

    if counts:
        language, hits = max(counts.items(), key=lambda item: item[1])
        if hits / total_letters >= _SCRIPT_SHARE_THRESHOLD:
            return language

    if latin >= _MIN_LATIN_LETTERS:
        return DEFAULT_LANGUAGE
    return None


def _message_role(message: Any) -> str:
    if isinstance(message, Mapping):
        return str(message.get("type") or message.get("role") or "")
    return str(getattr(message, "type", "") or getattr(message, "role", ""))


def _message_text(message: Any) -> str:
    content = (
        message.get("content") if isinstance(message, Mapping) else getattr(message, "content", "")
    )
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return str(content or "")


def conversation_language(
    messages: Iterable[Any] | None,
    default: str = DEFAULT_LANGUAGE,
) -> str:
    """Resolve the reply language from the user turns of a conversation.

    The newest decisive user turn wins, so a mid-thread switch to another
    language is honoured immediately. Turns with no signal are skipped instead of
    resetting to ``default``, which is what keeps a Chinese thread from snapping
    back to English after the user types "ok".
    """
    if not messages:
        return default
    ordered: Sequence[Any] = list(messages)
    for message in reversed(ordered):
        if _message_role(message) not in {"human", "user"}:
            continue
        language = detect_language(_message_text(message))
        if language is not None:
            return language
    return default


def language_directive(language: str) -> str:
    """Build the system-prompt clause that pins the reply language."""
    name = LANGUAGE_NAMES.get(language, LANGUAGE_NAMES[DEFAULT_LANGUAGE])
    return (
        f"REPLY LANGUAGE: {name}. The user writes in {name}, so write every "
        f"user-facing sentence in {name} - summaries, explanations, headings, "
        "warnings, status notes and follow-up questions. Keep technical tokens "
        "verbatim in their original form and do not translate them: part "
        "numbers, net and pin names, file paths, tool and library identifiers, "
        "command lines, code, log excerpts, units and standard names. Do not "
        "restate your answer in a second language, and do not apologise for the "
        "language choice."
    )


def reply_language(
    messages: Iterable[Any] | None = None,
    config: Mapping[str, Any] | None = None,
    default: str = DEFAULT_LANGUAGE,
) -> str:
    """Resolve the reply language, letting the caller override detection.

    ``configurable.reply_language`` wins when it names a supported language, so a
    UI or an automated suite can pin the output language regardless of the
    prompt.
    """
    if config:
        configurable = config.get("configurable") or {}
        if isinstance(configurable, Mapping):
            requested = configurable.get("reply_language")
            if isinstance(requested, str):
                candidate = requested.strip().lower().replace("_", "-")
                if candidate in LANGUAGE_NAMES:
                    return candidate
                # Accept locale forms such as "zh-CN" / "en-GB".
                head = candidate.split("-", 1)[0]
                if head in LANGUAGE_NAMES:
                    return head
    return conversation_language(messages, default=default)


def reply_language_directive(
    messages: Iterable[Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> str:
    """Shorthand for ``language_directive(reply_language(...))``."""
    return language_directive(reply_language(messages, config))


def with_reply_language(
    system_prompt: str,
    messages: Iterable[Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> str:
    """Append the reply-language clause to an existing system prompt."""
    return f"{system_prompt.rstrip()}\n\n{reply_language_directive(messages, config)}"


def localized(translations: Mapping[str, str], language: str) -> str:
    """Pick a canned string for ``language``, falling back to English.

    Used for the handful of fixed agent messages that never reach the model
    (guard rails, clarification prompts, report labels). Anything not translated
    stays English instead of silently mixing languages.
    """
    return translations.get(language) or translations[DEFAULT_LANGUAGE]
