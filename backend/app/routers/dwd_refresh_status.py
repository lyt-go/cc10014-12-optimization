"""明细刷新状态视图接口。

- GET 视图按水位展示待刷新与失败分区，支持 narrow/wide 两种边界；
- 刷新事件与状态视图在同一批次事务内提交，重复事件不会让视图待刷新数量增长；
- 看板摘要按场景汇总，详情接口返回水位、稽核范围与失败重试链。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, PageResult
from app.services.dwd_refresh_status import VIEW_STATUSES, DwdRefreshStatusService

router = APIRouter(prefix="/api/dwd_refresh_status", tags=["明细刷新状态视图"])

service = DwdRefreshStatusService()


@router.get("/view", response_model=PageResult[dict])
def get_status_view(
    table_name: str | None = Query(default=None, description="按明细表名称过滤"),
    status: str | None = Query(default=None, description="待刷新、刷新成功、刷新失败、稽核失败、已修复"),
    boundary: str = Query(default="wide", description="narrow=仅最终状态与完成时间；wide=含重试链与修复批次"),
    keyword: str | None = Query(default=None, description="按分区值或表名检索"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """明细刷新状态视图：同分区只返回一条当前状态，按水位与分区排列。"""
    if status and status not in VIEW_STATUSES:
        raise HTTPException(status_code=400, detail=f"状态过滤值非法，可选：{'、'.join(VIEW_STATUSES)}")
    if boundary not in ("narrow", "wide"):
        raise HTTPException(status_code=400, detail="boundary 仅支持 narrow 或 wide")
    items = service.list_status(table_name=table_name, status=status, boundary=boundary, keyword=keyword)
    total = len(items)
    start = (page - 1) * size
    return PageResult(items=items[start:start + size], total=total, page=page, size=size)


@router.get("/dashboard")
def get_dashboard() -> dict[str, Any]:
    """看板摘要：按场景汇总待刷新、失败、修复与成功分区。"""
    return service.dashboard()


@router.get("/events")
def get_events(
    table_name: str | None = Query(default=None),
    batch_id: str | None = Query(default=None, description="按批次回查原始刷新事件"),
) -> dict[str, Any]:
    """刷新事件流：重复写入的原始事件全部保留，支撑失败重试链回查。"""
    items = service.list_events(table_name=table_name, batch_id=batch_id)
    return {"items": items, "total": len(items)}


@router.get("/detail")
def get_detail(
    table_name: str = Query(..., description="明细表名称"),
    partition_value: str = Query(..., description="分区值"),
) -> dict[str, Any]:
    """单分区详情：当前状态、对应水位、稽核范围、重试链与历史视图记录。"""
    detail = service.get_status_detail(table_name, partition_value)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"分区 {table_name}/{partition_value} 暂无刷新状态")
    return detail


@router.post("/events", response_model=ActionResult)
def post_events(payload: dict[str, Any]) -> ActionResult:
    """聚合提交同一批次的刷新事件；视图字段缺失时事务回滚，可修正后重试。"""
    events = payload.get("events")
    if not isinstance(events, list) or not events:
        return ActionResult(ok=False, message="events 必须是非空数组")
    batch_id = str(payload.get("批次编号") or "").strip() or None
    try:
        result = service.record_events(events, batch_id=batch_id)
    except ValueError as exc:
        # 事务已回滚，事件流与视图均未改动，前端可修正后重新提交
        return ActionResult(ok=False, message=f"批次提交已回滚：{exc}")
    return ActionResult(ok=True, message="刷新事件与状态视图已同批次落库", entry=result)
