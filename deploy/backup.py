"""Create and restore a complete single-instance PicoPG data snapshot."""

from __future__ import annotations

import argparse
import os
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def create(data: Path, output: Path) -> None:
    data = data.resolve()
    output = output.resolve()
    if not data.is_dir():
        raise ValueError(f"data directory does not exist: {data}")
    if data == output or data in output.parents:
        raise ValueError("backup archive must be outside the data directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with tarfile.open(temporary, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            archive.add(data, arcname="data", recursive=True)
        with temporary.open("rb") as source:
            os.fsync(source.fileno())
        os.replace(temporary, output)
        directory = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def restore(archive_path: Path, target: Path) -> None:
    archive_path = archive_path.resolve()
    target = target.resolve()
    if not archive_path.is_file():
        raise ValueError(f"backup archive does not exist: {archive_path}")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError("restore target must not exist or must be an empty directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent))
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            for member in members:
                path = PurePosixPath(member.name)
                if (
                    not path.parts
                    or path.parts[0] != "data"
                    or path.is_absolute()
                    or ".." in path.parts
                    or member.issym()
                    or member.islnk()
                    or member.isdev()
                ):
                    raise ValueError(f"unsafe backup member: {member.name}")
            # Members were validated above; omit Python 3.12's filter= argument so
            # the deployment helper also runs on Ubuntu's older system Python.
            archive.extractall(staging, members=members)
        restored = staging / "data"
        if not restored.is_dir():
            raise ValueError("backup does not contain a data directory")
        if target.exists():
            target.rmdir()
        os.replace(restored, target)
    finally:
        try:
            staging.rmdir()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create_parser = commands.add_parser("create")
    create_parser.add_argument("--data", required=True, type=Path)
    create_parser.add_argument("--output", required=True, type=Path)
    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("--archive", required=True, type=Path)
    restore_parser.add_argument("--target", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "create":
            create(arguments.data, arguments.output)
        else:
            restore(arguments.archive, arguments.target)
    except (OSError, tarfile.TarError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
