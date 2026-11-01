import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv(
        "DATABASE_URL", "postgresql+psycopg://taskd:taskd_local_demo@localhost:55432/taskd"))
    artifact_root: Path = field(default_factory=lambda: Path(os.getenv("ARTIFACT_ROOT", "runtime/artifacts")).resolve())
    node_id: str = field(default_factory=lambda: os.getenv("NODE_ID", "local"))
    lease_seconds: float = field(default_factory=lambda: float(os.getenv("LEASE_SECONDS", "30")))
    heartbeat_seconds: float = field(default_factory=lambda: float(os.getenv("HEARTBEAT_SECONDS", "5")))
    poll_seconds: float = field(default_factory=lambda: float(os.getenv("POLL_SECONDS", "0.5")))
    retry_base_seconds: float = field(default_factory=lambda: float(os.getenv("RETRY_BASE_SECONDS", "2")))
    test_faults: bool = field(default_factory=lambda: os.getenv("ENABLE_TEST_FAULTS", "0") == "1")

    def __post_init__(self):
        if not self.database_url.startswith("postgresql+"):
            raise ValueError("This application requires PostgreSQL, not an in-memory/SQLite substitute")
        if not 0 < self.heartbeat_seconds < self.lease_seconds / 2:
            raise ValueError("Heartbeat must be positive and less than half the lease")


settings = Settings()
