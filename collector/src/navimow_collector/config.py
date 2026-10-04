"""Load the collector's small, explicit configuration surface."""

from __future__ import annotations

import os
import tomllib
from dataclasses import MISSING, Field, dataclass, fields, replace
from pathlib import Path
from types import UnionType
from typing import TypeVar, get_args, get_origin, get_type_hints


class ConfigError(Exception):
    """A configuration value could not be resolved safely."""


@dataclass(frozen=True, repr=False)
class Secret:
    """A secret value that remains redacted in diagnostics and configuration output."""

    value: str
    source_path: Path | None = None

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__

    def display(self) -> str:
        if self.source_path is None:
            return "<redacted>"
        return f"<redacted: from {self.source_path}>"

    def reveal(self) -> str:
        """Return the value only for the boundary that consumes the secret."""
        return self.value


@dataclass(frozen=True)
class StorageConfig:
    backend: str = "postgres"
    dsn: Secret | None = None
    migrate: bool = True


PUBLIC_CLIENT_ID = "homeassistant"
PUBLIC_CLIENT_SECRET = "57056e15-722e-42be-bbaa-b0cbfb208a52"


@dataclass(frozen=True)
class AuthConfig:
    """Operator-controlled OAuth values; the public default needs no registration."""

    client_id: str = PUBLIC_CLIENT_ID
    client_secret: Secret = Secret(PUBLIC_CLIENT_SECRET)
    state_file: str = "~/.local/state/navimow-collector/tokens.json"


@dataclass(frozen=True)
class CollectorConfig:
    """Where live collection keeps what must survive a restart."""

    state_dir: str = "~/.local/state/navimow-collector"


@dataclass(frozen=True)
class HealthConfig:
    """Where `collect` serves /health and /metrics: `host:port`, or empty for nowhere.

    Loopback by default, which a container's own health check reaches; a scraper in
    another container or host needs `0.0.0.0:9477`.
    """

    listen: str = "127.0.0.1:9477"

    def address(self) -> tuple[str, int] | None:
        if not self.listen:
            return None
        host, _, port = self.listen.rpartition(":")
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]  # an IPv6 address, bracketed to set its port apart
        if host and "[" not in host and "]" not in host and port.isdigit() and int(port) < 65536:
            return host, int(port)
        raise ConfigError(f"health.listen must be host:port or empty, not {self.listen!r}")


@dataclass(frozen=True)
class Config:
    storage: StorageConfig = StorageConfig()
    auth: AuthConfig = AuthConfig()
    collector: CollectorConfig = CollectorConfig()
    health: HealthConfig = HealthConfig()


def load_config(config_path: Path | None = None) -> Config:
    """Resolve TOML and environment values into the typed collector configuration."""
    path = resolve_config_path(config_path)
    values = _load_toml(path) if path is not None else {}
    _validate_sections(values)
    defaults = Config()
    sections = {
        field.name: _load_section(
            field.name,
            type(getattr(defaults, field.name)),
            _section_values(values, field.name),
        )
        for field in fields(Config)
    }
    # Config fields are discovered from dataclass metadata to make new sections
    # automatic; mypy cannot infer the dynamically assembled field names.
    config = replace(defaults, **sections)
    _validate_backend(config.storage.backend)
    config.health.address()  # only to validate: a bad address fails here, not at bind time
    return config


def resolve_config_path(config_path: Path | None = None) -> Path | None:
    """The configuration file in use: the one given, or the one NAVIMOW_CONFIG names."""
    if config_path is not None:
        return config_path
    value = os.environ.get("NAVIMOW_CONFIG")
    return Path(value) if value else None


def _load_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as config_file:
            parsed = tomllib.load(config_file)
    except FileNotFoundError as error:
        raise ConfigError(f"configuration file not found: {path}") from error
    except OSError as error:
        raise ConfigError(f"could not read configuration file {path}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"invalid TOML in {path}: {error}") from error
    return {str(key): value for key, value in parsed.items()}


def _validate_sections(values: dict[str, object]) -> None:
    allowed = {field.name for field in fields(Config)}
    unknown = set(values) - allowed
    if unknown:
        raise ConfigError(f"unknown configuration section: {sorted(unknown)[0]}")
    for name, value in values.items():
        if not isinstance(value, dict):
            raise ConfigError(f"configuration section [{name}] must be a table")


def _section_values(values: dict[str, object], name: str) -> dict[str, object]:
    value = values.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"configuration section [{name}] must be a table")
    return {str(key): item for key, item in value.items()}


Section = TypeVar("Section")


def _load_section(
    section_name: str, section_type: type[Section], values: dict[str, object]
) -> Section:
    hints = get_type_hints(section_type)
    section_fields = fields(section_type)  # type: ignore[arg-type]
    allowed = {field.name for field in section_fields}
    allowed_with_files = allowed | {
        f"{field.name}_file" for field in section_fields if _is_secret(hints[field.name])
    }
    unknown = set(values) - allowed_with_files
    if unknown:
        raise ConfigError(f"unknown configuration key: {section_name}.{sorted(unknown)[0]}")

    resolved: dict[str, object] = {}
    for field in section_fields:
        annotation = hints[field.name]
        if _is_secret(annotation):
            resolved[field.name] = _resolve_secret(
                section_name, field.name, values, _default(field)
            )
        else:
            raw = _environment_value(
                section_name, field.name, values.get(field.name, _default(field))
            )
            resolved[field.name] = _coerce(raw, annotation, f"{section_name}.{field.name}")
    return replace(section_type(), **resolved)  # type: ignore[type-var]


def _default(field: Field[object]) -> object:
    default = field.default
    if default is MISSING:
        raise TypeError(f"configuration field has no default: {field.name}")
    return default


def _environment_value(section: str, key: str, fallback: object) -> object:
    return os.environ.get(f"NAVIMOW_{section}_{key}".upper(), fallback)


def _resolve_secret(
    section: str, key: str, values: dict[str, object], fallback: object
) -> Secret | None:
    # The environment is one layer and the file another: a value from either form in
    # the environment replaces both forms in the file, rather than clashing with them.
    env_name = f"NAVIMOW_{section}_{key}".upper()
    environment = {
        key: os.environ.get(env_name),
        f"{key}_file": os.environ.get(f"{env_name}_FILE"),
    }
    layer = environment if any(v is not None for v in environment.values()) else values
    inline, file_name = layer.get(key), layer.get(f"{key}_file")
    label = f"{section}.{key}"
    if inline is not None and file_name is not None:
        raise ConfigError(f"{label} cannot be set both inline and as {key}_file")
    if file_name is not None:
        if not isinstance(file_name, str):
            raise ConfigError(f"{label}_file must be a string path")
        path = Path(file_name)
        try:
            return Secret(path.read_text(encoding="utf-8").rstrip("\n"), path)
        except OSError as error:
            raise ConfigError(f"could not read {label}_file {path}: {error}") from error
    if inline is None:
        if fallback is None or isinstance(fallback, Secret):
            return fallback
        raise TypeError(f"configuration secret field has invalid default: {label}")
    if not isinstance(inline, str):
        raise ConfigError(f"{label} must be a string")
    return Secret(inline)


def _coerce(value: object, annotation: object, key: str) -> object:
    if annotation is str:
        if not isinstance(value, str):
            raise ConfigError(f"{key} must be a string")
        return value
    if annotation is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            parsed = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}
            coerced = parsed.get(value.lower())
            if coerced is not None:
                return coerced
        raise ConfigError(f"{key} must be a boolean (true/false, 1/0, or yes/no)")
    raise ConfigError(f"unsupported configuration type for {key}")


def _is_secret(annotation: object) -> bool:
    return annotation is Secret or (
        get_origin(annotation) in (UnionType, None) and Secret in get_args(annotation)
    )


def _validate_backend(backend: str) -> None:
    from .storage import STORAGE_BACKENDS

    if backend not in STORAGE_BACKENDS:
        raise ConfigError(f"unknown storage backend: {backend}")
