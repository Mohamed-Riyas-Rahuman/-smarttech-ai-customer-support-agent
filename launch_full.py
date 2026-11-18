"""Start the managed SmartTech API and frontend using the project launcher."""
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
START_SCRIPT = ROOT / "scripts" / "start.ps1"


def main() -> int:
    if not START_SCRIPT.is_file():
        print(f"SmartTech startup script not found: {START_SCRIPT}", file=sys.stderr)
        return 1

    result = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(START_SCRIPT),
        ],
        cwd=ROOT,
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
