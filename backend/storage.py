import io
import os
import tarfile

import aiofiles
import aiofiles.os

STORAGE_ROOT = os.getenv("STORAGE_ROOT", "/data/modules")


def archive_path(namespace: str, name: str, provider: str, version: str) -> str:
    return os.path.join(STORAGE_ROOT, namespace, name, provider, f"{version}.tar.gz")


async def save_archive(namespace: str, name: str, provider: str, version: str, data: bytes) -> str:
    path = archive_path(namespace, name, provider, version)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    async with aiofiles.open(path, "wb") as f:
        await f.write(data)
    return path


async def delete_archive(path: str) -> None:
    try:
        await aiofiles.os.remove(path)
    except FileNotFoundError:
        pass


async def read_archive(path: str) -> bytes:
    async with aiofiles.open(path, "rb") as f:
        return await f.read()


def extract_readme(data: bytes) -> str | None:
    """Extract README.md from a .tar.gz archive. Returns content or None."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                basename = os.path.basename(member.name).lower()
                if basename in ("readme.md", "readme.txt", "readme"):
                    f = tar.extractfile(member)
                    if f is not None:
                        content = f.read()
                        return content.decode("utf-8", errors="replace")
    except (tarfile.TarError, OSError):
        pass
    return None
