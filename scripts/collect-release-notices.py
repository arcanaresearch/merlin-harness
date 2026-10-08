#!/usr/bin/env python3
"""Preserve dependency and CPython notices in the standalone release."""

from __future__ import annotations

import argparse
import json
import os
import sysconfig
import zipfile
from importlib.metadata import distribution
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--wheelhouse", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--browser-cache", type=Path)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
if not python_license.is_file():
    raise SystemExit(f"CPython license missing from build interpreter: {python_license}")
(args.output / "CPython-LICENSE.txt").write_bytes(python_license.read_bytes())
bootloader = distribution("pyinstaller")
bootloader_notices = [
    item for item in bootloader.files or () if "licenses" in Path(str(item)).parts
]
if not bootloader_notices:
    raise SystemExit("PyInstaller bootloader license missing from build environment")
for item in bootloader_notices:
    output = args.output / "PyInstaller" / Path(str(item)).name
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(Path(bootloader.locate_file(item)).read_bytes())
records = []
for wheel in sorted(args.wheelhouse.glob("*.whl")):
    if wheel.name.startswith("merlin_"):
        continue
    files = []
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            path = Path(name)
            if path.is_absolute() or ".." in path.parts:
                raise SystemExit(f"unsafe wheel path in {wheel.name}: {name}")
            if name.endswith("/") or not (
                name.endswith(".dist-info/METADATA")
                or any(
                    part.lower().startswith(
                        ("license", "copying", "copyright", "notice", "thirdpartynotice")
                    )
                    for part in path.parts
                )
            ):
                continue
            output = args.output / "dependencies" / wheel.name / path
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(archive.read(name))
            files.append(name)
    records.append({"wheel": wheel.name, "notice_files": files})
if not records:
    raise SystemExit("no dependency wheels found for release notice collection")
(args.output / "inventory.json").write_text(json.dumps(records, indent=2) + "\n")
print(f"Collected notices for {len(records)} dependency wheels and CPython")

if args.browser_cache is not None:
    from playwright.sync_api import sync_playwright

    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(args.browser_cache.resolve())
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel="chromium")
        try:
            page = browser.new_page()
            page.goto("chrome://credits/")
            credits = page.content()
            if "Chromium" not in credits or "Redistribution" not in credits:
                raise SystemExit("bundled browser did not expose its redistribution notices")
            (args.output / "Chrome-for-Testing-CREDITS.html").write_text(credits)
        finally:
            browser.close()
