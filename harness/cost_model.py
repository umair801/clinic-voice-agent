"""MODELED cost per minute, new design vs baseline. LLM tokens only. NOT a measurement.

Inputs that are real: prompt text size in this repo, baseline numbers from results/baseline.json, list prices in config/pricing.json.
Inputs that are ASSUMPTIONS (change them and re-run): tool-schema tokens, average history tokens, small-model share.
Real per-turn tokens and cost come from call logs (ava.telemetry) after live test calls and replace this.
"""
import json, os
from ava.telemetry import llm_cost_usd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSUME = {"tool_schema_tokens": 900, "history_tokens_avg": 600, "step_instruction_tokens": 220,
          "completion_tokens_per_turn": 60, "small_share": 0.9}


def main():
    base = json.load(open(os.path.join(ROOT, "results", "baseline.json")))
    prompt_chars = len(open(os.path.join(ROOT, "prompts", "system.md")).read())
    turns_per_min = 13 / 7.87   # from the replayed call (real log timing)
    in_tok = prompt_chars // 4 + ASSUME["tool_schema_tokens"] + ASSUME["history_tokens_avg"] + ASSUME["step_instruction_tokens"]
    out_tok = ASSUME["completion_tokens_per_turn"]
    s = ASSUME["small_share"]
    per_turn = s * llm_cost_usd("small", in_tok, out_tok) + (1 - s) * llm_cost_usd("large", in_tok, out_tok)
    new_per_min = per_turn * turns_per_min
    # baseline: 40,000-token prompt per turn, no routing, assumed large model, no caching (as modeled in Phase 1)
    base_per_turn = llm_cost_usd("large", 40000 + ASSUME["history_tokens_avg"], out_tok)
    base_per_min = base_per_turn * turns_per_min
    out = {"label": "MODELED, not measured", "assumptions": ASSUME, "turns_per_min": round(turns_per_min, 2),
           "new_input_tokens_per_turn": in_tok, "new_llm_usd_per_min": round(new_per_min, 5),
           "baseline_llm_usd_per_min_same_method": round(base_per_min, 5),
           "modeled_reduction_x": round(base_per_min / new_per_min, 1),
           "scope": "LLM tokens only. STT, TTS, telephony are not included and usually dominate once the LLM bill is small."}
    json.dump(out, open(os.path.join(ROOT, "results", "cost_model.json"), "w"), indent=2)
    print(json.dumps(out, indent=2))
