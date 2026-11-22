"""Worker record storage: <state dir>/workers/<pane id without %>.json, written by both the CLI and
the hook under a per-worker lock. What a record holds, and every change to it, is lifecycle.py's.
"""

import errno
import fcntl
import json
import os
import time
from contextlib import contextmanager

DEFAULT_STATE_DIR = os.path.join(os.path.expanduser("~"), ".local", "state", "fleet")


def state_dir(environ=None):
    environ = os.environ if environ is None else environ
    return environ.get("FLEET_STATE_DIR") or DEFAULT_STATE_DIR


class Store:
    """The workers/ directory. Every read-modify-write holds a per-worker lock, so the hook and the
    CLI cannot overwrite each other's update."""

    def __init__(self, root):
        self.root = root
        self.workers = os.path.join(root, "workers")

    def ensure(self):
        os.makedirs(self.workers, exist_ok=True)

    def path(self, pane):
        if not pane:
            raise ValueError("empty pane id")
        return os.path.join(self.workers, pane.lstrip("%") + ".json")

    def files(self):
        try:
            names = sorted(os.listdir(self.workers))
        except FileNotFoundError:
            return []
        return [os.path.join(self.workers, n) for n in names if n.endswith(".json")]

    @staticmethod
    def load(path):
        """The record, or None if the file is gone or unreadable."""
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            return None
        return rec if isinstance(rec, dict) else None

    @staticmethod
    def write(path, rec):
        tmp = "%s.%d" % (path, os.getpid())
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rec, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def update(path, fields, lock_timeout=2.0):
        """Merge `fields` into the record under the lock. Returns the new record, or None if the
        file no longer exists (the worker was removed meanwhile; never recreate it)."""
        return Store.change(path, lambda rec: rec.update(fields), lock_timeout)

    @staticmethod
    def change(path, fn, lock_timeout=2.0):
        """Read the record, let fn(rec) change it in place, write it back, all under the lock.
        Returns the new record, or None if the file no longer exists (never recreated)."""
        with locked(path, lock_timeout):
            rec = Store.load(path)
            if rec is None:
                return None
            fn(rec)
            Store.write(path, rec)
            return rec

    @staticmethod
    def remove(path):
        for p in (path, path + ".lock"):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass


@contextmanager
def locked(path, timeout):
    """flock on <path>.lock, giving up after `timeout` seconds (then proceeding unlocked: a stale
    lock must never wedge a hook or a command)."""
    fd = None
    try:
        fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        pass
    got = False
    if fd is not None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES) or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
    try:
        yield
    finally:
        if fd is not None:
            if got:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

