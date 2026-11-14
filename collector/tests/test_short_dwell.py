"""短驻留大额挂单检测：纯逻辑规则 + 同步管理器集成。"""
import asyncio

import pytest

from app.config import Settings
from app.dwell_detector import ShortDwellDetector
from app.orderbook import Change, GapError, StaleSnapshotError
from app.sync_manager import SyncManager

SYMBOL = "CU2601"


def chg(side, price, qty):
    return Change(side=side, price=price, qty=qty)


def fresh_detector(**kw) -> ShortDwellDetector:
    """已装快照基线的检测器：bids 70000:10 / asks 70100:12，seq=100，世代 1。"""
    d = ShortDwellDetector(**kw)
    d.reset(bids={70000: 10}, asks={70100: 12}, seq=100, generation=1)
    return d


# ---------- 检测规则 ----------

def test_basic_event_recorded_with_fields():
    d = fresh_detector()
    d.observe(101, [chg("bid", 69900, 80)])
    d.observe(102, [chg("bid", 69900, 0)])
    events = d.events()
    assert len(events) == 1
    e = events[0]
    assert (e.side, e.price) == ("bid", 69900)
    assert (e.start_seq, e.end_seq, e.dwell) == (101, 102, 1)
    assert e.initial_qty == 80 and e.generation == 1
    assert d.confirmed_total == 1


def test_ask_side_event():
    d = fresh_detector()
    d.observe(101, [chg("ask", 70200, 55)])
    d.observe(102, [chg("ask", 70200, 0)])
    assert d.events()[0].side == "ask"


def test_dwell_boundary_three_confirmed_four_dropped():
    d = fresh_detector()
    d.observe(101, [chg("bid", 69900, 60)])
    for seq in (102, 103):
        d.observe(seq, [chg("bid", 70000, 11)])  # 无关价位的变更
    d.observe(104, [chg("bid", 69900, 0)])       # dwell=3，恰好窗口内
    assert len(d.events()) == 1 and d.events()[0].dwell == 3

    d2 = fresh_detector()
    d2.observe(101, [chg("bid", 69900, 60)])
    for seq in (102, 103, 104):
        d2.observe(seq, [chg("bid", 70000, 11)])
    d2.observe(105, [chg("bid", 69900, 0)])      # dwell=4，超窗丢弃
    assert d2.events() == []


def test_modification_during_dwell_cancels():
    d = fresh_detector()
    d.observe(101, [chg("bid", 69900, 80)])
    d.observe(102, [chg("bid", 69900, 61)])   # 驻留期间改量：候选作废
    d.observe(103, [chg("bid", 69900, 0)])
    assert d.events() == []


def test_reassert_same_qty_keeps_candidate():
    d = fresh_detector()
    d.observe(101, [chg("bid", 69900, 80)])
    d.observe(102, [chg("bid", 69900, 80)])   # 量未变：不算改量
    d.observe(103, [chg("bid", 69900, 0)])
    assert len(d.events()) == 1 and d.events()[0].dwell == 2


def test_same_increment_add_then_remove_is_not_event():
    """同一条增量内先挂后撤，净效果无档，不得伪造事件。"""
    d = fresh_detector()
    d.observe(101, [chg("bid", 69900, 80), chg("bid", 69900, 0)])
    assert d.events() == [] and d.open_candidates == 0


def test_same_increment_net_qty_defines_start():
    """同一条增量内 30 -> 75，按最终盘口计：起点初始量为 75。"""
    d = fresh_detector()
    d.observe(101, [chg("bid", 69900, 30), chg("bid", 69900, 75)])
    d.observe(102, [chg("bid", 69900, 0)])
    assert d.events()[0].initial_qty == 75


def test_existing_level_growing_is_not_start():
    d = fresh_detector()
    d.observe(101, [chg("bid", 70000, 90)])   # 原本有 10 手，加量不是“从无档”
    d.observe(102, [chg("bid", 70000, 0)])
    assert d.events() == []


def test_snapshot_level_is_not_start():
    d = ShortDwellDetector()
    d.reset(bids={69900: 80}, asks={}, seq=50, generation=1)  # 快照自带大档
    d.observe(51, [chg("bid", 69900, 0)])
    assert d.events() == []


def test_below_threshold_no_candidate():
    d = fresh_detector()
    d.observe(101, [chg("bid", 69900, 49)])
    d.observe(102, [chg("bid", 69900, 0)])
    assert d.events() == []


def test_reset_discards_candidates_and_keeps_events():
    d = fresh_detector()
    d.observe(101, [chg("bid", 69910, 70)])
    d.observe(102, [chg("bid", 69910, 0)])    # 世代 1 确认一条
    d.observe(103, [chg("bid", 69900, 66)])   # 世代 1 未闭合候选
    d.reset(bids={69900: 66}, asks={}, seq=200, generation=2)  # 重连：候选不得跨世代拼接
    d.observe(201, [chg("bid", 69900, 0)])
    events = d.events()
    assert len(events) == 1
    assert events[0].generation == 1 and events[0].start_seq == 101


def test_abandon_discards_open_candidates():
    d = fresh_detector()
    d.observe(101, [chg("bid", 69900, 66)])
    d.abandon()                                # 缺号/断流
    d.observe(102, [chg("bid", 69900, 0)])
    assert d.events() == []


def test_duplicate_observe_ignored():
    d = fresh_detector()
    d.observe(101, [chg("bid", 69900, 66)])
    d.observe(101, [chg("bid", 69900, 66)])   # 重复投递：不得重复建候选
    d.observe(102, [chg("bid", 69900, 0)])
    d.observe(102, [chg("bid", 69900, 0)])    # 重复投递：不得重复记事件
    assert len(d.events()) == 1 and d.confirmed_total == 1


def test_capacity_keeps_most_recent():
    d = ShortDwellDetector(capacity=3)
    d.reset(bids={}, asks={}, seq=0, generation=1)
    seq = 0
    for i in range(5):
        price = 69900 - 10 * i
        d.observe(seq + 1, [chg("bid", price, 60)])
        d.observe(seq + 2, [chg("bid", price, 0)])
        seq += 2
    events = d.events()
    assert len(events) == 3
    assert d.confirmed_total == 5
    assert [e.start_seq for e in events] == [9, 7, 5]  # 新的在前


def test_events_limit_and_order():
    d = fresh_detector()
    for i, price in enumerate((69910, 69920, 69930)):
        d.observe(101 + 2 * i, [chg("bid", price, 60)])
        d.observe(102 + 2 * i, [chg("bid", price, 0)])
    assert [e.start_seq for e in d.events(limit=2)] == [105, 103]


# ---------- 与 SyncManager 集成 ----------

def make_manager() -> SyncManager:
    return SyncManager(Settings(symbol=SYMBOL, source_http="http://x", source_ws="ws://x"))


def install(mgr: SyncManager, gen: int, bids=None, seq: int = 100) -> None:
    mgr.generation = gen
    snapshot = {"type": "snapshot", "symbol": SYMBOL, "seq": seq,
                "bids": bids if bids is not None else [[69990, 5]],
                "asks": [[70010, 7]]}
    asyncio.run(mgr._install_snapshot(snapshot, [], gen))


def inc(seq, *changes):
    return {"type": "increment", "symbol": SYMBOL, "seq": seq,
            "changes": [{"side": s, "price": p, "qty": q} for s, p, q in changes]}


def test_buffered_increments_participate_in_detection():
    """快照在途期间缓冲的较新增量也要参与识别，事件带连接世代。"""
    mgr = make_manager()
    mgr.generation = 1
    snapshot = {"type": "snapshot", "symbol": SYMBOL, "seq": 100,
                "bids": [[69990, 5]], "asks": []}
    buffered = [inc(101, ("bid", 69900, 80)), inc(102, ("bid", 69900, 0))]
    asyncio.run(mgr._install_snapshot(snapshot, buffered, gen=1))
    view = mgr.short_dwell_view(100)
    assert view["count"] == 1
    e = view["events"][0]
    assert (e["start_seq"], e["end_seq"], e["dwell"]) == (101, 102, 1)
    assert (e["initial_qty"], e["generation"], e["price"], e["side"]) == (80, 1, 69900, "bid")
    assert mgr.status_view()["short_dwell"]["confirmed_total"] == 1


def test_duplicate_increment_not_recorded_twice():
    mgr = make_manager()
    install(mgr, gen=1)
    asyncio.run(mgr._apply_increment(inc(101, ("bid", 69900, 80))))
    asyncio.run(mgr._apply_increment(inc(101, ("bid", 69900, 80))))  # 重复投递
    asyncio.run(mgr._apply_increment(inc(102, ("bid", 69900, 0))))
    asyncio.run(mgr._apply_increment(inc(102, ("bid", 69900, 0))))   # 重复投递
    view = mgr.short_dwell_view(100)
    assert view["count"] == 1 and view["confirmed_total"] == 1


def test_gap_discards_candidate_and_resync_keeps_confirmed():
    mgr = make_manager()
    install(mgr, gen=1)
    asyncio.run(mgr._apply_increment(inc(101, ("bid", 69910, 70))))
    asyncio.run(mgr._apply_increment(inc(102, ("bid", 69910, 0))))   # 世代 1 确认
    asyncio.run(mgr._apply_increment(inc(103, ("bid", 69900, 66))))  # 未闭合候选
    with pytest.raises(GapError):
        asyncio.run(mgr._apply_increment(inc(108, ("bid", 1, 1))))   # 缺号
    # 新一轮恢复：快照里该档仍在，但只是基线；候选不得跨世代拼接
    install(mgr, gen=2, bids=[[69900, 66]], seq=200)
    asyncio.run(mgr._apply_increment(inc(201, ("bid", 69900, 0))))
    view = mgr.short_dwell_view(100)
    assert view["count"] == 1
    assert view["events"][0]["generation"] == 1
    assert view["events"][0]["start_seq"] == 101


def test_stale_snapshot_does_not_reset_detector():
    mgr = make_manager()
    install(mgr, gen=2)
    asyncio.run(mgr._apply_increment(inc(101, ("bid", 69900, 80))))
    stale = {"type": "snapshot", "symbol": SYMBOL, "seq": 50, "bids": [], "asks": []}
    with pytest.raises(StaleSnapshotError):
        asyncio.run(mgr._install_snapshot(stale, [], gen=1))  # 迟到快照不得动检测器
    asyncio.run(mgr._apply_increment(inc(102, ("bid", 69900, 0))))
    view = mgr.short_dwell_view(100)
    assert view["count"] == 1 and view["events"][0]["generation"] == 2
