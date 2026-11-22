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

    def remove(self, repo, worktree, force=False):
        """Remove `worktree` of `repo`, keeping its branch; `force` discards its changes. False when
        it was already gone."""
        if not os.path.isdir(worktree):
            return False
        # wt remove runs the remove hooks; it keeps the branch and returns once the worktree is gone.
        if not self.wt.remove(main_checkout(repo), worktree, force=force):
            raise FleetError("wt remove failed for %s" % worktree)
        return True

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
