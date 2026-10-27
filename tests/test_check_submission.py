import io
import os
import re
import sys
import unittest
import zipfile
import zlib

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import check_submission as cs  # noqa: E402

try:
    from PIL import Image
except ImportError:
    Image = None

FW = cs.load_firmware()
APP_URL = "https://github.com/user-attachments/files/123/SkyWatch.zip"
SHOT_URL = "https://github.com/user-attachments/assets/0f0e-1d2c"


def make_app(name="Sky Watch", version="1.0", code=b"\x00\xbf" * 600, assets=b"asset" * 20,
             abi=1, api=3, caps=0, entry=0, vma=0x20000280, code_crc=None):
    header = cs.HEADER.pack(cs.APP_MAGIC, 1, abi, api, len(code),
                            zlib.crc32(code) if code_crc is None else code_crc, entry, 1,
                            name.encode(), version.encode(), vma, caps, len(assets),
                            zlib.crc32(assets) & 0xFFFF)
    return header + code + assets


def zipped(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buffer.getvalue()


def sample_frame():
    """A box around the screen, a diagonal and a filled block."""
    rows = [[0] * 128 for _ in range(64)]
    for x in range(128):
        rows[0][x] = rows[63][x] = 1
    for y in range(64):
        rows[y][0] = rows[y][127] = 1
        rows[y][y * 2 % 128] = 1
    for y in range(20, 30):
        for x in range(90, 110):
            rows[y][x] = 1
    return cs.lcd_pages(rows)


def viewer_png(frame, pixel_size=7, lcd=True, ink=(0, 0, 0), paper=(242, 242, 242)):
    """A Live Viewer screenshot: k5viewer.js drawFrame geometry."""
    gap = 1 if lcd else 0
    unlit = tuple(round(p + (i - p) * 0.05) for i, p in zip(ink, paper))
    image = Image.new("RGB", (128 * (pixel_size - 1), 64 * pixel_size), paper)
    px = image.load()
    for y in range(64):
        for x in range(128):
            colour = ink if frame[(y >> 3) * 128 + x] >> (y & 7) & 1 else unlit
            for dy in range(pixel_size - gap):
                for dx in range(pixel_size - 1 - gap):
                    px[x * (pixel_size - 1) + dx, y * pixel_size + dy] = colour
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def form_body(**fields):
    values = {
        "App name": "Sky Watch",
        "Author": "F4XYZ",
        "Category": "Tools",
        "Description": "Track satellite passes and show the next one.",
        "Firmware tested": "v7.0.0 Fusion",
        "Source code": "_No response_",
        "Screenshot": '<img width="768" alt="shot" src="%s" />' % SHOT_URL,
        "App binary": "[SkyWatch.zip](%s)" % APP_URL,
        "Checks": "- [X] I tested this app on my radio.\n- [X] I wrote this app.\n- [X] I understand.",
    }
    values.update(fields)
    return "\n\n".join("### %s\n\n%s" % (label, value) for label, value in values.items())


class Downloads:
    """Replaces the attachment downloads with in-memory files."""
    def __init__(self, files):
        self.files, self.saved = files, cs.download

    def __enter__(self):
        cs.download = lambda url, limit: self.files[url]
        return self

    def __exit__(self, *exc):
        cs.download = self.saved


def run(body=None, app=None, shot=None, official=frozenset({"tetris", "foxhunt"})):
    files = {APP_URL: zipped({"SkyWatch.app": make_app()}) if app is None else app,
             SHOT_URL: viewer_png(sample_frame()) if shot is None else shot}
    with Downloads(files):
        return cs.check(form_body() if body is None else body, FW, official)


def errors(report):
    return [text for mark, text in report.lines if mark == "error"]


@unittest.skipUnless(Image, "Pillow is needed for the screenshots")
class SubmissionTest(unittest.TestCase):
    def test_valid_submission_is_ready_for_review(self):
        report = run()
        self.assertEqual(errors(report), [])
        self.assertEqual(report.app["id"], "skywatch")
        self.assertEqual(report.frame, sample_frame())
        text = cs.render(report, FW)
        self.assertIn("ready for review", text)
        self.assertIn("| Name | Sky Watch 1.0 (catalog id `skywatch`) |", text)
        self.assertTrue(text.startswith(cs.MARKER))

    def test_name_must_match_the_header(self):
        report = run(body=form_body(**{"App name": "Sky Watcher"}))
        self.assertTrue(any("header names it" in e for e in errors(report)))

    def test_official_name_clash(self):
        app = zipped({"Tetris.app": make_app(name="Tetris")})
        report = run(body=form_body(**{"App name": "Tetris"}), app=app)
        self.assertTrue(any("official app already uses the catalog id `tetris`" in e for e in errors(report)))

    def test_unknown_official_list_is_a_warning(self):
        report = run(official=None)
        self.assertEqual(errors(report), [])
        self.assertIn("warning", [mark for mark, _ in report.lines])

    def test_form_field_errors(self):
        body = form_body(**{"Description": "x" * 141, "Source code": "http://example.com",
                            "Category": "Misc", "Checks": "- [X] one\n- [ ] two\n- [X] three"})
        found = " ".join(errors(run(body=body)))
        for field in ("Description", "Source code", "Category", "Checks"):
            self.assertIn("**%s**" % field, found)

    def test_body_without_the_form(self):
        report = run(body="Here is my app, please add it.")
        self.assertEqual(len(errors(report)), 1)
        self.assertIn("no longer follows the submission form", errors(report)[0])

    def test_one_attachment_per_field(self):
        body = form_body(**{"App binary": "[a.zip](%s) [b.zip](%s2)" % (APP_URL, APP_URL)})
        self.assertTrue(any("one file only" in e for e in errors(run(body=body))))
        body = form_body(**{"Screenshot": "see the binary"})
        self.assertTrue(any("drop the file" in e for e in errors(run(body=body))))

    def test_blank_screenshot(self):
        report = run(shot=viewer_png(bytes(1024)))
        self.assertTrue(any("looks blank" in e for e in errors(report)))

    def test_cropped_screenshot(self):
        image = Image.open(io.BytesIO(viewer_png(sample_frame()))).crop((0, 0, 300, 448))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        self.assertTrue(any("not a Live Viewer screen" in e for e in errors(run(shot=buffer.getvalue()))))


@unittest.skipUnless(Image, "Pillow is needed for the screenshots")
class ScreenshotTest(unittest.TestCase):
    def test_every_viewer_pixel_size_and_effect(self):
        frame = sample_frame()
        for size in (3, 5, 7, 12):
            for lcd in (True, False):
                with self.subTest(size=size, lcd=lcd):
                    self.assertEqual(cs.decode_screenshot(viewer_png(frame, size, lcd)), frame)

    def test_inverted_colours(self):
        frame = sample_frame()
        png = viewer_png(frame, ink=(230, 230, 255), paper=(20, 20, 60))
        self.assertEqual(cs.decode_screenshot(png), frame)

    def test_not_an_image(self):
        with self.assertRaises(cs.CheckError):
            cs.decode_screenshot(b"GIF89a not really")


class BinaryTest(unittest.TestCase):
    def problems(self, data):
        report = cs.Report()
        app = cs.check_app(data, FW, report)
        return app, " ".join(errors(report))

    def test_valid_app(self):
        app, problems = self.problems(make_app(caps=0x21, api=4))
        self.assertEqual(problems, "")
        self.assertEqual(cs.cap_names(app["caps"], FW), ["FM", "CFG_PROXY"])

    def test_loader_checks(self):
        cases = {
            "ABI 2": make_app(abi=2),
            "needs API level 5": make_app(api=5),
            "needs API level 0": make_app(api=0),
            "(0x4)": make_app(caps=0x4),
            "linked at 0x20000000": make_app(vma=0x20000000),
            "4098 bytes of code": make_app(code=b"\0" * 4098),
            "entry point 0x3": make_app(entry=3),
            "3841 bytes of assets": make_app(assets=b"\0" * 3841),
            "code CRC": make_app(code_crc=1),
            "no printable ASCII name": make_app(name=""),
        }
        for expected, data in cases.items():
            with self.subTest(expected):
                app, problems = self.problems(data)
                self.assertIsNone(app)
                self.assertIn(expected, problems)

    def test_truncated_file(self):
        app, problems = self.problems(make_app()[:-1])
        self.assertIn("the header announces", problems)
        app, problems = self.problems(b"FAP1")
        self.assertIn("shorter than the 64-byte", problems)
        app, problems = self.problems(b"\0" * 64)
        self.assertIn("not an overlay app", problems)

    def test_zip_holds_one_app(self):
        data = make_app()
        self.assertEqual(cs.extract_app(zipped({"SkyWatch.app": data, "__MACOSX/._SkyWatch.app": b"x",
                                                "README.txt": b"hi"})), data)
        self.assertEqual(cs.extract_app(data), data)
        with self.assertRaisesRegex(cs.CheckError, "found 2"):
            cs.extract_app(zipped({"a.app": data, "b.app": data}))
        with self.assertRaisesRegex(cs.CheckError, "found 0"):
            cs.extract_app(zipped({"a.bin": data}))
        with self.assertRaisesRegex(cs.CheckError, "not a readable .zip"):
            cs.extract_app(b"PK\x03\x04 broken")
        with self.assertRaisesRegex(cs.CheckError, "over 16 KiB"):
            cs.extract_app(zipped({"big.app": b"\0" * (17 << 10)}))


class FormTest(unittest.TestCase):
    def test_template_labels_match_the_parser(self):
        with open(os.path.join(ROOT, ".github", "ISSUE_TEMPLATE", "submit-app.yml")) as f:
            template = f.read()
        labels = re.findall(r"^ {6}label: (.+)$", template, re.M)
        self.assertEqual(labels, list(cs.FIELDS))
        self.assertEqual(template.count("          required: true"), cs.CHECKBOXES)
        self.assertEqual(re.findall(r"^ {8}- (\w+)$", template, re.M), list(cs.CATEGORIES))
        # the workflow starts on the binary field's heading
        with open(os.path.join(ROOT, ".github", "workflows", "check-submission.yml")) as f:
            self.assertIn("'### App binary'", f.read())

    def test_parse_form(self):
        fields = cs.parse_form(form_body().replace("\n", "\r\n"))
        self.assertEqual(fields["name"], "Sky Watch")
        self.assertEqual(fields["source"], "")
        self.assertEqual(set(fields), set(cs.FIELDS.values()))

    def test_escape(self):
        self.assertEqual(cs.esc("a|b\n<i>@x *y*"), "a\\|b &lt;i&gt;&#64;x \\*y\\*")

    def test_braille(self):
        frame = bytearray(1024)
        frame[0] = 1           # top-left pixel
        frame[7 * 128 + 127] = 0x80  # bottom-right pixel
        lines = cs.braille(bytes(frame)).split("\n")
        self.assertEqual((len(lines), {len(line) for line in lines}), (16, {64}))
        self.assertEqual(lines[0][0], "⠁")
        self.assertEqual(lines[15][63], "⢀")


if __name__ == "__main__":
    unittest.main()
