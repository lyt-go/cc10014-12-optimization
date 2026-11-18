"""明细刷新状态视图服务。

把 DWD 明细层的刷新事件（定时刷新、补数、质量稽核）聚合成「按分区去重」的当前状态视图：

- 刷新事件与状态视图在同一个事务批次内提交，事件可重复写入，视图始终按
  (表名称, 分区值) 去重，待刷新数量不会因重复扫描持续增长；
- 状态视图字段不满足落库条件时整个批次回滚，调用方可重试恢复；
- 补数事件与定时刷新事件对同一分区给出不同完成状态时，按冲突优先次序裁决
  （补数事件 > 定时刷新 > 质量稽核），同批次再按事件顺序裁决；
- 较窄边界视图只呈现分区最终状态与完成时间，较宽边界视图额外呈现失败重试链、
  最终状态与修复批次；被覆盖的旧视图记录全部归档可回查。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from app.store import store

MODULE = "dwd_refresh_status"

# 三类刷新事件来源
EVENT_SCHEDULED = "定时刷新"
EVENT_BACKFILL = "补数事件"
EVENT_QUALITY = "质量稽核"
EVENT_TYPES = [EVENT_SCHEDULED, EVENT_BACKFILL, EVENT_QUALITY]

# 视图分区状态
STATUS_PENDING = "待刷新"
STATUS_SUCCESS = "刷新成功"
STATUS_FAILED = "刷新失败"
STATUS_AUDIT_FAILED = "稽核失败"
STATUS_REPAIRED = "已修复"
VIEW_STATUSES = [STATUS_PENDING, STATUS_SUCCESS, STATUS_FAILED, STATUS_AUDIT_FAILED, STATUS_REPAIRED]

# 冲突优先次序：数值越大越优先。补数（人工修复意图）优先于定时刷新，
# 质量稽核只负责同步稽核范围与标记稽核失败，不覆盖更高优先级的修复结论。
SOURCE_PRIORITY = {
    EVENT_BACKFILL: 3,
    EVENT_SCHEDULED: 2,
    EVENT_QUALITY: 1,
}

# 状态视图落库必须齐备的字段（store 事务会再次校验，这里用于给出可读错误）
VIEW_FIELDS = [
    "表名称", "分区值", "刷新水位", "批次编号", "事件类型", "当前状态",
    "完成时间", "稽核范围", "修复批次", "失败重试链", "更新时间",
]

# 较窄边界只对外暴露最终状态与完成时间；较宽边界暴露全部排障字段
NARROW_FIELDS = ["表名称", "分区值", "刷新水位", "当前状态", "完成时间", "批次编号"]
WIDE_FIELDS = VIEW_FIELDS + ["事件序号", "重试次数", "稽核规则"]


class DwdRefreshStatusService:
    # ------------------------------------------------------------------ 写入

    def record_events(
        self,
        events: list[dict[str, Any]],
        *,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        """聚合写入同一批次的刷新事件并刷新状态视图。

        事件允许重复（重复写入会保留在事件流里），但视图按分区去重；
        任一字段不满足落库条件时抛 ValueError，store 内事务整体回滚。
        """
        if not events:
            raise ValueError("刷新事件批次为空，无法提交")
        normalized: list[dict[str, Any]] = []
        shared_batch = (batch_id or "").strip()
        for index, raw in enumerate(events, start=1):
            event = self._normalize_event(raw, fallback_batch=shared_batch, seq=index)
            normalized.append(event)
        batch = normalized[0]["批次编号"]
        if any(event["批次编号"] != batch for event in normalized):
            raise ValueError("同一事务内的刷新事件必须属于同一个批次")
        return store.commit_refresh_batch(normalized, self._merge_view)

    def _normalize_event(
        self,
        raw: dict[str, Any],
        *,
        fallback_batch: str,
        seq: int,
    ) -> dict[str, Any]:
        event = dict(raw)
        event_type = str(event.get("事件类型", "")).strip()
        if event_type not in EVENT_TYPES:
            raise ValueError(f"不支持的刷新事件类型：{event_type or '空'}")
        table = str(event.get("表名称", "")).strip()
        partition = str(event.get("分区值", "")).strip()
        if not table or not partition:
            raise ValueError("刷新事件必须同时给出表名称与分区值")
        status = str(event.get("状态", "")).strip()
        if status not in VIEW_STATUSES:
            raise ValueError(f"刷新事件状态非法：{status or '空'}")
        batch = str(event.get("批次编号") or fallback_batch or "").strip()
        if not batch:
            raise ValueError("刷新事件缺少批次编号，事件与状态视图无法对应同一批次")
        watermark = str(event.get("刷新水位") or partition).strip()
        now = datetime.now().isoformat(timespec="seconds")
        is_repair = event_type == EVENT_BACKFILL
        if is_repair and status == STATUS_SUCCESS and not str(event.get("修复批次", "")).strip():
            event["修复批次"] = batch
        normalized = {
            "批次编号": batch,
            "事件类型": event_type,
            "表名称": table,
            "分区值": partition,
            "刷新水位": watermark,
            "状态": status,
            "事件时间": str(event.get("事件时间") or now),
            "完成时间": str(event.get("完成时间") or (now if status in (STATUS_SUCCESS, STATUS_REPAIRED) else "")),
            "稽核规则": str(event.get("稽核规则", "")).strip(),
            "稽核范围": str(event.get("稽核范围", "")).strip(),
            "修复批次": str(event.get("修复批次", "")).strip(),
            "失败原因": str(event.get("失败原因", "")).strip(),
            "数据行数": int(event.get("数据行数") or 0),
            "事件序号": int(event.get("事件序号") or seq),
        }
        return normalized

    # ----------------------------------------------------------- 视图合并规则

    def _merge_view(self, current: dict[str, Any] | None, event: dict[str, Any]) -> dict[str, Any]:
        """按冲突优先次序把单条事件合并进分区当前状态视图。"""
        now = datetime.now().isoformat(timespec="seconds")
        is_repair = event["事件类型"] == EVENT_BACKFILL
        if current is None:
            chain: list[dict[str, Any]] = []
            self._append_chain(chain, event)
            return {
                "表名称": event["表名称"],
                "分区值": event["分区值"],
                "刷新水位": event["刷新水位"],
                "批次编号": event["批次编号"],
                "事件类型": event["事件类型"],
                "当前状态": STATUS_REPAIRED if is_repair and event["状态"] == STATUS_SUCCESS else event["状态"],
                "完成时间": event["完成时间"],
                "稽核范围": event["稽核范围"],
                "修复批次": (event["修复批次"] or event["批次编号"]) if is_repair else event["修复批次"],
                "失败重试链": chain,
                "更新时间": now,
                "事件序号": event["事件序号"],
                "重试次数": len(chain),
                "稽核规则": event["稽核规则"],
            }

        merged = dict(current)
        merged["失败重试链"] = [dict(item) for item in current.get("失败重试链", [])]
        incoming_priority = SOURCE_PRIORITY[event["事件类型"]]
        current_priority = SOURCE_PRIORITY.get(str(current.get("事件类型", "")), 0)

        # 质量稽核通过事件只同步稽核范围，不改变刷新结论；稽核失败按低优先级标记
        if event["事件类型"] == EVENT_QUALITY and event["状态"] == STATUS_SUCCESS:
            if event["稽核范围"]:
                merged["稽核范围"] = event["稽核范围"]
            if event["稽核规则"]:
                merged["稽核规则"] = event["稽核规则"]
            merged["更新时间"] = now
            self._fill_defaults(merged, now)
            return merged

        higher = incoming_priority > current_priority
        same_level = incoming_priority == current_priority
        # 同分区同来源的重复事件以事件序号（事件顺序）裁决，后到的覆盖先到的
        later_in_batch = same_level and event["事件序号"] >= int(current.get("事件序号", 0))

        if higher or later_in_batch:
            merged["刷新水位"] = event["刷新水位"]
            merged["批次编号"] = event["批次编号"]
            merged["事件类型"] = event["事件类型"]
            merged["当前状态"] = STATUS_REPAIRED if is_repair and event["状态"] == STATUS_SUCCESS else event["状态"]
            merged["完成时间"] = event["完成时间"] or current.get("完成时间", "")
            merged["事件序号"] = event["事件序号"]
            if event["稽核范围"]:
                merged["稽核范围"] = event["稽核范围"]
            if event["稽核规则"]:
                merged["稽核规则"] = event["稽核规则"]
            if is_repair:
                # 补数事件即修复批次，较宽边界据此展示修复链路
                merged["修复批次"] = event["修复批次"] or event["批次编号"]
        elif event["事件类型"] == EVENT_QUALITY and event["状态"] == STATUS_AUDIT_FAILED:
            # 低优先级稽核失败：仅在分区尚未处于失败/修复态时落为稽核失败
            if merged["当前状态"] not in (STATUS_FAILED, STATUS_AUDIT_FAILED, STATUS_REPAIRED):
                merged["当前状态"] = STATUS_AUDIT_FAILED
            if event["稽核范围"]:
                merged["稽核范围"] = event["稽核范围"]
            if event["稽核规则"]:
                merged["稽核规则"] = event["稽核规则"]

        self._append_chain(merged["失败重试链"], event)
        merged["重试次数"] = len(merged["失败重试链"])
        merged["更新时间"] = now
        self._fill_defaults(merged, now)
        return merged

    def _append_chain(self, chain: list[dict[str, Any]], event: dict[str, Any]) -> None:
        """失败事件与修复事件都进入重试链，成功的常规刷新不入链。"""
        if event["状态"] in (STATUS_FAILED, STATUS_AUDIT_FAILED) or event["事件类型"] == EVENT_BACKFILL:
            chain.append({
                "事件类型": event["事件类型"],
                "状态": event["状态"],
                "批次编号": event["批次编号"],
                "事件时间": event["事件时间"],
                "失败原因": event["失败原因"],
                "修复批次": event["修复批次"],
            })

    def _fill_defaults(self, view: dict[str, Any], now: str) -> None:
        defaults = {
            "完成时间": "",
            "稽核范围": "",
            "修复批次": "",
            "失败重试链": [],
            "更新时间": now,
            "事件序号": 0,
            "重试次数": 0,
            "稽核规则": "",
        }
        for key, value in defaults.items():
            view.setdefault(key, value)
        view.setdefault("重试次数", len(view.get("失败重试链", [])))

    # ------------------------------------------------------------------ 查询

    def list_status(
        self,
        *,
        table_name: str | None = None,
        status: str | None = None,
        boundary: str = "wide",
        keyword: str | None = None,
    ) -> list[dict[str, Any]]:
        """返回按水位/分区排列的分区当前状态视图，每个分区仅一条。"""
        self.ensure_seed()
        rows = store.list_refresh_status()
        if table_name:
            rows = [row for row in rows if table_name.casefold() in row["表名称"].casefold()]
        if status:
            rows = [row for row in rows if row["当前状态"] == status]
        if keyword:
            kw = keyword.casefold()
            rows = [row for row in rows if kw in row["分区值"].casefold() or kw in row["表名称"].casefold()]
        rows.sort(key=lambda row: (row["表名称"], row["刷新水位"], row["分区值"]))
        fields = NARROW_FIELDS if boundary == "narrow" else WIDE_FIELDS
        return [{field: row.get(field, "") for field in fields} for row in rows]

    def get_status_detail(self, table_name: str, partition_value: str) -> dict[str, Any] | None:
        """单分区宽边界详情：当前状态 + 水位 + 稽核范围 + 完整重试链。"""
        self.ensure_seed()
        row = store.find_refresh_status(table_name, partition_value)
        if row is None:
            return None
        related_events = [
            event for event in store.list_refresh_events()
            if event["表名称"] == table_name and event["分区值"] == partition_value
        ]
        related_history = [
            item for item in store.list_refresh_status_history()
            if item["表名称"] == table_name and item["分区值"] == partition_value
        ]
        return {
            "current": {field: row.get(field, "") for field in WIDE_FIELDS},
            "events": related_events,
            "history": related_history,
        }

    def list_events(self, *, table_name: str | None = None, batch_id: str | None = None) -> list[dict[str, Any]]:
        """事件流查询：重复写入的原始事件全部保留，支撑失败重试链回查。"""
        self.ensure_seed()
        rows = store.list_refresh_events()
        if table_name:
            rows = [row for row in rows if table_name.casefold() in row["表名称"].casefold()]
        if batch_id:
            rows = [row for row in rows if row["批次编号"] == batch_id]
        return rows

    def dashboard(self) -> dict[str, Any]:
        """看板摘要：按场景（待刷新/失败/成功/修复）汇总分区与表维度数量。"""
        self.ensure_seed()
        rows = store.list_refresh_status()
        count_by_status = {name: 0 for name in VIEW_STATUSES}
        tables: dict[str, dict[str, int]] = {}
        for row in rows:
            count_by_status[row["当前状态"]] = count_by_status.get(row["当前状态"], 0) + 1
            bucket = tables.setdefault(row["表名称"], {name: 0 for name in VIEW_STATUSES})
            bucket[row["当前状态"]] += 1
        failed_partitions = [
            {"表名称": row["表名称"], "分区值": row["分区值"], "刷新水位": row["刷新水位"],
             "当前状态": row["当前状态"], "重试次数": row.get("重试次数", 0),
             "修复批次": row.get("修复批次", "")}
            for row in rows if row["当前状态"] in (STATUS_FAILED, STATUS_AUDIT_FAILED)
        ]
        return {
            "cards": [
                {"label": "分区总数", "value": len(rows)},
                {"label": "待刷新分区", "value": count_by_status[STATUS_PENDING]},
                {"label": "失败分区", "value": count_by_status[STATUS_FAILED] + count_by_status[STATUS_AUDIT_FAILED]},
                {"label": "已修复分区", "value": count_by_status[STATUS_REPAIRED]},
                {"label": "刷新成功分区", "value": count_by_status[STATUS_SUCCESS]},
            ],
            "count_by_status": count_by_status,
            "tables": [{"表名称": table, **bucket} for table, bucket in sorted(tables.items())],
            "failed_partitions": failed_partitions,
        }

    # ------------------------------------------------------- 质量稽核跨模块同步

    def sync_quality_result(
        self,
        *,
        table_name: str,
        rule_code: str,
        audit_scope: str,
        failed_partitions: list[str],
        total: int,
        failed: int,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        """质量稽核跨模块同步：把稽核范围与失败分区写入明细刷新状态视图。"""
        self.ensure_seed()
        batch = batch_id or f"QC-{date.today().isoformat()}-{datetime.now().strftime('%H%M%S')}"
        known = {(row["表名称"], row["分区值"]) for row in store.list_refresh_status()}
        events: list[dict[str, Any]] = []
        seq = 0
        for partition in sorted({value for value in failed_partitions if value}):
            seq += 1
            events.append({
                "批次编号": batch, "事件类型": EVENT_QUALITY, "表名称": table_name,
                "分区值": partition, "状态": STATUS_AUDIT_FAILED,
                "稽核规则": rule_code, "稽核范围": audit_scope,
                "失败原因": f"稽核失败 {failed}/{total} 行", "事件序号": seq,
            })
        if not events and any(table == table_name for table, _ in known):
            # 无失败分区时仍同步一次稽核范围，面板点击状态可看到对应稽核范围
            partitions = sorted({partition for table, partition in known if table == table_name})
            for partition in partitions:
                seq += 1
                events.append({
                    "批次编号": batch, "事件类型": EVENT_QUALITY, "表名称": table_name,
                    "分区值": partition, "状态": STATUS_SUCCESS,
                    "稽核规则": rule_code, "稽核范围": audit_scope, "事件序号": seq,
                })
        if not events:
            return {"批次编号": batch, "事件数": 0, "视图分区数": 0, "skipped": True}
        return self.record_events(events, batch_id=batch)

    # ------------------------------------------------------------ 示例数据引导

    def ensure_seed(self) -> None:
        """首次打开面板时引导一批刷新事件，覆盖待刷新/失败/成功三种场景。"""
        if store.refresh_seed_done() or store.list_refresh_events():
            store.mark_refresh_seed_done()
            return
        today = date.today()
        batch = f"DWD-SEED-{today.isoformat()}"
        events: list[dict[str, Any]] = []
        seq = 0

        def add(event_type: str, table: str, partition: str, status: str, **extra: Any) -> None:
            nonlocal seq
            seq += 1
            events.append({
                "批次编号": batch, "事件类型": event_type, "表名称": table,
                "分区值": partition, "刷新水位": partition, "状态": status,
                "事件时间": datetime.now().isoformat(timespec="seconds"),
                "事件序号": seq, **extra,
            })

        # 订单明细表：成功 -> 失败 -> 补数修复，演示失败重试链与修复批次
        add(EVENT_SCHEDULED, "dwd_order_detail", (today - timedelta(days=2)).isoformat(), STATUS_SUCCESS,
            数据行数=120, 完成时间=datetime.now().isoformat(timespec="seconds"))
        add(EVENT_SCHEDULED, "dwd_order_detail", (today - timedelta(days=1)).isoformat(), STATUS_FAILED,
            失败原因="上游分区延迟，清洗后行数为0")
        add(EVENT_BACKFILL, "dwd_order_detail", (today - timedelta(days=1)).isoformat(), STATUS_SUCCESS,
            修复批次=f"BF-SEED-{today.isoformat()}", 数据行数=118,
            完成时间=datetime.now().isoformat(timespec="seconds"))
        # 支付明细表：一个失败待修复、一个待刷新
        add(EVENT_SCHEDULED, "dwd_payment_detail", (today - timedelta(days=1)).isoformat(), STATUS_FAILED,
            失败原因="去重键空值率超过阈值")
        add(EVENT_SCHEDULED, "dwd_payment_detail", today.isoformat(), STATUS_PENDING)
        # 质量稽核范围同步到已修复分区
        add(EVENT_QUALITY, "dwd_order_detail", (today - timedelta(days=1)).isoformat(), STATUS_SUCCESS,
            稽核规则="QC-ORD-001", 稽核范围="order_amount 阈值[0,999999] 全量采样")
        self.record_events(events, batch_id=batch)
        store.mark_refresh_seed_done()
