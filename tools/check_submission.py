#!/usr/bin/env python3
"""Check an app submission of the UV Studio community catalog.

In GitHub Actions (--event): reads the submission issue, takes the .app and
the screen from the UV Studio payloads of its text or downloads its two
attachments, checks the .app the way the radio's loader does and decodes the
Live Viewer screenshot, then posts or updates one report comment, labels the
issue and names it after the app.

Locally: check_submission.py --body issue.md [--app App.zip] [--screenshot shot.png]
prints the report; --app and --screenshot replace the attachment downloads.
"""
import argparse
import base64
import io
import json
import os
import re
import struct
import sys
import urllib.error
import urllib.request
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
MARKER = "<!-- uvstudio-community-check -->"
BOT = "github-actions[bot]"
USER_AGENT = "uvstudio-community-check"

# Issue form labels (.github/ISSUE_TEMPLATE/submit-app.yml) -> field keys.
FIELDS = {
    "App name": "name",
    "Author": "author",
    "Category": "category",
    "Description": "description",
    "Firmware tested": "firmware",
    "Source code": "source",
    "Screenshot": "screenshot",
    "App binary": "binary",
    "Checks": "checks",
}
CATEGORIES = ("Radio", "Tools", "Games", "Demos")
CHECKBOXES = 3

APP_MAGIC = 0x31504146   # "FAP1"
APP_HDR_VERSION = 1
# app_header_t: magic hdr_version abi_major api_min code_size code_crc32
# entry_off flags name[16] version[16] link_vma required_caps asset_size asset_crc
HEADER = struct.Struct("<IHBBIIHH16s16sIIHH")

BINARY_MAX = 1 << 20     # downloaded zip
APP_FILE_MAX = 16 << 10  # .app inside the zip
SHOT_MAX = 5 << 20       # downloaded screenshot
SHOT_PIXELS = 1 << 22    # decoded screenshot

# Attachments of an issue: new and legacy GitHub upload URLs, nothing else.
ATTACHMENT = re.compile(
    r"https://(?:github\.com/user-attachments/(?:assets|files)/"
    r"|github\.com/[\w.-]+/[\w.-]+/files/"
    r"|user-images\.githubusercontent\.com/)[\w./%-]+")
# What UV Studio's "Propose an app" panel writes in a field instead of an
# attachment: the raw-deflated .app or 1024 LCD bytes, in base64url.
PAYLOAD = re.compile(r"\buvstudio-(app|screen):1:([A-Za-z0-9_-]+)")

LABELS = {
    "app-submission": ("1f6feb", "App submitted through the form"),
    "check-passed": ("2da44e", "The submission check found no problem"),
    "check-failed": ("d73a4a", "The submission check found problems to fix"),
}


class CheckError(Exception):
    pass


class Report:
    def __init__(self):
        self.lines = []     # (mark, Markdown text)
        self.fields = None  # form fields once the body follows the form
        self.app = None     # header facts once the binary is read
        self.frame = None   # 1024 LCD bytes once the screenshot is decoded

    def ok(self, text):
        self.lines.append(("ok", text))

    def warn(self, text):
        self.lines.append(("warning", text))

    def error(self, text):
        self.lines.append(("error", text))

    @property
    def errors(self):
        return sum(mark == "error" for mark, _ in self.lines)


def load_firmware(path=os.path.join(HERE, "firmware.json")):
    with open(path) as f:
        fw = json.load(f)
    fw["link_vma"] = int(fw["link_vma"], 0)
    return fw


def esc(text):
    """User text made inert in Markdown: one line, no HTML, mention or formatting."""
    text = " ".join(str(text).split())
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("@", "&#64;")
    return re.sub(r"([\\`*_\[\]|~])", r"\\\1", text)


def same_name(a, b):
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def catalog_id(name):
    """UV Studio's id of an app: its name in lowercase alphanumerics."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def parse_form(body):
    """{field key: value} of an issue form body made of '### Label' blocks."""
    fields = {}
    body = "\n" + (body or "").replace("\r\n", "\n")
    for block in re.split(r"\n### ", body)[1:]:
        label, _, value = block.partition("\n")
        key = FIELDS.get(label.strip())
        if key:
            value = value.strip()
            fields[key] = "" if value == "_No response_" else value
    return fields


def check_form(f, report):
    """Field checks; False when the body no longer follows the form at all."""
    missing = [label for label, key in FIELDS.items() if key not in f]
    if missing:
        report.error("The issue no longer follows the submission form (missing: %s). "
                     "Open a new submission from the form." % ", ".join(missing))
        return False
    before = report.errors
    one_line = lambda value, low, high: "\n" not in value and low <= len(value) <= high
    if not one_line(f["name"], 1, 16):
        report.error("**App name**: one line, 16 characters at most.")
    if not one_line(f["author"], 1, 24):
        report.error("**Author**: one line, 24 characters at most.")
    if f["category"] not in CATEGORIES:
        report.error("**Category**: one of %s." % ", ".join(CATEGORIES))
    if not one_line(f["description"], 10, 140):
        report.error("**Description**: one sentence, 10 to 140 characters (now %d)." % len(f["description"]))
    if not one_line(f["firmware"], 0, 40):
        report.error("**Firmware tested**: one line, 40 characters at most, or leave it empty.")
    if f["source"] and not re.fullmatch(r"https://[^\s<>|\"'`\\]{4,200}", f["source"]):
        report.error("**Source code**: an https:// link, or leave it empty.")
    if len(re.findall(r"^- \[[xX]\] ", f["checks"], re.M)) != CHECKBOXES:
        report.error("**Checks**: tick the %d boxes." % CHECKBOXES)
    if report.errors == before:
        report.ok("Form complete")
    return True


def download(url, limit):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(limit + 1)
    except (urllib.error.URLError, OSError) as e:
        raise CheckError("could not download the attachment (%s)." % esc(e))
    if len(data) > limit:
        raise CheckError("the attachment is over %d KiB." % (limit >> 10))
    return data


def attachment(text, limit):
    """The one file uploaded in a form field."""
    urls = list(dict.fromkeys(m.group(0) for m in ATTACHMENT.finditer(text)))
    if not urls:
        raise CheckError("drop the file in this field of the form.")
    if len(urls) > 1:
        raise CheckError("one file only (found %d)." % len(urls))
    return download(urls[0], limit)


def payload(text, kind, limit):
    """The bytes of the UV Studio payload of a form field, None without one."""
    found = [m.group(2) for m in PAYLOAD.finditer(text) if m.group(1) == kind]
    if not found:
        return None
    if len(found) > 1:
        raise CheckError("one UV Studio payload only (found %d)." % len(found))
    try:
        inflater = zlib.decompressobj(-15)
        data = inflater.decompress(base64.urlsafe_b64decode(found[0] + "=" * (-len(found[0]) % 4)), limit + 1)
    except (ValueError, zlib.error) as e:
        raise CheckError("the UV Studio payload is damaged (%s)." % esc(e))
    if len(data) > limit or not inflater.eof:
        raise CheckError("the UV Studio payload is damaged or too large.")
    return data


def extract_app(data):
    """The .app of the binary attachment: a zip holding one .app, or the .app itself."""
    if data[:4] == struct.pack("<I", APP_MAGIC):
        return data
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        apps = [i for i in archive.infolist()
                if not i.is_dir() and i.filename.lower().endswith(".app")
                and not i.filename.startswith("__MACOSX/")
                and not os.path.basename(i.filename).startswith(".")]
        if len(apps) != 1:
            raise CheckError("the zip must hold exactly one .app file (found %d)." % len(apps))
        if apps[0].flag_bits & 1:
            raise CheckError("the zip is encrypted.")
        if apps[0].file_size > APP_FILE_MAX:
            raise CheckError("the .app is over %d KiB." % (APP_FILE_MAX >> 10))
        return archive.read(apps[0])
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, EOFError, zlib.error) as e:
        raise CheckError("not a readable .zip (%s)." % esc(e))


def header_text(raw):
    text = raw.split(b"\0")[0]
    return text.decode("ascii") if all(0x20 <= c < 0x7F for c in text) else None


def cap_names(caps, fw):
    names = [name for name, bit in fw["caps"].items() if caps & bit]
    unknown = caps & ~sum(fw["caps"].values())
    return names + (["0x%X" % unknown] if unknown else [])


def check_app(data, fw, report):
    """The header facts, after the checks of the radio's loader
    (app_read_validated_header) and of UV Studio's installer."""
    if len(data) < HEADER.size:
        report.error("**App binary**: shorter than the 64-byte app header.")
        return None
    (magic, hdr_version, abi, api_min, code_size, code_crc, entry, flags, name, version,
     vma, caps, asset_size, asset_crc) = HEADER.unpack_from(data)
    if magic != APP_MAGIC or hdr_version != APP_HDR_VERSION:
        report.error("**App binary**: not an overlay app (no FAP1 version 1 header).")
        return None
    app = {"name": header_text(name), "version": header_text(version), "api": api_min,
           "caps": caps, "code": code_size, "assets": asset_size}
    problems = []
    if not app["name"]:
        problems.append("the header has no printable ASCII name")
    if not app["version"]:
        problems.append("the header has no printable ASCII version")
    if abi != fw["abi_major"]:
        problems.append("built for ABI %d, the firmware runs ABI %d" % (abi, fw["abi_major"]))
    if not 1 <= api_min <= fw["api_level"]:
        problems.append("needs API level %d, the firmware provides level %d" % (api_min, fw["api_level"]))
    unknown = caps & ~sum(fw["caps"].values())
    if unknown:
        problems.append("requires capabilities the firmware does not provide (0x%X)" % unknown)
    if vma != fw["link_vma"]:
        problems.append("linked at 0x%08X instead of 0x%08X" % (vma, fw["link_vma"]))
    if not 2 <= code_size <= fw["code_max"]:
        problems.append("%d bytes of code, %d at most" % (code_size, fw["code_max"]))
    elif entry & 1 or entry > code_size - 2:
        problems.append("entry point 0x%X outside the code" % entry)
    if asset_size > fw["asset_max"]:
        problems.append("%d bytes of assets, %d at most" % (asset_size, fw["asset_max"]))
    if len(data) != HEADER.size + code_size + asset_size:
        problems.append("the file is %d bytes, the header announces 64 + %d + %d"
                        % (len(data), code_size, asset_size))
    else:
        code = data[HEADER.size:HEADER.size + code_size]
        if zlib.crc32(code) != code_crc:
            problems.append("the code CRC does not match")
        if zlib.crc32(data[HEADER.size + code_size:]) & 0xFFFF != asset_crc:
            problems.append("the asset CRC does not match")
    for problem in problems:
        report.error("**App binary**: %s." % problem)
    if problems:
        return None
    report.ok("App binary: valid header, sizes and CRCs, like the radio's loader checks them")
    app["id"] = catalog_id(app["name"])
    return app


def lcd_pages(rows):
    """The 1024 bytes of the LCD: 8 pages of 128 column bytes, bit 0 on top."""
    out = bytearray(1024)
    for y in range(64):
        for x in range(128):
            if rows[y][x]:
                out[(y >> 3) * 128 + x] |= 1 << (y & 7)
    return bytes(out)


def decode_screenshot(data):
    """The 1024 LCD bytes of a Live Viewer screenshot: 128 x 64 cells, each
    read at its centre against the glass in its corner (the gap of the LCD
    effect; without it, the majority of cells, unlit on most screens)."""
    from PIL import Image
    try:
        image = Image.open(io.BytesIO(data), formats=("PNG", "JPEG", "WEBP"))
        if image.width * image.height > SHOT_PIXELS:
            raise CheckError("the image is too large; save it again from the Live Viewer.")
        image = image.convert("L")
    except CheckError:
        raise
    except Exception as e:
        raise CheckError("not a readable PNG (%s)." % esc(e))
    ratio = image.width / image.height
    if image.width < 128 or image.height < 64 or not 1.3 <= ratio <= 2.1:
        raise CheckError("%d x %d pixels is not a Live Viewer screen; save it with the Space "
                         "key, without cropping or a window around it." % (image.width, image.height))
    px, cw, ch = image.load(), image.width / 128, image.height / 64
    corners = sorted(px[int((x + 1) * cw) - 1, int((y + 1) * ch) - 1] for y in range(64) for x in range(128))
    glass = corners[len(corners) // 2]
    centre = [[px[int(x * cw + (cw - 1) / 2), int(y * ch + (ch - 1) / 2)] for x in range(128)]
              for y in range(64)]
    far = max(abs(v - glass) for row in centre for v in row)
    frame = lcd_pages([[int(abs(v - glass) > far / 2) for v in row] for row in centre])
    if far < 32 or frame in (bytes(1024), b"\xff" * 1024):
        raise CheckError("the screen looks blank.")
    return frame


def braille(frame):
    """The screen as 16 lines of 64 braille characters, 2 x 4 pixels each."""
    dots = ((0, 0, 0x01), (0, 1, 0x02), (0, 2, 0x04), (1, 0, 0x08),
            (1, 1, 0x10), (1, 2, 0x20), (0, 3, 0x40), (1, 3, 0x80))
    lit = lambda x, y: frame[(y >> 3) * 128 + x] >> (y & 7) & 1
    return "\n".join("".join(chr(0x2800 + sum(bit for dx, dy, bit in dots if lit(2 * cx + dx, 4 * cy + dy)))
                             for cx in range(64)) for cy in range(16))


def official_ids(fw, token=None):
    """Catalog ids of the official apps, from every archive/apps/v* folder."""
    def get(path):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
        if token:
            headers["Authorization"] = "Bearer " + token
        url = "https://api.github.com/repos/%s/contents/%s" % (fw["official_repo"], path)
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
            return json.load(response)
    ids = set()
    for entry in get(fw["official_apps"]):
        if entry["type"] == "dir" and re.fullmatch(r"v\d[\w.]*", entry["name"]):
            ids.update(catalog_id(e["name"][:-4]) for e in get(entry["path"]) if e["name"].endswith(".app"))
    return ids


def check(body, fw, official, files=None):
    """The report of a submission. official: catalog ids of the official apps,
    None when unknown. files: local bytes replacing the 'binary' and
    'screenshot' attachments."""
    files = files or {}
    report = Report()
    f = parse_form(body)
    if not check_form(f, report):
        return report
    report.fields = f

    try:
        data = files.get("binary")
        if data is None:
            data = payload(f["binary"], "app", APP_FILE_MAX)
        if data is None:
            data = attachment(f["binary"], BINARY_MAX)
        report.app = check_app(extract_app(data), fw, report)
    except CheckError as e:
        report.error("**App binary**: %s" % e)
    app = report.app
    if app:
        if not same_name(f["name"], app["name"]):
            report.error('**App name**: the app header names it "%s", the form says "%s". Use the '
                         "header's name: the radio and the catalog show it." % (esc(app["name"]), esc(f["name"])))
        if not app["id"]:
            report.error("**App name**: needs at least one letter or digit.")
        elif official is None:
            report.warn("Could not read the official app list: name clash not checked")
        elif app["id"] in official:
            report.error("**App name**: an official app already uses the catalog id `%s`; rename the app."
                         % app["id"])
        else:
            report.ok("No official app uses the catalog id `%s`" % app["id"])

    try:
        frame = None if "screenshot" in files else payload(f["screenshot"], "screen", 1024)
        if frame is None:
            report.frame = decode_screenshot(files.get("screenshot") or attachment(f["screenshot"], SHOT_MAX))
            report.ok("Screenshot: decoded as a 128 x 64 Live Viewer screen (preview below)")
        elif len(frame) != 1024 or frame in (bytes(1024), b"\xff" * 1024):
            raise CheckError("the UV Studio screen is blank or not 128 x 64 pixels.")
        else:
            report.frame = frame
            report.ok("Screenshot: 128 x 64 screen sent by UV Studio (preview below)")
    except CheckError as e:
        report.error("**Screenshot**: %s" % e)
    return report


def render(report, fw):
    marks = {"ok": "✅", "warning": "⚠️", "error": "❌"}
    n = report.errors
    head = "✅ ready for review" if not n else "❌ %d problem%s to fix" % (n, "s" if n > 1 else "")
    out = [MARKER, "## Submission check: " + head, ""]
    out += ["- %s %s" % (marks[mark], text) for mark, text in report.lines]
    f, app = report.fields, report.app
    if f and app:
        caps = ", ".join(cap_names(app["caps"], fw)) or "none"
        source = "<%s>" % f["source"] if f["source"] else "not given"
        out += ["", "### App", "",
                "| | |", "|---|---|",
                "| Name | %s %s (catalog id `%s`) |" % (esc(app["name"]), esc(app["version"]), app["id"]),
                "| Author | %s |" % esc(f["author"]),
                "| Category | %s |" % esc(f["category"]),
                "| Description | %s |" % esc(f["description"]),
                "| Code | %d / %d bytes |" % (app["code"], fw["code_max"]),
                "| Assets | %d / %d bytes |" % (app["assets"], fw["asset_max"]),
                "| Needs | API level %d, capabilities: %s |" % (app["api"], caps),
                "| Tested on | %s |" % (esc(f["firmware"]) or "not given"),
                "| Source | %s |" % source]
    if report.frame:
        out += ["", "### Screenshot as the catalog reads it", "", "```text", braille(report.frame), "```"]
    out += ["", "---", "Everything is in order: a maintainer will review the app." if not n else
            "Edit this issue to fix the problems: the check runs again after each edit."]
    return "\n".join(out) + "\n"


class GitHub:
    def __init__(self, repo, token):
        self.repo, self.token = repo, token

    def call(self, method, path, data=None):
        request = urllib.request.Request(
            "https://api.github.com/repos/%s/%s" % (self.repo, path), method=method,
            data=None if data is None else json.dumps(data).encode(),
            headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT,
                     "Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
        return json.loads(body) if body else None

    def publish(self, issue, text, passed, title):
        """One report comment per issue, updated in place; labels; the issue title."""
        number = issue["number"]
        comments = self.call("GET", "issues/%d/comments?per_page=100" % number)
        mine = [c for c in comments if c["user"]["login"] == BOT and MARKER in (c["body"] or "")]
        if mine:
            self.call("PATCH", "issues/comments/%d" % mine[0]["id"], {"body": text})
        else:
            self.call("POST", "issues/%d/comments" % number, {"body": text})
        for name, (color, description) in LABELS.items():
            try:
                self.call("POST", "labels", {"name": name, "color": color, "description": description})
            except urllib.error.HTTPError as e:
                if e.code != 422:   # 422: the label exists
                    raise
        verdict, stale = ("check-passed", "check-failed") if passed else ("check-failed", "check-passed")
        self.call("POST", "issues/%d/labels" % number, {"labels": ["app-submission", verdict]})
        if any(label["name"] == stale for label in issue.get("labels", [])):
            self.call("DELETE", "issues/%d/labels/%s" % (number, stale))
        if title and issue["title"] != title:
            self.call("PATCH", "issues/%d" % number, {"title": title})


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--event", help="GitHub event JSON: check its issue and report on it")
    ap.add_argument("--body", help="issue body file to check locally")
    ap.add_argument("--app", help="local .zip or .app instead of the binary attachment")
    ap.add_argument("--screenshot", help="local image instead of the screenshot attachment")
    ap.add_argument("--offline", action="store_true", help="skip the official app list")
    args = ap.parse_args()
    if bool(args.event) == bool(args.body):
        ap.error("give --event or --body")

    fw = load_firmware()
    token = os.environ.get("GITHUB_TOKEN")
    issue = None
    if args.event:
        with open(args.event) as f:
            issue = json.load(f)["issue"]
        body = issue.get("body") or ""
    else:
        with open(args.body) as f:
            body = f.read()
    files = {}
    for key, path in (("binary", args.app), ("screenshot", args.screenshot)):
        if path:
            with open(path, "rb") as f:
                files[key] = f.read()
    official = None
    if not args.offline:
        try:
            official = official_ids(fw, token)
        except Exception as e:
            print("official app list: %s" % e, file=sys.stderr)

    report = check(body, fw, official, files)
    text = render(report, fw)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(text)
    if issue:
        app = report.app
        title = "[App] %s %s" % (app["name"], app["version"]) if app else None
        GitHub(os.environ["GITHUB_REPOSITORY"], token).publish(issue, text, not report.errors, title)


if __name__ == "__main__":
    main()
