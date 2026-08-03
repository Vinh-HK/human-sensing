"""Fabrique de sources."""

from __future__ import annotations

from .base import Source


def build_source(cfg: dict, channels: list[int]) -> Source:
    kind = (cfg.get("type") or "fake").lower()
    if kind == "fake":
        from .fake import FakeSource

        return FakeSource(cfg, channels)
    if kind == "limesdr":
        from .limesdr import LimeSDRSource

        return LimeSDRSource(cfg, channels)
    if kind == "esp32csi":
        from .esp32csi import Esp32CsiSource

        return Esp32CsiSource(cfg, channels)
    if kind == "replay":
        from .replay import ReplaySource

        return ReplaySource(cfg, channels)
    raise ValueError(
        f"Source inconnue : {kind!r} (attendu : fake, limesdr, esp32csi, replay)"
    )


__all__ = ["Source", "build_source"]
