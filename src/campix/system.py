import platform
import shutil
import time
from pathlib import Path

import psutil


class Telemetry:
    def __init__(self, settings):
        self.started = time.monotonic()
        self.settings = settings
        self.process = psutil.Process()
        self.process.cpu_percent()
        psutil.cpu_percent()

    def read(self):
        disk = shutil.disk_usage(self.settings.data_dir)
        memory = psutil.virtual_memory()
        temperature = None
        thermal = Path("/sys/class/thermal/thermal_zone0/temp")
        if thermal.is_file():
            try:
                temperature = float(thermal.read_text()) / 1000
            except (OSError, ValueError):
                pass
        model = platform.machine()
        dt_model = Path("/proc/device-tree/model")
        if dt_model.is_file():
            model = dt_model.read_text().strip("\x00\n")
        return {
            "platform": {"system": platform.system(), "arch": platform.machine(), "model": model},
            "uptime_seconds": round(time.monotonic() - self.started),
            "host_uptime_seconds": round(time.time() - psutil.boot_time()),
            "cpu_percent": psutil.cpu_percent(),
            "process_cpu_percent": self.process.cpu_percent(),
            "memory_used_bytes": memory.used,
            "memory_total_bytes": memory.total,
            "process_memory_bytes": self.process.memory_info().rss,
            "disk_used_bytes": disk.used,
            "disk_total_bytes": disk.total,
            "disk_free_bytes": disk.free,
            "temperature_c": temperature,
            "demo": self.settings.demo,
        }
