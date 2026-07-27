"""version.py — the single place PlateNotate's version number lives.

`VERSION` (a plain text file next to this one) is the source of truth: bump it in the
same commit as the change, and the top bar, Settings and the About line all follow.
`git_state()` is what makes "your copy is behind" honest — it reads the checkout, so it
reports nothing at all when the app is running from a frozen bundle or a plain download.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent


def version() -> str:
    try:
        return (HERE / "VERSION").read_text().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def _git(*args, timeout=4):
    """Run a git command inside the app's own checkout. None if this isn't one."""
    try:
        r = subprocess.run(["git", "-C", str(HERE), *args],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def git_state(fetch: bool = False) -> dict:
    """{sha, branch, dirty, behind, ahead, remote} for the checkout — {} if there is none.

    `behind` counts commits on the upstream branch this copy hasn't got, i.e. exactly
    "an update is waiting". With fetch=False it uses whatever was last fetched, so it
    costs nothing and never blocks the UI on the network.
    """
    sha = _git("rev-parse", "--short", "HEAD")
    if sha is None:
        return {}
    if fetch:
        _git("fetch", "--quiet", timeout=20)
    out = {"sha": sha,
           "branch": _git("rev-parse", "--abbrev-ref", "HEAD") or "",
           "dirty": bool(_git("status", "--porcelain")),
           "remote": _git("remote", "get-url", "origin") or ""}
    counts = _git("rev-list", "--left-right", "--count", "HEAD...@{upstream}")
    if counts and len(counts.split()) == 2:
        ahead, behind = counts.split()
        out["ahead"], out["behind"] = int(ahead), int(behind)
    return out


RELEASES_API = "https://api.github.com/repos/Tiagocdt/platenotate/releases/latest"
RELEASES_PAGE = "https://github.com/Tiagocdt/platenotate/releases/latest"


def _as_tuple(v):
    """'1.2.10' → (1, 2, 10) so 1.2.10 sorts ABOVE 1.2.9 (string compare gets it wrong)."""
    out = []
    for part in str(v or "").lstrip("v").split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out or [0])


def latest_release(timeout=6) -> dict:
    """{version, url, newer} for the newest published release, or {} if it can't be
    reached. This is how the PACKAGED app knows an update exists — it has no git
    checkout to compare against, so without this its "check for updates" is a dead end.
    Only ever called on an explicit check: no telemetry, no background polling."""
    import json
    import urllib.request
    try:
        req = urllib.request.Request(RELEASES_API, headers={"Accept": "application/vnd.github+json",
                                                            "User-Agent": "PlateNotate"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            tag = (json.load(r) or {}).get("tag_name") or ""
    except Exception:                                   # noqa: BLE001 — offline is fine
        return {}
    if not tag:
        return {}
    return {"version": tag.lstrip("v"), "url": RELEASES_PAGE,
            "newer": _as_tuple(tag) > _as_tuple(version())}


def pull() -> dict:
    """Fast-forward the checkout to the upstream branch. {ok, msg} — never rewrites or
    merges: if the copy has local commits or edits, it says so and changes nothing."""
    st = git_state()
    if not st:
        return {"ok": False, "msg": "not a git checkout — update by downloading a new build"}
    if st.get("dirty"):
        return {"ok": False, "msg": "you have uncommitted changes here — commit or stash them first"}
    _git("fetch", "--quiet", timeout=30)
    before = _git("rev-parse", "HEAD")
    out = _git("merge", "--ff-only", "@{upstream}", timeout=30)
    if out is None:
        return {"ok": False, "msg": "cannot fast-forward (local commits diverge from the remote)"}
    after = _git("rev-parse", "HEAD")
    if before == after:
        return {"ok": True, "msg": f"already up to date (v{version()})"}
    return {"ok": True, "msg": f"updated to v{version()} — restart PlateNotate to load it",
            "restart": True}


if __name__ == "__main__":
    print(version())
