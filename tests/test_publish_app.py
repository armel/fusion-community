import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_submission as cs  # noqa: E402
import publish_app as pa  # noqa: E402
from test_check_submission import FW, form_body, make_app, sample_frame, uvstudio_payload  # noqa: E402

NOW = "2026-10-27T19:00:00Z"


def checked(version="1.0", name="Sky Watch", **fields):
    """The report of a submission made by UV Studio."""
    body = form_body(**{"App name": name,
                        "App binary": uvstudio_payload("app", make_app(name=name, version=version)),
                        "Screenshot": uvstudio_payload("screen", sample_frame()), **fields})
    return cs.check(body, FW, frozenset({"tetris"}))


def issue(number=3, login="f4xyz"):
    return {"number": number, "user": {"login": login}}


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)

    def manifest(self):
        with open(os.path.join(self.root, pa.MANIFEST)) as f:
            return json.load(f)

    def test_first_publication(self):
        report = checked(**{"Source code": "https://github.com/f4xyz/skywatch"})
        message = pa.publish(report, issue(), NOW, self.root)
        self.assertEqual(message, "Publish Sky Watch 1.0 (#3)")
        manifest = self.manifest()
        self.assertEqual((manifest["format"], manifest["updated"]), (1, NOW))
        entry, = manifest["apps"]
        self.assertEqual(entry["file"], "apps/skywatch/1.0/SkyWatch.app")
        with open(os.path.join(self.root, entry["file"]), "rb") as f:
            stored = f.read()
        self.assertEqual(stored, make_app())
        self.assertEqual(entry["sha256"], hashlib.sha256(stored).hexdigest())
        self.assertEqual(base64.b64decode(entry["screen"]), sample_frame())
        self.assertEqual({k: entry[k] for k in ("id", "name", "version", "category", "author", "owner",
                                                "source", "firmware", "issue", "size", "api", "caps")},
                         {"id": "skywatch", "name": "Sky Watch", "version": "1.0", "category": "tools",
                          "author": "F4XYZ", "owner": "f4xyz", "source": "https://github.com/f4xyz/skywatch",
                          "firmware": "v7.0.0 Fusion", "issue": 3, "size": len(stored), "api": 3, "caps": 0})

    def test_update_by_its_author(self):
        pa.publish(checked("1.0"), issue(3), NOW, self.root)
        message = pa.publish(checked("1.10"), issue(7, "F4XYZ"), NOW, self.root)
        self.assertEqual(message, "Update Sky Watch 1.10 (#7)")
        entry, = self.manifest()["apps"]
        self.assertEqual((entry["version"], entry["issue"]), ("1.10", 7))
        self.assertFalse(os.path.exists(os.path.join(self.root, "apps/skywatch/1.0")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "apps/skywatch/1.10/SkyWatch.app")))

    def test_refusals(self):
        pa.publish(checked("1.2"), issue(), NOW, self.root)
        cases = [
            ("not newer than the published 1.2", checked("1.2"), issue(4)),
            ("not newer than the published 1.2", checked("1.1.9"), issue(4)),
            ("belongs to the app of f4xyz", checked("2.0"), issue(5, "someone")),
            ("finds 1 problem: fix it first", checked(**{"Category": "Misc"}), issue(6)),
        ]
        for expected, report, by in cases:
            with self.subTest(expected):
                with self.assertRaisesRegex(pa.PublishError, expected):
                    pa.publish(report, by, NOW, self.root)
        entry, = self.manifest()["apps"]
        self.assertEqual(entry["version"], "1.2")

    def test_catalog_order_and_versions(self):
        pa.publish(checked(name="Zed Scope"), issue(1), NOW, self.root)
        pa.publish(checked(name="Alpha Game"), issue(2), NOW, self.root)
        self.assertEqual([e["id"] for e in self.manifest()["apps"]], ["alphagame", "zedscope"])
        self.assertLess(pa.version_key("1.9"), pa.version_key("1.10"))
        self.assertLess(pa.version_key("v0.7"), pa.version_key("0.7.1"))


class WorkflowTest(unittest.TestCase):
    def test_publication_starts_on_the_approved_label_only(self):
        with open(os.path.join(ROOT, ".github", "workflows", "publish-app.yml")) as f:
            workflow = f.read()
        self.assertIn("types: [labeled]", workflow)
        self.assertIn("github.event.label.name == 'approved'", workflow)
        self.assertIn("contents: write", workflow)
        # the steps read the issue from the event file, never from an expression
        self.assertNotIn("github.event.issue", workflow.split("steps:")[1])
        with open(os.path.join(ROOT, pa.MANIFEST)) as f:
            self.assertEqual(json.load(f)["format"], 1)


if __name__ == "__main__":
    unittest.main()
