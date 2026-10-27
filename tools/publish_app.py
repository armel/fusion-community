#!/usr/bin/env python3
"""Publish an approved app submission to the community catalog (apps.json),
or take a published app out of it.

GitHub Actions, on the `approved` label of a submission issue:
  publish_app.py prepare --event EVENT --message FILE
      checks the submission again, stores the .app under
      apps/<id>/<version>/ and its entry in apps.json, and writes the commit
      message to FILE. A refused submission gets a comment and loses the
      label instead, and FILE is not written.
  publish_app.py announce --event EVENT
      once the commit is pushed: comments, labels the issue published and
      closes it.

On the `removed` label of the issue of a published app:
  publish_app.py remove --event EVENT --message FILE --entry JSON
      deletes its apps.json entry and its folder, writes the commit message
      to FILE and the removed entry to JSON. A refusal gets a comment and
      the label is taken off instead.
  publish_app.py announce-removal --event EVENT --entry JSON
      once the commit is pushed: comments, takes the published label off
      and closes the issue if it is open.
"""
import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import shutil
import urllib.error

import check_submission as cs

ROOT = os.path.dirname(cs.HERE)
MANIFEST = "apps.json"
MARKER = "<!-- uvstudio-community-publish -->"
MAINTAINERS = ("admin", "maintain", "write")
APPROVED = "approved"
PUBLISHED = ("published", "0e8a16", "The app is in the community catalog")
REMOVED = "removed"


class PublishError(Exception):
    pass


def load_manifest(root=ROOT):
    path = os.path.join(root, MANIFEST)
    if not os.path.exists(path):
        return {"format": 1, "apps": []}
    with open(path) as f:
        return json.load(f)


def save_manifest(manifest, root=ROOT):
    manifest["apps"].sort(key=lambda entry: entry["id"])
    with open(os.path.join(root, MANIFEST), "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")


def version_key(version):
    return tuple(int(n) for n in re.findall(r"\d+", version))


def publish(report, issue, now, root=ROOT):
    """Store the app of a checked submission and its catalog entry; the
    commit message. Only the author of the first publication may update an
    app, with a newer version."""
    if report.errors:
        raise PublishError("the submission check finds %d problem%s: fix %s first."
                           % (report.errors, "s" if report.errors > 1 else "", "them" if report.errors > 1 else "it"))
    app, f = report.app, report.fields
    owner = issue["user"]["login"]
    manifest = load_manifest(root)
    old = next((entry for entry in manifest["apps"] if entry["id"] == app["id"]), None)
    if old:
        if old["owner"].lower() != owner.lower():
            raise PublishError("the catalog id `%s` belongs to the app of %s; rename this one."
                               % (app["id"], cs.esc(old["owner"])))
        if version_key(app["version"]) <= version_key(old["version"]):
            raise PublishError("version %s is not newer than the published %s."
                               % (cs.esc(app["version"]), cs.esc(old["version"])))

    relative = "apps/%s/%s/%s.app" % (app["id"], re.sub(r"[^\w.-]", "_", app["version"]),
                                      re.sub(r"[^A-Za-z0-9]", "", app["name"]))
    path = os.path.join(root, relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as out:
        out.write(report.binary)
    if old and old["file"] != relative:
        stale = os.path.join(root, old["file"])
        if os.path.exists(stale):
            os.remove(stale)
            try:
                os.removedirs(os.path.dirname(stale))
            except OSError:
                pass   # the app folder still holds the new version

    entry = {
        "id": app["id"],
        "name": app["name"],
        "version": app["version"],
        "file": relative,
        "size": len(report.binary),
        "codeSize": app["code"],
        "assetSize": app["assets"],
        "sha256": hashlib.sha256(report.binary).hexdigest(),
        "api": app["api"],
        "caps": app["caps"],
        "category": f["category"].lower(),
        "author": " ".join(f["author"].split()),
        "owner": owner,
        "description": " ".join(f["description"].split()),
        "source": f["source"],
        "firmware": f["firmware"],
        # the 1024 LCD bytes, base64 like UV Studio's js/app-screens.js
        "screen": base64.b64encode(report.frame).decode(),
        "issue": issue["number"],
        "published": now,
    }
    manifest["apps"] = [e for e in manifest["apps"] if e["id"] != app["id"]] + [entry]
    manifest["updated"] = now
    save_manifest(manifest, root)
    return "%s %s %s (#%d)" % ("Update" if old else "Publish", app["name"], app["version"], issue["number"])


def remove(issue, now, root=ROOT):
    """Take the app of an issue out of the catalog, its entry and its folder;
    the commit message and the removed entry. The issue is the one of its
    last publication, or any other submission of it by its owner."""
    manifest = load_manifest(root)
    entry = next((e for e in manifest["apps"] if e["issue"] == issue["number"]), None)
    if entry is None:
        app_id = cs.catalog_id(cs.parse_form(issue.get("body")).get("name", ""))
        entry = next((e for e in manifest["apps"] if e["id"] == app_id and
                      e["owner"].lower() == issue["user"]["login"].lower()), None)
    if entry is None:
        raise PublishError("no app of the community catalog comes from this issue.")
    # A hand-edited apps.json must not send the folder removal elsewhere.
    if not re.fullmatch(r"[a-z0-9]+", entry["id"]):
        raise PublishError("the catalog id `%s` is not a folder name." % cs.esc(entry["id"]))
    shutil.rmtree(os.path.join(root, "apps", entry["id"]), ignore_errors=True)
    manifest["apps"] = [e for e in manifest["apps"] if e["id"] != entry["id"]]
    manifest["updated"] = now
    save_manifest(manifest, root)
    return "Remove %s %s (#%d)" % (entry["name"], entry["version"], issue["number"]), entry


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def github():
    return cs.GitHub(os.environ["GITHUB_REPOSITORY"], os.environ.get("GITHUB_TOKEN"))


def comment(gh, number, text):
    gh.call("POST", "issues/%d/comments" % number, {"body": "%s\n%s" % (MARKER, text)})


def drop_label(gh, number, name):
    try:
        gh.call("DELETE", "issues/%d/labels/%s" % (number, name))
    except urllib.error.HTTPError as error:
        if error.code != 404:   # 404: the issue does not have it
            raise


def can_publish(gh, login):
    try:
        return gh.call("GET", "collaborators/%s/permission" % login)["permission"] in MAINTAINERS
    except urllib.error.HTTPError:
        return False


def prepare(event, message_path):
    issue, sender = event["issue"], event["sender"]["login"]
    token = os.environ.get("GITHUB_TOKEN")
    gh = github()
    number = issue["number"]
    try:
        if not can_publish(gh, sender):
            raise PublishError("only maintainers can publish an app.")
        fw = cs.load_firmware()
        try:
            official = cs.official_ids(fw, token)
        except Exception:
            official = None
        report = cs.check(issue.get("body") or "", fw, official)
        message = publish(report, issue, utc_now())
    except PublishError as e:
        comment(gh, number, "❌ Not published: %s" % e)
        drop_label(gh, number, APPROVED)
        print("not published: %s" % e)
        return
    with open(message_path, "w") as f:
        f.write(message + "\n")
    print(message)


def announce(event):
    issue = event["issue"]
    number = issue["number"]
    gh = github()
    entry = next(e for e in load_manifest()["apps"] if e["issue"] == number)
    comment(gh, number, "✅ Published: **%s %s** is in the Community tab of UV Studio. Thanks for sharing it!"
            % (cs.esc(entry["name"]), cs.esc(entry["version"])))
    name, color, description = PUBLISHED
    try:
        gh.call("POST", "labels", {"name": name, "color": color, "description": description})
    except urllib.error.HTTPError as e:
        if e.code != 422:   # 422: the label exists
            raise
    gh.call("POST", "issues/%d/labels" % number, {"labels": [name]})
    gh.call("PATCH", "issues/%d" % number, {"state": "closed", "state_reason": "completed"})


def withdraw(event, message_path, entry_path):
    issue, sender = event["issue"], event["sender"]["login"]
    gh = github()
    number = issue["number"]
    try:
        if not can_publish(gh, sender):
            raise PublishError("only maintainers can remove an app.")
        message, entry = remove(issue, utc_now())
    except PublishError as e:
        comment(gh, number, "❌ Not removed: %s" % e)
        drop_label(gh, number, REMOVED)
        print("not removed: %s" % e)
        return
    with open(message_path, "w") as f:
        f.write(message + "\n")
    with open(entry_path, "w") as f:
        json.dump(entry, f)
    print(message)


def announce_removal(event, entry_path):
    issue = event["issue"]
    number = issue["number"]
    gh = github()
    with open(entry_path) as f:
        entry = json.load(f)
    comment(gh, number, "🗑️ Removed: **%s %s** is no longer in the Community tab of UV Studio. "
            "Radios that installed it keep it." % (cs.esc(entry["name"]), cs.esc(entry["version"])))
    drop_label(gh, number, PUBLISHED[0])
    if issue["state"] == "open":
        gh.call("PATCH", "issues/%d" % number, {"state": "closed", "state_reason": "not_planned"})


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("step", choices=("prepare", "announce", "remove", "announce-removal"))
    ap.add_argument("--event", required=True, help="GitHub event JSON of the labeled issue")
    ap.add_argument("--message", help="prepare, remove: where to write the commit message")
    ap.add_argument("--entry", help="remove, announce-removal: the removed catalog entry (JSON)")
    args = ap.parse_args()
    with open(args.event) as f:
        event = json.load(f)
    if args.step in ("prepare", "remove") and not args.message:
        ap.error("%s needs --message" % args.step)
    if args.step in ("remove", "announce-removal") and not args.entry:
        ap.error("%s needs --entry" % args.step)
    if args.step == "prepare":
        prepare(event, args.message)
    elif args.step == "announce":
        announce(event)
    elif args.step == "remove":
        withdraw(event, args.message, args.entry)
    else:
        announce_removal(event, args.entry)


if __name__ == "__main__":
    main()
