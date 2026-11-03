"""Produce a clean source ZIP; never include private data or the ECNU reference."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(__file__).resolve().parents[1]
destination = root / "dist" / "jnu-network-keeper.zip"
destination.parent.mkdir(exist_ok=True)
with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
    for folder in ("jnu_network_keeper", "deploy", "tests", "scripts"):
        for file in sorted((root / folder).rglob("*")):
            if file.is_file() and "__pycache__" not in file.parts and file.suffix not in (".pyc", ".pyo"):
                archive.write(file, file.relative_to(root))
    for name in ("README.md", "STARTUP.md", "LINUX-RECOVERY.md", "VALIDATION.md", "config.example.json", ".gitignore", ".gitattributes", "windows.ps1", "start-windows.cmd", "setup-windows.ps1", "setup-linux.sh", "test-linux.sh", "uninstall-linux.sh", "check-cleanup-linux.sh", "cleanup-linux.py"):
        archive.write(root / name, name)
print(destination)
