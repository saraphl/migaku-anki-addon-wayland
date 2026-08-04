# Migaku Anki Addon — Wayland fork

A fork that makes the global lookup hotkeys work under Wayland. Tested on KDE Plasma / KWin.

In the upstream's implementation, when you bind your Anki browser lookups to something like Ctrl+Alt+C and then press that with the selected text, Migaku addon tries to transfer that over to Anki browser by simulating Ctrl+C and Ctrl+V key presses in succession. This however races with your already pressed keys, Ctrl key gets stuck pressed down (until the whole system or a process like `plasma-kwin_wayland` is restarted) and Anki stops responding. 

Upstream also copies the selected text and reads it back through X11, which fails on a Wayland session: the copy never reaches Wayland-native windows, and the clipboard reads back empty. This fork injects the copy through `/dev/uinput` instead, waits for you to release the hotkey before doing so, and reads the clipboard out of process.

## Requirements

**A clipboard helper.** Either `wl-clipboard` or `xclip` (or `xsel`); `wl-clipboard` is the better choice on Wayland. Only one is needed.

**Write access to `/dev/uinput`.** Run `getfacl /dev/uinput` — if you see a line like `user:yourname:rw-`, there's nothing to do. This is often already the case, since packages such as `steam-devices` and `game-devices-udev` grant it. Otherwise add a udev rule tagging the device with `uaccess`.

If either is missing, it's in the log at startup: `Virtual keyboard created` and `Clipboard read via <name>` mean things are working.

`tools/uinput_selftest.py` tests the copy and clipboard read with Anki closed, which helps when something breaks.

---

For the original readme, refer to [Migaku's repository](https://github.com/migaku-official/Migaku-Anki-Addon).
