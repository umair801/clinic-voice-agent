"""Run the baseline against all 11 repro checks and write results/.

Run from the repo root:
    python -c "from harness.run import main; main()"
"""
import json
import os
import sys
from dataclasses import asdict

sys.path.insert(0, os.getcwd())

from clinic.baseline_ava import BaselineAva
from clinic.baseline_deploy import BaselineDeployment
from clinic.data import approx_tokens
from harness import checks
from harness.scenario import BAD_CALL, FALLBACK_PROBE

OUT = "results"


def run_baseline():
    with open("config/pricing.json") as f:
        pricing = json.load(f)

    ava = BaselineAva()
    tr = ava.run(BAD_CALL)
    fb = BaselineAva().run([FALLBACK_PROBE])
    results = [
        checks.check_1_silent_tool(tr),
        checks.check_2_hello(tr),
        checks.check_3_overlap(tr),
        checks.check_4_language(tr),
        checks.check_5_false_booked(tr),
        checks.check_6_multi_patient(tr),
        checks.check_7_end_call(tr),
        checks.check_8_fallback(fb),
        checks.check_9_prompt(tr, len(ava.prompt), approx_tokens(ava.prompt)),
        checks.check_10_cost(tr, pricing),
        checks.check_11_deploy(BaselineDeployment()),
    ]
    return tr, results


def label(r):
    return "FAIL" if r.passed is False else ("PASS" if r.passed else "RECORDED")


def write_outputs(tr, results):
    os.makedirs(OUT, exist_ok=True)
    payload = {
        "label": "BASELINE. Deterministic simulation of the task's bad call. Not a real phone call.",
        "call_duration_s_in_replay": round(tr.duration_s, 1),
        "results": [dict(asdict(r), status=label(r)) for r in results],
    }
    with open(f"{OUT}/baseline.json", "w") as f:
        json.dump(payload, f, indent=2, default=str)
    with open(f"{OUT}/baseline_trace.json", "w") as f:
        json.dump(sorted(tr.events, key=lambda e: e.get("t", 0)), f, indent=2, default=str)

    lines = [
        "# Baseline results (Phase 1)",
        "",
        "Label: simulation of the task's bad call. These are NOT real phone calls and NOT production results.",
        "Timestamps tagged `log` in harness/scenario.py come from the task PDF; `harness` ones are our assumptions.",
        "",
        "| # | Failure | Status | Key measurement | Cause lives in |",
        "|---|---------|--------|-----------------|----------------|",
    ]
    for r in results:
        m = json.dumps(r.metric, default=str)
        if len(m) > 150:
            m = m[:147] + "..."
        lines.append(f"| {r.id} | {r.title} | {label(r)} | `{m}` | {r.where_it_lives} |")
    lines += ["", "## Evidence per failure", ""]
    for r in results:
        lines += [f"### {r.id}. {r.title}", f"Status: {label(r)}", "", r.evidence, "", f"Data source: {r.data_source}", ""]
    with open(f"{OUT}/BASELINE.md", "w") as f:
        f.write("\n".join(lines))


def main():
    tr, results = run_baseline()
    write_outputs(tr, results)
    print(f"{'#':>2}  {'STATUS':8} FAILURE")
    for r in results:
        print(f"{r.id:>2}  {label(r):8} {r.title}")
    failed = sum(1 for r in results if r.passed is False)
    print(f"\nbaseline reproduces {failed} of 10 pass/fail failures (check 10 is a recorded baseline). Wrote results/")
    return results


if __name__ == "__main__":
    main()
