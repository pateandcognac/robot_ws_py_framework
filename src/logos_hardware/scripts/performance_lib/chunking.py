"""
Text chunking helpers for the TTP pipeline.

The director first splits an utterance at emoji (split_text_emoji in
tts_action_server.py); these helpers then subdivide each text span by
sentence / long clause so cues stay performable: ~80 chars soft target,
~100 chars hard limit, breaking at sentence enders, then clause
punctuation, then whitespace. The emoji of a (text, emoji) pair stays
attached to the *last* subchunk, since it annotates the words nearest it.
"""

import re
from typing import Iterable, List, Optional, Set, Tuple

SOFT_LIMIT = 80
HARD_LIMIT = 100

_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?…])\s+')
_CLAUSE_BREAK_CHARS = ',;:—)…'


def _best_break(text: str, soft: int, hard: int) -> int:
    """Index to break an over-long span at, preferring punctuation."""
    window = text[:hard]
    for pattern in (
        lambda c: c in _CLAUSE_BREAK_CHARS,
        lambda c: c.isspace(),
    ):
        best = -1
        for i, ch in enumerate(window):
            if i < soft // 2:
                continue
            if pattern(ch):
                best = i
        if best > 0:
            return best + 1
    return hard  # no natural break: hard cut


def subchunk_text(text: str, soft: int = SOFT_LIMIT, hard: int = HARD_LIMIT) -> List[str]:
    """Split a text span into sentence/clause-sized chunks."""
    chunks: List[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text.strip()):
        sentence = sentence.strip()
        while len(sentence) > hard:
            cut = _best_break(sentence, soft, hard)
            head, sentence = sentence[:cut].strip(), sentence[cut:].strip()
            if head:
                chunks.append(head)
        if sentence:
            chunks.append(sentence)
    return chunks


def subchunk_pairs(
    pairs: Iterable[Tuple[str, str]],
    soft: int = SOFT_LIMIT,
    hard: int = HARD_LIMIT,
) -> List[Tuple[str, str]]:
    """
    Subdivide (text, emoji) pairs from the emoji splitter by sentence/clause.
    The pair's emoji stays on its last subchunk (nearest the emoji).
    """
    out: List[Tuple[str, str]] = []
    for text, emoji in pairs:
        subs = subchunk_text(text, soft, hard)
        if not subs:
            if emoji:
                out.append(("", emoji))
            continue
        for sub in subs[:-1]:
            out.append((sub, ""))
        out.append((subs[-1], emoji))
    return out


def find_emoji(text: str, emoji_keys: Iterable[str]) -> Optional[str]:
    """
    Return the first known emoji present in text (longest match wins at a
    given position), or None. Used to resolve LUT lookups for free-text
    gesture commands where emoji and prose share one string.
    """
    if not text:
        return None
    hits = []
    for emoji in emoji_keys:
        idx = text.find(emoji)
        if idx >= 0:
            hits.append((idx, -len(emoji), emoji))
    if not hits:
        return None
    hits.sort()
    return hits[0][2]


def strip_emoji(text: str, emoji_keys: Iterable[str]) -> str:
    """Remove all known emoji from text (for has-plain-text checks)."""
    for emoji in sorted(set(emoji_keys), key=len, reverse=True):
        if emoji in text:
            text = text.replace(emoji, " ")
    return " ".join(text.split())
