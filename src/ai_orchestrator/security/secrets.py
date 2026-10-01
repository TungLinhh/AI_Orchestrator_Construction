"""Secret access behind a provider interface.

The rule this module exists to enforce: a secret is a value that must never
reach a log, an event, a trace, an audit row, a task payload or a memory item.
The only way to obtain one is `SecretProvider.get`, and the only thing that may
print a secret's *name* or *presence* is `describe()`.

Implementations:
    EnvFileSecretProvider  — local dev, reads .secrets/runtime.env
    ProcessEnvSecretProvider — anything already in the process environment
    StaticSecretProvider   — tests, injected literals

Production is expected to add a Vault/External Secrets adapter; nothing else
in the codebase has to change because they only see the Protocol.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from ai_orchestrator.config.settings import SECRETS_FILE

# Names the platform knows about. Used by `scripts/verify_secrets.py` and by
# the /api/v1/system/secrets status endpoint, which reports presence only.
KNOWN_SECRET_NAMES: tuple[str, ...] = (
    "POSTGRES_PASSWORD",
    "JWT_SECRET",
    "ENCRYPTION_KEY",
    "INTERNAL_SERVICE_SECRET",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
)

# Which of the above are required for the platform to function at all.
REQUIRED_SECRET_NAMES: tuple[str, ...] = (
    "POSTGRES_PASSWORD",
    "JWT_SECRET",
    "ENCRYPTION_KEY",
    "INTERNAL_SERVICE_SECRET",
)

# Which represent an optional external model provider.
OPTIONAL_PROVIDER_SECRET_NAMES: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
)


@runtime_checkable
class SecretProvider(Protocol):
    """Read-only secret lookup. Implementations must not log values."""

    async def get(self, name: str) -> str | None: ...

    async def require(self, name: str) -> str:
        """Raises `MissingSecretError` when absent. Use at a system boundary."""
        ...

    def is_configured(self, name: str) -> bool: ...

    async def describe(self) -> dict[str, str]:
        """name -> 'configured' | 'missing'. Never the value, not even a prefix."""
        ...


class MissingSecretError(RuntimeError):
    """A required secret was not configured. The name is safe to log."""

    def __init__(self, name: str) -> None:
        super().__init__(f"required secret is not configured: {name}")
        self.name = name


class StaticSecretProvider:
    """In-memory provider. Used by tests and by the 'simulation' mode."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})

    async def get(self, name: str) -> str | None:
        return self._values.get(name) or None

    async def require(self, name: str) -> str:
        value = self._values.get(name)
        if not value:
            raise MissingSecretError(name)
        return value

    def is_configured(self, name: str) -> bool:
        return bool(self._values.get(name))

    async def describe(self) -> dict[str, str]:
        return {k: ("configured" if v else "missing") for k, v in self._values.items()}


class ProcessEnvSecretProvider:
    """Reads `os.environ`. For Kubernetes, CI, and anything injected by a CSI driver."""

    def __init__(self, prefix: str = "") -> None:
        self._prefix = prefix

    def _key(self, name: str) -> str:
        return f"{self._prefix}{name}"

    async def get(self, name: str) -> str | None:
        return os.environ.get(self._key(name)) or None

    async def require(self, name: str) -> str:
        value = os.environ.get(self._key(name))
        if not value:
            raise MissingSecretError(name)
        return value

    def is_configured(self, name: str) -> bool:
        return bool(os.environ.get(self._key(name)))

    async def describe(self) -> dict[str, str]:
        return {
            n: ("configured" if self.is_configured(n) else "missing") for n in KNOWN_SECRET_NAMES
        }


class EnvFileSecretProvider:
    """Reads a `KEY=value` file (mode 0600). Process env wins over the file.

    A dedicated provider rather than a pydantic-settings `env_file` because the
    Control Plane must be able to hot-reload a credential and report presence
    without reconstructing the whole Settings object.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or SECRETS_FILE
        self._cache: dict[str, str] = {}
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path.exists():
            return
        mode = self._path.stat().st_mode & 0o777
        if mode & 0o077:
            msg = (
                f"{self._path} is mode {mode:o}; secrets must not be group/world readable. "
                f"Run: chmod 600 {self._path}"
            )
            raise PermissionError(msg)
        for raw in self._path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            self._cache[key.strip()] = value.strip().strip("'\"")

    async def get(self, name: str) -> str | None:
        self.load()
        value = self._cache.get(name)
        if value:
            return value
        return os.environ.get(name) or None

    async def require(self, name: str) -> str:
        value = await self.get(name)
        if not value:
            raise MissingSecretError(name)
        return value

    def is_configured(self, name: str) -> bool:
        self.load()
        return bool(self._cache.get(name)) or bool(os.environ.get(name))

    async def describe(self) -> dict[str, str]:
        self.load()
        return {
            n: ("configured" if self.is_configured(n) else "missing") for n in KNOWN_SECRET_NAMES
        }


def default_secret_provider() -> SecretProvider:
    """File first (so a developer edits one file), environment as override."""
    return EnvFileSecretProvider()


__all__ = [
    "KNOWN_SECRET_NAMES",
    "OPTIONAL_PROVIDER_SECRET_NAMES",
    "REQUIRED_SECRET_NAMES",
    "EnvFileSecretProvider",
    "MissingSecretError",
    "ProcessEnvSecretProvider",
    "SecretProvider",
    "StaticSecretProvider",
    "default_secret_provider",
]
