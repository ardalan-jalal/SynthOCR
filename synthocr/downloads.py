"""Bounded, checksummed downloads shared by fonts and corpus snapshots."""

import hashlib
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(
    url: str, digest: str | None, target: Path, max_bytes: int, hosts: set[str], opener=None
) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in hosts
        or parsed.username
        or parsed.password
    ):
        raise ValueError(f"Downloads require HTTPS on {', '.join(sorted(hosts))}")
    if target.is_file():
        if target.stat().st_size > max_bytes:
            raise ValueError(f"Existing download exceeds {max_bytes} bytes: {target}")
        actual = sha256(target)
        if digest is None or actual == digest:
            return actual
        raise ValueError(f"Existing file has a different checksum: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with (opener or urlopen)(url, timeout=60) as response:
            final = urlparse(response.geturl()) if hasattr(response, "geturl") else parsed
            if final.scheme != "https" or final.hostname not in hosts:
                raise ValueError("Download redirected outside the allowed HTTPS hosts")
            size = response.headers.get("Content-Length") if hasattr(response, "headers") else None
            if size and int(size) > max_bytes:
                raise ValueError(f"Download exceeds {max_bytes} bytes: {url}")
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                temporary = Path(stream.name)
                hasher = hashlib.sha256()
                total = 0
                while chunk := response.read(65536):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"Download exceeds {max_bytes} bytes: {url}")
                    hasher.update(chunk)
                    stream.write(chunk)
        actual = hasher.hexdigest()
        if digest is not None and actual != digest:
            raise ValueError(f"SHA-256 verification failed: {url}")
        # Temporary files are owner-only; give the asset ordinary read permissions
        # so non-root users (such as the container's) can read it.
        temporary.chmod(0o644)
        # Link is atomic and refuses an existing destination, even after a race.
        os.link(temporary, target)
        return actual
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def within(root: Path, name: str) -> Path:
    relative = Path(name)
    target = (root / relative).resolve()
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not target.is_relative_to(root.resolve())
    ):
        raise ValueError("Catalog paths must stay inside the destination")
    return target
