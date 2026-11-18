"""内存数据仓库：支持模块间关联、版本管理、模拟表数据、血缘记录与状态流转。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 模拟真实表数据（分层存储的实际行数据）
        self._table_data: dict[str, list[dict[str, Any]]] = {}
        # 版本历史: "module:id" -> [{version, timestamp, data}, ...]
        self._versions: dict[str, list[dict[str, Any]]] = {}
        # 血缘边记录
        self._lineage_edges: list[dict[str, Any]] = []
        # 质量稽核结果
        self._quality_results: dict[str, list[dict[str, Any]]] = {}
        # 快照冻结数据: "snapshot_id" -> {table_name, rows, frozen_at}
        self._snapshot_data: dict[str, dict[str, Any]] = {}
        # 归档记录
        self._archive_records: list[dict[str, Any]] = []
        # 补数区间记录
        self._backfill_partitions: dict[str, list[dict[str, Any]]] = {}
        # DWD 明细刷新事件流（同批次事件聚合写入，重复事件也原样保留可回查）
        self._refresh_events: list[dict[str, Any]] = []
        # DWD 明细刷新状态视图：(表名称, 分区值) -> 当前状态（视图按分区去重）
        self._refresh_status: dict[tuple[str, str], dict[str, Any]] = {}
        # 状态视图历史版本：旧状态被覆盖前归档，保证旧明细刷新状态视图记录都可回查
        self._refresh_status_history: list[dict[str, Any]] = []
        # 示例刷新事件是否已引导过（测试清库后可重新引导）
        self._refresh_seed_done: bool = False

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_field(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def find_all_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get(field) == value]

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ---- 模拟真实表数据 ----

    def table_rows(self, table_name: str) -> list[dict[str, Any]]:
        return self._table_data.setdefault(table_name, [])

    def set_table_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._table_data[table_name] = rows

    def append_table_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._table_data.setdefault(table_name, []).extend(rows)

    # ---- 版本管理 ----

    def _version_key(self, module: str, entry_id: int) -> str:
        return f"{module}:{entry_id}"

    def save_version(self, module: str, entry_id: int, data: dict[str, Any]) -> dict[str, Any]:
        key = self._version_key(module, entry_id)
        versions = self._versions.setdefault(key, [])
        version = {
            "version": len(versions) + 1,
            "timestamp": datetime.now().isoformat(),
            "data": dict(data),
        }
        versions.append(version)
        return version

    def get_versions(self, module: str, entry_id: int) -> list[dict[str, Any]]:
        key = self._version_key(module, entry_id)
        return list(self._versions.get(key, []))

    def get_latest_version(self, module: str, entry_id: int) -> dict[str, Any] | None:
        versions = self.get_versions(module, entry_id)
        return versions[-1] if versions else None

    # ---- 血缘 ----

    def add_lineage(self, upstream: str, downstream: str, field_map: str,
                    transform_logic: str = "", batch_id: str = "") -> dict[str, Any]:
        edge = {
            "id": len(self._lineage_edges) + 1,
            "上游表名": upstream,
            "下游表名": downstream,
            "来源字段": field_map,
            "目标字段": field_map,
            "转换逻辑": transform_logic,
            "采集批次": batch_id,
            "链路版本": f"v{len(self._lineage_edges) + 1}",
            "status": "已生效",
            "pending": False,
            "abnormal": False,
        }
        self._lineage_edges.append(edge)
        # 同时写入 lineage_track 模块表供查询
        self.rows("lineage_track").append(edge)
        return edge

    def get_lineage_edges(self) -> list[dict[str, Any]]:
        return list(self._lineage_edges)

    # ---- 质量稽核结果 ----

    def save_quality_result(self, rule_id: str, result: dict[str, Any]) -> None:
        self._quality_results.setdefault(rule_id, []).append(result)

    def get_quality_results(self, rule_id: str) -> list[dict[str, Any]]:
        return list(self._quality_results.get(rule_id, []))

    # ---- 快照 ----

    def save_snapshot_data(self, snapshot_id: str, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._snapshot_data[snapshot_id] = {
            "table_name": table_name,
            "rows": [dict(r) for r in rows],
            "frozen_at": datetime.now().isoformat(),
            "row_count": len(rows),
        }

    def get_snapshot_data(self, snapshot_id: str) -> dict[str, Any] | None:
        return self._snapshot_data.get(snapshot_id)

    # ---- 归档 ----

    def add_archive_record(self, record: dict[str, Any]) -> None:
        self._archive_records.append(record)

    def get_archive_records(self) -> list[dict[str, Any]]:
        return list(self._archive_records)

    # ---- 补数分区 ----

    def save_backfill_partition(self, job_id: str, partition: dict[str, Any]) -> None:
        self._backfill_partitions.setdefault(job_id, []).append(partition)

    def get_backfill_partitions(self, job_id: str) -> list[dict[str, Any]]:
        return list(self._backfill_partitions.get(job_id, []))

    # ---- DWD 明细刷新事件与状态视图（事务化写入） ----

    def refresh_seed_done(self) -> bool:
        return self._refresh_seed_done

    def mark_refresh_seed_done(self) -> None:
        self._refresh_seed_done = True

    def list_refresh_events(self) -> list[dict[str, Any]]:
        """返回全部刷新事件（含重复写入的原始记录），供失败重试链回查。"""
        return [dict(event) for event in self._refresh_events]

    def list_refresh_status(self) -> list[dict[str, Any]]:
        """返回去重后的分区当前状态视图，每个分区只有一条。"""
        return [dict(row) for row in self._refresh_status.values()]

    def find_refresh_status(self, table_name: str, partition_value: str) -> dict[str, Any] | None:
        row = self._refresh_status.get((table_name, partition_value))
        return dict(row) if row else None

    def list_refresh_status_history(self) -> list[dict[str, Any]]:
        """返回被覆盖的旧状态视图记录，旧记录全部保留可回查。"""
        return [dict(row) for row in self._refresh_status_history]

    def commit_refresh_batch(
        self,
        events: list[dict[str, Any]],
        merge_view,
    ) -> dict[str, Any]:
        """同一批次内原子提交刷新事件并刷新分区状态视图。

        - 先在工作副本里完成全部合并与字段校验，任一条状态视图缺字段都会抛 ValueError，
          事件流与视图都不改动，模拟数据库事务回滚；
        - 校验通过后一次性落库：事件允许重复追加（事件流保留原始记录可回查），
          视图按 (表名称, 分区值) 去重覆盖（同分区始终只有一条当前状态），
          被覆盖的旧视图记录归档进历史表，保证旧明细刷新状态视图记录都可回查。
        ``merge_view(current, event)`` 由服务层提供，封装冲突优先次序等业务规则。
        """
        if not events:
            raise ValueError("刷新事件批次为空，无法提交")
        batch_id = str(events[0].get("批次编号", ""))
        if not batch_id:
            raise ValueError("刷新事件缺少批次编号，事件与状态视图无法对应同一批次")
        for event in events:
            if str(event.get("批次编号", "")) != batch_id:
                raise ValueError("同一事务内的刷新事件必须属于同一个批次")

        working_views = {key: dict(row) for key, row in self._refresh_status.items()}
        for event in events:
            key = (str(event.get("表名称", "")), str(event.get("分区值", "")))
            current = working_views.get(key)
            merged = merge_view(dict(current) if current else None, dict(event))
            # 状态视图字段不满足落库条件即整体回滚，保证视图行结构始终完整
            missing = [name for name in (
                "表名称", "分区值", "刷新水位", "批次编号", "事件类型", "当前状态",
                "完成时间", "稽核范围", "修复批次", "失败重试链", "更新时间")
                if name not in merged or merged[name] is None]
            if missing:
                raise ValueError(f"明细刷新状态视图字段缺失：{'、'.join(missing)}，事务回滚")
            working_views[key] = merged

        # 工作副本校验全部通过后才真正落库（事件追加 + 视图覆盖 + 旧视图归档）
        next_id = max((int(e.get("id", 0)) for e in self._refresh_events), default=0)
        for event in events:
            next_id += 1
            stored = dict(event)
            stored["id"] = next_id
            self._refresh_events.append(stored)
        for key, merged in working_views.items():
            old = self._refresh_status.get(key)
            if old is not None and old != merged:
                archived = dict(old)
                archived["归档原因"] = f"被批次 {batch_id} 覆盖"
                archived["归档时间"] = datetime.now().isoformat()
                self._refresh_status_history.append(archived)
            self._refresh_status[key] = merged
        return {"批次编号": batch_id, "事件数": len(events), "视图分区数": len(working_views)}


store = Store()
