"""Output guard for the receptionist (failure 5 again, plus verification). A sentence may claim a booking, a cancellation or
a move ONLY if the records confirmed it, and may not state appointment details to a caller who is not verified.
Same streaming behavior as ava.output_guard.SentenceGuard: a bad sentence is replaced BEFORE the caller hears it."""
import re

from ava.output_guard import ASK_MORE, GOODBYE_WORDS, NEGATED, REASONING, TRANSFER_SAY, SentenceGuard

BOOK_CLAIM = re.compile(r"(you('re| are)\s+(all\s+)?(booked|set|scheduled|confirmed)|(is|are|has been|have been)\s+(booked|scheduled|confirmed)|"
                        r"i('ve| have)\s+(booked|scheduled)|all\s+booked|appointment\s+is\s+(booked|confirmed))", re.I)
CANCEL_CLAIM = re.compile(r"((is|are|has been|have been|was|got)\s+cancel+ed|i('ve| have)\s+cancel+ed|all\s+cancel+ed)", re.I)
MOVE_CLAIM = re.compile(r"((is|are|has been|have been|was|got)\s+(rescheduled|moved|changed)|i('ve| have)\s+(rescheduled|moved|changed))", re.I)
DETAIL = re.compile(r"\b(your|his|her|their)\s+(next\s+)?(appointment|visit)\b.{0,60}(\d|monday|tuesday|wednesday|thursday|friday|a\.?m\.?|p\.?m\.?|january|"
                    r"february|march|april|may|june|july|august|september|october|november|december)", re.I)

SAFE = {"en": "One moment while I confirm that with the records.", "es": "Un momento mientras lo confirmo con los registros."}
SAFE_PHI = {"en": "I can share that once I have verified who I am speaking with.", "es": "Puedo compartir eso cuando haya verificado con quién hablo."}


def violations(state, text):
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if BOOK_CLAIM.search(sentence) and not NEGATED.search(sentence) and not state.booked():
            out.append("claims a booking but nothing is confirmed booked")
        if CANCEL_CLAIM.search(sentence) and not state.confirmed("cancel"):
            out.append("claims a cancellation but nothing is confirmed cancelled")
        if MOVE_CLAIM.search(sentence) and not state.confirmed("reschedule"):
            out.append("claims a move but no reschedule is confirmed")
        if DETAIL.search(sentence) and not state.verified:
            out.append("states appointment details to a caller who is not verified")
    return out


class ReceptionistGuard(SentenceGuard):
    def _check(self, sentence):
        sentence = self._speakable(sentence)
        st = self.state
        trail = sentence[len(sentence.rstrip()):]
        if REASONING.search(sentence):
            if self.on_block:
                self.on_block(["reasoning text about the caller"], sentence)
            return ""
        if TRANSFER_SAY.search(sentence):
            failing = bool(st.pending_ops()) or bool(st.gate and st.gate.locked) or st.urgent
            if not (st.human_requested or st.transfer_requested or failing):
                if self.on_block:
                    self.on_block(["transfer promised but the caller did not ask for a person"], sentence)
                return ""
        if GOODBYE_WORDS.search(sentence) and not st.urgent and (st.has_pending_work() and not st.pending_tools and not st.callback_saved):
            if self.on_block:
                self.on_block(["goodbye while a change is still being confirmed"], sentence)
            return "" if self.asked else ("Before you go, one change is still being confirmed. Would you like me to leave a message so a person follows up? " + trail)
        if GOODBYE_WORDS.search(sentence) and not st.caller_goodbye and not st.urgent:
            if self.on_block:
                self.on_block(["goodbye before the caller said goodbye"], sentence)
            return ASK_MORE.get(st.language, ASK_MORE["en"]) + (trail or " ")
        v = violations(st, sentence)
        if not v:
            return sentence
        if self.on_block:
            self.on_block(v, sentence)
        table = SAFE_PHI if any("not verified" in x for x in v) else SAFE
        return table.get(st.language, table["en"]) + trail
