"""ASR text control intent classification."""

from __future__ import annotations

import re

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ControlIntent(str, Enum):
    NORMAL = "normal"
    WAKE = "wake"
    INTERRUPT = "interrupt"
    EXIT = "exit"


@dataclass(frozen=True, slots=True)
class ControlIntentResult:
    intent: ControlIntent
    keyword: str = ""


DEFAULT_WAKE_KEYWORDS = (
    "你好小智",
    "小智你好",
    "小智同学",
)
DEFAULT_INTERRUPT_KEYWORDS = (
    "等一下",
    "我打断一下",
    "别说了",
    "打住",
)
DEFAULT_EXIT_KEYWORDS = (
    "好的谢谢",
    "辛苦了",
    "不用了",
    "没问题了",
    "没事了",
    "不聊了",
    "就这样吧",
    "今天先聊到这",
    "拜拜",
    "退出",
)

_TRAILING_PARTICLES = ("吧", "了", "啦", "呀", "啊")


@dataclass(frozen=True, slots=True)
class ControlKeywordSet:
    wake: tuple[str, ...] = DEFAULT_WAKE_KEYWORDS
    interrupt: tuple[str, ...] = DEFAULT_INTERRUPT_KEYWORDS
    exit: tuple[str, ...] = DEFAULT_EXIT_KEYWORDS

    @classmethod
    def from_kws_file(cls, path: Path | str | None) -> "ControlKeywordSet":
        if path is None:
            return cls()
        kws_path = Path(path)
        if not kws_path.exists():
            return cls()
        groups: dict[str, list[str]] = {"wake": [], "interrupt": [], "exit": []}
        for raw_line in kws_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            for action in groups:
                marker = f"@{action}:"
                if marker not in line:
                    continue
                keyword = line.split(marker, 1)[1].strip()
                if keyword:
                    groups[action].append(keyword)
                break
        return cls(
            wake=tuple(groups["wake"]) or DEFAULT_WAKE_KEYWORDS,
            interrupt=tuple(groups["interrupt"]) or DEFAULT_INTERRUPT_KEYWORDS,
            exit=tuple(groups["exit"]) or DEFAULT_EXIT_KEYWORDS,
        )

    def classify(self, text: str) -> ControlIntentResult:
        normalized = _normalize_text(text)
        if not normalized:
            return ControlIntentResult(ControlIntent.NORMAL)
        for intent, keywords in (
            (ControlIntent.EXIT, self.exit),
            (ControlIntent.INTERRUPT, self.interrupt),
            (ControlIntent.WAKE, self.wake),
        ):
            keyword = _match_keywords(normalized, keywords, intent=intent)
            if keyword:
                return ControlIntentResult(intent, keyword)
        return ControlIntentResult(ControlIntent.NORMAL)


def _match_keywords(
    normalized: str,
    keywords: tuple[str, ...],
    *,
    intent: ControlIntent,
) -> str:
    for keyword in sorted(keywords, key=len, reverse=True):
        normalized_keyword = _normalize_text(keyword)
        if not normalized_keyword:
            continue
        if normalized == normalized_keyword:
            return keyword
        if intent is ControlIntent.EXIT and _matches_exit_phrase(
            normalized,
            normalized_keyword,
        ):
            return keyword
    return ""


def _matches_exit_phrase(normalized: str, keyword: str) -> bool:
    if not normalized.startswith(keyword):
        return False
    suffix = normalized[len(keyword) :]
    return bool(suffix) and all(char in _TRAILING_PARTICLES for char in suffix)


def _normalize_text(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text).lower()


__all__ = [
    "ControlIntent",
    "ControlIntentResult",
    "ControlKeywordSet",
]
