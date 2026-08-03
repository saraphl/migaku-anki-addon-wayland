#!/usr/bin/env python3
"""Standalone check: does a uinput-injected Ctrl+C actually copy?

The addon conflates several things that can each fail independently -- the
hotkey listener, the injection, the modifiers the user is physically holding,
and the clipboard read. This exercises only the injection and the read, so a
failure here points somewhere very specific.

Usage:

    python3 tools/uinput_selftest.py            # nothing held (baseline)
    python3 tools/uinput_selftest.py --hold     # Ctrl+Alt held, as the hotkey does

Run it, focus the window you want to test, and select some text with the mouse.
"""

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "global_hotkeys")
)

import uinput  # noqa: E402

COUNTDOWN = 6
SETTLE_SECONDS = 0.3


def read_clipboard():
    for command in (
        ["wl-paste", "--no-newline"],
        ["xclip", "-out", "-selection", "clipboard"],
    ):
        try:
            result = subprocess.run(command, capture_output=True, timeout=2)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        return result.stdout.decode("utf-8", "replace")
    return None


def countdown(message):
    print(message)
    for remaining in range(COUNTDOWN, 0, -1):
        print(f"  injecting in {remaining}... ", end="\r", flush=True)
        time.sleep(1)
    print(" " * 40, end="\r")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--hold",
        action="store_true",
        help="hold Ctrl+Alt during the countdown; the addon releases them first",
    )
    args = parser.parse_args()

    try:
        keyboard = uinput.VirtualKeyboard()
    except OSError as e:
        print(f"Could not open {uinput.UINPUT_PATH}: {e}")
        return 1

    if args.hold:
        countdown(
            "Focus the target window, select text, then HOLD Ctrl+Alt "
            "and keep holding until this finishes."
        )
    else:
        countdown(
            "Focus the target window, select text, and hold no keys at all."
        )

    before = read_clipboard()
    if before is None:
        print("No clipboard helper found (need wl-clipboard or xclip).")
        keyboard.close()
        return 1

    if args.hold:
        # Exactly what the addon does: release everything, then tap Ctrl+C.
        keyboard.release(list(uinput.MODIFIER_CODES) + [uinput.KEY_C])
    keyboard.tap([uinput.KEY_LEFTCTRL, uinput.KEY_C])

    time.sleep(SETTLE_SECONDS)
    after = read_clipboard()
    keyboard.close()

    print(f"  before: {before[:120]!r}")
    print(f"  after : {after[:120]!r}")
    print()
    if after != before:
        print("RESULT: the copy landed. Injection works for this window.")
    else:
        print("RESULT: clipboard UNCHANGED -- the injected Ctrl+C did not copy.")
        if args.hold:
            print(
                "        Try again without --hold. If that works, the problem is\n"
                "        that releasing physically-held modifiers has no effect."
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
