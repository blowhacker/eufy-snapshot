"""Install a pinned official VGGT source tree without dependency metadata."""

from __future__ import annotations

import shutil
import site
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path


def main(commit: str) -> None:
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise SystemExit("expected a full lowercase Git commit")
    url = f"https://github.com/facebookresearch/vggt/archive/{commit}.tar.gz"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        archive = root / "vggt.tar.gz"
        urllib.request.urlretrieve(url, archive)
        source = root / "source"
        source.mkdir()
        with tarfile.open(archive, "r:gz") as bundle:
            for member in bundle.getmembers():
                target = (source / member.name).resolve()
                target.relative_to(source.resolve())
            bundle.extractall(source, filter="data")
        checkout = next(source.iterdir())
        destination = Path(site.getsitepackages()[0]) / "vggt"
        shutil.copytree(checkout / "vggt", destination)
        license_dir = Path("/usr/local/share/licenses/vggt")
        license_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(checkout / "LICENSE.txt", license_dir / "LICENSE.txt")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: install_vggt.py COMMIT")
    main(sys.argv[1])
