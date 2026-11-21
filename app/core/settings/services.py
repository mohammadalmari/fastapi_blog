"""External infrastructure: database and log files."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr

from .environment import ROOT_DIR, EnvironmentSettings

type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class ServicesSettings(EnvironmentSettings):
    """Connection details and logging knobs."""

    # -----------------------------------------------------------------------
    # Database
    # -----------------------------------------------------------------------

    # Database variables are namespaced DATABASE_* rather than FASTAPI_*.
    #
    # A single connection string, e.g.
    #   mongodb://user:pass@host:27017/?replicaSet=rs0
    # It can carry credentials, so it is kept out of reprs and logs.
    database_url: SecretStr = Field(
        default=SecretStr("mongodb://localhost:27017"),
        validation_alias="DATABASE_URL",
    )
    database_name: str = Field(default="fastapi_blog", validation_alias="DATABASE_NAME")

    # -----------------------------------------------------------------------
    # Logging (see app.core.logs for how these are used)
    # -----------------------------------------------------------------------

    # Root/console/app log level.
    log_level: LogLevel = "INFO"

    # Production only: rotating error.log + security.log live here. Point it at
    # a system path in a real deployment, e.g. /var/log/fastapi-blog
    log_dir: Path = ROOT_DIR / "logs"

    # Rotation tuning.
    log_max_bytes: int = Field(default=10 * 1024 * 1024, gt=0)  # 10 MB per file
    log_backup_count: int = Field(default=5, ge=0)
