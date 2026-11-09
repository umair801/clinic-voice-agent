"""Output-side guard for failure 5. An agent sentence may claim a booking ONLY if the clinic record confirms it.

Two layers:
  violations(state, text)  pure check, used by evals and by the live guard.
  SentenceGuard            streaming wrapper used in the voice worker. It holds each sentence until it is
                           complete, checks it, and REPLACES a false claim with a safe line before the caller
                           hears it. Every block is logged as a claim_blocked event.

Built from a real test call: the model said "you're set" after a slot was chosen but before booking was
confirmed. Detect-only logging was too late (the words had already been spoken), so the live path now blocks.
"""
import re

from ava.state import BookingStatus

CLAIM = re.compile(r"(you('re| are)\s+(all\s+)?(booked|set|scheduled|confirmed)|(is|are|has been|have been)\s+(booked|scheduled|confirmed)|"
                   r"i('ve| have)\s+(booked|scheduled)|appointment\s+is\s+(booked|confirmed)|all\s+booked)", re.I)
# Words that make a generic claim cover more than the caller ("everyone is booked", "the kids are all set").
WIDE = re.compile(r"\b(all|everyone|everybody|everything|both|children|kids)\b", re.I)
# A sentence that also says something is still open ("your visit is booked, Lucas still needs one") is honest.
OPEN = re.compile(r"\b(still|not|yet|don'?t|doesn'?t|haven'?t|hasn'?t|need|needs|left|remaining|without)\b", re.I)

GOODBYE_WORDS = re.compile(r"\b(good\s?bye|bye|adi[oó]s|take care|have a (great|good|nice|wonderful) day)\b", re.I)
ASK_MORE = {"en": "Is there anything else I can help you with?", "es": "¿Hay algo más en lo que pueda ayudarle?"}

# "No visit is booked yet" / "Sofia is not booked yet" are honest statements, not claims.
NEGATED = re.compile(r"\b(no|none|nothing|nobody)\b[^.!?]{0,30}\b(is|are|has been|have been)\s+(booked|scheduled|confirmed)\b|"
                     r"\b(booked|scheduled|confirmed)\s+yet\b", re.I)
PENDING_ASK = {"en": "Before you go, {who} still has no appointment. Should I book {who} now, or leave it for another time?",
               "es": "Antes de despedirnos, a {who} todavía le falta una cita. ¿La agendamos ahora o la dejamos para otro momento?"}

# The model's own reasoning must never reach the caller ("The caller said yes, but I should confirm...").
REASONING = re.compile(r"\b(the|this) (caller|user|patient)\b|\bi should (confirm|ask|check|clarify|verify)\b|"
                       r"\bi('ll| will) ask (them|the caller)\b|\brather than guess", re.I)
# "I'm transferring you" is a promise. It is allowed only when a person was asked for or something failed.
TRANSFER_SAY = re.compile(r"\b(transferring you|connecting you|putting you through|(i('ll| will)|let me|while i) (transfer|connect) you)\b", re.I)
PROVIDER_ID = re.compile(r"\bprovider\s+#?\d+\b", re.I)

SAFE_LINE = {"en": "One moment while I confirm that with the clinic.",
             "es": "Un momento mientras lo confirmo con la clínica."}


def violations(state, text):
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if not CLAIM.search(sentence) or NEGATED.search(sentence):
            continue
        named = [p for p in state.active_patients() if p.first_name and p.first_name.lower() in sentence.lower()]
        unconfirmed = [p for p in state.active_patients() if p.status != BookingStatus.BOOKED.value]
        if named:
            bad = [p.first_name for p in named if p.status != BookingStatus.BOOKED.value]
            if bad:
                out.append(f"claims booked for {bad} but status is not booked")
        elif not state.booked():
            out.append("claims a booking but nothing is confirmed booked")
        elif unconfirmed and WIDE.search(sentence) and not OPEN.search(sentence):
            out.append("claims everyone is booked while some patients are not confirmed")
        else:
            me = state.patient("self")
            if me and not me.declined and me.status != BookingStatus.BOOKED.value:
                out.append("generic booked claim but the caller's own visit is not confirmed")
    return out


class SentenceGuard:
    """Feed streamed text in, get safe text out. Complete sentences are checked; a false claim is replaced."""
    _END = re.compile(r"[.!?]+[\"')\]]*\s+")

    def __init__(self, state, on_block=None):
        self.state, self.on_block, self.buf = state, on_block, ""
        self.asked = False                # a question was already spoken in this reply

    @staticmethod
    def _speakable(sentence):
        """Plain spoken text: no markdown, no bullets, no provider id numbers."""
        s = re.sub(r"[*#`]+", "", sentence)
        s = re.sub(r"(?m)^\s*[-\u2022]\s+", "", s)
        s = re.sub(r"\s*\n+\s*", " ", s)
        return PROVIDER_ID.sub("a provider", s)

    def _check(self, sentence):
        sentence = self._speakable(sentence)
        if REASONING.search(sentence):
            if self.on_block:
                self.on_block(["reasoning text about the caller"], sentence)
            return ""
        if TRANSFER_SAY.search(sentence):
            st = self.state
            failing = any(p.status in (BookingStatus.PENDING.value, BookingStatus.FAILED.value) for p in st.patients)
            if not (st.human_requested or st.transfer_requested or failing):
                if self.on_block:
                    self.on_block(["transfer promised but the caller did not ask for a person"], sentence)
                return ""
        if GOODBYE_WORDS.search(sentence) and self.state.has_pending_work() and not self.state.pending_tools:
            # Someone still has no appointment. Never say goodbye over open work; ask once instead (failure 7, spoken side).
            if self.on_block:
                self.on_block(["goodbye while a person still has no appointment"], sentence)
            if self.asked:
                return ""
            names = [p.first_name or p.key for p in self.state.active_patients() if p.status != BookingStatus.BOOKED.value]
            who = " and ".join(names) or "someone"
            self.asked = True
            trail = sentence[len(sentence.rstrip()):]
            return PENDING_ASK.get(self.state.language, PENDING_ASK["en"]).format(who=who) + (trail or " ")
        if GOODBYE_WORDS.search(sentence) and not self.state.caller_goodbye:
            # The caller has not said goodbye. Never close the call for them; ask instead (failure 7, spoken side).
            if self.on_block:
                self.on_block(["goodbye before the caller said goodbye"], sentence)
            trail = sentence[len(sentence.rstrip()):]
            return ASK_MORE.get(self.state.language, ASK_MORE["en"]) + (trail or " ")
        v = violations(self.state, sentence)
        if not v:
            return sentence
        if self.on_block:
            self.on_block(v, sentence)
        trail = sentence[len(sentence.rstrip()):]
        return SAFE_LINE.get(self.state.language, SAFE_LINE["en"]) + trail

    def feed(self, text):
        self.buf += text
        out, pos = [], 0
        for m in self._END.finditer(self.buf):
            chunk = self._check(self.buf[pos:m.end()])
            self.asked = self.asked or "?" in chunk
            out.append(chunk)
            pos = m.end()
        self.buf = self.buf[pos:]
        return "".join(out)

    def flush(self):
        rest, self.buf = self.buf, ""
        return self._check(rest) if rest.strip() else rest
