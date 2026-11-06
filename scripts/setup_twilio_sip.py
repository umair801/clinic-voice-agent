"""Create a Twilio Elastic SIP trunk that sends calls on your number to LiveKit SIP.
Run by Umair AFTER buying a Twilio number and filling LIVEKIT_SIP_HOST (looks like xxxx.sip.livekit.cloud) in .env.
  python scripts/setup_twilio_sip.py             (dry run)
  python scripts/setup_twilio_sip.py --apply

NOT TESTED against live Twilio. If a step fails, the error text is printed; do the same step in the Twilio console
(Elastic SIP Trunking > Trunks > Origination URI = sip:<LIVEKIT_SIP_HOST>;transport=tcp, then attach the number).
Notes: do NOT use TLS on the origination URI if you want SIP REFER human transfer. Enable 'Call Transfer (SIP REFER)'
on the trunk in the console if the API field below is not accepted.
"""
import os, sys
import httpx
from dotenv import load_dotenv

load_dotenv()
APPLY = "--apply" in sys.argv
sid, tok = os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")
num, host = os.getenv("TWILIO_PHONE_NUMBER"), os.getenv("LIVEKIT_SIP_HOST")
missing = [k for k, v in {"TWILIO_ACCOUNT_SID": sid, "TWILIO_AUTH_TOKEN": tok, "TWILIO_PHONE_NUMBER": num, "LIVEKIT_SIP_HOST": host}.items() if not v]
if missing:
    raise SystemExit(f"Missing in .env: {missing}")
print(f"Would create trunk 'clinic-ava', origination sip:{host};transport=tcp, and attach {num}.")
if not APPLY:
    raise SystemExit("Dry run only. Re-run with --apply.")

auth = (sid, tok)
with httpx.Client(auth=auth, timeout=20) as c:
    t = c.post("https://trunking.twilio.com/v1/Trunks", data={"FriendlyName": "clinic-ava", "TransferMode": "enable-all"})
    t.raise_for_status(); tsid = t.json()["sid"]; print("trunk", tsid)
    c.post(f"https://trunking.twilio.com/v1/Trunks/{tsid}/OriginationUrls",
           data={"FriendlyName": "livekit", "SipUrl": f"sip:{host};transport=tcp", "Priority": 1, "Weight": 1, "Enabled": "true"}).raise_for_status()
    nums = c.get(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers.json", params={"PhoneNumber": num}).json()["incoming_phone_numbers"]
    if not nums:
        raise SystemExit(f"{num} is not on this Twilio account")
    c.post(f"https://trunking.twilio.com/v1/Trunks/{tsid}/PhoneNumbers", data={"PhoneNumberSid": nums[0]["sid"]}).raise_for_status()
    print("number attached. Call it to test.")
