"""短驻留大额挂单检测（纯逻辑，不依赖 asyncio，便于单测）。

事件定义（同侧、同价位）：
- 起点：一条增量应用后，该价位从“无档”变为 qty >= min_qty；
- 终点：随后 max_dwell 条增量内该价位归零（qty=0），且期间 qty 未被改动；
- 判定一律按“每条增量应用后的最终盘口”：同一条增量内的多次变更只算净效果，
  先挂后撤、先小后大等组合都不能伪造或改写事件；
- 快照已有档位只作基线、不作起点；快照在途期间缓冲的较新增量照常参与识别；
- 缺号 / 断流 / 重连时未闭合候选一律丢弃（abandon/reset），
  已确认事件保留，且绝不跨连接世代拼接。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .orderbook import Change


@dataclass(frozen=True)
class ShortDwellEvent:
    """一次已确认的短驻留大额挂单。"""

    side: str         # "bid" | "ask"
    price: int        # 价位
    start_seq: int    # 建立档位的增量序号
    end_seq: int      # 档位归零的增量序号
    initial_qty: int  # 建立时的量（起点增量应用后的最终量）
    generation: int   # 连接世代（事件绝不跨世代拼接）

    @property
    def dwell(self) -> int:
        """驻留时长：从建立到归零间隔的增量条数。"""
        return self.end_seq - self.start_seq

    def to_dict(self) -> dict:
        return {
            "side": self.side,
            "price": self.price,
            "start_seq": self.start_seq,
            "end_seq": self.end_seq,
            "dwell": self.dwell,
            "initial_qty": self.initial_qty,
            "generation": self.generation,
        }


@dataclass
class _Candidate:
    """未闭合候选：已见大额新档，等待窗口内归零。"""

    start_seq: int
    initial_qty: int


class ShortDwellDetector:
    """按增量流检测短驻留大额挂单。

    使用约定（由 SyncManager 保证）：
    - reset() 安装快照基线；之后 observe() 只喂“实际落簿”的增量，
      序号从快照序号 +1 开始逐条连续；重复增量不得传入；
    - 缺号 / 断流时调 abandon()；新一轮快照安装时调 reset()。

    内部维护一份影子盘口，与订单簿吃同一批快照 + 增量，状态天然一致。
    """

    def __init__(self, min_qty: int = 50, max_dwell: int = 3, capacity: int = 100) -> None:
        if min_qty <= 0:
            raise ValueError("min_qty must be positive")
        if max_dwell <= 0:
            raise ValueError("max_dwell must be positive")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.min_qty = min_qty
        self.max_dwell = max_dwell
        self.capacity = capacity
        self._events: deque[ShortDwellEvent] = deque(maxlen=capacity)
        self.confirmed_total = 0  # 累计确认数（不受容量截断影响）
        self._candidates: dict[tuple[str, int], _Candidate] = {}
        self._bids: dict[int, int] = {}
        self._asks: dict[int, int] = {}
        self._generation = 0
        self._last_seq: int | None = None

    # ---------- 生命周期 ----------

    def reset(self, bids: dict[int, int], asks: dict[int, int], seq: int, generation: int) -> None:
        """安装新快照：已有档位只作基线、不作起点；未闭合候选全部丢弃。"""
        self._bids = dict(bids)
        self._asks = dict(asks)
        self._last_seq = seq
        self._generation = generation
        self._candidates.clear()

    def abandon(self) -> None:
        """缺号 / 断流 / 重连：丢弃未闭合候选；已确认事件保留。"""
        self._candidates.clear()

    # ---------- 增量观察 ----------

    def observe(self, seq: int, changes: list[Change]) -> None:
        """观察一条已落簿的增量，按“本条应用后的最终盘口”判定。"""
        if self._last_seq is not None and seq <= self._last_seq:
            return  # 重复/乱序防御：正常路径上层已去重，这里保证幂等
        # 同一条增量内同档多次变更只取净效果（最后一次为准），
        # 因此“同条内先挂后撤”“同条内先小后大”都不能伪造或改写事件
        net: dict[tuple[str, int], int] = {}
        for c in changes:
            net[(c.side, c.price)] = c.qty
        for (side, price), qty in net.items():
            book = self._bids if side == "bid" else self._asks
            before = book.get(price, 0)
            key = (side, price)
            cand = self._candidates.get(key)
            if cand is not None:
                if qty == 0:
                    if seq - cand.start_seq <= self.max_dwell:
                        self._confirm(side, price, cand, seq)
                    del self._candidates[key]
                elif qty != cand.initial_qty:
                    del self._candidates[key]  # 驻留期间改量：候选作废
                # qty == cand.initial_qty：量未变，候选继续驻留
            elif before == 0 and qty >= self.min_qty:
                self._candidates[key] = _Candidate(start_seq=seq, initial_qty=qty)
            # 影子盘口与订单簿吃同一批增量，同步推进
            if qty == 0:
                book.pop(price, None)
            else:
                book[price] = qty
        # 驻留窗口已尽仍未归零的候选丢弃（终点必须满足 end_seq - start_seq <= max_dwell）
        expired = [k for k, c in self._candidates.items() if seq - c.start_seq >= self.max_dwell]
        for k in expired:
            del self._candidates[k]
        self._last_seq = seq

    def _confirm(self, side: str, price: int, cand: _Candidate, end_seq: int) -> None:
        self._events.append(ShortDwellEvent(
            side=side,
            price=price,
            start_seq=cand.start_seq,
            end_seq=end_seq,
            initial_qty=cand.initial_qty,
            generation=self._generation,
        ))
        self.confirmed_total += 1

    # ---------- 查询 ----------

    def events(self, limit: int | None = None) -> list[ShortDwellEvent]:
        """最近的事件，新的在前。"""
        n = len(self._events) if limit is None else max(0, min(limit, len(self._events)))
        return [self._events[-1 - i] for i in range(n)]

    @property
    def open_candidates(self) -> int:
        return len(self._candidates)
