import asyncio
import json
from dataclasses import asdict

from app.core.database import SessionLocal
from app.services.hashtag_ranking_service import HashtagRankingService


async def run() -> int:
    with SessionLocal() as database:
        result = await HashtagRankingService().sync_all(database)

    print(json.dumps(asdict(result), default=str, ensure_ascii=False))
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
