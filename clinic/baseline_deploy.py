"""FAILURE 11: dashboard edits vanish on deploy.

Models the baseline deployment as described in the task: the live agent prompt is edited in
the dashboard, the config-as-code repo is two weeks behind, and a deploy from the repo
overwrites the live agent.
"""
import hashlib


def _h(s):
    return hashlib.sha256(s.encode()).hexdigest()[:12]


class BaselineDeployment:
    def __init__(self):
        # Repo copy, two weeks behind.
        self.repo = {"prompt": "You are Ava. Book appointments. [repo version, 2 weeks old]"}
        # Live agent, edited by hand in the dashboard after the repo was last updated.
        self.live = {"prompt": "You are Ava. Book appointments. Never read DOB back twice. [dashboard edit]"}

    def dashboard_edit(self, new_prompt):
        self.live["prompt"] = new_prompt   # no review, no version, no eval, nothing written to the repo

    def deploy_from_repo(self):
        before = _h(self.live["prompt"])
        self.live = dict(self.repo)        # blind overwrite
        return {"live_hash_before": before, "live_hash_after": _h(self.live["prompt"])}
