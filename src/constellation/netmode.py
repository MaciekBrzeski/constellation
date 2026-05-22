"""Network-mode control for the constellation brain (the Switch).

A thin wrapper over ``scripts/switch/mode-{ap,wifi,status}.sh`` so both the
desktop panel and the web dashboard can query and toggle the brain's Wi-Fi
mode through one code path. The shell scripts stay the single source of
truth for the nmcli logic.

Switching mode may trigger a polkit prompt the first time the
``constellation-ap`` connection is created (creating a *system* connection
needs admin auth). From the desktop session a polkit agent handles that;
a headless service may not have one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

MODE_AP = "ap"
MODE_WIFI = "wifi"
MODE_UNKNOWN = "unknown"


def _scripts_dir() -> Path | None:
    """Locate ``scripts/switch/`` relative to this package, or ``None``.

    Editable install: the package lives at ``<repo>/src/constellation``, so
    the scripts sit at ``<repo>/scripts/switch``. Walk parents to find them.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "scripts" / "switch"
        if (candidate / "mode-status.sh").is_file():
            return candidate
    return None


def scripts_available() -> bool:
    """True when the mode-switch scripts can be found (GUIs hide the tab if not)."""
    return _scripts_dir() is not None


def _run(script: str, args: list[str], timeout: float) -> tuple[bool, str]:
    """Run ``scripts/switch/<script>`` and return ``(ok, combined_output)``."""
    scripts = _scripts_dir()
    if scripts is None:
        return False, "scripts/switch not found next to the constellation package"
    try:
        proc = subprocess.run(
            ["bash", str(scripts / script), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"{script} timed out after {timeout:.0f}s"
    except OSError as exc:
        return False, f"could not run {script}: {exc}"
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, output


def current_mode(timeout: float = 5.0) -> str:
    """Return the brain's current Wi-Fi mode: ``ap``, ``wifi`` or ``unknown``."""
    ok, output = _run("mode-status.sh", [], timeout)
    if not ok:
        return MODE_UNKNOWN
    word = output.splitlines()[-1].strip() if output else ""
    return word if word in (MODE_AP, MODE_WIFI) else MODE_UNKNOWN


def switch_to_ap(timeout: float = 60.0) -> tuple[bool, str]:
    """Bring up the constellation AP. Returns ``(ok, output)``. Kills internet."""
    return _run("mode-ap.sh", ["--no-pause"], timeout)


def switch_to_wifi(timeout: float = 60.0) -> tuple[bool, str]:
    """Leave AP mode and reconnect to normal Wi-Fi. Returns ``(ok, output)``."""
    return _run("mode-wifi.sh", ["--no-pause"], timeout)
