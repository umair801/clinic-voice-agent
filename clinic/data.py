"""Synthetic clinic data. No real PHI anywhere in this repo.

All names, dates of birth, providers and locations are invented for testing.
"""
import random

FAMILY = {
    "parent": {"id": "pt_1001", "first": "Maria", "last": "Alvarez", "dob": "1982-03-14"},
    "child1": {"id": "pt_1002", "first": "Lucas", "last": "Alvarez", "dob": "2013-06-02"},
    "child2": {"id": "pt_1003", "first": "Sofia", "last": "Alvarez", "dob": "2016-09-21"},
}

SPECIALTIES = [
    "Family Medicine", "Pediatrics", "Internal Medicine", "Cardiology",
    "Dermatology", "Behavioral Health", "Women's Health", "Podiatry",
]
CITIES = ["Riverton", "Lakeview", "Oakdale", "Fairmont", "Brookside", "Hillcrest"]
LAST = ["Nguyen", "Patel", "Okafor", "Reyes", "Kim", "Haddad", "Silva", "Novak", "Moore", "Ito"]
FIRST = ["Ana", "Ben", "Chloe", "Dev", "Elena", "Femi", "Grace", "Hugo", "Iris", "Jon"]


def build_locations(n=12, seed=58):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        city = CITIES[i % len(CITIES)]
        out.append({
            "id": f"loc_{i + 1:02d}",
            "name": f"{city} Community Health Center {i // len(CITIES) + 1}",
            "city": city,
            "hours": "Mon-Fri 8:00-17:00" if rng.random() > 0.3 else "Mon-Sat 8:00-19:00",
            "languages": ["English", "Spanish"] if rng.random() > 0.4 else ["English"],
        })
    return out


def build_providers(n=400, seed=58):
    rng = random.Random(seed)
    locs = build_locations()
    out = []
    for i in range(n):
        out.append({
            "id": f"prov_{i + 1:03d}",
            "name": f"Dr. {rng.choice(FIRST)} {rng.choice(LAST)}",
            "specialty": rng.choice(SPECIALTIES),
            "location_id": rng.choice(locs)["id"],
            "accepts_new_patients": rng.random() > 0.25,
            "visit_types": rng.sample(
                ["new patient", "follow up", "well child", "sick visit", "annual physical", "telehealth"], 3),
        })
    return out


def build_giant_prompt(target_chars=160_000):
    """Reproduces failure 9: every provider and location document pasted into the prompt.

    Pads with synthetic provider and location text until the prompt reaches target_chars,
    mirroring system_prompt_chars: 160000 from the task config.
    """
    header = (
        "You are Ava, the inbound phone assistant for a community health center.\n"
        "Greet the caller, identify the patient, book appointments, then wrap up.\n"
        "Switch the whole call to Spanish if the caller's message is in any language other than English.\n"
        "REFERENCE DOCUMENTS FOLLOW.\n"
    )
    parts = [header]
    total = len(header)
    for loc in build_locations():
        line = f"LOCATION {loc['id']}: {loc['name']}, {loc['city']}, hours {loc['hours']}, languages {', '.join(loc['languages'])}\n"
        parts.append(line)
        total += len(line)
    providers = build_providers(4000)
    for p in providers:
        if total >= target_chars:
            break
        line = (f"PROVIDER {p['id']}: {p['name']}, {p['specialty']}, at {p['location_id']}, "
                f"new patients {'yes' if p['accepts_new_patients'] else 'no'}, "
                f"visit types {', '.join(p['visit_types'])}\n")
        parts.append(line)
        total += len(line)
    text = "".join(parts)
    return text[:target_chars]


def approx_tokens(text_or_len):
    """Rough token estimate: 4 characters per token. An estimate, not a tokenizer count."""
    n = text_or_len if isinstance(text_or_len, int) else len(text_or_len)
    return max(1, n // 4)
