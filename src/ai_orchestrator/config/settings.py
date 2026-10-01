"""Typed configuration.

Every knob is resolved here exactly once. Nothing else in the codebase reads
`os.environ` directly — that is what makes configuration auditable and what
stops a secret from leaking into a log line by accident.

Precedence (lowest to highest): built-in default -> `.secrets/runtime.env` ->
real environment. `.secrets/runtime.env` exists so a developer can keep one
file at rest with mode 0600 instead of exporting variables by hand.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


class _UnprefixedSecretsFile(PydanticBaseSettingsSource):
    """Reads a dotenv-style secrets file whose keys carry no `AO_` prefix.

    Secrets deliberately do not live in the same namespace as ordinary
    configuration. A file named `.secrets/runtime.env` full of bare
    `OPENROUTER_API_KEY=` lines is the convention operators already know, and
    mixing it with prefixed application settings makes it far too easy to export
    a secret into a shell that logs its environment.
    """

    def __init__(self, settings_cls: type[BaseSettings], path: Path) -> None:
        super().__init__(settings_cls)
        self._path = path
        self._data: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        """Parse the file into a case-insensitive lookup keyed by field name.

        Keys are matched case-insensitively because the file is written in
        SCREAMING_SNAKE (`POSTGRES_PASSWORD`) while the fields are snake_case
        (`postgres_password`). An `AO_` prefix is accepted and stripped, so the
        same file can carry both secrets and ordinary configuration.
        """
        if self._data is not None:
            return self._data
        raw: dict[str, str] = {}
        if self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                raw[key.strip()] = value.strip().strip("'\"")
        prefix = self.settings_cls.model_config.get("env_prefix", "")
        lowered = {k.lower().removeprefix(prefix.lower()): v for k, v in raw.items()}
        self._data = lowered
        return lowered

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[str, str, bool]:
        value = self._load().get(field_name.lower())
        return value or "", field_name, value is not None

    def __call__(self) -> dict[str, str]:
        # Only keys that name a real field, so a stray note in the file cannot
        # become a validation error under extra='ignore'.
        known = {name.lower() for name in self.settings_cls.model_fields}
        return {k: v for k, v in self._load().items() if k in known}


# `<repo>/.secrets/runtime.env` — the project root is src/ai_orchestrator/config/../..
PROJECT_ROOT: Path = Path(__file__).resolve().parents[3]
SECRETS_FILE: Path = PROJECT_ROOT / ".secrets" / "runtime.env"
DEV_DATA_DIR: Path = PROJECT_ROOT / ".devdata"


class Environment(StrEnum):
    """Deployment environment. Drives safety defaults, never business logic."""

    LOCAL = "local"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Process-wide settings.

    Grouped by subsystem. `AO_` prefix is the only namespace we read.
    """

    model_config = SettingsConfigDict(
        env_prefix="AO_",
        # The secrets file is read by `_UnprefixedSecretsFile` instead, because
        # its keys are intentionally unprefixed. See `settings_customise_sources`.
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- core --
    environment: Environment = Environment.LOCAL
    service_name: str = "ai-orchestrator"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # JSON to stdout. Console renderer is for humans running `make dev`.
    log_json: bool = True

    # ----------------------------------------------------------- database --
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 55432
    postgres_user: str = "ao"
    postgres_db: str = "ai_orchestrator"
    test_db_name: str = "ai_orchestrator_test"
    postgres_password: SecretStr = SecretStr("")
    # The role the *application* connects as.
    #
    # This is not the same as `postgres_user`. The owner role holds BYPASSRLS,
    # which means an application connected as the owner would run every query
    # with tenant isolation switched off — the RLS policies would exist, look
    # correct, and enforce nothing. Splitting the two roles is what makes the
    # policies real. Migrations connect as the owner; everything else does not.
    app_db_user: str = "ao_app"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_pool_timeout_s: int = 30
    # Applied to the app role, not the owner role. See docs/SECURITY.md.
    db_statement_timeout_ms: int = 30_000
    db_connect_timeout_s: int = 10

    # -------------------------------------------------------------- events --
    nats_url: str = "nats://127.0.0.1:4222"
    nats_enabled: bool = True
    nats_stream_name: str = "AO_EVENTS"
    nats_max_age_hours: int = 72
    nats_max_msgs: int = 1_000_000

    # ----------------------------------------------------------- workflows --
    temporal_enabled: bool = True
    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "ai-orchestrator"
    temporal_task_queue: str = "ao-workflows"
    # Deterministic replay safety: never change this while workflows are running.
    temporal_build_id: str = "v1"

    # -------------------------------------------------------------- model --
    # 'fake' is a deterministic scripted provider: full pipeline, zero spend,
    # no network. Tests and CI use it. Real providers are opt-in.
    model_provider_default: str = "fake"
    openai_api_key: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    gemini_api_key: SecretStr = SecretStr("")
    openrouter_api_key: SecretStr = SecretStr("")
    model_request_timeout_s: float = 60.0
    model_max_retries: int = 2
    # Embeddings: 'hash' is a deterministic local embedder (no network, no spend).
    embedding_provider_default: str = "hash"
    embedding_dimensions: int = 512

    # ------------------------------------------------------------- secrets --
    jwt_secret: SecretStr = SecretStr("")
    encryption_key: SecretStr = SecretStr("")
    internal_service_secret: SecretStr = SecretStr("")

    # Turn the API's authentication off. For a local demo on loopback, where pasting a
    # token into a browser prompt is more friction than the thing being demonstrated.
    #
    # Not a security control, and deliberately hard to leave on: it is refused in
    # production, and refused in `test` where a suite would silently stop exercising
    # authorisation at all. What it leaves reachable is the whole control plane —
    # including `POST /approvals/{id}/approve`, so "a human approved this" becomes
    # "something did". That is why the substitute principal is a named, obviously
    # non-human actor rather than a blank one, and why every request is logged.
    api_auth_disabled: bool = False

    # -------------------------------------------------------------- policy --
    # Hard ceilings. An agent cannot raise its own limits; the control plane
    # clamps any request above these at the API boundary.
    global_max_delegation_depth: int = 4
    global_max_fanout_per_agent: int = 8
    global_max_active_descendants: int = 16
    default_task_timeout_s: int = 900
    subagent_default_ttl_s: int = 600
    default_max_tokens_per_task: int = 200_000
    default_max_cost_usd_per_task: float = 5.0

    # ------------------------------------------------------------- approval --
    approval_default_ttl_s: int = 3600
    # What happens when nobody answers in time: ESCALATE fails closed.
    approval_expiry_action: Literal["ESCALATE", "REJECT"] = "ESCALATE"
    # Answer every approval request the moment it is raised, so a demonstration
    # runs to the end unattended.
    #
    # It is a switch, not a shortcut. The request row is still written and the
    # decision is still recorded through the same service a person uses, by a
    # distinct principal, so the audit reads "approved automatically" rather than
    # silently not existing. Turning it off makes the request wait for a person
    # and changes nothing else — the same code serves both. Default off: a
    # platform that decides on its own unless told otherwise is a platform that
    # can be talked into deciding on its own.
    approval_auto_approve: bool = False
    #: Consultations allowed per run. See `peer_consultation.CONSULT_LIMIT_PER_RUN`.
    consultation_limit_per_run: int = 4
    #: Tool calls one run may make. A cap on a confused model is the only thing
    #: between it and an unbounded bill.
    #:
    #: Raised to 48 on request, and that is a deliberate looseness rather than a
    #: measurement. What is measured (F199) is that a successful run makes a
    #: median of **one** tool call and that every observed failure stopped exactly
    #: at the ceiling -- so 48 is roughly twice the rate at which a lost model
    #: spends, and the failure it is most likely to prevent is a real run being
    #: cut off while it is still working, not a runaway being contained. Raising
    #: the ceiling costs nothing until something goes wrong, and F206 means
    #: "something went wrong" is currently invisible: tool calls are not recorded
    #: per execution, so no distribution exists to set this against. The number
    #: is a guess and is marked as one. It matches `max_requests` so there is one
    #: ceiling to reason about rather than two that disagree.
    max_tool_calls: int = 48

    # ------------------------------------------------------------- sandbox --
    # Refuses to execute code when true. The only permitted implementation is
    # the out-of-process container sandbox; see docs/SECURITY.md.
    sandbox_enabled: bool = True
    sandbox_backend: Literal["disabled", "subprocess"] = "disabled"

    # --------------------------------------------------------- observability --
    otel_enabled: bool = True
    otel_exporter_otlp_endpoint: str = "http://127.0.0.1:4318"
    otel_service_version: str = "0.1.0"
    metrics_enabled: bool = True

    # --------------------------------------------------------------- limits --
    rate_limit_task_create_per_min: int = 60
    rate_limit_delegation_per_min: int = 300
    rate_limit_tool_invoke_per_min: int = 600

    # ------------------------------------------------------------ seed/demo --
    seed_organization_slug: str = "autonomous-demo-company"
    seed_idempotency_namespace: str = "ao-seed-v1"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Precedence: explicit init > AO_-prefixed env > unprefixed secrets file.

        A real environment variable always beats the file, so a container that
        injects a credential at runtime overrides whatever is at rest.
        """
        return (
            init_settings,
            env_settings,
            _UnprefixedSecretsFile(settings_cls, SECRETS_FILE),
            dotenv_settings,
            file_secret_settings,
        )

    @field_validator(
        "postgres_password",
        "jwt_secret",
        "encryption_key",
        "internal_service_secret",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        # An env file with `KEY=` must behave like an absent key, not "".
        return None if value == "" else value

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @staticmethod
    def _dsn(settings: Settings, user: str, database: str) -> str:
        pwd = settings.postgres_password.get_secret_value()
        auth = f"{user}:{pwd}" if pwd else user
        return f"postgresql+asyncpg://{auth}@{settings.postgres_host}:{settings.postgres_port}/{database}"

    @property
    def async_database_url(self) -> str:
        """The application DSN: least-privilege role, RLS enforced."""
        return self._dsn(self, self.app_db_user, self.postgres_db)

    @property
    def admin_database_url(self) -> str:
        """Owner DSN for tooling that legitimately crosses tenants.

        Backups and administrative tooling need to see every tenant; the
        application never does. Kept as a distinct property so that a request
        handler reaching for it is a visible mistake.
        """
        return self._dsn(self, self.postgres_user, self.postgres_db)

    @property
    def migration_database_url(self) -> str:
        """Owner DSN. For Alembic and the seed script only.

        Never used by a request path: the owner has BYPASSRLS, so a leaked owner
        DSN silently disables tenant isolation.
        """
        return self._dsn(self, self.postgres_user, self.postgres_db)

    @property
    def sync_database_url(self) -> str:
        """Alias kept for Alembic configuration. Migrations are synchronous."""
        return self.migration_database_url

    def owner_dsn_for(self, database: str) -> str:
        """The owner DSN for one *named* database.

        Alembic has to be pointable at the test schema as well as the development
        one, and there was no way to say which without editing a file — so
        `make migrate` prepared whichever database the settings happened to name,
        the tests ran against another, and the first sign of it was
        `UndefinedColumnError` in a test that had passed a minute earlier.

        The owner role, because DDL is what migrations do and the application role
        is least-privilege by design. Same driver as `migration_database_url`, so
        this cannot introduce a dependency the existing path did not already have.
        """
        pwd = self.postgres_password.get_secret_value()
        auth = f"{self.postgres_user}:{pwd}" if pwd else self.postgres_user
        return f"postgresql+asyncpg://{auth}@{self.postgres_host}:{self.postgres_port}/{database}"

    @property
    def test_database_url(self) -> str:
        return self._dsn(self, self.app_db_user, self.test_db_name)

    @property
    def test_migration_database_url(self) -> str:
        return self._dsn(self, self.postgres_user, self.test_db_name)

    @property
    def log_json_text(self) -> bool:
        """Production and test always emit JSON; local dev may use console."""
        return self.log_json or self.environment is not Environment.LOCAL

    def redacted_summary(self) -> dict[str, object]:
        """Safe to log, safe to print. Never includes a secret value."""
        return {
            "environment": self.environment.value,
            "database": f"{self.postgres_host}:{self.postgres_port}/{self.postgres_db}",
            "nats_enabled": self.nats_enabled,
            "temporal_enabled": self.temporal_enabled,
            "model_provider_default": self.model_provider_default,
            "embedding_provider_default": self.embedding_provider_default,
            "sandbox_enabled": self.sandbox_enabled,
            "otel_enabled": self.otel_enabled,
        }

    def available_model_providers(self) -> list[str]:
        """Which real providers have a credential. Names only, never values."""
        present = {
            "openai": bool(self.openai_api_key.get_secret_value()),
            "anthropic": bool(self.anthropic_api_key.get_secret_value()),
            "google": bool(self.gemini_api_key.get_secret_value()),
            "openrouter": bool(self.openrouter_api_key.get_secret_value()),
        }
        return sorted(name for name, ok in present.items() if ok)

    def validate_for_startup(self) -> None:
        """Fail fast on a configuration that would be unsafe or non-functional.

        Called from the app lifespan. A misconfigured process must not accept
        traffic and then fail halfway through a business transaction.
        """
        problems: list[str] = []

        if self.api_auth_disabled:
            # Two refusals, not one. Production is the obvious one. `test` is the
            # subtle one: a suite running with auth off is not testing authorisation,
            # and nothing about it would look wrong.
            if self.is_production:
                problems.append("api_auth_disabled is not allowed in production")
            if self.environment is Environment.TEST:
                problems.append(
                    "api_auth_disabled must not be used in tests; the suite would stop "
                    "exercising authorisation without anything failing"
                )

        if self.is_production:
            if self.postgres_password.get_secret_value() == "":
                problems.append("POSTGRES_PASSWORD is required in production")
            for name in ("jwt_secret", "encryption_key", "internal_service_secret"):
                if getattr(self, name).get_secret_value() == "":
                    problems.append(f"{name.upper()} is required in production")
            if self.model_provider_default == "fake":
                problems.append("model_provider_default='fake' is not allowed in production")
            if not self.sandbox_enabled:
                problems.append("sandbox_enabled must be true in production")
            if self.log_json is False:
                problems.append("log_json must be true in production")
        elif self.environment is Environment.TEST:
            if self.model_provider_default != "fake":
                problems.append("tests must use model_provider_default='fake' for determinism")

        if self.sandbox_enabled and self.sandbox_backend == "subprocess":
            problems.append(
                "sandbox_backend='subprocess' executes code as the control-plane user; "
                "it is refused. Use the container sandbox described in docs/SECURITY.md."
            )

        if self.embedding_dimensions < 8 or self.embedding_dimensions > 2000:
            problems.append("embedding_dimensions must be in [8, 2000] (pgvector HNSW limit)")

        # The app role must not be the owner. Connecting as the owner means
        # BYPASSRLS, which means the tenant policies are decorative.
        if self.is_production and self.app_db_user == self.postgres_user:
            problems.append(
                "app_db_user must differ from postgres_user; the owner role bypasses RLS"
            )
        if self.app_db_user in {"postgres", "root"}:
            problems.append(f"app_db_user={self.app_db_user!r} is a superuser role")

        if problems:
            msg = "invalid configuration:\n  - " + "\n  - ".join(problems)
            raise ValueError(msg)

    @model_validator(mode="after")
    def _defaults_for_environment(self) -> Settings:
        if self.environment is Environment.TEST:
            # Tests must never reach a shared event bus or workflow namespace.
            if self.nats_enabled is True and self.nats_url == "nats://127.0.0.1:4222":
                object.__setattr__(self, "nats_enabled", True)  # local test nats is fine
            object.__setattr__(self, "log_json", True)
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. Tests call `reset_settings_cache()`."""
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()


__all__ = [
    "DEV_DATA_DIR",
    "PROJECT_ROOT",
    "SECRETS_FILE",
    "Annotated",
    "Environment",
    "Field",
    "Settings",
    "get_settings",
    "reset_settings_cache",
]
