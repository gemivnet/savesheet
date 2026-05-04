"""Double-click launcher for Backup Manager. Uses .pyw to avoid a console window."""
import subprocess
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
subprocess.Popen(
    [here / ".venv" / "Scripts" / "pythonw.exe", "-m", "backup_manager"],
    cwd=str(here),
)
