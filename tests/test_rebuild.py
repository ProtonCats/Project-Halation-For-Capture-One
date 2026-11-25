# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import rebuild as rb, treehash

INPUTS = {"base": "Release-2", "pe": "a" * 64, "unix": "b" * 64}


class Stop(BaseException):
    """A kill between two moves."""


class Rebuild(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "data"
        self.staging = self.data / "rebuild"
        self.put("pfx/system.reg", "prefix")
        self.put("pfx/drive_c/app.exe", "app")
        self.prefix = treehash.snapshot(self.data / "pfx")
        self.addCleanup(lambda: self.assertEqual(treehash.compare(self.data / "pfx", self.prefix), []))

    def put(self, path, text):
        path = self.data / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def old_set(self, names=rb.NAMES):
        for name in names:
            self.put(f"{name}/content", "old")

    def staged_set(self, names=rb.NAMES):
        rb.begin(self.data, INPUTS)
        for name in names:
            self.put(f"rebuild/{name}/content", "new")
        rb.ready(self.data, names)

    def contents(self):
        return {name: (self.data / name / "content").read_text() for name in rb.NAMES if (self.data / name).exists()}

    def test_the_new_set_takes_the_place_of_the_old_one(self):
        self.old_set()
        self.staged_set()
        rb.switch(self.data)
        self.assertEqual(self.contents(), dict.fromkeys(rb.NAMES, "new"))
        rb.discard(self.data)
        self.assertFalse(self.staging.exists())

    def test_a_first_set_needs_no_old_one(self):
        self.staged_set()
        rb.switch(self.data)
        self.assertEqual(self.contents(), dict.fromkeys(rb.NAMES, "new"))

    def test_a_folder_the_new_set_lacks_leaves_with_the_old_one(self):
        self.old_set()
        new = ("runner-work", "runners/patched")
        self.staged_set(new)
        rb.switch(self.data)
        self.assertEqual(self.contents(), dict.fromkeys(new, "new"))


    def test_a_kill_between_any_two_moves_is_finished_forward(self):
        for had in (rb.NAMES, ("runner-work", "runners/patched"), ()):
            for staged in (rb.NAMES, ("runner-work", "runners/patched")):
                count = 0
                while True:
                    with self.subTest(had=had, staged=staged, killed_after=count):
                        for name in rb.NAMES:
                            self.remove(name)
                        self.remove("rebuild")
                        self.old_set(had)
                        self.staged_set(staged)
                        left, real = [count], rb.move
                        def move(source, target):
                            if not left[0]:
                                raise Stop
                            left[0] -= 1
                            real(source, target)
                        try:
                            with patch.object(rb, "move", side_effect=move):
                                rb.switch(self.data)
                            finished = True
                        except Stop:
                            finished = False
                            self.assertEqual(rb.state(self.data)["state"], "switching")
                            rb.recover(self.data)
                        self.assertEqual(self.contents(), dict.fromkeys([n for n in rb.NAMES if n in staged], "new"))
                        rb.recover(self.data)
                        self.assertEqual(self.contents(), dict.fromkeys([n for n in rb.NAMES if n in staged], "new"))
                    count += 1
                    if finished:
                        break
                self.assertGreater(count, len(staged))

    def test_an_undo_brings_the_old_set_back_from_any_point(self):
        for had in (rb.NAMES, ("runner-work", "runners/patched"), ()):
            for killed_after in range(12):
                with self.subTest(had=had, killed_after=killed_after):
                    for name in rb.NAMES:
                        self.remove(name)
                    self.remove("rebuild")
                    self.old_set(had)
                    self.staged_set()
                    rb.switch(self.data)
                    left, real = [killed_after], rb.move
                    def move(source, target):
                        if not left[0]:
                            raise Stop
                        left[0] -= 1
                        real(source, target)
                    try:
                        with patch.object(rb, "move", side_effect=move):
                            rb.undo(self.data)
                    except Stop:
                        rb.recover(self.data)
                    self.assertEqual(self.contents(), dict.fromkeys([n for n in rb.NAMES if n in had], "old"))
                    self.assertEqual(rb.state(self.data)["state"], "undone")

    def remove(self, name):
        import shutil
        path = self.data / name
        if path.exists():
            shutil.rmtree(path)

    def test_recover_leaves_every_other_state_alone(self):
        self.old_set()
        rb.recover(self.data)
        self.staged_set()
        before = treehash.snapshot(self.data)
        rb.recover(self.data)
        self.assertEqual(treehash.compare(self.data, before), [])

    def test_a_folder_that_is_not_setups_own_is_refused_and_kept(self):
        outside = self.data / "outside"
        self.put("outside/keep", "keep")
        for kind in ("link", "file", "system.reg", "drive_c", ".setup.json", ".runner.json"):
            for name in (*rb.NAMES, "rebuild"):
                with self.subTest(kind=kind, name=name):
                    for other in (*rb.NAMES, "rebuild"):
                        path = self.data / other
                        if path.is_symlink() or path.is_file():
                            path.unlink()
                        else:
                            self.remove(other)
                    self.old_set([n for n in rb.NAMES if n != name])
                    if name != "rebuild":
                        self.staged_set()
                    path = self.data / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if kind == "link":
                        path.symlink_to(outside)
                    elif kind == "file":
                        path.write_text("keep")
                    else:
                        (path / kind).mkdir(parents=True)
                        (path / "rebuild.json").write_text(json.dumps({"state": "building", "inputs": INPUTS}))
                    with self.assertRaisesRegex(ValueError, "not a folder setup made"):
                        rb.switch(self.data) if name != "rebuild" else rb.begin(self.data, INPUTS)
                    self.assertTrue(os.path.lexists(path))
                    self.assertEqual((outside / "keep").read_text(), "keep")

    def test_a_staging_folder_without_its_record_is_never_deleted(self):
        self.put("rebuild/something", "keep")
        for text in (None, "", "{", "[]", '{"state": 1}', '{"state": "unknown", "inputs": {}}'):
            with self.subTest(text=text):
                if text is not None:
                    self.put("rebuild/rebuild.json", text)
                with self.assertRaisesRegex(ValueError, "not a folder setup made"):
                    rb.begin(self.data, INPUTS)
                with self.assertRaisesRegex(ValueError, "not a folder setup made"):
                    rb.discard(self.data)
                self.assertEqual((self.staging / "something").read_text(), "keep")

    def test_a_prefix_inside_the_staging_folder_stops_the_delete(self):
        for place in ("rebuild/pfx", "rebuild/old/pfx", "rebuild/runners/patched", "rebuild/old/runner-work"):
            with self.subTest(place=place):
                self.remove("rebuild")
                self.staged_set()
                self.put(f"{place}/system.reg", "prefix")
                with self.assertRaisesRegex(ValueError, "not a folder setup made"):
                    rb.discard(self.data)
                self.assertTrue((self.data / place / "system.reg").exists())

    def test_a_link_inside_is_removed_as_a_link(self):
        self.put("outside/keep", "keep")
        self.staged_set()
        (self.staging / "runner-work/link").symlink_to(self.data / "outside")
        rb.discard(self.data)
        self.assertEqual((self.data / "outside/keep").read_text(), "keep")

    def test_a_new_start_clears_an_unfinished_staging_folder(self):
        rb.begin(self.data, INPUTS)
        self.put("rebuild/runner-work/half", "half")
        rb.begin(self.data, {**INPUTS, "pe": "c" * 64})
        self.assertEqual([p.name for p in self.staging.iterdir()], ["rebuild.json"])
        self.assertEqual(rb.state(self.data)["inputs"]["pe"], "c" * 64)

    def test_a_staging_folder_in_the_middle_of_a_switch_is_never_cleared(self):
        self.old_set()
        self.staged_set()
        real = rb.move
        with patch.object(rb, "move", side_effect=[real, Stop]) as move:
            move.side_effect = lambda s, t, n=iter((0, 1)): real(s, t) if next(n) == 0 else (_ for _ in ()).throw(Stop())
            with self.assertRaises(Stop):
                rb.switch(self.data)
        for action in (lambda: rb.begin(self.data, INPUTS), lambda: rb.discard(self.data)):
            with self.assertRaisesRegex(ValueError, "in the middle of a switch"):
                action()
        rb.recover(self.data)
        self.assertEqual(self.contents(), dict.fromkeys(rb.NAMES, "new"))

    def test_the_space_needed_is_the_present_set_and_a_margin(self):
        with patch.object(rb.shutil, "disk_usage", return_value=SimpleNamespace(free=rb.FIRST_SET - 1)), \
                self.assertRaisesRegex(ValueError, r"needs 8\.0 GB.*8\.0 GB free"):
            rb.check_space(self.data)
        with patch.object(rb.shutil, "disk_usage", return_value=SimpleNamespace(free=rb.FIRST_SET)):
            rb.check_space(self.data)
        self.old_set()
        self.put("runners/patched/large", "x" * 300000)
        need = rb.MARGIN + 300000
        with patch.object(rb.shutil, "disk_usage", return_value=SimpleNamespace(free=need - 1)), \
                self.assertRaisesRegex(ValueError, "free"):
            rb.check_space(self.data)
        with patch.object(rb.shutil, "disk_usage", return_value=SimpleNamespace(free=need + 200000)):
            rb.check_space(self.data)

    def test_two_at_once_are_refused(self):
        with rb.lock(self.data):
            with self.assertRaisesRegex(ValueError, "another setup is running"), rb.lock(self.data):
                pass
        with rb.lock(self.data):
            pass


if __name__ == "__main__":
    unittest.main()
