"""end_call guard and silence policy. Fixes failure 7.

The baseline ended the call from a silence timer while a booking tool was still running, and its
wrap_up -> end_call edge fired on the word "no" ("No, wait, my son too"). Here the call may end only when:
  1. the caller gave an explicit goodbye (not just "no"),
  2. no work is pending (tools in flight, unbooked patients, unfinished party),
  3. the agent has said its own goodbye first.
Silence never ends a call that has pending work. It re-prompts, then offers a human handoff.
"""
import re
from dataclasses import dataclass

from ava.state import BookingStatus

CONTINUATION = re.compile(r"\b(wait|also|and my|my son|my daughter|my kid|my kids|one more|another|actually|but|hold on|"
                          r"before you|one sec|second)\b", re.I)
GOODBYE = re.compile(r"\b(goodbye|good bye|bye|that'?s all(?!\s+(?:correct|right|fine|good|okay|ok|true)\b)|that is all(?!\s+(?:correct|right|fine|good|okay|ok|true)\b)|that'?s it|that is it|nothing else|i'?m done|"
                     r"i am done|no thanks|no thank you|all set|we'?re good|we are good|adi[oó]s|eso es todo|nada m[aá]s)\b", re.I)
BARE_NO = re.compile(r"^\s*(no|nope|nah)[\s,.!?]*(thanks|thank you|gracias)?[\s.!?]*$", re.I)

# The caller's answer to "book now or leave it?" when it is a clear "leave it". Used only after Ava asked.
DECLINE_OPEN = re.compile(r"\b(leave (it|her|him|them)\b|another time|some other time|next time|not now|maybe later|"
                          r"call (you )?back later|don'?t need|do not need|no need|skip)", re.I)
BOOK_NOW = re.compile(r"\bbook (it|him|her|me|them|now)\b", re.I)

SILENCE_REPROMPT_AFTER_S = 8.0
MAX_REPROMPTS = 2
SILENCE_FINAL_WAIT_S = 20.0


def detect_goodbye(text, step_is_wrap_up):
    """True only for an explicit goodbye. A bare "no" counts only right after 'anything else?'."""
    if CONTINUATION.search(text):
        return False
    if GOODBYE.search(text):
        return True
    return bool(step_is_wrap_up and BARE_NO.match(text))


@dataclass
class GuardDecision:
    allowed: bool
    reason: str


def can_end_call(state):
    if state.has_pending_work():
        open_names = [p.first_name or p.key for p in state.active_patients() if p.status != BookingStatus.BOOKED.value]
        detail = f" Still open: {', '.join(open_names)}." if open_names else ""
        return GuardDecision(False, "pending work." + detail + " Tell the caller what is still open and ask if they want to finish it now. "
                             "If the caller says someone does not need a visit, call decline_patient for that person. "
                             "Do NOT transfer the call unless the caller asks for a person.")
    if not state.caller_goodbye:
        return GuardDecision(False, "caller has not said goodbye")
    if not state.agent_goodbye_said:
        return GuardDecision(False, "say goodbye to the caller first")
    return GuardDecision(True, "caller said goodbye, no pending work, agent said goodbye")


def silence_action(state, silent_for_s):
    """What to do after the caller has been silent for silent_for_s seconds.

    Returns one of: "wait", "reprompt", "handoff", "end".
    """
    if silent_for_s < SILENCE_REPROMPT_AFTER_S:
        return "wait"
    if state.silence_prompts_sent < MAX_REPROMPTS:
        return "reprompt"
    if silent_for_s < SILENCE_REPROMPT_AFTER_S * (MAX_REPROMPTS + 1) + SILENCE_FINAL_WAIT_S:
        return "wait"
    # Caller is gone. Pending work is never silently dropped.
    if state.has_pending_work():
        return "handoff"   # record a callback task for the clinic, then close politely
    return "end"


def silence_step(state, silent_for_s):
    """One tick of the silence timer. Reprompts come at 8s and 16s of silence, the close at 44s.

    The voice layer calls this every second while the caller is silent, so a caller who never speaks again
    is still reprompted twice and then closed. (The first version acted once, on the 'away' event only, and a
    silent caller was never closed.)
    """
    if state.silence_prompts_sent < MAX_REPROMPTS:
        if silent_for_s >= SILENCE_REPROMPT_AFTER_S * (state.silence_prompts_sent + 1):
            return "reprompt"
        return "wait"
    return silence_action(state, silent_for_s)
