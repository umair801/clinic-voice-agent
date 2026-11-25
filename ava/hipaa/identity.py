"""Caller identity check: first name + last name + date of birth, one extra detail when more than one person matches.

Rules: never say which field was wrong, stop after max_attempts, a caller who is not the patient gets no health details."""
import re
from dataclasses import dataclass, field

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"])}


def normalize_dob(text):
    """Accepts 1948-04-18, 04/18/1948, 4-18-48 (two-digit year: 00-29 is 20xx, else 19xx), 'April 18 1948', '18 April 1948'.
    Returns ISO date or None. US order (month first) for numeric forms."""
    t = (text or "").strip().lower().replace(",", " ")
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", t)
    y = m = d = None
    g = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", t)
    if g:
        y, m, d = map(int, g.groups())
    else:
        g = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})", t)
        if g:
            m, d, y = map(int, g.groups())
        else:
            g = re.fullmatch(r"([a-z]+)\s+(\d{1,2})\s+(\d{2}|\d{4})", t)
            if g and g.group(1) in MONTHS:
                m, d, y = MONTHS[g.group(1)], int(g.group(2)), int(g.group(3))
            else:
                g = re.fullmatch(r"(\d{1,2})\s+([a-z]+)\s+(\d{2}|\d{4})", t)
                if g and g.group(2) in MONTHS:
                    d, m, y = int(g.group(1)), MONTHS[g.group(2)], int(g.group(3))
    if y is None:
        return None
    if y < 100:
        y += 2000 if y <= 29 else 1900
    days = [31, 29 if (y % 4 == 0 and (y % 100 or y % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    if not (1 <= m <= 12 and 1 <= d <= days[m - 1] and 1900 <= y <= 2100):
        return None
    return f"{y:04d}-{m:02d}-{d:02d}"


@dataclass
class IdentityResult:
    status: str                      # verified | need_more | failed | locked | not_found
    patient: object = None
    message: str = ""                # safe to say to the caller, never reveals which field was wrong


@dataclass
class IdentityGate:
    ehr: object
    max_attempts: int = 3
    attempts: int = 0
    patient: object = None
    on_behalf: bool = False          # caller says they are NOT the patient
    candidates: list = field(default_factory=list)

    @property
    def locked(self):
        return self.attempts >= self.max_attempts and self.patient is None

    def may_share_phi(self):
        return self.patient is not None and not self.on_behalf

    async def verify(self, first_name, last_name, dob_text):
        if self.locked:
            return IdentityResult("locked", message="I can't verify you on this call. Let me connect you with a staff member.")
        dob = normalize_dob(dob_text)
        self.attempts += 1
        generic = "I couldn't match those details."
        if not (first_name or "").strip() or not (last_name or "").strip() or not dob:
            return self._miss(generic)
        found = await self.ehr.find_patients(last_name.strip(), first_name.strip(), dob)
        if len(found) == 1:
            self.patient = found[0]
            return IdentityResult("verified", found[0], "Thank you, you're verified.")
        if len(found) > 1:
            self.candidates = found
            self.attempts -= 1       # asking for one more detail is not a failed try
            return IdentityResult("need_more", message="I need one more detail to be sure. Can you tell me your ZIP code?")
        return self._miss(generic)

    async def add_detail(self, zip_code):
        z = "".join(c for c in str(zip_code) if c.isdigit())[:5]
        hits = [c for c in self.candidates if c.zip and c.zip[:5] == z] if z else []
        self.attempts += 1
        if len(hits) == 1:
            self.patient = hits[0]
            return IdentityResult("verified", hits[0], "Thank you, you're verified.")
        return self._miss("I couldn't match those details.")

    def _miss(self, msg):
        if self.attempts >= self.max_attempts:
            return IdentityResult("locked", message="I can't verify you on this call. Let me connect you with a staff member.")
        return IdentityResult("failed", message=msg + " Let's try once more.")

    def third_party(self):
        """Caller is not the patient: allow only office hours, location, general questions. No records, no bookings by name."""
        self.on_behalf = True
