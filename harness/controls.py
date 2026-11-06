"""Control cases: prove each check CAN pass.

A check that always fails proves nothing. Each control changes exactly one thing in the
baseline (a config value or one scripted event) and the matching check must flip to PASS.
These are not fixes. The real fixes come in Phases 2 to 6.

    python -c "from harness.controls import main; main()"
"""
import os
import sys

sys.path.insert(0, os.getcwd())

from clinic.baseline_ava import BaselineAva
from harness import checks
from harness.scenario import BAD_CALL


def _script_with(**edits):
    script = [dict(e) for e in BAD_CALL]
    for e in script:
        for match, change in edits.items():
            key, val = match.split("=", 1)
            if str(e.get(key)) == val:
                e.update(change)
    return script


def run_controls():
    out = []

    a = BaselineAva(); a.cfg["soft_timeout_s"] = 1
    r = checks.check_1_silent_tool(a.run(BAD_CALL))
    out.append(("1 filler after 1s instead of 8s", r.passed, r.metric["silence_s"]))

    b = BaselineAva(); b.cfg["speculative_turn"] = False
    r = checks.check_3_overlap(b.run(BAD_CALL))
    out.append(("3 speculative_turn off", r.passed, r.metric["overlap_total_s"]))

    script = _script_with(**{"text=Sí, correct": {"text": "Yes, correct"}})
    r = checks.check_4_language(BaselineAva().run(script))
    out.append(("4 caller says 'Yes' instead of 'Sí'", r.passed, r.metric["final_language"]))

    d = BaselineAva(); d.cfg["silence_end_call_s"] = 10 ** 6
    r = checks.check_7_end_call(d.run(BAD_CALL))
    out.append(("7 silence timer disabled", r.passed, r.metric["violations"]))

    script = _script_with(**{"who=parent": {"times_out": False}})
    r = checks.check_5_false_booked(BaselineAva().run(script))
    out.append(("5 first booking does not time out", r.passed, r.metric["false_claims"]))
    return out


def main():
    ok = True
    for name, passed, value in run_controls():
        print(f"{'PASS' if passed else 'FAIL'}  control: {name} (value={value})")
        ok = ok and bool(passed)
    print("\nall controls flipped to PASS" if ok else "\nA CONTROL DID NOT FLIP: the matching check is suspect")
    return ok


if __name__ == "__main__":
    main()
