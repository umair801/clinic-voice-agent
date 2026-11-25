"""Retention: remove stored call files older than N days. Dry run by default, so nothing is deleted unless asked."""
import os
import time


def purge(directory, days, apply=False, now=None, suffixes=(".jsonl", ".json", ".txt")):
    """Returns list of paths older than `days`. Deletes them only when apply=True. Never touches the audit log (other folder)."""
    now = now or time.time()
    cutoff = now - days * 86400
    hit = []
    if not os.path.isdir(directory):
        return hit
    for name in sorted(os.listdir(directory)):
        p = os.path.join(directory, name)
        if os.path.isfile(p) and name.endswith(suffixes) and os.path.getmtime(p) < cutoff:
            hit.append(p)
            if apply:
                os.remove(p)
    return hit
