"""Worktrees against real temporary git repositories (a bare repo in a temp dir is `origin`, so no
network) and a fake wt. Skipped when git is not installed."""

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from fakes import FakeWt

from fleetlib.errors import FleetError
from fleetlib.system import Git
from fleetlib.worktrees import Worktrees

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class Repo(unittest.TestCase):
    """origin: a bare repo with branches main and remote-only; repo/main: a clone of it."""

    def setUp(self):
        env = mock.patch.dict(os.environ, GIT_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="fleet-wts-"))
        self.addCleanup(shutil.rmtree, self.dir)
        self.origin = os.path.join(self.dir, "origin.git")
        self.repo = os.path.join(self.dir, "repo")
        self.main = os.path.join(self.repo, "main")
        self.git("init", "--quiet", "--bare", "-b", "main", self.origin)
        seed = os.path.join(self.dir, "seed")
        self.git("clone", "--quiet", self.origin, seed)
        self.git("-C", seed, "commit", "--quiet", "--allow-empty", "-m", "one")
        self.git("-C", seed, "push", "--quiet", "origin", "HEAD:main", "HEAD:remote-only")
        self.git("clone", "--quiet", self.origin, self.main)
        # newer git re-creates a deleted origin/HEAD on fetch, which would hide the fallbacks
        self.git("-C", self.main, "config", "remote.origin.followRemoteHEAD", "never")
        # where the fake `wt switch` says it put the new worktree
        self.new_wt = os.path.join(self.repo, "feat")
        os.makedirs(self.new_wt)
        self.wt = FakeWt()
        self.wt.switch_result = (0, '{"path": "%s"}' % self.new_wt)
        self.said, self.warned = [], []
        self.wts = Worktrees(Git(), self.wt, say=lambda *l: self.said.extend(l), warn=lambda *l: self.warned.extend(l))

    def git(self, *args):
        subprocess.run(["git"] + list(args), check=True, stdout=subprocess.DEVNULL)

    def switches(self):
        return [c[1:] for c in self.wt.calls if c[0] == "switch"]


class Layout(Repo):

    def test_layout_repo_or_its_main_checkout(self):
        self.assertEqual(self.wts.locate(self.repo), self.repo)
        self.assertEqual(self.wts.locate(self.main), self.repo)

    def test_layout_neither(self):
        with self.assertRaises(FleetError) as e:
            self.wts.locate(self.dir)
        self.assertEqual(str(e.exception), "%s does not follow <repo>/main (no git checkout at <repo>/main). "
                                           "Pass --no-worktree to run there without a worktree." % self.dir)


class Ensure(Repo):

    def test_new_branch_from_origin_default(self):
        self.assertEqual(self.wts.ensure(self.repo, "feat"), (self.new_wt, self.repo))
        self.assertEqual(self.switches(), [(self.main, "feat", True, "origin/main")])
        self.assertEqual(self.wt.calls[1:], [("hook", self.new_wt, h) for h in ("pre-start", "post-start", "post-switch")])
        self.assertEqual(self.said, ["worktree %s on new branch feat from origin/main" % self.new_wt,
                                     "running wt pre-start hooks...", "running wt post-start hooks...",
                                     "running wt post-switch hooks..."])
        self.assertEqual(self.warned, [])

    def test_default_base_follows_origin_head(self):
        self.git("-C", self.main, "remote", "set-head", "origin", "remote-only")
        self.wts.ensure(self.main, "feat")
        self.assertEqual(self.switches(), [(self.main, "feat", True, "origin/remote-only")])

    def test_default_base_falls_back_to_origin_main(self):
        self.git("-C", self.main, "remote", "set-head", "origin", "-d")
        self.wts.ensure(self.repo, "feat")
        self.assertEqual(self.switches(), [(self.main, "feat", True, "origin/main")])

    def test_no_discoverable_base(self):
        self.git("-C", self.origin, "branch", "-m", "main", "trunk")
        self.git("-C", self.main, "remote", "set-head", "origin", "-d")
        self.git("-C", self.main, "update-ref", "-d", "refs/remotes/origin/main")
        with self.assertRaises(FleetError) as e:
            self.wts.ensure(self.repo, "feat")
        self.assertEqual(str(e.exception), "cannot find origin's default branch; pass --base REF")
        self.assertEqual(self.switches(), [])

    def test_explicit_base(self):
        self.wts.ensure(self.repo, "feat", base="origin/remote-only")
        self.assertEqual(self.switches(), [(self.main, "feat", True, "origin/remote-only")])
        self.assertIn("worktree %s on new branch feat from origin/remote-only" % self.new_wt, self.said)

    def test_existing_local_branch(self):
        self.git("-C", self.main, "branch", "feat")
        self.wts.ensure(self.repo, "feat", base="origin/remote-only")
        self.assertEqual(self.switches(), [(self.main, "feat", False, "origin/remote-only")])
        self.assertIn("worktree %s from existing branch feat" % self.new_wt, self.said)

    def test_tracking_origin_branch(self):
        self.wts.ensure(self.repo, "remote-only")
        self.assertEqual(self.switches(), [(self.main, "remote-only", False, None)])
        self.assertIn("worktree %s tracking origin/remote-only" % self.new_wt, self.said)

    def test_branch_fetched_from_origin(self):
        # pushed after the clone: only the fetch can tell that origin has it
        seed = os.path.join(self.dir, "seed")
        self.git("-C", seed, "push", "--quiet", "origin", "HEAD:later")
        self.wts.ensure(self.repo, "later")
        self.assertEqual(self.switches(), [(self.main, "later", False, None)])

    def test_fetch_failure_warns_and_continues(self):
        self.git("-C", self.main, "remote", "set-url", "origin", os.path.join(self.dir, "gone.git"))
        self.assertEqual(self.wts.ensure(self.repo, "feat"), (self.new_wt, self.repo))
        self.assertEqual(self.warned, ["warning: git fetch failed; using local refs"])
        self.assertEqual(self.switches(), [(self.main, "feat", True, "origin/main")])

    def test_reuses_existing_worktree(self):
        self.wt.list_out = '{"items": [{"branch": "feat", "worktree": {"path": "%s"}}]}' % self.new_wt
        self.assertEqual(self.wts.ensure(self.main, "feat"), (self.new_wt, self.repo))
        self.assertEqual(self.said, ["reusing worktree %s" % self.new_wt])
        self.assertEqual(self.wt.calls, [])

    def test_invalid_branch_name(self):
        for bad in ("a..b", "-x", "a b"):
            with self.assertRaises(FleetError) as e:
                self.wts.ensure(self.repo, bad)
            self.assertEqual(str(e.exception), "'%s' is not a valid branch name" % bad)
        self.assertEqual(self.wt.calls, [])

    def test_switch_without_a_path(self):
        self.wt.switch_result = (0, "{}")
        with self.assertRaises(FleetError) as e:
            self.wts.ensure(self.repo, "feat")
        self.assertEqual(str(e.exception), "wt switch did not report the new worktree's path: {}")

    def test_failing_hook_warns(self):
        self.wt.hook = lambda d, h: h != "post-start"
        self.wts.ensure(self.repo, "feat")
        self.assertEqual(self.warned, ["warning: wt post-start hooks failed in %s" % self.new_wt])


class Removal(Repo):

    def worktree(self):
        d = os.path.join(self.repo, "wk")
        self.git("-C", self.main, "worktree", "add", "--quiet", "-b", "wk", d)
        return d

    def test_clean_worktree_is_removed_branch_kept(self):
        d = self.worktree()
        self.wts.check_removable(d)
        self.assertTrue(self.wts.remove(self.repo, d))
        self.assertEqual(self.wt.removed, [(self.main, d, False)])
        self.assertEqual(self.said, [])

    def test_dirty_worktree_refused_with_its_changes(self):
        d = self.worktree()
        with open(os.path.join(d, "new.txt"), "w") as f:
            f.write("x")
        with self.assertRaises(FleetError) as e:
            self.wts.check_removable(d)
        self.assertEqual(str(e.exception), "uncommitted changes in %s (above). Commit them, or re-run with "
                                           "--force to discard them." % d)
        self.assertEqual(self.said, ["?? new.txt"])

    def test_change_list_is_capped(self):
        d = self.worktree()
        for i in range(25):
            open(os.path.join(d, "f%02d" % i), "w").close()
        with self.assertRaises(FleetError):
            self.wts.check_removable(d)
        self.assertEqual(self.said, ["?? f%02d" % i for i in range(20)])

    def test_unreadable_status_refused(self):
        d = os.path.join(self.dir, "not-a-repo")
        os.makedirs(d)
        with self.assertRaises(FleetError) as e:
            self.wts.check_removable(d)
        self.assertEqual(str(e.exception), "cannot read git status of %s; not removing it "
                                           "(--force to discard anyway)" % d)

    def test_forced_removal_of_a_dirty_worktree(self):
        d = self.worktree()
        open(os.path.join(d, "new.txt"), "w").close()
        self.assertTrue(self.wts.remove(self.repo, d, force=True))
        self.assertEqual(self.wt.removed, [(self.main, d, True)])

    def test_gone_worktree_is_nothing_to_remove(self):
        d = os.path.join(self.repo, "gone")
        self.wts.check_removable(d)
        self.assertFalse(self.wts.remove(self.repo, d))
        self.assertEqual(self.wt.removed, [])

    def test_wt_remove_failure(self):
        d = self.worktree()
        self.wt.remove = lambda repo, worktree, force=False: False
        with self.assertRaises(FleetError) as e:
            self.wts.remove(self.repo, d)
        self.assertEqual(str(e.exception), "wt remove failed for %s" % d)


if __name__ == "__main__":
    unittest.main()
