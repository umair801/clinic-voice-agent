"""Voice-side checks for the receptionist agent (needs livekit-agents). Run: python -c "from tests.receptionist_voice import main; main()"

Proves: the two agents expose exactly their own tools, the receptionist's streaming guard replaces a false "booked" before the
caller hears it, and a refactor of the shared base did not change the clinic agent's tool set."""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CLINIC_TOOLS = {"set_party_size", "identify_patient", "decline_patient", "find_slots", "choose_slot", "find_provider",
                "book_appointment", "read_back_summary", "request_human_transfer", "end_call"}
RECEPTIONIST_TOOLS = {"verify_identity", "add_verification_detail", "caller_is_not_the_patient", "list_my_appointments",
                      "find_open_slots", "book_visit", "cancel_visit", "reschedule_visit", "take_message", "practice_info",
                      "request_human_transfer", "end_call"}


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f": {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(1)


def main():
    from livekit.agents import Agent, llm
    from ava.ehr.mock import MockEHR
    from ava.hipaa.audit import AuditLog
    from ava.receptionist.core import MessageStore, ReceptionistCore
    from ava.telemetry import CallLogger
    from ava.voice.agent import AvaAgent
    from ava.voice.receptionist_agent import ReceptionistAgent

    def names(cls):
        ag = cls.__new__(cls)
        return {t.info.name for t in llm.find_function_tools(ag)}

    check("clinic agent tool set unchanged by the refactor", names(AvaAgent) == CLINIC_TOOLS, str(names(AvaAgent) ^ CLINIC_TOOLS))
    check("receptionist has its own tools only", names(ReceptionistAgent) == RECEPTIONIST_TOOLS, str(names(ReceptionistAgent) ^ RECEPTIONIST_TOOLS))

    async def run():
        d = tempfile.mkdtemp()
        core = ReceptionistCore("t", MockEHR(), audit=AuditLog(os.path.join(d, "a.jsonl")), logger=CallLogger("t", log_dir=d, alert_webhook=""),
                                messages=MessageStore(os.path.join(d, "m")), log_dir=d)
        core.mark_greeted()

        def chunk(text):
            return llm.ChatChunk(id="x", delta=llm.ChoiceDelta(role="assistant", content=text))

        async def fake(agent, chat_ctx, tools, model_settings):
            for t in ["You're all boo", "ked for December first. ", "Anything else?"]:
                yield chunk(t)

        original = Agent.default.llm_node
        Agent.default.llm_node = staticmethod(fake)
        try:
            ag = ReceptionistAgent.__new__(ReceptionistAgent)
            ag.core = core
            ag._reset_reply()
            out = ""
            async for c in ag.llm_node(None, [], None):
                if isinstance(c, str):
                    out += c
        finally:
            Agent.default.llm_node = original
        return out

    out = asyncio.run(run())
    check("false 'booked' is replaced before the caller hears it", "booked" not in out.lower() and "confirm" in out.lower(), out)
    check("honest text around it is kept", "Anything else" in out, out)
    print("receptionist voice OK")


if __name__ == "__main__":
    main()
