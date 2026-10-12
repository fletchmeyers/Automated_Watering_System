'''
CircuitPython

The chip's watchdog, so a node out in the garden heals itself: if code.py
ever stops feeding it for TIMEOUT_S (a sensor or bus that hangs, say from
corroded pins), the board resets and starts again. code.py feeds it every
pass of its loop, and every packet sent feeds it too (communication_garden
and wifi_link call feed()), so a long sync can't trip it.

A file named no_watchdog on the CIRCUITPY drive turns it off, for working
at the console without the board resetting under you.

Written by Fletcher Meyers
October 2026
'''

import os

TIMEOUT_S = 8          # the RP2040's longest is about 8.3 s
OFF_FILE  = "no_watchdog"

_dog = None


def start():
    '''Start the watchdog (unless OFF_FILE is on the drive); True if running.
    Says if the last reset was the watchdog's.'''
    global _dog
    try:
        import microcontroller
        if microcontroller.cpu.reset_reason == microcontroller.ResetReason.WATCHDOG:
            print("[WATCHDOG] Restarted: the code had stopped (hung) before this reset.")
    except Exception:
        pass
    if OFF_FILE in os.listdir("/"):
        print(f"[WATCHDOG] Off ({OFF_FILE} is on the CIRCUITPY drive).")
        return False
    try:
        from microcontroller import watchdog
        from watchdog import WatchDogMode
        watchdog.timeout = TIMEOUT_S
        watchdog.mode = WatchDogMode.RESET
        watchdog.feed()
        _dog = watchdog
        print(f"[WATCHDOG] On: the board resets if the code stops for {TIMEOUT_S} s.")
        return True
    except Exception as e:
        print(f"[WATCHDOG] Couldn't start it: {e}")
        return False


def feed():
    if _dog is not None:
        _dog.feed()


def running():
    return _dog is not None


def stop():
    '''Before handing the console over (Ctrl+C): don't reset under the user.'''
    global _dog
    if _dog is not None:
        try:
            _dog.deinit()
        except Exception as e:
            print(f"[WATCHDOG] Couldn't stop it: {e}")
        _dog = None
