"""A minimal writer for ``/dev/uinput``.

python-evdev does all of this and more, but it is a C extension: vendoring it
would tie ``lib/linux`` to a single CPython ABI, the way ``lib/macos_3xx``
already is, and would break for anyone running a different Anki build. Almost
all of that library is device enumeration, capability parsing and keyboard
layout handling, none of which we need -- we only ever release a few keys and
tap Ctrl+C. That subset is a handful of ioctls and a 24 byte struct, so we
talk to the kernel directly and keep ``lib/linux`` pure Python.

Writing to ``/dev/uinput`` requires permission. It is root-only by default,
though many systems already grant it: Steam and several input utilities ship
udev rules tagging the device for ``uaccess``, which gives the active local
session user an ACL. Check with ``getfacl /dev/uinput`` -- a ``user:<you>:rw-``
line means it is already handled.

Otherwise, add a rule in ``/etc/udev/rules.d/`` (yours, and not package-owned):

    KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"

Note that ``uaccess`` grants access to the *active local session*, so this does
not apply over SSH.
"""

import ctypes
import fcntl
import logging
import os
import struct
import time

logger = logging.getLogger("migaku.hotkeys.uinput")

UINPUT_PATH = "/dev/uinput"

# The compositor has to enumerate a newly created device before it will route
# its events. Without this pause the first events after creation are silently
# dropped, which is why creating the device per keypress is unreliable.
DEVICE_SETTLE_SECONDS = 0.2

# linux/input-event-codes.h
EV_SYN = 0x00
EV_KEY = 0x01
SYN_REPORT = 0
BUS_VIRTUAL = 0x06

UINPUT_MAX_NAME_SIZE = 80

# struct input_event: struct timeval (two longs), then u16 type, u16 code and
# s32 value. Native sizing keeps this correct on both 32 and 64 bit.
INPUT_EVENT_FORMAT = "llHHi"


class InputId(ctypes.Structure):
    _fields_ = [
        ("bustype", ctypes.c_uint16),
        ("vendor", ctypes.c_uint16),
        ("product", ctypes.c_uint16),
        ("version", ctypes.c_uint16),
    ]


class UinputSetup(ctypes.Structure):
    _fields_ = [
        ("id", InputId),
        ("name", ctypes.c_char * UINPUT_MAX_NAME_SIZE),
        ("ff_effects_max", ctypes.c_uint32),
    ]


# The _IOW/_IO macros from asm-generic/ioctl.h, which encode direction, a type
# letter, a sequence number and the argument size into the request number.
_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_NONE = 0
_IOC_WRITE = 1

_UINPUT_IOCTL_BASE = ord("U")


def _ioc(direction, number, size):
    return (
        (direction << _IOC_DIRSHIFT)
        | (_UINPUT_IOCTL_BASE << _IOC_TYPESHIFT)
        | (number << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


UI_DEV_CREATE = _ioc(_IOC_NONE, 1, 0)
UI_DEV_DESTROY = _ioc(_IOC_NONE, 2, 0)
UI_DEV_SETUP = _ioc(_IOC_WRITE, 3, ctypes.sizeof(UinputSetup))
UI_SET_EVBIT = _ioc(_IOC_WRITE, 100, ctypes.sizeof(ctypes.c_int))
UI_SET_KEYBIT = _ioc(_IOC_WRITE, 101, ctypes.sizeof(ctypes.c_int))


KEY_LEFTCTRL = 29
KEY_RIGHTCTRL = 97
KEY_LEFTSHIFT = 42
KEY_RIGHTSHIFT = 54
KEY_LEFTALT = 56
KEY_RIGHTALT = 100
KEY_LEFTMETA = 125
KEY_RIGHTMETA = 126
KEY_C = 46

#: Every modifier, both sides. We release all of them before synthesizing a
#: copy: the hotkey only records that e.g. Ctrl was held, not which Ctrl, and
#: the user may be holding something the binding does not mention.
MODIFIER_CODES = (
    KEY_LEFTCTRL,
    KEY_RIGHTCTRL,
    KEY_LEFTSHIFT,
    KEY_RIGHTSHIFT,
    KEY_LEFTALT,
    KEY_RIGHTALT,
    KEY_LEFTMETA,
    KEY_RIGHTMETA,
)


def _build_key_codes():
    """Maps the key names produced by KeyboardHandler.parse_raw_key to codes."""
    codes = {}

    for row, first in (("qwertyuiop", 16), ("asdfghjkl", 30), ("zxcvbnm", 44)):
        for offset, char in enumerate(row):
            codes[char] = first + offset

    for offset, char in enumerate("1234567890"):
        codes[char] = 2 + offset

    for offset in range(10):  # f1 - f10
        codes["f{}".format(offset + 1)] = 59 + offset
    codes["f11"] = 87
    codes["f12"] = 88
    for offset in range(8):  # f13 - f20
        codes["f{}".format(offset + 13)] = 183 + offset

    codes.update(
        {
            "-": 12,
            "=": 13,
            "[": 26,
            "]": 27,
            ";": 39,
            "'": 40,
            "`": 41,
            "\\": 43,
            ",": 51,
            ".": 52,
            "/": 53,
            "esc": 1,
            "backspace": 14,
            "tab": 15,
            "enter": 28,
            "ctrl": KEY_LEFTCTRL,
            "ctrl_l": KEY_LEFTCTRL,
            "ctrl_r": KEY_RIGHTCTRL,
            "shift": KEY_LEFTSHIFT,
            "shift_l": KEY_LEFTSHIFT,
            "shift_r": KEY_RIGHTSHIFT,
            "alt": KEY_LEFTALT,
            "alt_l": KEY_LEFTALT,
            "alt_r": KEY_RIGHTALT,
            "alt_gr": KEY_RIGHTALT,
            "cmd": KEY_LEFTMETA,
            "cmd_l": KEY_LEFTMETA,
            "cmd_r": KEY_RIGHTMETA,
            "space": 57,
            "caps_lock": 58,
            "num_lock": 69,
            "scroll_lock": 70,
            "print_screen": 99,
            "home": 102,
            "up": 103,
            "page_up": 104,
            "left": 105,
            "right": 106,
            "end": 107,
            "down": 108,
            "page_down": 109,
            "insert": 110,
            "delete": 111,
            "media_volume_mute": 113,
            "media_volume_down": 114,
            "media_volume_up": 115,
            "pause": 119,
            "menu": 127,
            "media_next": 163,
            "media_play_pause": 164,
            "media_previous": 165,
        }
    )
    return codes


KEY_CODES = _build_key_codes()


def key_code(name):
    """Resolves a KeySequence key name to a Linux key code, or None."""
    if not name:
        return None
    return KEY_CODES.get(name.lower())


class VirtualKeyboard:
    """A virtual keyboard device held open for the lifetime of the addon.

    Events are written at the kernel input layer, below the display server, so
    they reach Wayland-native windows and are subject to the compositor's own
    key state tracking rather than being injected into it sideways.
    """

    def __init__(self, name="Migaku Anki virtual keyboard"):
        self._fd = os.open(UINPUT_PATH, os.O_WRONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self._fd, UI_SET_EVBIT, EV_KEY)
            for code in sorted(set(KEY_CODES.values())):
                fcntl.ioctl(self._fd, UI_SET_KEYBIT, code)

            setup = UinputSetup()
            setup.id.bustype = BUS_VIRTUAL
            setup.id.vendor = 0x1234
            setup.id.product = 0x5678
            setup.id.version = 1
            setup.name = name.encode("utf-8")[: UINPUT_MAX_NAME_SIZE - 1]
            setup.ff_effects_max = 0

            fcntl.ioctl(self._fd, UI_DEV_SETUP, setup)
            fcntl.ioctl(self._fd, UI_DEV_CREATE)
        except OSError:
            os.close(self._fd)
            self._fd = None
            raise

        time.sleep(DEVICE_SETTLE_SECONDS)
        logger.info("Virtual keyboard created")

    def _write(self, event_type, code, value):
        os.write(
            self._fd, struct.pack(INPUT_EVENT_FORMAT, 0, 0, event_type, code, value)
        )

    def _sync(self):
        self._write(EV_SYN, SYN_REPORT, 0)

    def release(self, codes):
        """Sends a release for each code, as its own event frame.

        Releasing a key that is not held is harmless, and releasing the keys
        the user is physically holding is what keeps the compositor's modifier
        state balanced -- an unmatched press is what leaves Ctrl stuck.
        """
        for code in codes:
            self._write(EV_KEY, code, 0)
        self._sync()

    def tap(self, codes):
        """Presses each code in order, then releases them in reverse.

        Every state change gets its own SYN_REPORT. Events between two
        SYN_REPORTs are delivered as one simultaneous batch, so pressing and
        releasing a key inside a single frame describes a keystroke of zero
        duration -- real hardware can never produce that, and applications are
        free to collapse or drop it. Separate frames make this an ordinary
        sequence of keystrokes.
        """
        for code in codes:
            self._write(EV_KEY, code, 1)
            self._sync()
        for code in reversed(codes):
            self._write(EV_KEY, code, 0)
            self._sync()

    def close(self):
        if self._fd is None:
            return
        try:
            fcntl.ioctl(self._fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self._fd)
        self._fd = None

    def __del__(self):
        try:
            self.close()
        except Exception:  # pragma: no cover - interpreter shutdown
            pass
