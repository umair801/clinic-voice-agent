"""Small prompt plus retrieval for provider and location data. Fixes failure 9.

The baseline pasted every provider and location document into a 160,000 character prompt on every
turn. Here the prompt carries only rules, and provider and location facts are fetched on demand by
a tool, returning a few short lines. Keyword retrieval, stdlib only, no embeddings, no network.
"""
import re
from clinic.data import build_locations, build_providers

SYNONYMS = {
    "kid": "pediatrics", "kids": "pediatrics", "child": "pediatrics", "children": "pediatrics", "son": "pediatrics",
    "daughter": "pediatrics", "baby": "pediatrics", "heart": "cardiology", "skin": "dermatology", "rash": "dermatology",
    "foot": "podiatry", "feet": "podiatry", "mental": "behavioral", "anxiety": "behavioral", "therapy": "behavioral",
    "pregnant": "women's", "checkup": "annual", "physical": "annual", "family": "family", "doctor": "family",
}


def _toks(text):
    out = []
    for t in re.findall(r"[a-zA-Z']+", text.lower()):
        out.append(t)
        if t in SYNONYMS:
            out.extend(SYNONYMS[t].split())
    return out


class KnowledgeBase:
    def __init__(self, providers=None, locations=None):
        self.locations = {l["id"]: l for l in (locations or build_locations())}
        self.providers = providers or build_providers(400)
        self._docs = []
        for p in self.providers:
            loc = self.locations.get(p["location_id"], {})
            spec = set(_toks(p["specialty"]))
            place = set(_toks(f"{loc.get('city', '')} {loc.get('name', '')}"))
            other = set(_toks(f"{p['name']} {' '.join(p['visit_types'])} "
                              f"{'new patients' if p['accepts_new_patients'] else ''}"))
            self._docs.append((p, spec, place, other))

    def find_providers(self, query, k=3, city=None, new_patients=None):
        q = set(_toks(query))
        scored = []
        for p, spec, place, other in self._docs:
            loc = self.locations.get(p["location_id"], {})
            if city and loc.get("city", "").lower() != city.lower():
                continue
            if new_patients and not p["accepts_new_patients"]:
                continue
            # specialty match matters most, then place, then everything else
            score = 3 * len(q & spec) + 2 * len(q & place) + len(q & other)
            if score:
                scored.append((score, p["id"], p))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [p for _, _, p in scored[:k]]

    def format(self, providers):
        """Short lines for the model. This is the ONLY provider data that enters the prompt."""
        lines = []
        for p in providers:
            loc = self.locations.get(p["location_id"], {})
            lines.append(f"{p['id']}: {p['name']}, {p['specialty']}, {loc.get('name', '?')} ({loc.get('hours', '?')}), "
                         f"{'accepting new patients' if p['accepts_new_patients'] else 'not accepting new patients'}")
        return "\n".join(lines) if lines else "No matching provider found."

    def location(self, loc_id):
        return self.locations.get(loc_id)
