import logging
import shutil
import time
from aqt.qt import QObject, QTimer, QProcess
from aqt import mw
from magicy.keyboard import Key, Controller
from anki.utils import is_mac, is_win, is_lin

from .. import config
from .. import util

from .key_sequence import KeySequence
from .keyboard import KeyboardHandler

if is_lin:
    from . import uinput
else:
    uinput = None

logger = logging.getLogger("migaku.hotkeys")

# Wayland only hands the clipboard to the *focused* client, and Anki is never
# focused when a global hotkey fires. Qt's clipboard read then blocks the GUI
# thread waiting for data that never arrives, freezing the app. These helpers
# read it out of process instead -- via the compositor's data-control protocol
# for wl-paste, or Xwayland's CLIPBOARD for xclip/xsel -- so neither the focus
# requirement nor a hang can reach the GUI thread.
CLIPBOARD_COMMANDS = [
    ["wl-paste", "--no-newline"],
    ["xclip", "-out", "-selection", "clipboard"],
    ["xsel", "--clipboard"],
]

# A helper that cannot deliver tends to hang rather than fail, so cap it.
CLIPBOARD_TIMEOUT_MS = 750

# Qt6 uses scoped enums, Qt5 (Anki <= 2.1.49) exposes the members on the class.
try:
    PROCESS_NOT_RUNNING = QProcess.ProcessState.NotRunning
except AttributeError:  # pragma: no cover - only hit on Qt5
    PROCESS_NOT_RUNNING = QProcess.NotRunning

# How long to let the source application act on the synthesized copy.
COPY_SETTLE_MS = 100

# The hotkey fires on press, while its keys are still physically held, and a
# held Alt turns the injected Ctrl+C into Ctrl+Alt+C, which copies nothing.
# Faking a release does not help: libinput tracks key state per device and
# discards a release for a key that device never pressed, so releasing keys
# held on the real keyboard is silently ignored. Waiting for the user to let go
# is the only thing that actually clears the modifiers -- and as a bonus it
# means we never inject a press on top of a held key, which is what used to
# strand Ctrl until the session restarted.
KEY_RELEASE_POLL_MS = 15
KEY_RELEASE_TIMEOUT_SECONDS = 2.0

# Injecting at the kernel input layer is what makes the copy reach Wayland
# windows, but it also means our own events come back through the listener as
# ordinary hardware input. The injected Ctrl+C can then re-match the hotkey
# that produced it, and since releasing the trigger key clears its auto-repeat
# record, nothing stops the next one: a self-sustaining loop that fired ~3000
# times a second in testing. Actions stay ignored for a window afterwards, so
# the listener never reacts to input this addon generated.
SELF_INJECT_BLOCK_SECONDS = 0.25


def available_clipboard_commands():
    return [c for c in CLIPBOARD_COMMANDS if shutil.which(c[0])]


class HotkeyHandlerBase(QObject):
    hotkeys = [
        (
            "open_dict",
            KeySequence("f", KeySequence.Ctrl | KeySequence.Alt),
            "Open dictionary",
        ),
        (
            "search_dict",
            KeySequence("d", KeySequence.Ctrl | KeySequence.Alt),
            "Search selected text in dictionary",
        ),
        (
            "set_sentence",
            KeySequence("s", KeySequence.Ctrl | KeySequence.Alt),
            "Send sentence to card creator",
        ),
        (
            "add_definition",
            KeySequence("g", KeySequence.Ctrl | KeySequence.Alt),
            "Send definition to card creator",
        ),
        (
            "search_collection",
            KeySequence("b", KeySequence.Ctrl | KeySequence.Alt),
            "Search selected text in card collection",
        ),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)

        logger.info("Initializing HotkeyHandler...")

        self.selected_text_handler = None
        self.ignore_actions_until = 0.0

        self.clipboard_commands = available_clipboard_commands() if is_lin else []
        self.clipboard_process = None
        if is_lin:
            logger.info(
                "Clipboard helpers available: "
                + (", ".join(c[0] for c in self.clipboard_commands) or "none")
            )

        self.virtual_keyboard = None
        if is_lin:
            try:
                self.virtual_keyboard = uinput.VirtualKeyboard()
            except OSError as e:
                logger.warning(
                    f"Could not open {uinput.UINPUT_PATH} ({e}); falling back to "
                    "X11 key injection, which is unreliable under Wayland. See "
                    "the udev rule in global_hotkeys/uinput.py."
                )

        self.keyboard_controller = Controller()

        logger.info("Creating KeyboardHandler...")
        self.keyboard_handler = KeyboardHandler()
        self.keyboard_handler.action_fired.connect(self.on_action_fired)
        logger.info(f"KeyboardHandler created. Available: {self.keyboard_handler.is_available()}")

        logger.info("Registering hotkey actions...")
        for action, default_sequence, _ in self.hotkeys:
            sequence_tuple = config.get("hotkey_" + action)
            if sequence_tuple:
                sequence = KeySequence(*sequence_tuple)
                self.keyboard_handler.add_action(action, sequence)
            else:
                sequence = default_sequence
                self.keyboard_handler.add_action(action, sequence)
        logger.info(f"Registered {len(self.hotkeys)} hotkey actions")

    def is_available(self):
        return self.keyboard_handler.is_available()

    def on_action_fired(self, action):
        if time.monotonic() < self.ignore_actions_until:
            # Input we synthesized ourselves, echoed back by the listener.
            return

        if action == "open_dict":
            mw.migaku_connection.open_dict()
            self.focus_dictionary()
        if action in [
            "search_dict",
            "set_sentence",
            "add_definition",
            "search_collection",
        ]:
            self.request_selected_text(action)

    def configured_sequence(self, action):
        """The key sequence currently bound to an action, config or default."""
        sequence_tuple = config.get("hotkey_" + action)
        if sequence_tuple:
            return KeySequence(*sequence_tuple)
        for name, default_sequence, _ in self.hotkeys:
            if name == action:
                return default_sequence
        return None

    def hotkey_is_released(self, sequence):
        """Whether the keyboard is clear enough to inject a clean Ctrl+C.

        The trigger key comes from the configured binding rather than being
        hardcoded, so rebinding the hotkey keeps this correct.
        """
        if self.keyboard_handler.modifiers != 0:
            return False
        if sequence is not None and sequence.key:
            return sequence.key not in self.keyboard_handler.key_press_times
        return True

    def copy_when_released(self, action, deadline):
        """Waits for the hotkey to be let go, then synthesizes the copy."""
        if not self.hotkey_is_released(self.configured_sequence(action)):
            if time.monotonic() < deadline:
                QTimer.singleShot(
                    KEY_RELEASE_POLL_MS,
                    lambda: self.copy_when_released(action, deadline),
                )
                return
            # Proceeding is very likely to copy nothing, but a modifier state
            # that never clears would otherwise disable lookups entirely, so
            # degrade loudly rather than going silent.
            logger.warning(
                "Hotkey still held after "
                f"{KEY_RELEASE_TIMEOUT_SECONDS}s; copying anyway"
            )

        # Set before injecting, so the echo cannot race ahead of the guard.
        self.ignore_actions_until = time.monotonic() + SELF_INJECT_BLOCK_SECONDS
        self.virtual_keyboard.tap([uinput.KEY_LEFTCTRL, uinput.KEY_C])
        QTimer.singleShot(COPY_SETTLE_MS, self.read_clipboard)

    def request_selected_text(self, action):
        # One line per lookup. Kept deliberately: a burst here is the signature
        # of the addon re-triggering on its own injected input.
        self._request_count = getattr(self, "_request_count", 0) + 1
        logger.info(f"request_selected_text #{self._request_count}: {action}")

        self.selected_text_handler = action

        if is_lin and self.virtual_keyboard is not None:
            self.copy_when_released(
                action, time.monotonic() + KEY_RELEASE_TIMEOUT_SECONDS
            )
            return

        modifier_keys = [
            Key.alt_gr,
            Key.alt,
            Key.alt_l,
            Key.alt_r,
            Key.cmd,
            Key.cmd_l,
            Key.cmd_r,
            Key.ctrl,
            Key.ctrl_l,
            Key.ctrl_r,
            Key.shift,
            Key.shift_l,
            Key.shift_r,
        ]

        if not is_lin:
            for key in modifier_keys:
                self.keyboard_controller.release(key)

        with self.keyboard_controller.pressed(Key.cmd if is_mac else Key.ctrl):
            self.keyboard_controller.press("c")
            self.keyboard_controller.release("c")

        QTimer.singleShot(100, self.handle_selected_text)

    def read_clipboard(self, index=0):
        """Read the clipboard out of process, without blocking the GUI thread.

        Helpers are tried in order: one that is installed but cannot deliver
        typically hangs or returns nothing rather than failing outright, so
        every outcome falls through to the next candidate. If none are
        installed we fall back to Qt, which is correct on X11 and merely
        unreliable on Wayland.
        """
        self.cancel_clipboard_read()

        if index >= len(self.clipboard_commands):
            if index > 0:
                logger.info("Clipboard unreadable from every helper")
            else:
                logger.debug("No clipboard helper installed; using Qt")
                self.handle_selected_text()
            return

        command = self.clipboard_commands[index]
        process = QProcess(self)
        self.clipboard_process = process

        process.finished.connect(lambda *_: self.on_clipboard_finished(process, index))
        process.errorOccurred.connect(
            lambda *_: self.on_clipboard_error(process, index)
        )
        QTimer.singleShot(
            CLIPBOARD_TIMEOUT_MS, lambda: self.on_clipboard_timeout(process, index)
        )

        process.start(command[0], command[1:])

    def cancel_clipboard_read(self):
        process = self.clipboard_process
        self.clipboard_process = None
        if process is not None:
            self.dispose_process(process)

    @staticmethod
    def dispose_process(process):
        """Delete a QProcess without Qt complaining that it is still running.

        kill() only *requests* termination, so deleting straight afterwards
        destroys the wrapper while the child is still alive. Waiting for
        finished() to fire instead keeps the reaping off the GUI thread.

        Note there is deliberately no disconnect() here: a wildcard disconnect
        also tears down the internal `destroyed` connections Qt relies on, and
        it warns about that every time. Stale handlers are harmless anyway,
        because each one checks that it still owns self.clipboard_process.
        """
        if process.state() == PROCESS_NOT_RUNNING:
            process.deleteLater()
            return

        process.finished.connect(process.deleteLater)
        process.kill()

    def note_clipboard_source(self, index):
        """Log which helper actually delivers, the first time it changes."""
        name = self.clipboard_commands[index][0]
        if getattr(self, "_clipboard_source", None) != name:
            self._clipboard_source = name
            logger.info(f"Clipboard read via {name}")

    def on_clipboard_finished(self, process, index):
        if self.clipboard_process is not process:
            return
        self.clipboard_process = None

        error = bytes(process.readAllStandardError()).decode("utf-8", "replace")
        text = bytes(process.readAllStandardOutput()).decode("utf-8", "replace")
        self.dispose_process(process)

        if error.strip():
            logger.debug(f"{self.clipboard_commands[index][0]}: {error.strip()}")

        if not text:
            self.read_clipboard(index + 1)
            return

        self.note_clipboard_source(index)
        self.dispatch_selected_text(text)

    def on_clipboard_error(self, process, index):
        if self.clipboard_process is not process:
            return
        self.clipboard_process = None
        logger.warning(
            f"{self.clipboard_commands[index][0]} failed: {process.errorString()}"
        )
        self.dispose_process(process)
        self.read_clipboard(index + 1)

    def on_clipboard_timeout(self, process, index):
        if self.clipboard_process is not process:
            return
        logger.warning(f"{self.clipboard_commands[index][0]} timed out")
        self.read_clipboard(index + 1)

    def handle_selected_text(self):
        self.dispatch_selected_text(mw.app.clipboard().text())

    def dispatch_selected_text(self, text):
        action = self.selected_text_handler
        if not action:
            return
        self.selected_text_handler = None

        if action == "search_dict":
            mw.migaku_connection.search_dict(text)
            self.focus_dictionary()
        elif action == "set_sentence":
            mw.migaku_connection.set_sentence(text)
            self.focus_dictionary()
        elif action == "add_definition":
            mw.migaku_connection.add_definition(text)
            self.focus_dictionary()
        elif action == "search_collection":
            util.open_browser(text)

    def focus_dictionary(self):
        # Implemented in derived classes if required
        pass


if is_win:
    import ctypes

    class HotkeyHandler(HotkeyHandlerBase):
        def focus_dictionary(self, retry_count=5):
            enum_windows_proc = ctypes.WINFUNCTYPE(
                ctypes.c_bool, ctypes.c_int, ctypes.POINTER(ctypes.c_int)
            )

            did_focus = False

            # window_handle: hwnd
            def foreach_window(window_handle: int, _lparam):
                if not ctypes.windll.user32.IsWindowVisible(window_handle):
                    return True

                nonlocal did_focus
                title_length = ctypes.windll.user32.GetWindowTextLengthW(window_handle)
                title_buff = ctypes.create_unicode_buffer(title_length + 1)
                ctypes.windll.user32.GetWindowTextW(
                    window_handle, title_buff, title_length + 1
                )

                title = title_buff.value
                if title == "Migaku Dictionary":
                    ctypes.windll.user32.SetForegroundWindow(window_handle)
                    did_focus = True
                    return False
                return True

            ctypes.windll.user32.EnumWindows(enum_windows_proc(foreach_window), 0)

            if retry_count > 0 and not did_focus:
                QTimer.singleShot(100, lambda: self.focus_dictionary(retry_count - 1))

            return did_focus

elif is_mac:

    class HotkeyHandler(HotkeyHandlerBase):
        hotkeys = [
            ("open_dict", KeySequence(), "Open dictionary"),
            ("search_dict", KeySequence(), "Search selected text in dictionary"),
            ("set_sentence", KeySequence(), "Send sentence to card creator"),
            ("add_definition", KeySequence(), "Send definition to card creator"),
            (
                "search_collection",
                KeySequence(),
                "Search selected text in card collection",
            ),
        ]

else:
    # Dictionary focus not required
    class HotkeyHandler(HotkeyHandlerBase):
        pass
