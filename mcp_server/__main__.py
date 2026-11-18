import os
from urllib.parse import unquote, urlsplit

from app.core.logging import configure_logging
from mcp_server.server import mcp


def main() -> None:
    database_url = os.getenv("DATABASE_URL", "")
    password = urlsplit(database_url).password
    configure_logging(
        os.getenv("LOG_LEVEL", "INFO"),
        secrets=(database_url, password, unquote(password) if password else None),
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
