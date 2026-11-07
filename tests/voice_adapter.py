"""Checks the voice worker's llm_node adapter around the output guard, with a fake model stream.
Needs livekit-agents installed (it is in requirements.txt). Run: python -c "from tests.voice_adapter import main; main()"
"""
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from livekit.agents import Agent, llm
    from ava.backend.booking import BookingService
    from ava.backend.client import InProcessTransport
    from ava.backend.store import SQLiteStore
    from ava.core import AvaCore
    from ava.telemetry import CallLogger
    from ava.voice.agent import AvaAgent

    def chunk(text=None, tool=False):
        if tool:
            tc = llm.FunctionToolCall(name="book_appointment", arguments="{}", call_id="c1")
            return llm.ChatChunk(id="x", delta=llm.ChoiceDelta(role="assistant", tool_calls=[tc]))
        return llm.ChatChunk(id="x", delta=llm.ChoiceDelta(role="assistant", content=text))

    async def run():
        d = tempfile.mkdtemp()
        core = AvaCore("t", InProcessTransport(BookingService(SQLiteStore())), logger=CallLogger("t", log_dir=d, alert_webhook=""), book_pause=0)
        core.mark_greeted()
        await core.identify_patient("self", "Maria", "1982-03-14")
        slot = (await core.find_slots("pediatrics", 3))["slots"][0]["slot_id"]
        await core.choose_slot("self", slot)

        async def fake(agent, chat_ctx, tools, model_settings):
            for t in ["Maria, you", "'re set for Nov", "ember 12. Lucas can have the next ", "opening. Does that work?"]:
                yield chunk(t)
            yield chunk(tool=True)

        original = Agent.default.llm_node
        Agent.default.llm_node = staticmethod(fake)
        try:
            ag = AvaAgent.__new__(AvaAgent)
            ag.core = core
            ag._reset_reply()
            out, kinds = "", []
            async for c in ag.llm_node(None, [], None):
                kinds.append("str" if isinstance(c, str) else "tool" if getattr(c.delta, "tool_calls", None) else "chunk")
                if isinstance(c, str):
                    out += c
        finally:
            Agent.default.llm_node = original
        return out, kinds, core

    def filler_checks():
        """Filler: spoken once, before the first tool call, only if the model said nothing, never for end_call."""
        def tool_chunk(*names):
            tcs = [llm.FunctionToolCall(name=n, arguments="{}", call_id=f"c{i}") for i, n in enumerate(names)]
            return llm.ChatChunk(id="x", delta=llm.ChoiceDelta(role="assistant", tool_calls=tcs))

        async def drive(chunks, hops=1):
            d = tempfile.mkdtemp()
            core = AvaCore("t2", InProcessTransport(BookingService(SQLiteStore())), logger=CallLogger("t2", log_dir=d, alert_webhook=""), book_pause=0)
            core.mark_greeted()

            async def fake(agent, chat_ctx, tools, model_settings):
                for c in chunks:
                    yield c

            original = Agent.default.llm_node
            Agent.default.llm_node = staticmethod(fake)
            try:
                ag = AvaAgent.__new__(AvaAgent)
                ag.core = core
                ag._reset_reply()
                out = []
                for _ in range(hops):                       # several model calls inside one reply
                    async for c in ag.llm_node(None, [], None):
                        out.append(c if isinstance(c, str) else "<tool>")
                return out, ag
            finally:
                Agent.default.llm_node = original

        results = []
        out, ag = asyncio.run(drive([tool_chunk("identify_patient")]))
        results.append(("filler before first lookup tool", out[0] == "One moment while I look that up. " and out[-1] == "<tool>" and ag._filler_played))
        out, ag = asyncio.run(drive([chunk("Let me check."), tool_chunk("identify_patient")]))
        results.append(("no filler when the model already spoke", [o for o in out if o != "<tool>"] == ["Let me check."] and not ag._filler_played))
        out, ag = asyncio.run(drive([tool_chunk("end_call")]))
        results.append(("no filler for end_call", out == ["<tool>"] and not ag._filler_played))
        out, ag = asyncio.run(drive([tool_chunk("identify_patient")], hops=3))
        results.append(("filler only once across hops", sum(1 for o in out if o.startswith("One moment")) == 1))
        out, ag = asyncio.run(drive([tool_chunk("book_appointment")]))
        results.append(("book filler wording", out[0] == "One moment while I book that. "))
        out, ag = asyncio.run(drive([chunk("Thanks, goodbye."), tool_chunk("end_call")]))
        results.append(("model goodbye is detected (no second goodbye)", bool(__import__("ava.voice.agent", fromlist=["GOODBYE_WORDS"]).GOODBYE_WORDS.search(ag._reply_text))))
        out, ag = asyncio.run(drive([tool_chunk("find_slots")]))
        results.append(("lookup filler is not detected as goodbye", not __import__("ava.voice.agent", fromlist=["GOODBYE_WORDS"]).GOODBYE_WORDS.search(ag._reply_text)))
        return results

    fres = filler_checks()
    for name, good in fres:
        print(("PASS" if good else "FAIL"), "filler:", name)
    fok = all(g for _, g in fres)
    out, kinds, core = asyncio.run(run())
    ok = ("you're set" not in out and "One moment while I confirm" in out and "Lucas can have the next opening." in out
          and out.endswith("work?") and kinds[-1] == "tool")
    blocked = [e for e in __import__("ava.telemetry", fromlist=["read_call"]).read_call(core.log.path) if e.get("event") == "claim_blocked"]
    print(("PASS" if ok and len(blocked) == 1 else "FAIL"), "llm_node blocks a false claim, keeps the rest, passes tool calls:", repr(out), kinds)
    sys.exit(0 if ok and len(blocked) == 1 and fok else 1)


if __name__ == "__main__":
    main()
