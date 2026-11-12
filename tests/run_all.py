"""Run everything offline. Usage: python -c "from tests.run_all import main; main()" """
import subprocess, sys

STEPS = [
    ("baseline harness", "from harness.run import main; main()"),
    ("baseline controls", "from harness.controls import main; main()"),
    ("orchestration evals", "import sys; from evals.run import main; sys.exit(0 if main() else 1)"),
    ("modeled cost", "from harness.cost_model import main; main()"),
    ("voice adapter (needs livekit)", "from tests.voice_adapter import main; main()"),
    ("railway log import", "from tests.log_import import main; main()"),
    ("web test line guard", "from tests.web_call import main; main()"),
    ("callback reference", "from tests.callback_ref import main; main()"),
    ("thinking field per model", "from tests.thinking_arg import main; main()"),
]

def main():
    bad = 0
    for name, code in STEPS:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        print(f"{'OK  ' if r.returncode == 0 else 'FAIL'} {name}")
        if r.returncode:
            bad += 1
            print(r.stdout[-1500:], r.stderr[-1500:])
    print("ALL OK" if not bad else f"{bad} step(s) failed")
    return bad == 0
