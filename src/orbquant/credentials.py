"""Local Alpaca credential resolution; secret values never enter reports."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


CREDENTIALS_FILENAME = "alpaca-credentials.toml"


def default_credentials_path() -> Path:
    """Find the repository-local file for editable installs, even from another cwd."""
    package_dir = Path(__file__).resolve().parent
    if package_dir.parent.name == "src":
        return package_dir.parent.parent / CREDENTIALS_FILENAME
    # A packaged installation can use a file in its working directory or the
    # explicit ORBQUANT_CREDENTIALS_FILE / --credentials-file override.
    return Path.cwd() / CREDENTIALS_FILENAME


@dataclass(frozen=True)
class AlpacaCredentials:
    api_key: str | None = field(repr=False)
    secret_key: str | None = field(repr=False)
    file_path: Path
    file_exists: bool
    source: str

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.secret_key)


def _credential(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Alpaca {name} must be a quoted string")
    value = value.strip()
    if any(not 33 <= ord(character) <= 126 for character in value):
        raise ValueError(f"Alpaca {name} must be a single-line ASCII key without spaces")
    return value or None


def load_alpaca_credentials(
    credentials_file: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    api_key: str | None = None,
    secret_key: str | None = None,
) -> AlpacaCredentials:
    """Resolve explicit arguments, environment overrides, then local TOML.

    Does not mutate the environment, authenticate, or log secret values. A
    missing default file is permitted for existing environment-only users;
    an explicitly selected missing file is an error.
    """
    env = os.environ if environ is None else environ
    override = credentials_file or env.get("ORBQUANT_CREDENTIALS_FILE")
    path = Path(override).expanduser().resolve() if override else default_credentials_path()
    explicit_key = _credential(api_key, "api_key")
    explicit_secret = _credential(secret_key, "secret_key")
    env_key = _credential(env.get("APCA_API_KEY_ID"), "APCA_API_KEY_ID")
    env_secret = _credential(env.get("APCA_API_SECRET_KEY"), "APCA_API_SECRET_KEY")
    key = explicit_key or env_key
    secret = explicit_secret or env_secret
    key_source = "explicit" if explicit_key else "environment" if env_key else None
    secret_source = "explicit" if explicit_secret else "environment" if env_secret else None
    exists = path.is_file()
    if not exists and override:
        raise ValueError(f"Alpaca credentials file was not found: {path}")
    if not (key and secret):
        if exists:
            try:
                record = tomllib.loads(path.read_text(encoding="utf-8-sig"))
            except (tomllib.TOMLDecodeError, UnicodeDecodeError):
                # Parser diagnostics may contain fragments of invalid secret
                # text. Only this sanitized message reaches callers/logs.
                raise ValueError("Alpaca credentials file is invalid TOML; check its quoted values") from None
            except OSError:
                raise ValueError("Alpaca credentials file could not be read") from None
            section = record.get("alpaca")
            if not isinstance(section, dict):
                raise ValueError("Alpaca credentials file must contain an [alpaca] table")
            if not key:
                key = _credential(section.get("api_key"), "api_key")
                key_source = "file" if key else None
            if not secret:
                secret = _credential(section.get("secret_key"), "secret_key")
                secret_source = "file" if secret else None
    sources = {source for source in (key_source, secret_source) if source}
    source = next(iter(sources)) if len(sources) == 1 else "mixed" if sources else "missing"
    return AlpacaCredentials(key, secret, path, exists, source)
