'''
CircuitPython boot.py — runs once at power-up, before code.py.

Turns on a second USB serial port ("data") next to the usual console, so
the Pi can pull this node's SD log over a USB cable (usb_sync.py on the Pi)
without disturbing the console's print output. Changes here only take
effect after a hard reset (unplug, or the reset button).
'''

import usb_cdc

usb_cdc.enable(console=True, data=True)
