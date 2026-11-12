"""Model routing (failure 10). Small model for routine turns, large model only where it earns its cost.

Each decision returns (tier, reason) so the reason is logged per turn and the savings are auditable.
"""
import re

from ava.state import BookingStatus, Step

FRUSTRATION = re.compile(r"\b(this is ridiculous|frustrated|useless|human|real person|operator|representative|speak to (a|someone)|"
                         r"you('re| are) not (listening|understanding)|what the hell|stupid)\b", re.I)


# The caller asked for several people to be booked together, so more than one booking in a single turn is allowed.
MULTI_BOOKING_OK = re.compile(r"(\ball (three|of us|of them|four|the appointments)\b|\b(everyone|everybody|both|together|each of us|the three of us)\b|"
                              r"\b(todos|todas|los tres|las tres|juntos|juntas)\b)", re.I)
HUMAN_REQUEST = re.compile(r"(\b(real person|human|operator|representative)\b|(talk|speak|transfer me)\s+(to|with)\s+(a |an )?(person|someone|somebody|agent)|"
                           r"una persona|hablar con alguien|un humano)", re.I)


def route(state, last_user_text="", last_tool_error=False):
    if last_tool_error:
        return "large", "tool error on previous step"
    if state.transfer_requested:
        return "large", "transfer or escalation in progress"
    if FRUSTRATION.search(last_user_text or ""):
        return "large", "caller frustration or request for a person"
    if any(p.status in (BookingStatus.PENDING.value, BookingStatus.FAILED.value) for p in state.patients):
        return "large", "booking outcome needs careful wording (pending or failed)"
    # Multi-person collection used to go to the large model. Measured on real phone calls: the small model booked the same
    # family calls correctly (3 of 3, once each), while the large model added about 1s per turn and about 8 times the cost.
    return "small", "routine turn"
