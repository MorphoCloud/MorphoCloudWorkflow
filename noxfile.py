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

    for path in paths:
        src_path = src_dir / path
        target_path = target_dir / path
        if src_path.is_dir():
            session.log(f"Copying directory {src_path} -> {target_path}")
            shutil.copytree(src_path, target_path, dirs_exist_ok=True)
        else:
            session.log(f"Copying file {src_path} -> {target_path}")
            shutil.copy2(src_path, target_path)

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
            "cloud-config",
        ],
    )
