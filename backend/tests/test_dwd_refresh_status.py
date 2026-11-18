"""明细刷新状态视图测试。

覆盖需求要点：
- 刷新事件与状态视图同批次提交；事件重复写入而视图按分区去重，待刷新数量不增长；
- 状态视图字段缺失时事务回滚，修正后可重新提交恢复；
- 较窄边界只显示分区最终状态与完成时间，较宽边界显示失败重试链、最终状态与修复批次；
- 补数事件与定时刷新事件对同一分区给出不同完成状态时按优先次序裁决；
- 旧状态视图记录归档可回查；
- DWD 构建、补数调度、质量稽核沿调用链同步视图。
"""
from __future__ import annotations

import pytest

from app.store import store
from app.services.backfill_job import BackfillJobService
from app.services.dwd_layer import DwdLayerService
from app.services.dwd_refresh_status import (
    EVENT_BACKFILL,
    EVENT_QUALITY,
    EVENT_SCHEDULED,
    STATUS_AUDIT_FAILED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REPAIRED,
    STATUS_SUCCESS,
    DwdRefreshStatusService,
)
from app.services.quality_check import QualityCheckService


def reset_store():
    from app.seed import SEED_ROWS
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    store._table_data.clear()
    store._versions.clear()
    store._lineage_edges.clear()
    store._quality_results.clear()
    store._snapshot_data.clear()
    store._archive_records.clear()
    store._backfill_partitions.clear()
    store._refresh_events.clear()
    store._refresh_status.clear()
    store._refresh_status_history.clear()
    store._refresh_seed_done = False


def make_event(seq, *, event_type=EVENT_SCHEDULED, table="dwd_order_detail",
               partition="2026-11-10", status=STATUS_PENDING, batch="B-001", **extra):
    event = {
        "批次编号": batch, "事件类型": event_type, "表名称": table,
        "分区值": partition, "刷新水位": partition, "状态": status,
        "事件序号": seq,
    }
    event.update(extra)
    return event


class TestDwdRefreshStatus:
    def setup_method(self):
        reset_store()

    # ------------------------------------------------------------ 批次与去重

    def test_events_and_view_share_same_batch(self):
        svc = DwdRefreshStatusService()
        result = svc.record_events([
            make_event(1, partition="2026-11-10", status=STATUS_SUCCESS),
            make_event(2, partition="2026-11-11", status=STATUS_PENDING),
        ])
        assert result["批次编号"] == "B-001"
        assert result["事件数"] == 2
        view = svc.list_status()
        assert len(view) == 2
        assert {row["批次编号"] for row in view} == {"B-001"}

    def test_duplicate_events_do_not_inflate_pending_view(self):
        """事件重复写入而视图去重：同分区始终只有一条当前状态。"""
        svc = DwdRefreshStatusService()
        for _ in range(3):
            svc.record_events([make_event(1, partition="2026-11-10", status=STATUS_PENDING)])
        assert len(store.list_refresh_events()) == 3
        view = svc.list_status(status=STATUS_PENDING)
        assert len(view) == 1
        assert svc.dashboard()["cards"][1] == {"label": "待刷新分区", "value": 1}

    def test_mixed_batch_is_rejected(self):
        svc = DwdRefreshStatusService()
        with pytest.raises(ValueError):
            svc.record_events([
                make_event(1, batch="B-001"),
                make_event(2, batch="B-002"),
            ])
        assert store.list_refresh_events() == []
        assert store.list_refresh_status() == []

    # ------------------------------------------------------------ 事务回滚

    def test_missing_view_field_rolls_back_entire_batch(self, monkeypatch):
        svc = DwdRefreshStatusService()
        good = make_event(1, partition="2026-11-10", status=STATUS_PENDING)

        def broken_merge(current, event):
            merged = DwdRefreshStatusService._merge_view(svc, current, event)
            del merged["刷新水位"]  # 模拟视图字段没有落库
            return merged

        monkeypatch.setattr(store, "commit_refresh_batch",
                            lambda events, merge: type(store).commit_refresh_batch(store, events, broken_merge))
        with pytest.raises(ValueError, match="事务回滚"):
            svc.record_events([good])
        # 回滚后事件流与视图都没有任何残留
        assert store.list_refresh_events() == []
        assert store.list_refresh_status() == []

        def restore_merge(current, event):
            return DwdRefreshStatusService._merge_view(svc, current, event)

        monkeypatch.undo()
        # 修正后同一批次可重新提交恢复
        result = svc.record_events([good])
        assert result["视图分区数"] == 1
        assert len(svc.list_status()) == 1

    # ------------------------------------------------------------ 宽窄边界

    def test_narrow_boundary_hides_retry_chain(self):
        svc = DwdRefreshStatusService()
        svc.record_events([
            make_event(1, partition="2026-11-10", status=STATUS_FAILED, 失败原因="上游延迟"),
            make_event(2, event_type=EVENT_BACKFILL, partition="2026-11-10",
                       status=STATUS_SUCCESS, 修复批次="BF-009"),
        ])
        narrow = svc.list_status(boundary="narrow")
        assert len(narrow) == 1
        assert set(narrow[0].keys()) == {
            "表名称", "分区值", "刷新水位", "当前状态", "完成时间", "批次编号"}
        assert narrow[0]["当前状态"] == STATUS_REPAIRED
        assert narrow[0]["完成时间"]

        wide = svc.list_status(boundary="wide")
        assert wide[0]["修复批次"] == "BF-009"
        chain = wide[0]["失败重试链"]
        assert len(chain) == 2  # 失败事件 + 补数修复事件都在链上
        assert chain[0]["失败原因"] == "上游延迟"
        assert wide[0]["重试次数"] == 2

    # ------------------------------------------------------------ 冲突优先次序

    def test_backfill_overrides_scheduled_failure(self):
        """较宽边界：补数事件优先于定时刷新，分区最终状态为已修复并带修复批次。"""
        svc = DwdRefreshStatusService()
        svc.record_events([make_event(1, status=STATUS_FAILED, 失败原因="行数为0")])
        svc.record_events([make_event(1, event_type=EVENT_BACKFILL, status=STATUS_SUCCESS,
                                      修复批次="BF-001", batch="B-002")])
        row = svc.list_status(boundary="wide")[0]
        assert row["当前状态"] == STATUS_REPAIRED
        assert row["事件类型"] == EVENT_BACKFILL
        assert row["修复批次"] == "BF-001"

    def test_scheduled_success_does_not_override_backfill_repair(self):
        """低优先级的定时刷新成功不能覆盖已经完成的补数修复。"""
        svc = DwdRefreshStatusService()
        svc.record_events([make_event(1, event_type=EVENT_BACKFILL, status=STATUS_SUCCESS,
                                      修复批次="BF-001")])
        svc.record_events([make_event(1, status=STATUS_SUCCESS, batch="B-002")])
        row = svc.list_status(boundary="wide")[0]
        assert row["当前状态"] == STATUS_REPAIRED
        assert row["事件类型"] == EVENT_BACKFILL
        assert row["修复批次"] == "BF-001"

    def test_same_source_conflict_resolved_by_event_order(self):
        """同批次同来源事件按事件顺序裁决，后到的定时刷新成功覆盖先到的失败。"""
        svc = DwdRefreshStatusService()
        svc.record_events([
            make_event(1, status=STATUS_FAILED),
            make_event(2, status=STATUS_SUCCESS, 完成时间="2026-11-10T12:00:00"),
        ])
        row = svc.list_status(boundary="wide")[0]
        assert row["当前状态"] == STATUS_SUCCESS
        assert row["事件序号"] == 2
        assert row["完成时间"] == "2026-11-10T12:00:00"

    def test_history_keeps_overwritten_view_records(self):
        """旧明细刷新状态视图记录被覆盖后仍可回查。"""
        svc = DwdRefreshStatusService()
        svc.record_events([make_event(1, status=STATUS_FAILED)])
        svc.record_events([make_event(1, event_type=EVENT_BACKFILL, status=STATUS_SUCCESS,
                                      修复批次="BF-001", batch="B-002")])
        history = store.list_refresh_status_history()
        assert len(history) == 1
        assert history[0]["当前状态"] == STATUS_FAILED
        assert "归档原因" in history[0]
        detail = svc.get_status_detail("dwd_order_detail", "2026-11-10")
        assert len(detail["history"]) == 1
        assert len(detail["events"]) == 2

    # ------------------------------------------------------------ 质量稽核同步

    def test_quality_sync_marks_failed_partition_and_scope(self):
        svc = DwdRefreshStatusService()
        svc.record_events([make_event(1, status=STATUS_SUCCESS, 完成时间="2026-11-10T09:00:00")])
        svc.sync_quality_result(
            table_name="dwd_order_detail", rule_code="QC-ORD-001",
            audit_scope="order_amount 阈值[0,999999]",
            failed_partitions=["2026-11-10"], total=10, failed=2)
        row = svc.list_status(boundary="wide")[0]
        assert row["当前状态"] == STATUS_AUDIT_FAILED
        assert row["稽核范围"] == "order_amount 阈值[0,999999]"

    def test_quality_pass_only_syncs_scope(self):
        """稽核通过事件不改变刷新结论，只同步稽核范围。"""
        svc = DwdRefreshStatusService()
        svc.record_events([make_event(1, event_type=EVENT_BACKFILL, status=STATUS_SUCCESS,
                                      修复批次="BF-001")])
        svc.sync_quality_result(
            table_name="dwd_order_detail", rule_code="QC-ORD-001",
            audit_scope="全量采样", failed_partitions=[], total=10, failed=0)
        row = svc.list_status(boundary="wide")[0]
        assert row["当前状态"] == STATUS_REPAIRED
        assert row["稽核范围"] == "全量采样"

    # ------------------------------------------------------------ 跨模块调用链

    def test_dwd_build_emits_partition_events_and_diagnostics(self):
        """DWD 构建从只返回总行数升级为按分区诊断，并同步状态视图。"""
        store.set_table_rows("ods_orders", [
            {"order_id": "O001", "order_amount": "100.50", "order_status": "已完成", "dt": "2026-11-01"},
            {"order_id": "O002", "order_amount": "200.00", "order_status": "待支付", "dt": "2026-11-02"},
        ])
        dwd = DwdLayerService()
        result = dwd.build_dwd_from_ods(1)
        assert result["ok"] is True
        assert result["总行数"] == 2
        partitions = {item["分区值"]: item for item in result["分区诊断"]}
        assert set(partitions) == {"2026-11-01", "2026-11-02"}
        view = DwdRefreshStatusService().list_status(table_name="dwd_order_detail")
        assert {row["分区值"] for row in view} == {"2026-11-01", "2026-11-02"}
        assert {row["批次编号"] for row in view} == {result["批次编号"]}

    def test_backfill_repairs_failed_dwd_partition(self):
        """补数调度沿调用链修正明细表结果：失败分区转为已修复并记录修复批次。"""
        svc = DwdRefreshStatusService()
        svc.record_events([make_event(1, status=STATUS_FAILED)])
        # 构造作用在 dwd_order_detail 上的补数任务（seed 里没有该目标表的任务）
        bf = BackfillJobService()
        entry, missing = bf.create_entry({
            "任务编号": "BF-DWD-001", "目标表名": "dwd_order_detail",
            "起始日期": "2026-11-10", "截止日期": "2026-11-10",
            "覆盖策略": "覆盖写入",
        })
        assert not missing
        bf.run_action(entry["id"], "启动任务")
        row = svc.list_status(boundary="wide")[0]
        assert row["当前状态"] == STATUS_REPAIRED
        assert row["修复批次"].startswith("BF-BF-DWD-001-")
        assert row["失败重试链"][0]["状态"] == STATUS_FAILED

    def test_quality_check_execution_syncs_dwd_view(self):
        """质量稽核执行后跨模块同步明细刷新状态视图。"""
        # seed 的 QC 规则作用在 ODS 表上，这里新建一条 DWD 表规则
        qc = QualityCheckService()
        entry, missing = qc.create_entry({
            "规则编号": "QC-DWD-001", "校验表名": "dwd_order_detail",
            "校验字段": "order_amount", "校验表达式": ">=0 AND <=999999",
            "阈值下界": "0", "阈值上界": "999999", "阻断级别": "阻断", "采样比例": "1.0",
        })
        assert not missing
        store.set_table_rows("dwd_order_detail", [
            {"order_id": "O001", "order_amount": "100.00", "dt": "2026-11-10"},
            {"order_id": "O002", "order_amount": "-1.00", "dt": "2026-11-10"},
        ])
        result = qc.execute_check(entry["id"])
        assert result["ok"] is True
        row = DwdRefreshStatusService().list_status(boundary="wide")[0]
        assert row["当前状态"] == STATUS_AUDIT_FAILED
        assert "order_amount" in row["稽核范围"]

    # ------------------------------------------------------------ 看板与详情

    def test_dashboard_groups_by_scenario(self):
        svc = DwdRefreshStatusService()
        svc.record_events([
            make_event(1, partition="2026-11-10", status=STATUS_PENDING),
            make_event(2, partition="2026-11-11", status=STATUS_FAILED),
            make_event(3, table="dwd_payment_detail", partition="2026-11-10",
                       status=STATUS_SUCCESS, 完成时间="2026-11-10T08:00:00"),
        ])
        board = svc.dashboard()
        counts = {card["label"]: card["value"] for card in board["cards"]}
        assert counts["分区总数"] == 3
        assert counts["待刷新分区"] == 1
        assert counts["失败分区"] == 1
        assert counts["刷新成功分区"] == 1
        assert {item["表名称"] for item in board["tables"]} == {
            "dwd_order_detail", "dwd_payment_detail"}
        assert board["failed_partitions"][0]["分区值"] == "2026-11-11"

    def test_detail_returns_watermark_scope_and_chain(self):
        svc = DwdRefreshStatusService()
        svc.record_events([
            make_event(1, status=STATUS_FAILED, 失败原因="清洗为空"),
            make_event(2, event_type=EVENT_QUALITY, status=STATUS_AUDIT_FAILED,
                       稽核规则="QC-ORD-001", 稽核范围="order_amount 全量", 失败原因="越界"),
        ])
        detail = svc.get_status_detail("dwd_order_detail", "2026-11-10")
        assert detail is not None
        assert detail["current"]["刷新水位"] == "2026-11-10"
        assert detail["current"]["稽核范围"] == "order_amount 全量"
        assert len(detail["events"]) == 2
