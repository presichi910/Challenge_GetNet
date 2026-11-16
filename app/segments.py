"""Split an answer into numbered segments, and decide in CODE which segments may skip the evidence check.

The first verifier asked a model to LIST the statements of an answer that the evidence does not support. A model that
lists decides what to leave out, and it left out unsupported steps (q05, q08: between 2 and 10 statements for the same
answer). Here the code splits the answer, the model must answer for EVERY segment, and a segment it does not answer is
unsupported by construction. Nothing can be omitted.

A segment may be labelled "not a claim" only if it is a greeting, thanks, apology or a closing/offer of help, and the
code does not take the model's word for it: the segment must look like one (a closing lexicon in English, Portuguese
and Spanish), be short, carry NO digit, currency or percentage, and not start with an action verb. Anything else needs a
quote from the evidence, whatever the model labelled it.
"""
from __future__ import annotations

import re

from app.guard import words

MAX_SEGMENTS = 60
MAX_CLOSING_WORDS = 25

_ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "vs.", "approx.", "ex.", "p. ex.", "ou seja.", "sr.", "sra.", "dr.", "dra.", "no.", "nº.")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÁÉÍÓÚÂÊÔÃÕÇÀÜÑ¿¡\"“(\[0-9])")
_LIST_MARKER = re.compile(r"^\s*(?:[-*•–—]|\d{1,2}[.)])\s+")
_HEADING_LINE = re.compile(r"^\s*(?:#{1,6}\s+.+|(?:\*\*|__)[^*_]{1,80}(?:\*\*|__):?\s*)$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")

# the words that make a segment a greeting, thanks, apology or closing. Any one of them, plus the limits, is enough.
_CLOSING = re.compile(
    r"(?<!\w)(?:hello|hi|hey|thanks|thank you|you're welcome|you are welcome|glad|happy to (?:help|assist)|let me know|feel free|"
    r"anything else|if you (?:need|would like|'d like|want)|i can (?:help|assist|open|arrange)|i'm sorry|i am sorry|sorry|apolog\w*|"
    r"have a (?:great|good|nice)|good luck|of course|no problem|i hope|"
    r"olá|oi|bom dia|boa tarde|boa noite|obrigad[oa]|de nada|fico feliz|à disposição|a disposição|avise|qualquer dúvida|se precisar|"
    r"posso (?:ajudar|abrir|auxiliar)|desculp\w*|lamento|boa sorte|espero que|sinta-se à vontade|"
    r"hola|buen(?:os|as) (?:días|tardes|noches)|gracias|con gusto|a tu disposición|a su disposición|avísame|avísenme|cualquier duda|"
    r"si necesitas|si necesita|puedo (?:ayudar|abrir|ayudarte)|disculp\w*|buena suerte)(?!\w)", re.IGNORECASE)

_LEAD_IN = {"please", "then", "first", "next", "also", "now", "just", "and", "so", "por", "favor", "depois", "então", "entao",
            "primeiro", "também", "agora", "luego", "después", "primero", "ahora"}
IMPERATIVES = {
    # English
    "press", "open", "select", "go", "restart", "turn", "check", "click", "tap", "enter", "contact", "call", "send", "verify",
    "make", "connect", "move", "unplug", "plug", "try", "ensure", "confirm", "wait", "power", "reboot", "reset", "update",
    "install", "use", "remove", "insert", "scan", "log", "sign", "access", "navigate", "choose", "switch", "set", "change", "add",
    "retry", "run", "reload", "replace", "hold", "push", "dial", "boost", "improve", "reach",
    # Portuguese
    "pressione", "abra", "selecione", "acesse", "reinicie", "ligue", "desligue", "verifique", "clique", "toque", "entre", "conecte",
    "mova", "tente", "certifique-se", "aguarde", "atualize", "remova", "insira", "escolha", "mude", "altere", "adicione",
    "repita", "recarregue", "substitua", "segure", "disque", "ative", "confirme", "envie", "contate", "fale",
    # Spanish
    "presiona", "abre", "selecciona", "ve", "reinicia", "enciende", "apaga", "verifica", "haz", "conecta", "mueve", "intenta",
    "asegúrate", "espera", "actualiza", "usa", "quita", "inserta", "elige", "cambia", "agrega", "repite", "recarga", "reemplaza",
    "mantén", "marca", "activa", "confirma", "envía", "llama", "contacta",
}


def _clean(line: str) -> str:
    line = _LIST_MARKER.sub("", line)
    line = re.sub(r"(\*\*|__|`)", "", line)
    return re.sub(r"\s+", " ", line).strip()


def _sentences(text: str) -> list[str]:
    protected = text
    for i, abbr in enumerate(_ABBREVIATIONS):
        def hide_dots(m: re.Match, i: int = i) -> str:  # `i` is bound as a default: this abbreviation's marker, not the loop's last value
            return m.group(0).replace(".", f"\u0001{i}\u0001")

        protected = re.sub(re.escape(abbr), hide_dots, protected, flags=re.IGNORECASE)
    parts = _SENTENCE_END.split(protected)
    return [re.sub(r"\u0001\d+\u0001", ".", p).strip() for p in parts if p.strip()]


def split_segments(answer: str) -> list[str] | None:
    """The answer as numbered-able segments: one per sentence, list item or table row. Headings are structure, not
    claims, and are left out. None if there are more than MAX_SEGMENTS (the caller falls back to the older check)."""
    out: list[str] = []
    header_row = False  # the last segment is a table row that may turn out to be the header
    for line in (answer or "").splitlines():
        if _TABLE_RULE.match(line):
            if header_row and out:
                out.pop()  # the row above a |---| rule is the table header: structure, not a claim
            header_row = False
            continue
        header_row = False
        if not line.strip() or _HEADING_LINE.match(line):
            continue
        if line.strip().startswith("|"):  # a table row is one segment: its cells joined
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            text = _clean(" | ".join(c for c in cells if c))
            if len(words(text)) >= 1:
                out.append(text)
                header_row = True
            continue
        for sentence in _sentences(_clean(line)):
            if len(words(sentence)) >= 1:
                out.append(sentence)
    return out if len(out) <= MAX_SEGMENTS else None


def _starts_with_action_verb(segment: str) -> bool:
    toks = words(segment)
    while toks and toks[0] in _LEAD_IN:
        toks = toks[1:]
    return bool(toks) and toks[0] in IMPERATIVES


def may_skip_evidence(segment: str) -> bool:
    """True only for a greeting, thanks, apology or closing: short, with the closing lexicon, no figures, no action verb."""
    if len(words(segment)) > MAX_CLOSING_WORDS or re.search(r"\d|R\$|%|€|\$", segment):
        return False
    if _starts_with_action_verb(segment):
        return False
    return _CLOSING.search(segment) is not None
