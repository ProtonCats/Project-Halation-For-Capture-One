#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
"""Click through short-lived XInput pointer devices on an X server, for the button-mapping repro.

Each cycle creates an XInput master, asks for its XTEST pointer to join the core pointer, presses and releases
button 1 through that device and removes the master at once. On Xvfb the clicks were seen arriving through the
new master, not the core pointer: the reattach does not take effect. Run it against a private X server only,
about 2 s after a Wine program starts there: Wine reads raw XInput events only in its desktop thread, and while
start-up keeps that thread busy, it reads each click after the device is gone. Usage: device_churn.py --display :N [--cycles 40] [--gap 0.3]. Needs xinput, libX11, libXi, libXtst.
"""
import argparse
import ctypes
import os
import subprocess
import time

x11 = ctypes.CDLL("libX11.so.6")
xi = ctypes.CDLL("libXi.so.6")
xtst = ctypes.CDLL("libXtst.so.6")
x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XFlush.argtypes = [ctypes.c_void_p]
x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
xi.XOpenDevice.restype = ctypes.c_void_p
xi.XOpenDevice.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
xi.XCloseDevice.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
xtst.XTestFakeDeviceButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int,
                                            ctypes.c_void_p, ctypes.c_int, ctypes.c_ulong]
# This side's own X errors (a device already gone) are counted, never fatal.
ERRORS = []
HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(lambda d, e: ERRORS.append(1) or 0)


def xinput(display, *args):
    return subprocess.run(["xinput", *args], env={**os.environ, "DISPLAY": display}, capture_output=True,
                          text=True, timeout=10)


def churn(display, cycles, gap, alive=lambda: True):
    """Run the cycles; returns how many completed. alive() is checked before each cycle."""
    x11.XSetErrorHandler(HANDLER)
    dpy = x11.XOpenDisplay(display.encode())
    if not dpy:
        raise SystemExit(f"cannot open display {display}")
    done = 0
    try:
        for i in range(cycles):
            if not alive():
                break
            name = f"churn{os.getpid()}x{i}"
            xinput(display, "create-master", name)
            r = xinput(display, "list", "--id-only", f"{name} XTEST pointer")
            if r.returncode or not r.stdout.strip().isdigit():
                continue
            dev = int(r.stdout.strip())
            xinput(display, "reattach", str(dev), "Virtual core pointer")
            handle = xi.XOpenDevice(dpy, dev)
            if handle:
                for press in (1, 0):
                    xtst.XTestFakeDeviceButtonEvent(dpy, handle, 1, press, None, 0, 0)
                xi.XCloseDevice(dpy, handle)
            x11.XFlush(dpy)
            xinput(display, "remove-master", f"{name} pointer")
            done = i + 1
            time.sleep(gap)
    finally:
        x11.XCloseDisplay(dpy)
    return done


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--display", required=True)
    ap.add_argument("--cycles", type=int, default=40)
    ap.add_argument("--gap", type=float, default=0.3)
    a = ap.parse_args()
    print(f"cycles completed: {churn(a.display, a.cycles, a.gap)}, own X errors: {len(ERRORS)}")
