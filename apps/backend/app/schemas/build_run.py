from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.build_run import BuildRunStage, BuildRunStatus


class BuildRunOut(BaseModel):
    """提供给前端轮询的公开字段，不包含数据库主键和 active_slot。"""

    model_config = ConfigDict(from_attributes=True)

    project_id: int
    run_id: str
    status: BuildRunStatus
    stage: BuildRunStage | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
