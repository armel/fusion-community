# Fusion community apps

Overlay apps for the F4HWN firmware (UV-K1 / UV-K5 V3), shared by their authors and listed in the **Community** tab of the [UV Studio](https://armel.github.io/uvstudio/) Apps view.

> [!WARNING]
> Community apps are not reviewed by F4HWN. An overlay app runs native code with full access to the radio, transmitter included. Install only apps whose author you trust, and transmit only where your licence allows it.

## Submit an app

The easiest way is UV Studio: **Apps > Community > Propose an app**. It checks your `.app`, captures its screen from the radio while the app runs (or reads a Live Viewer PNG), shows the card as UV Studio will draw it, then opens GitHub with the submission ready: you only click **Create**.

By hand:

1. Build your `.app` against the current `app_api.h` of the [firmware](https://github.com/armel/uv-k1-k5v3-firmware-custom) and test it on your radio.
2. While it runs, open the UV Studio Live Viewer and press **Space** to save a screenshot (PNG).
3. Put the `.app` in a `.zip`: GitHub does not accept `.app` attachments.
4. [Open a submission](https://github.com/armel/fusion-community/issues/new?template=submit-app.yml) and fill in the form: name, author, category, a one-sentence description, the screenshot and the zip.

A bot checks the submission within a minute or two and replies with a report. Edit the issue to fix what it reports: the check runs again after each edit. Once the check passes, a maintainer reviews the app; once published, it is listed in [`apps.json`](apps.json), which UV Studio reads, and the issue is closed.

To update your app, submit the new version the same way: it replaces the published one when it comes from the same GitHub account with a higher version number.

## What the check looks at

- **The form**: the required fields filled in, within their lengths, and the three boxes ticked.
- **The binary**: the zip holds one `.app` that passes the checks of the radio's loader: FAP1 header, ABI, API level, capabilities, link address, code up to 4096 bytes, assets up to 3840 bytes, CRCs.
- **The name**: the one in the app header, and not the name of an official app.
- **The screenshot**: a Live Viewer screen it can read back as 128 × 64 pixels. The report shows it as text, the way the catalog will draw it.

## Rules

- One app per submission.
- The description is in English and is shown as written: official apps are translated, community apps are not.
- You keep the rights to your app; submitting it allows its redistribution through UV Studio. Sharing the source code is encouraged: the card of an app with a source link shows it.
- A maintainer can refuse or remove an app at any time, for example if it misbehaves on the air.

## Maintainers

- **Publish** an app: add the `approved` label to its submission once the check passes. The *Publish or remove app* workflow checks it again, stores the `.app` under `apps/<id>/<version>/`, adds or updates its entry in `apps.json`, commits, then comments and closes the issue. A refusal (check failing, someone else's app id, version not newer) is explained in a comment and the label is taken off.
- **Refuse** a submission: close the issue with a comment.
- **Remove** a published app: add the `removed` label to the issue of its publication (or to any other submission of it by its author), even closed. The same workflow deletes its entry from `apps.json` and its folder under `apps/`, commits, then comments, takes the `published` label off and closes the issue if it is open; with nothing to remove, it says so and takes the label off. UV Studio stops listing the app within about 5 minutes (GitHub caches `apps.json`); radios that installed it keep it. Its id is free again: a new submission under that name is published only if you approve it.
- `tools/firmware.json` holds what the current firmware accepts (ABI, API level, capabilities, sizes). Update it when the firmware's app API changes, together with the same limits in UV Studio's `js/app-submit.js`.
- Keep blank issues enabled (`.github/ISSUE_TEMPLATE/config.yml`): UV Studio opens a blank issue prefilled with the submission.
- Check a submission locally:

  ```sh
  pip install -r tools/requirements.txt
  python tools/check_submission.py --body issue.md --app App.zip --screenshot shot.png
  ```

- Run the tests with `python -m unittest discover -s tests`.
