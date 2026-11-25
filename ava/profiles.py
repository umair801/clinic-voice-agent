"""Agent profiles: one engine, many agents.

Every agent (clinic front desk, medical practice receptionist, ...) is a PROFILE: a config file, a prompt file and a
releases folder, all in the repo. The voice worker, the web call endpoint, the release tool and the evals all take an
agent_id and resolve their paths here, so adding an agent means adding a registry entry and files, not new code paths.

Layout (agents/registry.json):
  {"default": "clinic", "agents": {"<id>": {"title", "status", "config", "releases"}}}
  "clinic" keeps its original paths (config/agent.json, releases/) so the live phone line is untouched.
  Every other agent lives in agents/<id>/ (agent.json, system.md, releases/).

Not a LiveKit agent name: one worker registers ONE LiveKit agent name (WORKER_NAME) and reads the agent_id from the
dispatch metadata. Renaming WORKER_NAME would also require re-creating the SIP dispatch rule for the phone number.
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(ROOT, "agents", "registry.json")
WORKER_NAME = "clinic-ava"


class UnknownAgent(ValueError):
    """The agent_id is not in the registry. Callers must reject it, never fall back silently for an explicit id."""


def _load(root=None):
    path = os.path.join(root, "agents", "registry.json") if root else REGISTRY
    with open(path) as f:
        return json.load(f)


def default_agent_id(root=None):
    return _load(root)["default"]


def normalize(agent_id, root=None):
    """None or empty means the default agent. Anything else must be registered."""
    reg = _load(root)
    if agent_id is None or str(agent_id).strip() == "":
        return reg["default"]
    agent_id = str(agent_id).strip()
    if agent_id not in reg["agents"]:
        raise UnknownAgent(f"unknown agent_id {agent_id!r}")
    return agent_id


def get(agent_id=None, root=None):
    """Resolved profile: ids and absolute paths."""
    base = root or ROOT
    reg = _load(root)
    aid = normalize(agent_id, root)
    entry = reg["agents"][aid]
    rel_dir = os.path.normpath(os.path.join(base, entry["releases"]))
    return {"id": aid, "title": entry["title"], "kind": entry.get("kind", "clinic"), "evals": entry.get("evals", "evals.run"), "status": entry.get("status", "draft"),
            "config_path": os.path.normpath(os.path.join(base, entry["config"])),
            "releases_dir": rel_dir, "pointer_path": os.path.join(rel_dir, "POINTER.json"), "root": base}


def list_agents(root=None, public=True):
    """Agents for the hub page. public=True hides drafts."""
    reg = _load(root)
    out = []
    for aid, e in reg["agents"].items():
        if public and e.get("status", "draft") != "live":
            continue
        out.append({"id": aid, "title": e["title"], "status": e.get("status", "draft")})
    return out


def agent_id_from_metadata(raw):
    """LiveKit job metadata is a string. Return the agent_id in it, or None when absent or not JSON."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data.get("agent_id") if isinstance(data, dict) else None


def metadata_field(raw, name):
    """Any string field from the dispatch metadata (None when absent or not JSON). Used for backend-signed options such as ehr_mode."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    v = data.get(name) if isinstance(data, dict) else None
    return v if isinstance(v, str) else None
