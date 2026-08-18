#!/usr/bin/env pythonw
"""Double-clickable launcher for running USBme Photo Cast from source on Windows.

Double-clicking this file runs the app with pythonw.exe, so no console window
appears. It's equivalent to:  pythonw usbme_photo_cast.py

(For the packaged, no-Python-needed version, grab the .exe from the Releases page.)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usbme_photo_cast

usbme_photo_cast.App().mainloop()
