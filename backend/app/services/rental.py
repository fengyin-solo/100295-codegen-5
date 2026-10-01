"""租赁结算业务服务：登记、改租、退租、批次结算、收款核销与台账汇总。

口径约定：
* 金额在引擎/仓储层是 Decimal，对外序列化时统一转 float（两位小数）；
* 应收/已收只从 ``rental_store.bills`` 里取，台账页、租赁单详情、概览
  共用同一份汇总，不做第二处加总；
* 租赁单号天然承担幂等键：同一份单子重复提交直接返回原单。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.services.rental_billing import (
    CENT,
    DEPOSIT_ALGORITHMS,
    apply_payment,
    billed_through,
    create_deposit_bill,
    create_refund_bill,
    deposit_refund_preview,
    generate_rent_bills,
    money,
    parse_date,
    recalc_on_term_change,
    rent_balance,
    settle_batch,
    supersede_refund_bill,
)
from app.services.rental_store import rental_store

REQUIRED_ITEM_FIELDS = ["设备编号", "设备名称", "起租日期", "到期日期", "月租金额", "押金金额", "押金算法"]
RENT_TYPES = ("租金", "租金重算")
DEPOSIT_TYPES = ("押金", "押金退还")


class RentalError(ValueError):
    """参数/状态类错误，路由层转成可读提示。"""


# --------------------------------------------------------------------------
# 序列化：Decimal/date 都转成前端能直接渲染的形式
# --------------------------------------------------------------------------
def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _bill_summary(bills: list[dict[str, Any]]) -> dict[str, Decimal]:
    """对一组账单做带符号汇总。

    租金/押金正数计应收；押金退还是负账单，应收计入「应退」、已收（负）
    计入「已退」。已冲销账单一律不参与。
    """
    rent_due = rent_paid = Decimal("0.00")
    deposit_in = deposit_refund_due = deposit_refund_paid = Decimal("0.00")
    for bill in bills:
        if bill["状态"] != "有效":
            continue
        kind = bill["账单类型"]
        amount, paid = money(bill["金额"]), money(bill["已收金额"])
        if kind in RENT_TYPES:
            rent_due += amount
            rent_paid += paid
        elif kind == "押金":
            deposit_in += amount
        elif kind == "押金退还":
            deposit_refund_due += -amount
            deposit_refund_paid += -paid
    return {
        "租金应收": rent_due.quantize(CENT),
        "租金已收": rent_paid.quantize(CENT),
        "租金未收": max(Decimal("0.00"), rent_due - rent_paid).quantize(CENT),
        "租金预收": max(Decimal("0.00"), rent_paid - rent_due).quantize(CENT),
        "押金已收": deposit_in.quantize(CENT),
        "应退押金": deposit_refund_due.quantize(CENT),
        "已退押金": deposit_refund_paid.quantize(CENT),
        "待退押金": (deposit_refund_due - deposit_refund_paid).quantize(CENT),
    }


def serialize_bill(bill: dict[str, Any]) -> dict[str, Any]:
    data = _jsonable(bill)
    gap = money(bill["金额"]) - money(bill["已收金额"])
    if money(bill["金额"]) < 0:
        # 负账单（应退）：gap 为负表示还有没退的
        data["未结金额"] = float(abs(min(Decimal("0.00"), gap)))
        data["预收金额"] = 0.0
    else:
        data["未结金额"] = float(max(Decimal("0.00"), gap))
        data["预收金额"] = float(max(Decimal("0.00"), -gap))
    return data


def serialize_item(item: dict[str, Any], *, with_bills: bool = False) -> dict[str, Any]:
    item_id = int(item["id"])
    bills = rental_store.item_bills(item_id)
    summary = _bill_summary(bills)
    today = date.today()
    if item.get("实际退租日期"):
        status = "已退租"
    else:
        gap = (parse_date(item["到期日期"]) - today).days
        if gap < 0:
            status = "已逾期"
        elif gap <= 30:
            status = "即将到期"
        else:
            status = "在租"
    data = _jsonable(item)
    data["状态"] = status
    data.update({key: float(value) for key, value in summary.items()})
    data["押金测算"] = _jsonable(deposit_refund_preview(item))
    if with_bills:
        data["账单"] = [serialize_bill(bill) for bill in bills]
    return data


def serialize_lease(lease: dict[str, Any], *, with_items: bool = True) -> dict[str, Any]:
    data = _jsonable(lease)
    items = rental_store.lease_items(int(lease["id"]))
    if with_items:
        data["设备明细"] = [serialize_item(item, with_bills=False) for item in items]
    summary = _bill_summary(rental_store.lease_bills(int(lease["id"])))
    data.update({key: float(value) for key, value in summary.items()})
    data["设备台数"] = len(items)
    data["在租台数"] = sum(1 for item in items if not item.get("实际退租日期"))
    return data


# --------------------------------------------------------------------------
# 服务本体
# --------------------------------------------------------------------------
class RentalService:
    # ---- 租赁单登记 -----------------------------------------------------
    def register_lease(self, values: dict[str, Any]) -> tuple[dict[str, Any], str, bool]:
        """登记租赁单（可含多台设备）。

        返回 (租赁单, 消息, 是否新建)。租赁单号重复时幂等返回原单，
        不产生第二条头、第二份押金。
        """
        lease_no = str(values.get("租赁单号") or "").strip()
        renter = str(values.get("承租方") or "").strip()
        if not lease_no:
            raise RentalError("租赁单号必填，它同时是重复提交的去重依据")
        if not renter:
            raise RentalError("承租方必填")

        existing = rental_store.find_lease_by_no(lease_no)
        if existing is not None:
            return serialize_lease(existing), f"租赁单 {lease_no} 已存在，重复提交未重复入账", False

        raw_items = values.get("设备明细") or values.get("items") or []
        if not isinstance(raw_items, list) or not raw_items:
            raise RentalError("至少登记一台设备的租期信息")

        lease = {
            "id": rental_store.next_lease_id(),
            "租赁单号": lease_no,
            "承租方": renter,
            "联系电话": str(values.get("联系电话") or "").strip(),
            "登记时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "备注": str(values.get("备注") or "").strip(),
        }
        rental_store.leases.append(lease)

        created_items: list[dict[str, Any]] = []
        for raw in raw_items:
            item = self._build_item(lease["id"], raw)
            rental_store.items.append(item)
            created_items.append(item)
            create_deposit_bill(item, batch_no=f"YD-{datetime.now().strftime('%Y%m%d%H%M%S')}-{item['id']}")

        return serialize_lease(lease), f"租赁单 {lease_no} 已登记，设备 {len(created_items)} 台，押金已入账", True

    def _build_item(self, lease_id: int, raw: dict[str, Any]) -> dict[str, Any]:
        missing = [field for field in REQUIRED_ITEM_FIELDS if not str(raw.get(field) or "").strip()]
        if missing:
            raise RentalError(f"设备 {raw.get('设备编号', '?')} 缺少必填字段：{'、'.join(missing)}")
        start = parse_date(raw["起租日期"])
        end = parse_date(raw["到期日期"])
        if end < start:
            raise RentalError(f"设备 {raw['设备编号']} 到期日期早于起租日期")
        monthly = money(raw["月租金额"])
        deposit = money(raw["押金金额"])
        if monthly < 0 or deposit < 0:
            raise RentalError(f"设备 {raw['设备编号']} 月租与押金不能为负")
        algorithm = str(raw["押金算法"]).strip()
        if algorithm not in DEPOSIT_ALGORITHMS:
            raise RentalError(
                f"押金算法需为：{'、'.join(DEPOSIT_ALGORITHMS)}（设备 {raw['设备编号']}）"
            )
        ratio = raw.get("违约金比例", 0)
        try:
            ratio_val = Decimal(str(ratio or 0))
        except Exception as exc:  # noqa: BLE001
            raise RentalError("违约金比例需为数字") from exc
        if ratio_val < 0:
            raise RentalError("违约金比例不能为负")
        return {
            "id": rental_store.next_item_id(),
            "租赁单ID": lease_id,
            "设备编号": str(raw["设备编号"]).strip(),
            "设备名称": str(raw["设备名称"]).strip(),
            "起租日期": start,
            "到期日期": end,
            "月租金额": monthly,
            "押金金额": deposit,
            "押金算法": algorithm,
            "违约金比例": str(ratio_val),
            "实际退租日期": None,
            "变更记录": [],
        }

    # ---- 改租 -----------------------------------------------------------
    def change_term(self, item_id: int, values: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """中途变更租期/月租：记录历史、按最后一次约定重算已出账单。"""
        item = rental_store.find_item(item_id)
        if item is None:
            raise RentalError(f"设备明细 {item_id} 不存在")
        new_start = parse_date(values.get("新起租日期"))
        new_end = parse_date(values.get("新到期日期"))
        if new_end < new_start:
            raise RentalError("变更后的到期日期不能早于起租日期")
        if "新月租金额" in values and money(values["新月租金额"]) < 0:
            raise RentalError("新月租不能为负")
        if "新押金算法" in values and values["新押金算法"]:
            algorithm = str(values["新押金算法"]).strip()
            if algorithm not in DEPOSIT_ALGORITHMS:
                raise RentalError(f"押金算法需为：{'、'.join(DEPOSIT_ALGORITHMS)}")
            item["押金算法"] = algorithm
        if "新违约金比例" in values and str(values["新违约金比例"] or "") != "":
            item["违约金比例"] = str(Decimal(str(values["新违约金比例"])))

        seq = len(item["变更记录"]) + 1
        record = {
            "序号": seq,
            "变更时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "原起租日期": item["起租日期"].isoformat(),
            "原到期日期": item["到期日期"].isoformat(),
            "原月租金额": str(money(item["月租金额"])),
            "新起租日期": new_start.isoformat(),
            "新到期日期": new_end.isoformat(),
            "新月租金额": str(money(values.get("新月租金额", item["月租金额"]))),
            "变更原因": str(values.get("变更原因") or "").strip(),
        }
        item["起租日期"] = new_start
        item["到期日期"] = new_end
        item["月租金额"] = money(values.get("新月租金额", item["月租金额"]))
        item["变更记录"].append(record)

        revised = recalc_on_term_change(item, change_seq=seq)
        refund = supersede_refund_bill(
            item, batch_no=f"CG-{datetime.now().strftime('%Y%m%d%H%M%S')}-{item_id}", change_seq=seq
        )
        detail = serialize_item(item, with_bills=True)
        tail = f"，押金退还单同步重开" if refund is not None else ""
        return detail, (
            f"租期第 {seq} 次变更已生效，原账单保留原值，重算 {len(revised)} 个已出账期{tail}"
        )

    # ---- 退租登记 -------------------------------------------------------
    def register_return(self, values: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        """登记一批设备的实际退租日期（同批约定同一天退租）。

        只登记退租事实，不出账；入账发生在「批次结算」。
        """
        item_ids = values.get("设备明细IDs") or values.get("item_ids") or []
        return_day = parse_date(values.get("退租日期"))
        if not item_ids:
            raise RentalError("请选择本批退租的设备")
        if return_day is None:
            raise RentalError("退租日期必填")
        updated: list[dict[str, Any]] = []
        for raw_id in item_ids:
            item = rental_store.find_item(int(raw_id))
            if item is None:
                raise RentalError(f"设备明细 {raw_id} 不存在")
            if return_day < parse_date(item["起租日期"]):
                raise RentalError(f"设备 {item['设备编号']} 退租日期早于起租日期")
            item["实际退租日期"] = return_day
            updated.append(item)
        return [serialize_item(item) for item in updated], (
            f"已登记 {len(updated)} 台设备于 {return_day} 退租，待批次结算入账"
        )

    # ---- 批次结算 -------------------------------------------------------
    def settle(self, values: dict[str, Any]) -> dict[str, Any]:
        """按设备逐台生成月租并对已退租的入账；没退租的留到下一批。

        结算后按最新约定算法生成押金退还单（幂等）。
        """
        cutoff = parse_date(values.get("结算日期"))
        if cutoff is None:
            raise RentalError("结算日期必填")
        item_ids = values.get("设备明细IDs") or values.get("item_ids")
        ids = [int(raw) for raw in item_ids] if item_ids else None
        bills, settled, skipped = settle_batch(cutoff, ids)

        refunds: list[dict[str, Any]] = []
        for item in settled:
            refund = create_refund_bill(item, batch_no=f"JS-{cutoff.strftime('%Y%m%d')}")
            if refund is not None:
                refunds.append(refund)
        return {
            "结算日期": cutoff.isoformat(),
            "本次入账设备": [serialize_item(item, with_bills=True) for item in settled],
            "生成租金账单": [serialize_bill(bill) for bill in bills],
            "押金退还单": [serialize_bill(bill) for bill in refunds],
            "跳过": skipped,
        }

    # ---- 账单与收款 -----------------------------------------------------
    def list_bills(
        self,
        *,
        lease_id: int | None = None,
        item_id: int | None = None,
        bill_type: str | None = None,
        include_superseded: bool = True,
    ) -> list[dict[str, Any]]:
        bills = rental_store.bills
        if lease_id is not None:
            item_ids = {int(row["id"]) for row in rental_store.lease_items(lease_id)}
            bills = [row for row in bills if int(row["设备明细ID"]) in item_ids]
        if item_id is not None:
            bills = [row for row in bills if int(row["设备明细ID"]) == item_id]
        if bill_type:
            bills = [row for row in bills if row["账单类型"] == bill_type]
        if not include_superseded:
            bills = [row for row in bills if row["状态"] == "有效"]
        bills = sorted(bills, key=lambda row: (int(row["设备明细ID"]), row["起始日期"], int(row["id"])))
        return [serialize_bill(bill) for bill in bills]

    def receive_payment(self, bill_id: int, values: dict[str, Any]) -> dict[str, Any]:
        bill = next((row for row in rental_store.bills if int(row["id"]) == bill_id), None)
        if bill is None:
            raise RentalError(f"账单 {bill_id} 不存在")
        try:
            amount = money(values.get("金额"))
        except Exception as exc:  # noqa: BLE001
            raise RentalError("收款金额需为数字") from exc
        if amount <= 0:
            raise RentalError("收款金额必须大于 0")
        try:
            applied, left = apply_payment(
                bill, amount, at=str(values.get("收款时间") or datetime.now().strftime("%Y-%m-%d %H:%M"))
            )
        except ValueError as exc:
            raise RentalError(str(exc)) from exc
        kind = "退款" if bill["账单类型"] == "押金退还" else "收款"
        return {
            "账单": serialize_bill(bill),
            f"本次{kind}": float(applied),
            "剩余未结": float(left),
            "message": f"本次{kind} {applied} 元，剩余未结 {left} 元",
        }

    # ---- 查询与汇总 -----------------------------------------------------
    def list_leases(self, keyword: str | None = None) -> list[dict[str, Any]]:
        leases = rental_store.leases
        if keyword:
            leases = [
                row for row in leases
                if keyword in str(row["租赁单号"]) or keyword in str(row["承租方"])
            ]
        return [serialize_lease(row) for row in leases]

    def lease_detail(self, lease_id: int) -> dict[str, Any]:
        lease = rental_store.find_lease(lease_id)
        if lease is None:
            raise RentalError(f"租赁单 {lease_id} 不存在")
        data = serialize_lease(lease)
        data["设备明细"] = [
            serialize_item(item, with_bills=True) for item in rental_store.lease_items(lease_id)
        ]
        return data

    def item_detail(self, item_id: int) -> dict[str, Any]:
        item = rental_store.find_item(item_id)
        if item is None:
            raise RentalError(f"设备明细 {item_id} 不存在")
        return serialize_item(item, with_bills=True)

    def ledger(self) -> dict[str, Any]:
        """租赁台账：账单全量 + 从同一份账单算出的汇总。"""
        rental_store.ensure_ready()
        bills = [serialize_bill(row) for row in sorted(
            rental_store.bills, key=lambda row: (int(row["设备明细ID"]), row["起始日期"], int(row["id"]))
        )]
        summary = _bill_summary(rental_store.bills)
        items = rental_store.items
        today = date.today()
        pending_settle = 0
        for item in items:
            if not item.get("实际退租日期"):
                continue
            covered = billed_through(int(item["id"]))
            if covered is None or covered < parse_date(item["实际退租日期"]):
                pending_settle += 1
        payload = {
            "账单": bills,
            "汇总": {key: float(value) for key, value in summary.items()},
            "在租台数": sum(1 for item in items if not item.get("实际退租日期")),
            "设备总台数": len(items),
            "已退租台数": sum(1 for item in items if item.get("实际退租日期")),
            "待结算台数": pending_settle,
            "当前日期": today.isoformat(),
        }
        return payload

    def overview(self) -> dict[str, Any]:
        """租赁概览：在租台数等指标全部随设备明细与账单实时重算。"""
        ledger = self.ledger()
        items = rental_store.items
        due_soon = sum(
            1 for item in items
            if not item.get("实际退租日期")
            and 0 <= (parse_date(item["到期日期"]) - date.today()).days <= 30
        )
        overdue = sum(
            1 for item in items
            if not item.get("实际退租日期") and parse_date(item["到期日期"]) < date.today()
        )
        summary = ledger["汇总"]
        cards = [
            {"label": "在租台数", "value": ledger["在租台数"]},
            {"label": "已退租台数", "value": ledger["已退租台数"]},
            {"label": "待结算台数", "value": ledger["待结算台数"]},
            {"label": "30日内到期", "value": due_soon},
            {"label": "已逾期在租", "value": overdue},
            {"label": "租金应收（元）", "value": round(summary["租金应收"], 2)},
            {"label": "租金已收（元）", "value": round(summary["租金已收"], 2)},
            {"label": "租金未收（元）", "value": round(summary["租金未收"], 2)},
            {"label": "租金预收（元）", "value": round(summary["租金预收"], 2)},
            {"label": "押金待退（元）", "value": round(summary["待退押金"], 2)},
        ]
        leases = self.list_leases()
        return {"cards": cards, "租赁单": leases}
