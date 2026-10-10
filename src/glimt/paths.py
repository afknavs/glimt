"""Where Glimt keeps its files. Stdlib only: the CLI fast path imports this."""

import os
import secrets
from pathlib import Path

DEFAULT_PORT = 18770


def home() -> Path:
    path = Path(os.environ.get("GLIMT_HOME") or Path.home() / ".glimt")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def config_file() -> Path:
    return home() / "config.toml"


def secrets_dir() -> Path:
    path = home() / "secrets"
    path.mkdir(exist_ok=True, mode=0o700)
    return path


def user_bridges_dir() -> Path:
    return home() / "bridges"


def log_file() -> Path:
    return home() / "daemon.log"


def write_private(path: Path, text: str) -> None:
    """Write a file only the current user can read, atomically."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def token() -> str:
    """The bearer token local clients present to the daemon; created on first use."""
    path = home() / "token"
    if not path.exists():
        write_private(path, secrets.token_urlsafe(32))
    return path.read_text().strip()


def port() -> int:
    if os.environ.get("GLIMT_PORT"):
        return int(os.environ["GLIMT_PORT"])
    path = config_file()
    if path.exists():
        import tomllib

        try:
            return int(tomllib.loads(path.read_text()).get("daemon", {}).get("port", DEFAULT_PORT))
        except (tomllib.TOMLDecodeError, ValueError):
            pass
    return DEFAULT_PORT
