"""The bad call, replayed as a script.

Every entry carries a source tag:
  "log"     = the timestamp comes from the Linear Health task PDF.
  "harness" = we added it, because the log is silent there. These make the replay runnable
              but they are our assumptions, not facts from the task.
"""

SOURCE_NOTES = {
    "log": "taken from the task's call log",
    "harness": "added by the harness; the log gives no timing here",
}

BAD_CALL = [
    # 00:00 Ava greets the caller
    {"t": 0.0, "kind": "agent_greet", "text": "Thanks for calling the clinic, this is Ava.", "dur": 3.0, "src": "log"},
    # 00:05 Caller asks to book for self and two kids; 00:07 identify_patient called
    {"t": 5.0, "kind": "caller", "text": "Hi, I need to book for me and my two kids", "dur": 2.0, "src": "log"},
    {"t": 7.0, "kind": "tool", "name": "identify_patient", "src": "log"},          # returns after 7.8s => 14.8
    # 00:13 Caller: "Hello? Are you there?"
    {"t": 13.0, "kind": "caller", "text": "Hello? Are you there?", "dur": 1.5, "src": "log"},
    # 00:16 Ava: "Are you still there?"
    {"t": 16.0, "kind": "agent_say", "text": "Are you still there?", "dur": 1.5, "src": "log"},
    # 00:30 Ava asks for date of birth
    {"t": 30.0, "kind": "agent_say", "text": "What is the date of birth?", "dur": 2.0, "src": "log"},
    # 00:33 Caller hears two half-answers stitched together.
    # Reply times are harness assumptions; the overlap itself is what the log describes.
    {"t": 31.5, "kind": "caller_partial", "text": "Fourteenth of March",
     "reply_at": 32.0, "reply": "Thank you, let me check that date", "reply_dur": 3.0, "src": "harness"},
    {"t": 32.6, "kind": "caller_final", "text": "Fourteenth of March, eighty-two",
     "reply_at": 33.0, "reply": "Got it, one moment while I confirm", "reply_dur": 2.5, "src": "harness"},
    # 00:41 Ava reads DOB back. Caller: "Sí, correct"
    {"t": 38.5, "kind": "agent_say", "text": "Is March fourteenth, nineteen eighty-two correct?", "dur": 2.5, "src": "log"},
    {"t": 41.0, "kind": "caller", "text": "Sí, correct", "dur": 1.2, "src": "log"},
    # 00:43 Ava switches the rest of the call to Spanish
    {"t": 43.0, "kind": "agent_say", "text": "Perfecto, continuamos en español.", "dur": 2.0, "src": "log"},
    # 01:20 Caller asks for English. Ava stays in Spanish
    {"t": 80.0, "kind": "caller", "text": "English, please", "dur": 1.2, "src": "log",
     "reply": "Gracias, continuamos en español", "reply_at": 81.5, "reply_dur": 2.0},
    # 02:05 book_appointment returns timeout. Start time is a harness assumption (log gives only the return).
    {"t": 95.0, "kind": "tool", "name": "book_appointment", "who": "parent", "slot": "2026-11-12T09:00",
     "times_out": True, "latency": 30.0, "src": "harness"},
    # 02:40 "Is there anything else?" Caller: "No, wait, my son too"
    {"t": 160.0, "kind": "agent_say", "text": "Is there anything else?", "dur": 1.5, "src": "log"},
    {"t": 162.0, "kind": "caller", "text": "No, wait, my son too", "dur": 2.0, "src": "log"},
    # The log is silent from 02:42 to 07:50, so the harness elides that window and adds a
    # minimal exchange on each side of it. Harness assumption: Ava starts the second booking
    # around 410s and that tool hangs, so the 60s silence timer is what ends the call near 07:50.
    {"t": 165.0, "kind": "agent_say", "text": "Of course. What is his name and date of birth?", "dur": 3.0, "src": "harness"},
    {"t": 168.0, "kind": "elided", "until": 404.0, "src": "harness"},
    {"t": 404.0, "kind": "caller", "text": "His name is Lucas, born June second twenty thirteen", "dur": 3.0, "src": "harness"},
    {"t": 408.0, "kind": "agent_say", "text": "Booking Lucas now.", "dur": 2.0, "src": "harness"},
    {"t": 410.0, "kind": "tool", "name": "book_appointment", "who": "child1", "slot": "2026-11-12T09:30",
     "hangs": True, "hang_s": 90.0, "src": "harness"},
    {"t": 410.0, "kind": "silence_timer_check", "src": "harness"},
]

# Separate mini-scenario for failure 8 (fallback model). The log gives no timestamp for it.
FALLBACK_PROBE = {"t": 100.0, "kind": "fallback_probe", "resume_after": "identify", "src": "harness"}
