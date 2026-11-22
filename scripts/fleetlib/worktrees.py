"""Worktrees: the git/wt policy behind `fleet new` and `fleet rm`.

The one place that knows the repo layout rule (a repo is a directory whose `main/` holds the main
checkout; worker worktrees sit beside it, where worktrunk's template puts them), finds or creates the
worktree for a branch, and removes a worktree only when that loses nothing.

Dependencies: a git runner (system.Git: run/ok/out) and the wt adapter (system.Wt). Progress goes
to `say`, warnings to `warn` (callables taking lines as arguments, like Ctx.say).
"""

import json
import os

from .errors import FleetError


def main_checkout(repo):
    """The main checkout of a repo that follows the layout rule."""
    return os.path.join(repo, "main")


class Worktrees:
    def __init__(self, git, wt, say, warn):
        self.git = git
        self.wt = wt
        self.say = say
        self.warn = warn

    def locate(self, src):
        """The repo of `src`, which is <repo> or <repo>/main."""
        if os.path.basename(src) == "main" and os.path.exists(os.path.join(src, ".git")):
            return os.path.dirname(src)
        if os.path.exists(os.path.join(main_checkout(src), ".git")):
            return src
        raise FleetError("%s does not follow <repo>/main (no git checkout at <repo>/main). "
                         "Pass --no-worktree to run there without a worktree." % src)

    def ensure(self, src, branch, base=None):
        """Find or make the worktree for `branch` of the repo at `src` (<repo> or <repo>/main), from
        `base` (default: origin's default branch) when the branch is new. Returns (dir, repo)."""
        if not self.git.ok(".", "check-ref-format", "--branch", branch):
            raise FleetError("'%s' is not a valid branch name" % branch)
        repo = self.locate(src)
        main = main_checkout(repo)
        # Worktrees go through worktrunk (wt): its worktree-path template decides where they live,
        # and its hooks (e.g. a post-start build) make them usable before claude starts.
        if not self.wt.available():
            raise FleetError("wt (worktrunk) not found; install it, or pass --no-worktree")
        existing = _wt_path_of(self.wt.list_json(main), branch)
        if existing:
            self.say("reusing worktree %s" % existing)
            return existing, repo
        if not self.git.ok(main, "fetch", "--quiet", "origin"):
            self.warn("warning: git fetch failed; using local refs")
        create, how = False, ""
        if self._has_ref(main, "refs/heads/" + branch):
            how = "from existing branch %s" % branch
        elif not base and self._has_ref(main, "refs/remotes/origin/" + branch):
            how = "tracking origin/%s" % branch  # wt switch creates the local tracking branch
        else:
            if not base:
                base = self._default_base(main)
            create, how = True, "on new branch %s from %s" % (branch, base)
        # --no-hooks, then run them here: wt switch would start post-start hooks in the background,
        # and claude must not start (and spawn its MCP servers) before they finish.
        rc, out = self.wt.switch(main, branch, create=create, base=base)
        if rc != 0:
            raise FleetError("wt switch failed for %s" % branch)
        d = _switch_path(out)
        if not d or not os.path.isdir(d):
            raise FleetError("wt switch did not report the new worktree's path: %s" % out.strip())
        self.say("worktree %s %s" % (d, how))
        for h in ("pre-start", "post-start", "post-switch"):
            self.say("running wt %s hooks..." % h)
            if not self.wt.hook(d, h):
                self.warn("warning: wt %s hooks failed in %s" % (h, d))
        return d, repo

    def check_removable(self, worktree):
        """Raise, after listing the changes, if removing `worktree` would lose uncommitted work. A
        worktree that is already gone is fine to remove."""
        if not os.path.isdir(worktree):
            return
        rc, porcelain = self.git.run(worktree, "status", "--porcelain")
        if rc != 0:
            raise FleetError("cannot read git status of %s; not removing it (--force to discard anyway)" % worktree)
        if porcelain.strip():
            _, short = self.git.run(worktree, "status", "--short")
            self.say(*short.splitlines()[:20])
            raise FleetError("uncommitted changes in %s (above). Commit them, or re-run with --force to "
                             "discard them." % worktree)

    def remove(self, repo, worktree, force=False, branch="merged"):
        """Remove `worktree` of `repo`; `force` discards its changes. `branch` says what happens to
        its branch: "merged" deletes it (and origin's) when worktrunk sees it merged into the default
        branch, squash and rebase merges included; "keep" keeps it; "delete" deletes it locally even
        unmerged, keeping origin's (deleting that could close an open PR).
        Returns the lines reporting the branch's fate, or None when the worktree was already gone."""
        if not os.path.isdir(worktree):
            return None
        main = main_checkout(repo)
        if branch == "merged" and not self.git.ok(main, "fetch", "--quiet", "origin"):
            # worktrunk's merge check reads origin/<default>: stale, a fresh merge looks unmerged
            self.warn("warning: git fetch failed; checking the merge against local refs")
        # wt remove runs the remove hooks and returns once the worktree is gone.
        ok, res = self.wt.remove(main, worktree, force=force, branch=branch)
        if not ok:
            raise FleetError("wt remove failed for %s" % worktree)
        name = res.get("branch") or ""
        if not name:
            return []
        outcome = res.get("branch_outcome")
        if outcome == "deleted":
            if branch == "delete":
                lines = ["branch %s deleted (-D)" % name]
                if self._on_origin(main, name) is not False:  # there, or can't tell
                    lines.append("origin/%s kept: -D deletes only the local branch" % name)
                return lines
            return ["branch %s deleted (merged)" % name] + self._delete_on_origin(main, name)
        if res.get("branch_checked_out_at") or outcome == "retained_checked_out":
            where = res.get("branch_checked_out_at")
            return ["branch %s kept: checked out in another worktree%s" % (name, " (%s)" % where if where else "")]
        if branch == "keep":
            return ["branch %s kept (--keep-branch)" % name]
        reason = _KEPT_BECAUSE.get(outcome, "wt reported %s" % (outcome or "nothing"))
        return ["branch %s kept: %s" % (name, reason)]

    def _on_origin(self, main, name):
        """Whether origin has branch `name`; None when origin can't be asked."""
        rc, _ = self.git.run(main, "ls-remote", "--exit-code", "--heads", "origin", "refs/heads/" + name)
        return True if rc == 0 else False if rc == 2 else None

    def _delete_on_origin(self, main, name):
        """Delete origin's `name`, once its merge made the local one go. Lines to report; a failure
        is only warned about, since the worktree is gone by now."""
        there = self._on_origin(main, name)
        if there is False:
            return []
        if there and self.git.ok(main, "push", "--quiet", "origin", "--delete", name):
            return ["origin/%s deleted" % name]
        self.warn("warning: could not delete origin/%s; delete it with: git push origin --delete %s" % (name, name))
        return []

    def _has_ref(self, main, ref):
        return self.git.ok(main, "show-ref", "--verify", "--quiet", ref)

    def _default_base(self, main):
        b = self.git.out(main, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
        if b:
            return b
        for cand in ("origin/main", "origin/master"):
            if self.git.ok(main, "rev-parse", "--verify", "--quiet", cand):
                return cand
        raise FleetError("cannot find origin's default branch; pass --base REF")


# Why worktrunk kept a branch, by its `wt remove --format json` branch_outcome.
_KEPT_BECAUSE = {
    "retained_unmerged": "not merged (use -D to delete it)",
    "retained_raced": "it moved during the removal",
    "retained_failed": "deleting it failed",
    "not_attempted": "wt did not try to delete it",
}


def _wt_path_of(list_json, branch):
    """The path of `branch`'s worktree in `wt list --format json` output, or ""."""
    try:
        data = json.loads(list_json) if list_json.strip() else {}
    except ValueError:
        return ""
    items = data.get("items", []) if isinstance(data, dict) else data
    for it in items or []:
        if isinstance(it, dict) and it.get("branch") == branch:
            p = (it.get("worktree") or {}).get("path")
            if p:
                return p
    return ""


def _switch_path(out):
    """The new worktree's path in `wt switch --format json` output, or ""."""
    try:
        d = json.loads(out)
    except ValueError:
        return ""
    if not isinstance(d, dict):
        return ""
    return d.get("path") or d.get("worktree_path") or (d.get("worktree") or {}).get("path") or ""
