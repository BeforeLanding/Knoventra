"""Bounded copy from an already parsed multipart file, before ingestion."""
from pathlib import Path
from typing import BinaryIO


class UploadTooLarge(ValueError):
    pass


def copy_upload(source: BinaryIO, destination: Path, max_bytes: int) -> int:
    if max_bytes < 1:
        raise ValueError("upload size limit must be positive")
    total = 0
    with destination.open("wb") as output:
        while True:
            # At the boundary, read at most one extra byte to detect oversize.
            chunk = source.read(min(64 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise UploadTooLarge("upload exceeds configured file size limit")
            output.write(chunk)
    if total == 0:
        raise ValueError("upload file is empty")
    return total
