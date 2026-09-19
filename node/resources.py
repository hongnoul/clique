"""Resource probing (MVP implementation). Missing telemetry -> None."""

from __future__ import annotations

import os

from common.types import ResourceSnapshot

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


def probe() -> ResourceSnapshot:
    snap = ResourceSnapshot()
    if psutil is None:
        return snap
    try:
        snap.cpu_percent = psutil.cpu_percent(interval=None)
    except Exception:
        pass
    try:
        vm = psutil.virtual_memory()
        snap.memory_total_mb = int(vm.total / 1_048_576)
        snap.memory_available_mb = int(vm.available / 1_048_576)
    except Exception:
        pass
    try:
        batt = psutil.sensors_battery()
        snap.on_battery = (not batt.power_plugged) if batt else None
    except Exception:
        pass
    try:
        snap.load_avg_1m = os.getloadavg()[0]
    except OSError:
        pass
    return snap
