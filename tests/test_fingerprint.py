import tempfile
import unittest
from pathlib import Path

from backup_manager import fingerprint
from backup_manager.config import Job
from backup_manager.fingerprint import FingerprintStore, changed_since_success, job_fingerprint


def make_job(**kw):
    base = dict(id="j", name="J", type="rclone", source="a", destination="b")
    base.update(kw)
    return Job(**base)


class JobFingerprint(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def test_stable(self):
        self.assertEqual(job_fingerprint(make_job(), self.base), job_fingerprint(make_job(), self.base))

    def test_name_group_and_depends_on_are_ignored(self):
        a = job_fingerprint(make_job(), self.base)
        b = job_fingerprint(make_job(name="Renamed", group="sync", depends_on=["x"]), self.base)
        self.assertEqual(a, b)

    def test_settings_change_it(self):
        self.assertNotEqual(job_fingerprint(make_job(), self.base),
                            job_fingerprint(make_job(destination="c"), self.base))

    def test_logic_version_bump_changes_it(self):
        before = job_fingerprint(make_job(), self.base)
        old = fingerprint.JOB_LOGIC_VERSION["rclone"]
        fingerprint.JOB_LOGIC_VERSION["rclone"] = old + 1
        try:
            self.assertNotEqual(before, job_fingerprint(make_job(), self.base))
        finally:
            fingerprint.JOB_LOGIC_VERSION["rclone"] = old

    def test_every_job_type_has_a_logic_version(self):
        for t in ("adbsync", "mtp", "ios_backup", "rclone", "ffs", "script"):
            self.assertIn(t, fingerprint.JOB_LOGIC_VERSION)

    def test_in_repo_script_contents_are_part_of_it(self):
        script = self.base / "run.ps1"
        script.write_text("one")
        job = make_job(type="script", script_path="run.ps1")
        before = job_fingerprint(job, self.base)
        script.write_text("two")
        self.assertNotEqual(before, job_fingerprint(job, self.base))

    def test_missing_script_is_stable_not_an_error(self):
        job = make_job(type="script", script_path="gone.ps1")
        self.assertEqual(job_fingerprint(job, self.base), job_fingerprint(job, self.base))

    def test_windows_separators_resolve_inside_the_repo(self):
        (self.base / "tools").mkdir()
        script = self.base / "tools" / "x.ps1"
        script.write_text("one")
        job = make_job(type="script", script_path="tools\\x.ps1")
        before = job_fingerprint(job, self.base)
        script.write_text("two")
        self.assertNotEqual(before, job_fingerprint(job, self.base))

    def test_absolute_script_is_not_read(self):
        outside = self.base / "outside.ps1"
        outside.write_text("one")
        job = make_job(type="script", script_path=str(outside))
        before = job_fingerprint(job, self.base)
        outside.write_text("two")
        self.assertEqual(before, job_fingerprint(job, self.base))

    def test_windows_absolute_paths_count_as_absolute(self):
        self.assertTrue(fingerprint._is_absolute("G:\\scripts\\x.ps1"))
        self.assertTrue(fingerprint._is_absolute("\\\\server\\share\\x.ps1"))
        self.assertTrue(fingerprint._is_absolute("/usr/bin/x"))
        self.assertFalse(fingerprint._is_absolute("tools\\x.ps1"))

    def test_same_named_sibling_in_a_subfolder_is_included(self):
        (self.base / "tools").mkdir()
        (self.base / "tools" / "sync.ps1").write_text("ps")
        sibling = self.base / "tools" / "sync.py"
        sibling.write_text("one")
        job = make_job(type="script", script_path="tools/sync.ps1")
        before = job_fingerprint(job, self.base)
        sibling.write_text("two")
        self.assertNotEqual(before, job_fingerprint(job, self.base))


class Store(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "fp.yaml"

    def test_missing_file_loads_empty(self):
        s = FingerprintStore(self.path)
        s.load()
        self.assertIsNone(s.get("j"))

    def test_set_persists_and_reloads(self):
        s = FingerprintStore(self.path)
        s.set("j", "abc")
        s2 = FingerprintStore(self.path)
        s2.load()
        self.assertEqual(s2.get("j"), "abc")
        self.assertFalse(self.path.with_name("fp.yaml.tmp").exists())

    def test_non_mapping_file_loads_empty(self):
        self.path.write_text("- a\n- b\n")
        s = FingerprintStore(self.path)
        s.load()
        self.assertIsNone(s.get("a"))


class ChangedSinceSuccess(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.store = FingerprintStore(self.base / "fp.yaml")
        self.job = make_job()

    def test_never_succeeded_is_not_flagged(self):
        self.assertFalse(changed_since_success(self.job, self.base, self.store, has_success=False))

    def test_succeeded_with_no_recorded_fingerprint_is_flagged(self):
        self.assertTrue(changed_since_success(self.job, self.base, self.store, has_success=True))

    def test_matching_fingerprint_is_not_flagged(self):
        self.store.set(self.job.id, job_fingerprint(self.job, self.base))
        self.assertFalse(changed_since_success(self.job, self.base, self.store, has_success=True))

    def test_edited_job_is_flagged(self):
        self.store.set(self.job.id, job_fingerprint(self.job, self.base))
        self.job.destination = "elsewhere"
        self.assertTrue(changed_since_success(self.job, self.base, self.store, has_success=True))
