"""Create the LiveKit inbound SIP trunk and the dispatch rule that sends each call to a fresh room with the 'clinic-ava' agent.
Run by Umair AFTER LIVEKIT_* and TWILIO_PHONE_NUMBER are in .env.
  python scripts/setup_livekit_sip.py            (dry run, prints what it would do)
  python scripts/setup_livekit_sip.py --apply
"""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
from livekit import api

load_dotenv()
APPLY = "--apply" in sys.argv
need = ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "TWILIO_PHONE_NUMBER"]
missing = [k for k in need if not os.getenv(k)]
if missing:
    raise SystemExit(f"Missing in .env: {missing}")
number = os.environ["TWILIO_PHONE_NUMBER"]
print(f"Would create inbound trunk for {number} and a dispatch rule (room prefix 'call-', agent 'clinic-ava').")
if not APPLY:
    raise SystemExit("Dry run only. Re-run with --apply.")

async def main():
    lk = api.LiveKitAPI(os.environ["LIVEKIT_URL"], os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
    trunk = await lk.sip.create_sip_inbound_trunk(api.CreateSIPInboundTrunkRequest(
        trunk=api.SIPInboundTrunkInfo(name="clinic-inbound", numbers=[number], krisp_enabled=True)))
    rule = await lk.sip.create_sip_dispatch_rule(api.CreateSIPDispatchRuleRequest(
        name="clinic-ava-rule", trunk_ids=[trunk.sip_trunk_id],
        rule=api.SIPDispatchRule(dispatch_rule_individual=api.SIPDispatchRuleIndividual(room_prefix="call-")),
        room_config=api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name="clinic-ava")])))
    print("trunk", trunk.sip_trunk_id, "rule", rule.sip_dispatch_rule_id)
    await lk.aclose()

asyncio.run(main())
