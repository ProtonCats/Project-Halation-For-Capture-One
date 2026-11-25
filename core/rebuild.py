# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
"""Put a newly built runner in the place of the one setup built before, and touch nothing else.

The new folders are built in <data>/rebuild and then moved into place one by one; <data>/rebuild/rebuild.json says how
far that got, so a move that was cut short is finished. Only the folders in NAMES are moved, and only <data>/rebuild is
ever deleted. A link, or anything that looks like a Wine prefix, in one of these places is refused.
"""
import contextlib
import fcntl
import json
import os
import shutil

from . import setup as profiles

NAMES = ("runner-work", "runners/patched-pe", "runner-work-unix", "runners/patched")  # the one ./run uses comes last
STAGING, JOURNAL, OLD, LOCK = "rebuild", "rebuild.json", "old", ".setup.lock"
STATES = ("building", "ready", "switching", "switched", "undoing", "undone")
PREFIX_SIGNS = ("system.reg", "drive_c", profiles.MARKER, ".runner.json")
FIRST_SET, MARGIN = 8 * 10**9, 10**9


def foreign(folder):
    raise ValueError(f"{folder} is not a folder setup made; move it somewhere else and run setup again")


def own(folder):
    """Refuse a link, a file or a Wine prefix in the place of a folder that is about to be moved or deleted."""
    if folder.is_symlink() or not folder.is_dir() or any(os.path.lexists(folder / sign) for sign in PREFIX_SIGNS):
        foreign(folder)


def state(data):
    """The journal of the staging folder, None when there is no staging folder."""
    staging = data / STAGING
    if not os.path.lexists(staging):
        return None
    own(staging)
    try:
        journal = json.loads((staging / JOURNAL).read_bytes())
        if journal["state"] not in STATES or not isinstance(journal["inputs"], dict):
            raise ValueError("unexpected content")
    except (OSError, ValueError, KeyError, TypeError):
        foreign(staging)
    return journal


def record(data, journal):
    profiles.replace_file(data / STAGING / JOURNAL, (json.dumps(journal) + "\n").encode())
    profiles.sync(data / STAGING)


def move(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    os.rename(source, target)


def discard(data):
    """Delete the staging folder, with whatever old folders were moved into it."""
    journal, staging = state(data), data / STAGING
    if journal is None:
        return
    if journal["state"] in ("switching", "undoing"):
        raise ValueError(f"{staging} is in the middle of a switch")
    for root in (staging, staging / OLD):
        for folder in (root.iterdir() if root.is_dir() else ()):
            if folder.is_dir() and folder != staging / OLD:
                own(folder)
        for name in NAMES:
            if os.path.lexists(root / name):
                own(root / name)
    shutil.rmtree(staging)


def check_space(data):
    """Refuse when the disk cannot hold a second set beside the present one."""
    present = 0
    for name in NAMES:
        for folder, _, files in os.walk(data / name):
            present += sum(os.lstat(os.path.join(folder, file)).st_size for file in files)
    need = present + MARGIN if present else FIRST_SET
    free = shutil.disk_usage(data).free
    if free < need:
        raise ValueError(f"building the runner needs {need / 1e9:.1f} GB in {data}, {free / 1e9:.1f} GB free; nothing was changed")


def begin(data, inputs):
    """An empty staging folder for a set made from inputs; a staging folder that is in the way is cleared."""
    discard(data)
    (data / STAGING).mkdir()
    record(data, {"state": "building", "inputs": inputs})


def ready(data, names):
    record(data, {**state(data), "state": "ready", "names": list(names)})


def switch(data):
    """Move the old folders into the staging folder and the new ones into their place; repeating it is harmless."""
    journal = state(data)
    if journal["state"] == "ready":
        for name in NAMES:
            for folder in (data / name, data / STAGING / name):
                if os.path.lexists(folder):
                    own(folder)
        journal = {**journal, "state": "switching", "had": [name for name in NAMES if os.path.lexists(data / name)]}
        record(data, journal)
    for name in NAMES:
        live, new, old = data / name, data / STAGING / name, data / STAGING / OLD / name
        if name in journal["had"] and not os.path.lexists(old):
            move(live, old)
        if name in journal["names"] and os.path.lexists(new):
            move(new, live)
    record(data, {**journal, "state": "switched"})


def undo(data):
    """Move the new folders back out and the old ones back in; repeating it is harmless."""
    journal = {**state(data), "state": "undoing"}
    record(data, journal)
    for name in reversed(NAMES):
        live, new, old = data / name, data / STAGING / name, data / STAGING / OLD / name
        if name in journal["names"] and not os.path.lexists(new):
            move(live, new)
        if name in journal["had"] and os.path.lexists(old):
            move(old, live)
    record(data, {**journal, "state": "undone"})


def recover(data):
    """Finish a switch or an undo that was cut short."""
    journal = state(data)
    if journal and journal["state"] == "switching":
        switch(data)
    elif journal and journal["state"] == "undoing":
        undo(data)


@contextlib.contextmanager
def lock(data):
    """Only one setup at a time works in data."""
    data.mkdir(parents=True, exist_ok=True)
    with open(os.open(data / LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "r+") as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f"another setup is running in {data}") from None
        yield
