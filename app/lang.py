"""Tiny language detector for English / Portuguese / Spanish. No dependencies, deterministic.

Used by the judge (code, not the LLM, decides whether the answer is in the user's language) and by the
chat graph (to check the final answer). `detect_language` returns None when it cannot tell, and callers
must read None as "do not fail on this".

Only words that belong to ONE of the three languages are counted; words shared by two of them
(de, que, no, para, como, mas, ser...) are left out on purpose.
"""
from __future__ import annotations

import re

_WORDS: dict[str, frozenset[str]] = {
    "en": frozenset(
        "the and is are you your to of with for what whats how can my it this that will be not have "
        "please hi hello there when which on in at from by if or we our would could an about write why "
        "where was were been i am does did thanks thank want need show tell".split()),
    "pt": frozenset(
        "não nao você voce uma um os do da dos das em na nas com ao aos é são estão vou quero meu minha "
        "seu sua mais muito obrigado obrigada olá ola posso pode quando qual também já ainda tem têm "
        "ontem hoje dinheiro vendas receber conta preciso falar atendente pessoa bom boa dia".split()),
    "es": frozenset(
        "el los las del en con es son qué cómo cuándo cuál quiero necesito mi mis su sus gracias hola "
        "también más muy pero hay puedo puede dinero ventas al y un una lo cuenta cobro hablar persona "
        "buenos días tengo cuando cual".split()),
}
_CHARS = {"pt": "ãõçâêô", "es": "ñ¿¡"}
_MIN_HITS = 2          # fewer matching words than this: not enough to tell
_MARGIN = 2            # the winner needs at least this times the runner-up
_CHAR_BONUS_CAP = 2    # a pt/es-looking character counts for little, so a pt term inside an English answer does not flip it


def _scores(text: str) -> dict[str, int]:
    t = text.lower()
    tokens = re.findall(r"[^\W\d_]+", t)
    out = {}
    for lang, words in _WORDS.items():
        hits = sum(1 for w in tokens if w in words)
        chars = sum(t.count(c) for c in _CHARS.get(lang, ""))
        out[lang] = hits + min(_CHAR_BONUS_CAP, chars)
    return out


def detect_language(text: str) -> str | None:
    """'en', 'pt', 'es', or None when the text is too short or too mixed to say."""
    ranked = sorted(_scores(text or "").items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    if top < _MIN_HITS or top < _MARGIN * second:
        return None
    return best


def same_language(a: str, b: str) -> bool | None:
    """True/False when both texts have a clear language, None when either is unclear."""
    la, lb = detect_language(a), detect_language(b)
    return None if la is None or lb is None else la == lb
