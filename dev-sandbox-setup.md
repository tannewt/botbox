# `/dev/sandbox` — USB passthrough for bubblewrap

Give a bubblewrap-sandboxed process access to selected USB devices with
stable names and hot-plug support, by `mknod`ing real device nodes into a
dedicated directory and bind-mounting that directory into the sandbox.

## Files

**`/etc/tmpfiles.d/usb-sandbox.conf`** — ensures the directory exists at
boot (devtmpfs is recreated each boot):

```
d /dev/sandbox 0755 root root -
```

**`/etc/udev/rules.d/99-sandbox.rules`** — one rule per line, no
continuations:

```udev
SUBSYSTEM=="tty", SUBSYSTEMS=="usb", KERNEL=="ttyACM*|ttyUSB*", ENV{ID_SERIAL}=="?*", ACTION=="add", RUN+="/bin/sh -c 'n=/dev/sandbox/$env{ID_BUS}-$env{ID_SERIAL}-if$env{ID_USB_INTERFACE_NUM}; mknod \"$n\" c %M %m; chgrp uucp \"$n\"; chmod 660 \"$n\"'"
SUBSYSTEM=="tty", SUBSYSTEMS=="usb", KERNEL=="ttyACM*|ttyUSB*", ENV{ID_SERIAL}=="?*", ACTION=="remove", RUN+="/bin/rm -f /dev/sandbox/$env{ID_BUS}-$env{ID_SERIAL}-if$env{ID_USB_INTERFACE_NUM}"
```

Naming mirrors `/dev/serial/by-id/`. Use `dialout` instead of `uucp` on
Debian/Ubuntu/Fedora (check with `getent group | grep -iE 'dialout|uucp'`).

## Apply

```sh
sudo systemd-tmpfiles --create /etc/tmpfiles.d/usb-sandbox.conf
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=tty --action=add
ls -la /dev/sandbox/
```

Expected: real character devices, not symlinks:

```
crw-rw---- 1 root uucp 166, 0 ... usb-Espressif_USB_JTAG_serial_debug_unit_E4...-if00
```

## Bind into bubblewrap

```
--dev-bind /dev/sandbox /dev/sandbox
```

One bind covers every current and future device the rules drop in.
Hot-plug works because directory binds share dentries — new `mknod`s on the
host appear inside the sandbox automatically.

## Extending

For raw USB (esptool, openocd, libusb), add a rule that mknods
`/dev/bus/usb/<bus>/<dev>` into `/dev/sandbox` under a different prefix
(e.g. `usbraw-<serial>`). Same bind mount carries it.

## Troubleshooting

- **Rule doesn't fire**: `udevadm test $(udevadm info -q path -n /dev/ttyACM0) 2>&1 | grep -E 'sandbox|ID_SERIAL|RUN'`
- **`Invalid key/value pair`**: literal newline inside the `RUN+=` quoted
  string. Keep each rule on one physical line.
- **`ID_SERIAL` empty**: board has no iSerial descriptor; fall back to
  `$env{ID_VENDOR_ID}:$env{ID_MODEL_ID}` plus `ID_PATH`.
- **Permission denied**: `sudo usermod -aG uucp $USER` (or `dialout`), then
  re-login.
