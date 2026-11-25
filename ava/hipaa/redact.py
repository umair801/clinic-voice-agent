"""Best-effort removal of personal details from text before it is stored. This lowers risk, it is not a guarantee."""
import re

MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
NUMWORDS = r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine|ten)"
PATTERNS = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("DATE", re.compile(rf"\b(?:\d{{1,2}}[/.-]\d{{1,2}}[/.-]\d{{2,4}}|\d{{4}}-\d{{2}}-\d{{2}}|(?:{MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{2,4}})?|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})(?:,?\s+\d{{2,4}})?)\b", re.I)),
    ("PHONE", re.compile(r"(?:\+?\d[\s().-]?){7,15}\d")),
    ("SPOKEN_DIGITS", re.compile(rf"\b(?:{NUMWORDS}[\s,-]+){{4,}}{NUMWORDS}\b", re.I)),
    ("ID", re.compile(r"\b\d{5,}\b")),
]


def redact(text, names=()):
    """names: known words to hide (patient first/last name, caller name). Returns text with [LABEL] markers."""
    if not text:
        return text
    out = text
    for label, pat in PATTERNS:
        out = pat.sub(f"[{label}]", out)
    for n in sorted({n for n in names if n and len(n) > 1}, key=len, reverse=True):
        out = re.sub(rf"\b{re.escape(n)}\b", "[NAME]", out, flags=re.I)
    return out
