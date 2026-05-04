from __future__ import annotations

import sys
from pathlib import Path

from .app import App


def main() -> None:
    # Look for config in the current directory, or parent of backup_manager package
    config_path = Path.cwd() / "backup_config.yaml"
    if not config_path.exists():
        # Try parent of the package directory
        pkg_dir = Path(__file__).resolve().parent.parent
        config_path = pkg_dir / "backup_config.yaml"

    if not config_path.exists():
        print(f"No backup_config.yaml found. Copy backup_config.example.yaml to backup_config.yaml and edit it.")
        print(f"Looked in: {Path.cwd()}")
        sys.exit(1)

    app = App(config_path)
    app.run()


if __name__ == "__main__":
    main()
