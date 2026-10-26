"""Headless, reproducible fabrication panel builder for KiCad 10 boards.

The core takes board files plus a config and returns board files. It must not
import ``wx`` and must not import ``pcbnew``; ``eedesigncore`` is the only
reader and writer of board data.
"""

__version__ = "0.1.0"
