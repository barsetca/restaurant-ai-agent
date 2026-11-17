import asyncio
import json

from app.agent.checkpoint import setup_checkpoint_schema
from app.core.config import get_settings


async def _run() -> None:
    settings = get_settings()
    await setup_checkpoint_schema(settings.database_url)
    print(json.dumps({"status": "ok", "component": "langgraph_checkpoint_schema"}))


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
