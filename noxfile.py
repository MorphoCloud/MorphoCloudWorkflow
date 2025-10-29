import argparse
import re
import shutil
from pathlib import Path
from typing import Optional

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


def _collect_files_to_copy(
    session: nox.Session,
    src_dir: Path,
    paths: list[str],
    exclude_paths: Optional[list[str]],
) -> list[Path]:
    if exclude_paths is None:
        exclude_paths = []

    # Normalize exclude paths to absolute paths for comparison
    exclude_paths_set = {
        str((src_dir / exclude).resolve()) for exclude in exclude_paths
    }

    # Collect files to be copied
    files_to_copy = []
    for path in paths:
        src_path = src_dir / path
        if src_path.is_dir():
            if str(src_path.resolve()) in exclude_paths_set:
                session.log(f"Ignoring {src_path}")
                continue
            session.log(f"Analysing directory {src_path}")
            for file_path in src_path.rglob("*"):
                if file_path.is_file():
                    if str(file_path.resolve()) in exclude_paths_set:
                        session.log(f"Ignoring {file_path}")
                        continue
                    files_to_copy.append(file_path)
        elif src_path.is_file():
            if str(src_path.resolve()) in exclude_paths_set:
                session.log(f"Ignoring {src_path}")
                continue
            files_to_copy.append(src_path)
    return files_to_copy


def _patch_files(target_dir: Path) -> None:
    # .github/ISSUE_TEMPLATE/config.yml
    pattern = re.compile(r"^blank_issues_enabled: .*")
    replacement = "blank_issues_enabled: false"
    _update_file(
        target_dir / ".github/ISSUE_TEMPLATE/config.yml",
        pattern,
        replacement,
    )


def _vendorize(
    session: nox.Session, paths: list[str], exclude_paths: Optional[list[str]] = None
) -> None:
    """
    Vendorize files into a directory. Directory must exist.
    """

    project = "MorphoCloudWorkflow"

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--commit", action="store_true", help="Commit onto the current branch."
    )
    parser.add_argument(
        "--branch",
        action="store_true",
        help=f"Make a branch (e.g update-to-{project.lower()}-SHA).",
    )
    parser.add_argument(
        "target", type=Path, help="The target directory to vendorize file into."
    )
    args = parser.parse_args(session.posargs)

    if not args.target.is_dir():
        msg = f"Target directory {args.target} does not exist"
        raise AssertionError(msg)

    src_dir = Path(__file__).parent
    target_dir = args.target

    files_to_copy = _collect_files_to_copy(session, src_dir, paths, exclude_paths)

    session.log(f"Target directory {target_dir}")

    # Copy files
    for src_file in files_to_copy:
        relative_path = src_file.relative_to(src_dir)
        target_file = target_dir / relative_path
        target_file.parent.mkdir(parents=True, exist_ok=True)
        session.log(f"Copying file {relative_path}")
        shutil.copy2(src_file, target_file)

    _patch_files(target_dir)

    if args.commit:
        org = "MorphoCloud"

        # if any, extract SHA associated with the last update
        with session.chdir(target_dir):
            title = session.run(
                "git",
                "log",
                "-n",
                "1",
                f"--grep=^fix: Update to {org}/{project}" + "@[0-9a-fA-F]\\{1,40\\}$",
                "--pretty=format:%s",
                external=True,
                log=True,
                silent=True,
            ).strip()

            before = title.split("@")[1] if title else None

        after = session.run(
            "git", "rev-parse", "--short", "HEAD", external=True, log=False, silent=True
        ).strip()

        changes = (
            session.run(
                "git",
                "shortlog",
                f"{before}..{after}",
                "--no-merges",
                external=True,
                log=False,
                silent=True,
            ).strip()
            if before
            else None
        )

        with session.chdir(target_dir):
            if args.branch:
                session.run(
                    "git",
                    "switch",
                    "-c",
                    f"update-to-{project.lower()}-{after}",
                    external=True,
                )

            session.run("git", "add", "-A", external=True)
            session.run(
                "git",
                "commit",
                "-m",
                f"""fix: Update to {org}/{project}@{after}

List of {project} changes:

```
$ git shortlog {before}..{after} --no-merges
{changes}
```

See https://github.com/{org}/{project}/compare/{before}...{after}
"""
                if changes
                else f"fix: Update to {org}/{project}@{after}",
                external=True,
            )
            if args.branch:
                command = f"cd {src_dir.stem}; pipx run nox -s {session.name} -- /path/to/{target_dir.stem} --commit --branch"
                session.log(
                    f'Complete! Now run: cd {target_dir}; gh pr create --fill --body "Created by running `{command}`"'
                )
            else:
                session.log(f"Complete! Now run: cd {target_dir}; git push origin main")


@nox.session
def vendorize(session: nox.Session) -> None:
    _vendorize(
        session,
        [
            ".github",
            ".pre-commit-config.yaml",
            "issue-commands.md",
            "workshop-issue-commands.md",
            "cloud-config",
            "scripts/list-instance-credentials.sh",
        ],
        [".github/dependabot.yml"],
    )


CLOUD_CONFIG_EXOSPHERE_PATTERN = (
    r"^exosphere_sha=\"([0-9a-fA-F]{1,40})\" \# ([\w\d\-\_\.]+)$"
)


class ExosphereVersionParseError(RuntimeError):
    """Raised when the Exosphere version cannot be parsed from cloud-config."""


def _exosphere_version() -> tuple[Optional[str], Optional[str]]:
    """Extracts the Exosphere version and branch from cloud-config."""
    txt = Path("cloud-config").read_text()
    match = next(
        iter(re.finditer(CLOUD_CONFIG_EXOSPHERE_PATTERN, txt, flags=re.MULTILINE)), None
    )
    return (match.group(1), match.group(2)) if match else (None, None)


def _update_file(filepath: Path, regex: re.Pattern[str], replacement: str) -> None:
    pattern = re.compile(regex)
    with filepath.open() as doc_file:
        updated_content = [pattern.sub(replacement, line) for line in doc_file]
    with filepath.open("w") as doc_file:
        doc_file.writelines(updated_content)


@nox.session(name="bump-exosphere")
def bump_exosphere(session: nox.Session) -> None:
    org = "MorphoCloud"
    project = "exosphere"

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--commit", action="store_true", help="Commit onto the current branch."
    )
    parser.add_argument(
        "--branch",
        action="store_true",
        help=f"Make a branch (e.g update-to-{project.lower()}-SHA).",
    )
    parser.add_argument(
        "exosphere",
        type=Path,
        help="The exosphere source directory to lookup the updates.",
    )

    args = parser.parse_args(session.posargs)

    if not args.exosphere.is_dir():
        msg = f"Exosphere directory {args.target} does not exist"
        raise AssertionError(msg)

    exosphere_src_dir = args.exosphere

    current_version, current_branch = _exosphere_version()
    if current_version is None or current_branch is None:
        session.error("Failed to extract Exosphere version from cloud-config")

    with session.chdir(exosphere_src_dir):
        updated_version = session.run(
            "git", "rev-parse", "HEAD", external=True, log=True, silent=True
        ).strip()

        if current_version == updated_version:
            session.log(
                f"Skipping. Current exosphere version is already the latest: {current_version}"
            )
            return

        changes = session.run(
            "git",
            "shortlog",
            f"{current_version}..{updated_version}",
            "--no-merges",
            external=True,
            log=True,
            silent=True,
        ).strip()

    _update_file(
        Path("cloud-config"),
        re.compile(CLOUD_CONFIG_EXOSPHERE_PATTERN),
        f'exosphere_sha="{updated_version}" # {current_branch}',
    )

    if args.commit:
        if args.branch:
            session.run(
                "git",
                "switch",
                "-c",
                f"update-to-{project.lower()}-{updated_version[:9]}",
                external=True,
            )

        src_dir = Path(__file__).parent

        session.run("git", "add", "cloud-config", external=True)
        session.run(
            "git",
            "commit",
            "-m",
            f"""fix(cloud-config): Update to {org}/{project}@{updated_version[:9]}

List of {project} changes:

```
$ git shortlog {current_version[:9]}..{updated_version[:9]} --no-merges
{changes}
```

See https://github.com/{org}/{project}/compare/{current_version[:9]}...{updated_version[:9]}
""",
            external=True,
        )
        if args.branch:
            command = f"cd {src_dir.stem}; pipx run nox -s {session.name} -- /path/to/exosphere --commit --branch"
            session.log(
                f'Complete! Now run: cd {src_dir}; gh pr create --fill --body "Created by running `{command}`"'
            )
        else:
            session.log(f"Complete! Now run: cd {src_dir}; git push origin main")


@nox.session(name="display-exosphere-version", venv_backend="none")
def display_exosphere_version(session: nox.Session) -> None:
    version, branch = _exosphere_version()
    if version is None or branch is None:
        session.error("Failed to extract Exosphere version from cloud-config")
    session.log(f"Exosphere version [{version}] branch [{branch}]")
