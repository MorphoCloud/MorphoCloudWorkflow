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
