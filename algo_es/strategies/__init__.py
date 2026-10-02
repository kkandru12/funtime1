"""Ported Apex Globex ES strategies.

Each module exposes `Strategy` with `__init__(config)` and
`on_bar(bar, state) -> Signal | None`.

See PORT_NOTES.md for what was ported, stripped, and changed.
"""
from . import bb2c, dma520, smacross, squeeze, vob
from .signal import Signal

STRATEGIES = {
    "vob": vob.Strategy,
    "squeeze": squeeze.Strategy,
    "bb2c": bb2c.Strategy,
    "dma520": dma520.Strategy,
    "smacross": smacross.Strategy,
}

__all__ = ["Signal", "STRATEGIES", "vob", "squeeze", "bb2c", "dma520", "smacross"]
