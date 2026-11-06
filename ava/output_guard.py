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

SAFE_LINE = {"en": "One moment while I confirm that with the clinic.",
             "es": "Un momento mientras lo confirmo con la clínica."}


def violations(state, text):
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if not CLAIM.search(sentence):
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

    def _check(self, sentence):
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
            out.append(self._check(self.buf[pos:m.end()]))
            pos = m.end()
        self.buf = self.buf[pos:]
        return "".join(out)

    def flush(self):
        rest, self.buf = self.buf, ""
        return self._check(rest) if rest.strip() else rest
