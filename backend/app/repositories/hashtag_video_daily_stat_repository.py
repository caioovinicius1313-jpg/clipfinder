import uuid
from datetime import date
from typing import Literal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.hashtag_video_daily_stat import HashtagVideoDailyStat

RankingMetric = Literal["views", "view_delta"]


class HashtagVideoDailyStatRepository:
    def latest_date(
        self,
        database: Session,
        project_id: uuid.UUID,
        *,
        before: date | None = None,
    ) -> date | None:
        statement = select(func.max(HashtagVideoDailyStat.snapshot_date)).where(
            HashtagVideoDailyStat.project_id == project_id
        )
        if before is not None:
            statement = statement.where(HashtagVideoDailyStat.snapshot_date < before)
        return database.scalar(statement)

    def list_for_date(
        self,
        database: Session,
        project_id: uuid.UUID,
        snapshot_date: date,
        *,
        metric: RankingMetric,
        limit: int,
    ) -> list[HashtagVideoDailyStat]:
        if metric == "view_delta":
            ordering = (
                func.coalesce(HashtagVideoDailyStat.view_delta, -1).desc(),
                HashtagVideoDailyStat.views.desc(),
            )
        else:
            ordering = (HashtagVideoDailyStat.views.desc(),)

        statement = (
            select(HashtagVideoDailyStat)
            .where(
                HashtagVideoDailyStat.project_id == project_id,
                HashtagVideoDailyStat.snapshot_date == snapshot_date,
            )
            .order_by(*ordering)
            .limit(limit)
        )
        return list(database.scalars(statement))

    def list_latest_before(
        self,
        database: Session,
        project_id: uuid.UUID,
        snapshot_date: date,
        *,
        limit: int,
    ) -> tuple[date | None, list[HashtagVideoDailyStat]]:
        previous_date = self.latest_date(database, project_id, before=snapshot_date)
        if previous_date is None:
            return None, []
        return previous_date, self.list_for_date(
            database,
            project_id,
            previous_date,
            metric="views",
            limit=limit,
        )

    def replace_snapshot(
        self,
        database: Session,
        *,
        project_id: uuid.UUID,
        snapshot_date: date,
        stats: list[HashtagVideoDailyStat],
    ) -> None:
        database.execute(
            delete(HashtagVideoDailyStat).where(
                HashtagVideoDailyStat.project_id == project_id,
                HashtagVideoDailyStat.snapshot_date == snapshot_date,
            )
        )
        database.add_all(stats)
        database.commit()

    def get_by_id(
        self,
        database: Session,
        project_id: uuid.UUID,
        stat_id: uuid.UUID,
    ) -> HashtagVideoDailyStat | None:
        statement = select(HashtagVideoDailyStat).where(
            HashtagVideoDailyStat.id == stat_id,
            HashtagVideoDailyStat.project_id == project_id,
        )
        return database.scalar(statement)
