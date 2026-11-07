"""Describe this computer for a test or benchmark report (docs/HARDWARE_TESTING.md).

    uv run python infrastructure/scripts/machine_report.py [OUT.json]

Prints (and optionally writes) the operating system, CPU, memory, GPUs, power
state and tool versions, so results from different machines can be compared.
Nothing is sent anywhere; it only runs read-only system queries.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def _run(command: list[str], timeout: float = 20) -> str:
    """Output of a read-only system query, or "" when it is unavailable."""
    if shutil.which(command[0]) is None:
        return ""
    try:
        done = subprocess.run(  # noqa: S603 - fixed, read-only queries
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip()


def _gpus() -> list[str]:
    system = platform.system()
    if system == "Darwin":
        out = _run(["system_profiler", "SPDisplaysDataType", "-json"])
        try:
            cards = json.loads(out).get("SPDisplaysDataType", [])
        except ValueError:
            return []
        return [
            " ".join(
                str(card.get(key, ""))
                for key in ("sppci_model", "spdisplays_vram", "sppci_cores")
                if card.get(key)
            )
            for card in cards
        ]
    if system == "Windows":
        out = _run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_VideoController | "
                "ForEach-Object { $_.Name + ' (driver ' + $_.DriverVersion + ')' }",
            ]
        )
        return [line for line in out.splitlines() if line.strip()]
    out = _run(["lspci"])
    return [line for line in out.splitlines() if "VGA" in line or "3D controller" in line]


def _power() -> dict[str, Any]:
    system = platform.system()
    if system == "Darwin":
        settings = _run(["pmset", "-g"])
        battery = _run(["pmset", "-g", "batt"]).splitlines()
        low_power = next(
            (line.split()[-1] == "1" for line in settings.splitlines() if "lowpowermode" in line),
            None,
        )
        return {"low_power_mode": low_power, "source": battery[0] if battery else None}
    if system == "Windows":
        return {"plan": _run(["powercfg", "/getactivescheme"])}
    governor = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    return {"cpu_governor": governor.read_text().strip() if governor.exists() else None}


def _cpu() -> str:
    system = platform.system()
    if system == "Darwin":
        return _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.processor()
    if system == "Linux":
        for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    return platform.processor()


def _blender() -> str:
    """Blender's version, found the way the app finds it (it is often not on PATH)."""
    try:
        from ai3d_engine.runtime import find_tool  # noqa: PLC0415

        path = find_tool("blender")
    except Exception:
        path = None
    if not path:
        return ""
    lines = _run([str(path), "--version"], timeout=90).splitlines()
    return lines[0] if lines else ""


def report() -> dict[str, Any]:
    import psutil  # noqa: PLC0415 - a backend dependency; imported once the report runs

    memory = psutil.virtual_memory()
    return {
        "os": f"{platform.system()} {platform.release()} ({platform.platform()})",
        "machine": platform.machine(),
        "cpu": _cpu(),
        "cores": {
            "physical": psutil.cpu_count(logical=False),
            "logical": psutil.cpu_count(logical=True),
        },
        "memory_gib": round(memory.total / 2**30, 1),
        "memory_available_gib": round(memory.available / 2**30, 1),
        "gpus": _gpus(),
        "power": _power(),
        "python": sys.version.split()[0],
        "node": _run(["node", "--version"]),
        "pnpm": _run(["pnpm", "--version"]),
        "blender": _blender(),
    }


def main(argv: list[str]) -> int:
    facts = report()
    text = json.dumps(facts, indent=2)
    print(text)
    if argv:
        Path(argv[0]).write_text(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
