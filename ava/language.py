"""Language policy. Fixes failure 4.

The baseline rule flipped the whole call to Spanish on ANY non-English message ("Sí", a name)
and had no path back. This policy:
  1. switches immediately on a clear, explicit request ("Spanish please", "en español", "English please");
  2. otherwise switches only after SUSTAINED evidence: two consecutive utterances that are clearly in
     the other language (at least 3 words, several language-specific function words);
  3. never counts single words, acknowledgements, names or accents;
  4. switches back on an explicit request or on the same sustained-evidence rule.

Heuristic and offline on purpose (no model call, no added latency). It is word-list based, so it can
be wrong on code-switching and dialect. Its measured behavior on test phrases is in the eval suite,
and real accuracy on accented callers must be measured on test calls.
"""
import re
from dataclasses import dataclass

# Words that are strongly Spanish and not English words. Ambiguous ones (no, me, a, he, sin, pie) left out on purpose.
ES_MARKERS = {
    "que", "qué", "de", "la", "el", "los", "las", "un", "una", "unos", "es", "soy", "eres", "estoy", "está", "están",
    "mi", "mis", "tu", "su", "para", "con", "por", "como", "cómo", "cuando", "cuándo", "quiero", "necesito", "quisiera",
    "cita", "citas", "hijo", "hija", "hijos", "hijas", "nombre", "hola", "buenos", "buenas", "días", "dias", "tardes",
    "gracias", "favor", "puedo", "puede", "tengo", "tiene", "tienen", "muy", "también", "pero", "donde", "dónde",
    "hay", "nacimiento", "fecha", "mañana", "lunes", "martes", "miércoles", "jueves", "viernes", "doctor", "doctora",
    "necesitamos", "familia", "esposa", "esposo", "mamá", "papá", "nació", "nacio", "años", "sí", "si", "y", "o",
    "al", "del", "lo", "le", "se", "ya", "bien", "cuál", "cual", "cuánto", "ayuda", "ayudar",
}
# Of those, words that also exist in English and carry little signal alone.
WEAK = {"si", "sí", "y", "o", "a", "lo", "le", "se", "ya", "bien", "es", "un", "la", "el", "mi", "tu", "su", "al", "de", "doctor"}
EN_MARKERS = {
    "the", "and", "is", "are", "was", "to", "of", "my", "i", "for", "you", "can", "need", "want", "appointment",
    "please", "thank", "thanks", "yes", "no", "with", "that", "this", "have", "has", "do", "does", "it", "in", "on",
    "me", "kids", "son", "daughter", "daughters", "sons", "book", "tomorrow", "today", "name", "birth", "date", "okay",
}
ES_REQUEST = re.compile(r"(en\s+espa[nñ]ol|habla(s)?\s+espa[nñ]ol|hablan\s+espa[nñ]ol|espa[nñ]ol\s+por\s+favor|"
                        r"(speak|in|switch to|continue in|can we (speak|do this in)|do you speak)\s+spanish|spanish\s+(please|por favor))", re.I)
EN_REQUEST = re.compile(r"(in\s+english|english\s+(please|por favor)|speak\s+english|habla(s)?\s+ingl[eé]s|en\s+ingl[eé]s|"
                        r"(switch|go back|back)\s+to\s+english|can we (speak|do this in)\s+english|ingl[eé]s\s+por\s+favor|"
                        r"^\s*english[\s,.!?]*(please)?[\s.!?]*$)", re.I)

SUSTAINED_TURNS = 2
MIN_WORDS = 3
STRONG_MIN_WORDS = 5      # a full sentence...
STRONG_MARKERS = 3        # ...with at least 3 distinct language-specific words switches at once


def _tokens(text):
    return [t for t in re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ']+", text.lower())]


def clearly_spanish(text):
    toks = _tokens(text)
    if len(toks) < MIN_WORDS:
        return False
    strong = [t for t in toks if t in ES_MARKERS and t not in WEAK]
    en = [t for t in toks if t in EN_MARKERS and t not in ES_MARKERS]
    return len(set(strong)) >= 2 and len(strong) >= len(en)


def clearly_english(text):
    toks = _tokens(text)
    if len(toks) < MIN_WORDS:
        return False
    en = [t for t in toks if t in EN_MARKERS and t not in ES_MARKERS]
    es = [t for t in toks if t in ES_MARKERS and t not in WEAK]
    return len(set(en)) >= 2 and len(en) > len(es)


def _strong(text, lang):
    toks = _tokens(text)
    if len(toks) < STRONG_MIN_WORDS:
        return False
    if lang == "es":
        hits = {t for t in toks if t in ES_MARKERS and t not in WEAK}
        en = {t for t in toks if t in EN_MARKERS and t not in ES_MARKERS}
        return len(hits) >= STRONG_MARKERS and len(hits) > len(en)
    hits = {t for t in toks if t in EN_MARKERS and t not in ES_MARKERS}
    es = {t for t in toks if t in ES_MARKERS and t not in WEAK}
    return len(hits) >= STRONG_MARKERS and len(hits) > len(es)


@dataclass
class LanguageDecision:
    language: str
    switched: bool
    reason: str


def observe(state, text):
    """Update state.language from one caller utterance. Returns a LanguageDecision.

    Mutates state.language, state.non_english_streak and state.english_streak.
    """
    # 1. explicit requests always win, in either direction
    if EN_REQUEST.search(text) and not ES_REQUEST.search(text):
        sw = state.language != "en"
        state.language, state.non_english_streak, state.english_streak = "en", 0, 0
        return LanguageDecision("en", sw, "explicit request for English")
    if ES_REQUEST.search(text):
        sw = state.language != "es"
        state.language, state.non_english_streak, state.english_streak = "es", 0, 0
        return LanguageDecision("es", sw, "explicit request for Spanish")

    # 2. a full sentence that is unmistakably in the other language switches immediately
    if state.language == "en" and _strong(text, "es"):
        state.language, state.non_english_streak, state.english_streak = "es", 0, 0
        return LanguageDecision("es", True, "full Spanish sentence with several Spanish-only words")
    if state.language == "es" and _strong(text, "en"):
        state.language, state.non_english_streak, state.english_streak = "en", 0, 0
        return LanguageDecision("en", True, "full English sentence with several English-only words")

    # 3. otherwise sustained evidence
    if clearly_spanish(text):
        state.non_english_streak += 1
        state.english_streak = 0
    elif clearly_english(text):
        state.english_streak += 1
        state.non_english_streak = 0
    # short, ambiguous or name-only utterances change nothing and do not reset streaks

    if state.language == "en" and state.non_english_streak >= SUSTAINED_TURNS:
        state.language, state.non_english_streak = "es", 0
        return LanguageDecision("es", True, f"{SUSTAINED_TURNS} consecutive clearly Spanish utterances")
    if state.language == "es" and state.english_streak >= SUSTAINED_TURNS:
        state.language, state.english_streak = "en", 0
        return LanguageDecision("en", True, f"{SUSTAINED_TURNS} consecutive clearly English utterances")
    return LanguageDecision(state.language, False, "no change")
