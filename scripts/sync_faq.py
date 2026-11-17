import argparse
import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.rag.runtime import FaqConfigurationError, build_faq_sync_service
from app.rag.sync_service import EmptyFaqSourceError
from app.rag.vector_store import VectorConfigurationError


def _error_result(message: str) -> str:
    return json.dumps(
        {
            "added": 0,
            "updated": 0,
            "removed": 0,
            "skipped_unchanged": 0,
            "skipped_inactive": 0,
            "skipped_empty": 0,
            "errors": 1,
            "message": message,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


async def _run(fixture: Path | None, allow_empty_source: bool = False) -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    store = None
    try:
        service, store = build_faq_sync_service(settings, fixture=fixture)
        result = await service.sync(allow_empty_source=allow_empty_source)
        print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
        return 0
    except (EmptyFaqSourceError, FaqConfigurationError, VectorConfigurationError) as exc:
        logging.getLogger(__name__).error("FAQ synchronization configuration failed")
        print(_error_result(str(exc)))
        return 1
    except Exception:
        logging.getLogger(__name__).error("FAQ synchronization failed")
        print(_error_result("FAQ synchronization failed; check configuration and service availability"))
        return 1
    finally:
        if store is not None:
            await store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronize FAQ into Qdrant")
    parser.add_argument("--fixture", type=Path, help="Use a local JSON fixture instead of Google Sheets")
    parser.add_argument(
        "--allow-empty-source",
        action="store_true",
        help="Intentionally remove all FAQ points when the source has no active entries",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.fixture, allow_empty_source=args.allow_empty_source))


if __name__ == "__main__":
    raise SystemExit(main())
