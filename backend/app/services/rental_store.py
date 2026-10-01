"""租赁结算专用内存仓储。

与通用的 ``store`` 分开：租赁域是「单（头）—设备（行）—账单（流水）」三层结构，
塞进通用扁平表里反而难维护。真实项目换成数据库时，只替换这一层即可，
service 层看到的仍然是 leases / items / bills 三张表。

金额一律用 ``Decimal`` 存储、按分（ROUND_HALF_UP）保留两位，避免浮点尾差
导致「应收和已收对不上」。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.services.rental_seed import build_rental_seed


def money(value: Any) -> Decimal:
    """把入参统一成两位小数的 Decimal；空值按 0 处理。"""
    if value is None or value == "":
        return Decimal("0.00")
    if isinstance(value, Decimal):
        amount = value
    else:
        amount = Decimal(str(value))
    return amount.quantize(Decimal("0.01"))


class RentalStore:
    def __init__(self) -> None:
        self._ready = False
        self.reset()

    def reset(self) -> None:
        """恢复到种子数据（仅头/行，账单由 bootstrap 补）；测试之间用它清场。"""
        seed = build_rental_seed()
        self.leases: list[dict[str, Any]] = seed["leases"]
        self.items: list[dict[str, Any]] = seed["items"]
        self.bills: list[dict[str, Any]] = seed["bills"]
        self._lease_seq = max((int(row["id"]) for row in self.leases), default=0)
        self._item_seq = max((int(row["id"]) for row in self.items), default=0)
        self._bill_seq = max((int(row["id"]) for row in self.bills), default=0)
        self._ready = False

    def ensure_ready(self) -> None:
        """首次使用前用计费引擎把示例账单补齐（懒加载，避开模块初始化导入环）。"""
        if self._ready:
            return
        # 先置位再引导：bootstrap 内部直接访问 .bills，不应重入
        self._ready = True
        from app.services.rental_seed import bootstrap as seed_bootstrap

        seed_bootstrap(self)

    def reload_seed(self) -> None:
        """重置并重新跑计费引导，供测试回到统一的示例状态。"""
        self.reset()
        self.ensure_ready()

    # ---- 主键序列 -------------------------------------------------------
    def next_lease_id(self) -> int:
        self._lease_seq += 1
        return self._lease_seq

    def next_item_id(self) -> int:
        self._item_seq += 1
        return self._item_seq

    def next_bill_id(self) -> int:
        self._bill_seq += 1
        return self._bill_seq

    # ---- 租赁单 ---------------------------------------------------------
    def find_lease(self, lease_id: int) -> dict[str, Any] | None:
        self.ensure_ready()
        return next((row for row in self.leases if int(row["id"]) == lease_id), None)

    def find_lease_by_no(self, lease_no: str) -> dict[str, Any] | None:
        self.ensure_ready()
        return next((row for row in self.leases if row["租赁单号"] == lease_no), None)

    def lease_items(self, lease_id: int) -> list[dict[str, Any]]:
        self.ensure_ready()
        return [row for row in self.items if int(row["租赁单ID"]) == lease_id]

    def find_item(self, item_id: int) -> dict[str, Any] | None:
        self.ensure_ready()
        return next((row for row in self.items if int(row["id"]) == item_id), None)

    # ---- 账单 -----------------------------------------------------------
    def item_bills(self, item_id: int) -> list[dict[str, Any]]:
        self.ensure_ready()
        rows = [row for row in self.bills if int(row["设备明细ID"]) == item_id]
        return sorted(rows, key=lambda row: (row["起始日期"], int(row["id"])))

    def lease_bills(self, lease_id: int) -> list[dict[str, Any]]:
        self.ensure_ready()
        item_ids = {int(row["id"]) for row in self.lease_items(lease_id)}
        return [row for row in self.bills if int(row["设备明细ID"]) in item_ids]

    def effective_bills(self, lease_id: int | None = None) -> list[dict[str, Any]]:
        """有效账单（冲销单不参与汇总）。"""
        self.ensure_ready()
        rows = self.bills if lease_id is None else self.lease_bills(lease_id)
        return [row for row in rows if row["状态"] == "有效"]

    def add_bill(self, bill: dict[str, Any]) -> dict[str, Any]:
        self.ensure_ready()
        bill.setdefault("id", self.next_bill_id())
        bill.setdefault("状态", "有效")
        for key in ("金额", "已收金额"):
            if key in bill and not isinstance(bill[key], Decimal):
                bill[key] = money(bill[key])
        self.bills.append(bill)
        return bill


rental_store = RentalStore()
