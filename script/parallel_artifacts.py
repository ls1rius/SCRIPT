"""Bounded parallel hashing and verified publication of immutable artifact trees.

Only regular files/directories are accepted. A failed copy retains ``.staging``
beside its destination for inspection; callers must explicitly resolve it.
This module does not alter running experiments or infer that sources are frozen.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import time


def _worker_count(workers):
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 16:
        raise ValueError('Artifact workers must be an integer from 1 through 16')
    return workers


def _inventory(root, exclude_names):
    files = []
    directories = []
    def fail(error):
        raise error
    for base, dirs, names in os.walk(root, followlinks=False, onerror=fail):
        dirs[:] = sorted(d for d in dirs if d not in exclude_names)
        for name in dirs:
            path = Path(base)/name
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise ValueError('Artifact symlink or non-directory rejected: '+str(path))
            directories.append(path.relative_to(root).as_posix())
        for name in sorted(names):
            if name in exclude_names:
                continue
            path = Path(base)/name
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError('Artifact symlink or special file rejected: '+str(path))
            files.append(path.relative_to(root).as_posix())
    return sorted(files), sorted(directories)


def _signature(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _read_file(root, relative, destination=None):
    # Walk each component with dir_fd/O_NOFOLLOW, including directories. This
    # also rejects a directory replaced by a symlink after inventory creation.
    parts = PurePosixPath(relative).parts
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(fd, 'rb') as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError('Artifact is no longer a regular file: '+relative)
            digest = hashlib.sha256()
            output = destination.open('xb') if destination is not None else None
            try:
                while True:
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                    if output is not None:
                        output.write(block)
                if output is not None:
                    output.flush()
                    os.fchmod(output.fileno(), stat.S_IMODE(before.st_mode))
            finally:
                if output is not None:
                    output.close()
            after = os.fstat(source.fileno())
            current = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
            if _signature(before) != _signature(after) or _signature(after) != _signature(current):
                raise ValueError('Artifact changed while reading: '+relative)
            if destination is not None:
                os.utime(destination, ns=(before.st_atime_ns, before.st_mtime_ns))
            return digest.hexdigest()
    finally:
        os.close(directory)


def tree_hashes(folder, *, workers=4, exclude_names=('__pycache__',)):
    """Return the same sorted relative SHA256 mapping as a serial file walk."""
    _worker_count(workers)
    root = Path(folder).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Artifact root must be a directory')
    files, directories = _inventory(root, exclude_names)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        values = list(pool.map(lambda name: _read_file(root, name), files))
    if _inventory(root, exclude_names) != (files, directories):
        raise ValueError('Artifact inventory changed while hashing')
    return dict(zip(files, values))


def copy_tree_verified(source, destination, *, snapshot=None, workers=4,
                       exclude_names=('__pycache__',)):
    """Copy/hash files together, recheck the source, then atomically publish.

    Supply an already captured ``snapshot`` to avoid another initial hash pass.
    The source must remain immutable for the operation. Final source hashes and
    copied-byte hashes must both equal that snapshot, including all file names.
    """
    _worker_count(workers)
    started = time.monotonic()
    source = Path(source).resolve(strict=True)
    destination = Path(destination).absolute()
    if not source.is_dir():
        raise ValueError('Artifact source must be a directory')
    if destination.resolve().is_relative_to(source):
        raise ValueError('Artifact destination cannot be inside its source')
    if destination.exists() or destination.is_symlink():
        raise ValueError('Artifact destination already exists')
    expected = tree_hashes(source, workers=workers, exclude_names=exclude_names) if snapshot is None else dict(snapshot)
    for name, digest in expected.items():
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or '..' in path.parts or str(path) != name
                or not isinstance(digest, str) or len(digest) != 64
                or any(c not in '0123456789abcdef' for c in digest)):
            raise ValueError('Invalid artifact snapshot entry: '+str(name))
    files, directories = _inventory(source, exclude_names)
    if set(files) != set(expected):
        raise ValueError('Artifact snapshot file inventory differs')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name+'.staging')
    staging.mkdir()  # Deterministic exclusive ownership; failures remain visible.
    for name in directories:
        (staging/name).mkdir()
    def copy_one(name):
        digest = _read_file(source, name, staging/name)
        if digest != expected[name]:
            raise ValueError('Artifact copied bytes differ from snapshot: '+name)
        return digest
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(copy_one, files))
    if tree_hashes(source, workers=workers, exclude_names=exclude_names) != expected:
        raise ValueError('Artifact source changed before publication')
    if _inventory(source, exclude_names) != (files, directories):
        raise ValueError('Artifact directories changed before publication')
    if destination.exists() or destination.is_symlink():
        raise ValueError('Artifact destination appeared before publication')
    staging.rename(destination)
    return dict(hashes=expected, files=len(files), workers=workers,
                seconds=time.monotonic()-started, destination=str(destination))
