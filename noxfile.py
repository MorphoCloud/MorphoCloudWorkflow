import argparse
import shutil
from pathlib import Path

import nox

nox.needs_version = ">=2024.3.2"
nox.options.default_venv_backend = "uv|virtualenv"
nox.options.sessions = ["lint"]


@nox.session
def lint(session: nox.Session) -> str:
    """
    Run linters on the codebase.
    """
    session.install("pre-commit")
    session.run("pre-commit", "run", "-a")


def _vendorize(session: nox.Session, paths: list[str]) -> None:
    """
    Vendorize files into a directory. Directory must exist.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "target", type=Path, help="The target directory to vendorize file into."
    )
    args = parser.parse_args(session.posargs)

    if not args.target.is_dir():
        msg = f"Target directory {args.target} does not exist"
        raise AssertionError(msg)

    src_dir = Path(__file__).parent
    target_dir = args.target

    for path in paths:
        src_path = src_dir / path
        target_path = target_dir / path
        if src_path.is_dir():
            session.log(f"Copying directory {src_path} -> {target_path}")
            shutil.copytree(src_path, target_path, dirs_exist_ok=True)
        else:
            session.log(f"Copying file {src_path} -> {target_path}")
            shutil.copy2(src_path, target_path)


@nox.session
def vendorize(session: nox.Session) -> None:
    _vendorize(
        session,
        [
            ".github",
            ".pre-commit-config.yaml",
            "issue-commands.md",
            "cloud-config",
        ],
    )
