"""设备外租结算业务规则。

所有"会算的台账"逻辑都收在这一层，路由层不做判断：

- 租赁登记按租赁单号幂等，同一份单子重复提交只入账一次；
- 一批设备可登记同一天退租，结算按设备逐台生成月租，未退租的设备本批不出账、留到下一批；
- 押金到期按登记时约定的算法退（支持扣减比例与逾期日扣），结果以负数账单入账（应退）；
- 中途变更租期/租金/押金约定后，已出账单保留原值，差额以调整账单重算入账；
- 台账页应收/已收与概览在租台数全部由本服务的同一份汇总函数算出，不允许各处各算。
"""
from __future__ import annotations

import calendar
from datetime import date, datetime
from typing import Any

from app.store import store

LEASE_TABLE = "rental_leases"
LINE_TABLE = "rental_lines"
BILL_TABLE = "rental_bills"
PAYMENT_TABLE = "rental_payments"
AMEND_TABLE = "rental_amendments"

# 押金约定算法：登记时选定，退租时按同一算法结算，中途变更则以最后一次约定重算。
DEPOSIT_RULES: dict[str, tuple[float, str]] = {
    "全额退还": (1.0, "到期全额退还；逾期每日按押金 0.5% 扣减，扣完为止"),
    "扣10%押金": (0.9, "到期退还 90%；逾期每日另按押金 0.5% 扣减，扣完为止"),
    "扣20%押金": (0.8, "到期退还 80%；逾期每日另按押金 0.5% 扣减，扣完为止"),
}
OVERDUE_RATE = 0.005

RENT_FAMILY = ("月租", "尾期租金", "租金调整")
DEPOSIT_FAMILY = ("押金结算", "押金调整")

STATUS_RENTING = "在租"
STATUS_PARTIAL = "部分退租"
STATUS_RETURNED = "已退租"
STATUS_SETTLED = "已结清"


def money(value: float) -> float:
    """金额统一保留两位小数；加极小偏移避开浮点表示误差导致的多一分钱。"""
    return round(value + 1e-9, 2)


def parse_date(value: Any) -> date | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def add_months(day: date, months: int) -> date:
    month_index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(month_index, 12)
    month += 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def month_diff(later: date, earlier: date) -> int:
    return (later.year - earlier.year) * 12 + later.month - earlier.month


def deposit_rule_meta(name: str, custom_ratio: Any = None) -> tuple[float, str]:
    if name in DEPOSIT_RULES:
        return DEPOSIT_RULES[name]
    try:
        ratio = float(custom_ratio)
    except (TypeError, ValueError):
        ratio = 1.0
    ratio = min(max(ratio, 0.0), 1.0)
    return ratio, f"到期按 {money(ratio * 100):g}% 退还；逾期每日另按押金 0.5% 扣减，扣完为止"


class RentalService:
    # ------------------------------------------------------------------ 读取

    def list_leases(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(LEASE_TABLE))
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("租赁单号", ""))
                or keyword in str(row.get("承租方", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows.sort(key=lambda row: int(row["id"]), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = rows[start:start + size]
        return [self._lease_view(row) for row in page_rows], total

    def get_lease(self, lease_id: int) -> dict[str, Any] | None:
        lease = store.find(LEASE_TABLE, lease_id)
        if lease is None:
            return None
        lines = self._lines(lease_id)
        bills = self._bills(lease_id)
        payments = self._payments(lease_id)
        amendments = store.rows(AMEND_TABLE)
        return {
            "lease": self._lease_view(lease),
            "lines": lines,
            "bills": sorted(bills, key=lambda row: (int(row["line_id"]), int(row["id"]))),
            "payments": sorted(payments, key=lambda row: int(row["id"])),
            "amendments": [row for row in amendments if int(row["lease_id"]) == lease_id],
            "summary": self._summary(lease=lease, lines=lines, bills=bills, payments=payments),
        }

    def list_bills(self, lease_id: int | None = None, line_id: int | None = None) -> list[dict[str, Any]]:
        bills = list(store.rows(BILL_TABLE))
        if lease_id is not None:
            bills = [row for row in bills if int(row["lease_id"]) == lease_id]
        if line_id is not None:
            bills = [row for row in bills if int(row["line_id"]) == line_id]
        return sorted(bills, key=lambda row: int(row["id"]))

    def list_payments(self, lease_id: int | None = None) -> list[dict[str, Any]]:
        payments = list(store.rows(PAYMENT_TABLE))
        if lease_id is not None:
            payments = [row for row in payments if int(row["lease_id"]) == lease_id]
        return sorted(payments, key=lambda row: int(row["id"]))

    # ------------------------------------------------------------ 汇总（单一数据源）

    def summary(self) -> dict[str, Any]:
        """台账页与租赁概览共用的汇总口径：任何页面读到的数都从这里取。"""
        leases = store.rows(LEASE_TABLE)
        lines = store.rows(LINE_TABLE)
        bills = store.rows(BILL_TABLE)
        payments = store.rows(PAYMENT_TABLE)
        totals = self._summary(lease=None, lines=lines, bills=bills, payments=payments)
        totals["在租台数"] = sum(1 for line in lines if not line.get("实退日期"))
        totals["租赁单数"] = len(leases)
        totals["设备总台数"] = len(lines)
        return totals

    def _summary(
        self,
        *,
        lease: dict[str, Any] | None,
        lines: list[dict[str, Any]],
        bills: list[dict[str, Any]],
        payments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """同一份汇总逻辑：传 lease 时只算这张单子，不传时算全量。"""
        rent_receivable = 0.0
        deposit_refundable = 0.0
        for bill in bills:
            amount = float(bill.get("金额", 0.0))
            if bill.get("类型") in RENT_FAMILY:
                rent_receivable += amount
            elif bill.get("类型") in DEPOSIT_FAMILY:
                deposit_refundable += -amount
        rent_received = sum(float(p["金额"]) for p in payments if p.get("种类") == "租金")
        deposit_received = sum(float(p["金额"]) for p in payments if p.get("种类") == "押金")
        deposit_refunded = sum(float(p["金额"]) for p in payments if p.get("种类") == "押金退款")
        data = {
            "应收租金": money(rent_receivable),
            "已收租金": money(rent_received),
            "未收租金": money(rent_receivable - rent_received),
            "已收押金": money(deposit_received),
            "应退押金": money(deposit_refundable),
            "已退押金": money(deposit_refunded),
            "未退押金": money(deposit_refundable - deposit_refunded),
        }
        if lease is not None:
            total = len(lines)
            returned = sum(1 for line in lines if line.get("实退日期"))
            data["设备总台数"] = total
            data["已退租台数"] = returned
            data["在租台数"] = total - returned
        return data

    # ------------------------------------------------------------------ 登记

    def create_lease(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], bool]:
        """登记租赁单并返回 (单据, 缺失字段, 是否重复提交被幂等跳过)。"""
        required = ["租赁单号", "承租方", "起租日", "约定到期日", "押金规则", "devices"]
        missing = [field for field in required if not values.get(field)]
        if missing:
            return None, missing, False

        lease_no = str(values["租赁单号"]).strip()
        leases = store.rows(LEASE_TABLE)
        duplicated = next((row for row in leases if row.get("租赁单号") == lease_no), None)
        if duplicated is not None:
            # 同一份租赁单重复提交：不再入账，直接回传原单，由调用方提示幂等命中。
            return self._lease_view(duplicated), [], True

        start = parse_date(values["起租日"])
        planned_end = parse_date(values["约定到期日"])
        if start is None or planned_end is None:
            return None, ["起租日/约定到期日（需为 YYYY-MM-DD）"], False
        if planned_end < start:
            return None, ["约定到期日不能早于起租日"], False

        devices = values.get("devices") or []
        if not isinstance(devices, list) or not devices:
            return None, ["devices"], False

        ratio, rule_desc = deposit_rule_meta(str(values["押金规则"]), values.get("退还比例"))
        lease_id = self._next_id(LEASE_TABLE)
        lease = {
            "id": lease_id,
            "租赁单号": lease_no,
            "承租方": str(values["承租方"]).strip(),
            "起租日": start.isoformat(),
            "约定到期日": planned_end.isoformat(),
            "押金规则": str(values["押金规则"]).strip(),
            "退还比例": ratio,
            "押金规则说明": rule_desc,
            "变更次数": 0,
            "status": STATUS_RENTING,
            "pending": True,
            "abnormal": False,
        }
        leases.append(lease)

        clean_devices: list[dict[str, Any]] = []
        for index, device in enumerate(devices, start=1):
            parsed, error = self._parse_device(device)
            if error:
                leases.remove(lease)
                return None, [f"第 {index} 台设备：{error}"], False
            clean_devices.append(parsed)

        collect_deposit = bool(values.get("立即收押金"))
        paid_date = values.get("押金收取日期")
        for seq, device in enumerate(clean_devices, start=1):
            line = self._append_line(lease_id, seq, lease, device)
            if collect_deposit:
                key = f"pay:deposit:{lease_id}:{line['id']}"
                self._append_payment(
                    lease_id,
                    int(line["id"]),
                    "押金",
                    float(device["押金"]),
                    parse_date(paid_date) or start,
                    f"{lease_no}-押金{seq}",
                    f"登记 {lease_no} 第 {seq} 台设备押金",
                    key,
                )
        self._refresh_status(lease)
        return self._lease_view(lease), [], False

    def _parse_device(self, device: Any) -> tuple[dict[str, Any] | None, str]:
        if not isinstance(device, dict):
            return None, "设备明细格式不正确"
        for field in ("设备编号", "设备名称", "月租金", "押金"):
            if device.get(field) in (None, ""):
                return None, f"缺少 {field}"
        try:
            rate = float(device["月租金"])
            deposit = float(device["押金"])
        except (TypeError, ValueError):
            return None, "月租金与押金必须是数字"
        if rate < 0 or deposit < 0:
            return None, "月租金与押金不能为负数"
        return {
            "设备编号": str(device["设备编号"]).strip(),
            "设备名称": str(device["设备名称"]).strip(),
            "月租金": money(rate),
            "押金": money(deposit),
        }, ""

    def _append_line(
        self,
        lease_id: int,
        seq: int,
        lease: dict[str, Any],
        device: dict[str, Any],
    ) -> dict[str, Any]:
        line = {
            "id": self._next_id(LINE_TABLE),
            "lease_id": lease_id,
            "行号": seq,
            "设备编号": device["设备编号"],
            "设备名称": device["设备名称"],
            "月租金": device["月租金"],
            "押金": device["押金"],
            "起租日": lease["起租日"],
            "约定到期日": lease["约定到期日"],
            "押金规则": lease["押金规则"],
            "退还比例": lease["退还比例"],
            "实退日期": None,
            "settled": False,
        }
        store.rows(LINE_TABLE).append(line)
        return line

    # ------------------------------------------------------------------ 退租

    def register_returns(
        self, lease_id: int, items: list[dict[str, Any]], return_date: Any
    ) -> tuple[dict[str, Any] | None, str]:
        """一批设备登记同一天退租；未提交的设备保持在租，留给下一批结算。"""
        lease = store.find(LEASE_TABLE, lease_id)
        if lease is None:
            return None, f"租赁单 {lease_id} 不存在"
        if not items:
            return None, "请至少选择一台退租设备"

        batch_date = parse_date(return_date)
        if batch_date is None:
            return None, "退租日期需为 YYYY-MM-DD"

        lines = {int(line["id"]): line for line in self._lines(lease_id)}
        targets: list[dict[str, Any]] = []
        for item in items:
            line_id = self._as_int(item.get("line_id") if isinstance(item, dict) else item)
            line = lines.get(line_id)
            if line is None:
                return None, f"设备行 {line_id} 不属于本租赁单"
            if line.get("实退日期"):
                return None, f"设备 {line['设备编号']} 已登记退租，不能重复登记"
            if batch_date < parse_date(line["起租日"]):
                return None, f"设备 {line['设备编号']} 退租日期早于起租日"
            item_date = parse_date(item.get("实退日期")) if isinstance(item, dict) else None
            line["_batch_date"] = (item_date or batch_date).isoformat()
            targets.append(line)

        for line in targets:
            line["实退日期"] = line.pop("_batch_date")
        self._refresh_status(lease)
        return self._lease_view(lease), f"已登记 {len(targets)} 台设备退租，在租 {self._lease_view(lease)['在租台数']} 台"

    # ------------------------------------------------------------------ 结算

    def settle(self, lease_id: int, as_of: Any = None) -> tuple[dict[str, Any] | None, str]:
        """按设备逐台出账：只对已退租且未结算的设备生成月租与押金账单。

        重复结算安全：每张账单有幂等键，已入账的不会再生成第二张；
        未退租设备本批跳过，等下一批退租后再结。
        """
        lease = store.find(LEASE_TABLE, lease_id)
        if lease is None:
            return None, f"租赁单 {lease_id} 不存在"
        bill_date = parse_date(as_of) or date.today()
        batch_no = self._next_batch_no(bill_date)

        created: list[dict[str, Any]] = []
        skipped = 0
        for line in self._lines(lease_id):
            if not line.get("实退日期") or line.get("settled"):
                if not line.get("实退日期"):
                    skipped += 1
                continue
            created.extend(self._bill_line(line, lease, bill_date, batch_no))
            line["settled"] = True
        self._refresh_status(lease)

        if not created:
            tail = f"，{skipped} 台仍在租、留待下一批" if skipped else ""
            return self._lease_view(lease), f"本批没有新增账单（已入账的不重复出账{tail}）"
        message = f"批次 {batch_no} 新入账 {len(created)} 张账单"
        if skipped:
            message += f"，{skipped} 台未退租留到下一批"
        return self._lease_view(lease), message

    def _bill_line(
        self,
        line: dict[str, Any],
        lease: dict[str, Any],
        bill_date: date,
        batch_no: str,
    ) -> list[dict[str, Any]]:
        start = parse_date(line["起租日"])
        end = parse_date(line["实退日期"])
        rate = float(line["月租金"])
        created: list[dict[str, Any]] = []

        # 满月按月租出账，不足月的零头按天折算（日租 = 月租 / 30）。
        full_months, remainder_days = self._rent_periods(start, end)
        for seq in range(full_months):
            period_start = add_months(start, seq)
            period_end = add_months(start, seq + 1)
            key = f"rent:{line['id']}:{seq}"
            bill = self._get_bill(key)
            if bill is None:
                bill = self._append_bill(
                    lease, line, "月租", money(rate), period_start, period_end,
                    bill_date, batch_no, key,
                    f"{period_start.strftime('%Y-%m')} 月租（第 {seq + 1} 个月，满月）",
                )
            created.append(bill)
        if remainder_days > 0:
            period_start = add_months(start, full_months)
            amount = money(rate / 30.0 * remainder_days)
            key = f"rent:{line['id']}:tail:{full_months}"
            bill = self._get_bill(key)
            if bill is None:
                bill = self._append_bill(
                    lease, line, "尾期租金", amount, period_start, end,
                    bill_date, batch_no, key,
                    f"退租零头 {remainder_days} 天，按日租（月租/30）折算",
                )
            created.append(bill)

        # 押金按约定算法在退租当日结算；应退金额以负数账单入账。
        deposit_key = f"deposit:{line['id']}:v{lease['变更次数']}"
        if self._get_bill(deposit_key) is None:
            refund = self._deposit_refund(line)
            self._append_bill(
                lease, line, "押金结算", money(-refund), start, end,
                bill_date, batch_no, deposit_key,
                f"按约定「{line['押金规则']}」结算押金，应退 {money(refund):.2f}",
            )
        return created

    @staticmethod
    def _rent_periods(start: date, end: date) -> tuple[int, int]:
        """把租期拆成 (满月个数, 零头天数)；起租当日退租按 0 天计。"""
        if end <= start:
            return 0, 0
        diff = month_diff(end, start)
        if end.day >= start.day:
            full = diff
        else:
            full = diff - 1
        full = max(full, 0)
        anchor = add_months(start, full)
        remainder = (end - anchor).days
        return full, max(remainder, 0)

    def _deposit_refund(self, line: dict[str, Any]) -> float:
        deposit = float(line["押金"])
        ratio = float(line.get("退还比例", 1.0))
        end = parse_date(line["实退日期"])
        planned = parse_date(line["约定到期日"])
        overdue_days = max(0, (end - planned).days)
        penalty = deposit * (1.0 - ratio) + deposit * OVERDUE_RATE * overdue_days
        return money(max(0.0, deposit - penalty))

    # ------------------------------------------------------------------ 变更

    def amend_lease(self, lease_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """中途改约定：新约定对后续生效；已出账单原值保留，差额以调整账单重算。

        以 client_token 幂等：同一份变更提交两次只生效一次。
        """
        lease = store.find(LEASE_TABLE, lease_id)
        if lease is None:
            return None, f"租赁单 {lease_id} 不存在"
        token = str(values.get("client_token") or "").strip()
        if token and any(row.get("幂等键") == f"amend:{token}" for row in store.rows(AMEND_TABLE)):
            return self._lease_view(lease), "变更申请重复提交，已跳过（幂等）"

        change_date = parse_date(values.get("变更日")) or date.today()
        new_start = parse_date(values.get("起租日"))
        new_planned = parse_date(values.get("约定到期日"))
        new_rule = str(values.get("押金规则") or "").strip()
        ratio, rule_desc = deposit_rule_meta(new_rule or lease["押金规则"], values.get("退还比例"))

        if new_planned and new_start and new_planned < new_start:
            return None, "约定到期日不能早于起租日"

        device_changes: dict[int, dict[str, float]] = {}
        for item in values.get("明细") or []:
            if not isinstance(item, dict):
                continue
            line_id = self._as_int(item.get("line_id"))
            change: dict[str, float] = {}
            if item.get("月租金") not in (None, ""):
                try:
                    change["月租金"] = float(item["月租金"])
                except (TypeError, ValueError):
                    return None, f"设备行 {line_id} 的月租金不是数字"
            if item.get("押金") not in (None, ""):
                try:
                    change["押金"] = float(item["押金"])
                except (TypeError, ValueError):
                    return None, f"设备行 {line_id} 的押金不是数字"
            if change:
                device_changes[line_id] = change

        lease["变更次数"] = int(lease.get("变更次数", 0)) + 1
        version = int(lease["变更次数"])
        snapshot = {
            "版本": version,
            "变更日": change_date.isoformat(),
            "起租日": new_start.isoformat() if new_start else None,
            "约定到期日": new_planned.isoformat() if new_planned else None,
            "押金规则": new_rule or None,
            "退还比例": ratio if (new_rule or values.get("退还比例") is not None) else None,
            "明细": [
                {"line_id": line_id, **change} for line_id, change in device_changes.items()
            ],
            "说明": str(values.get("说明") or "").strip(),
        }

        adjustments = 0
        for line in self._lines(lease_id):
            if new_start:
                line["起租日"] = new_start.isoformat()
            if new_planned:
                line["约定到期日"] = new_planned.isoformat()
            if new_rule:
                line["押金规则"] = new_rule
                line["退还比例"] = ratio
            change = device_changes.get(int(line["id"]))
            if change:
                if "月租金" in change:
                    line["月租金"] = money(change["月租金"])
                if "押金" in change:
                    line["押金"] = money(change["押金"])

            # 只对已退租并已出账的设备重算；未退租的设备按新约定等到退租结算时直接出新账。
            if line.get("settled"):
                adjustments += self._adjust_line(line, lease, change_date, version)

        if new_start:
            lease["起租日"] = new_start.isoformat()
        if new_planned:
            lease["约定到期日"] = new_planned.isoformat()
        if new_rule:
            lease["押金规则"] = new_rule
            lease["退还比例"] = ratio
            lease["押金规则说明"] = rule_desc

        amend_row = {
            "id": self._next_id(AMEND_TABLE),
            "lease_id": lease_id,
            "版本": version,
            "变更日": change_date.isoformat(),
            "说明": snapshot["说明"],
            "内容": snapshot,
            "幂等键": f"amend:{token}" if token else f"amend:{lease_id}:v{version}",
        }
        store.rows(AMEND_TABLE).append(amend_row)
        self._refresh_status(lease)
        return self._lease_view(lease), f"已按第 {version} 次约定变更重算，生成 {adjustments} 张调整账单（原账单保留原值）"

    def _adjust_line(
        self, line: dict[str, Any], lease: dict[str, Any], change_date: date, version: int
    ) -> int:
        lease_id = int(lease["id"])
        line_id = int(line["id"])
        bills = self._bills(lease_id)
        created = 0

        # 租金：用新约定重算整段已发生租期，与已入账租金（含历次调整）求差额。
        start = parse_date(line["起租日"])
        end = parse_date(line["实退日期"])
        full_months, remainder_days = self._rent_periods(start, end)
        rate = float(line["月租金"])
        recalculated_rent = money(rate * full_months + rate / 30.0 * remainder_days)
        booked_rent = sum(
            float(bill["金额"]) for bill in bills
            if int(bill["line_id"]) == line_id and bill.get("类型") in RENT_FAMILY
        )
        rent_delta = money(recalculated_rent - booked_rent)
        rent_key = f"rent_adj:{line_id}:v{version}"
        if rent_delta != 0 and self._get_bill(rent_key) is None:
            self._append_bill(
                lease, line, "租金调整", rent_delta, start, end,
                change_date, f"AMEND-V{version}", rent_key,
                f"第 {version} 次约定变更：按新月租 {rate:.2f} 重算 {start}~{end} 的差额",
            )
            created += 1

        # 押金：按新约定重算应退，与已入账的应退求差额；正数为多退，负数为追回。
        new_refund = self._deposit_refund(line)
        booked_refund = -sum(
            float(bill["金额"]) for bill in bills
            if int(bill["line_id"]) == line_id and bill.get("类型") in DEPOSIT_FAMILY
        )
        deposit_delta = money(new_refund - booked_refund)
        deposit_key = f"deposit_adj:{line_id}:v{version}"
        if deposit_delta != 0 and self._get_bill(deposit_key) is None:
            self._append_bill(
                lease, line, "押金调整", money(-deposit_delta), start, end,
                change_date, f"AMEND-V{version}", deposit_key,
                f"第 {version} 次约定变更：押金应退由 {booked_refund:.2f} 调整为 {new_refund:.2f}",
            )
            created += 1
        return created

    # ------------------------------------------------------------------ 收退款

    def register_payment(self, lease_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        lease = store.find(LEASE_TABLE, lease_id)
        if lease is None:
            return None, f"租赁单 {lease_id} 不存在"
        kind_map = {"rent": "租金", "deposit": "押金", "refund": "押金退款"}
        kind = kind_map.get(str(values.get("种类") or "").strip())
        if kind is None:
            return None, "收退款种类只能是 rent（收租）、deposit（收押金）、refund（退押金）"

        token = str(values.get("client_token") or "").strip()
        if token and any(row.get("幂等键") == f"pay:{token}" for row in store.rows(PAYMENT_TABLE)):
            return self._lease_view(lease), "收退款重复提交，已跳过（幂等）"

        paid_date = parse_date(values.get("日期")) or date.today()
        items = values.get("明细") or []
        if not isinstance(items, list) or not items:
            return None, "请至少填写一条设备金额"

        lines = {int(line["id"]): line for line in self._lines(lease_id)}
        total_amount = 0.0
        planned: list[tuple[dict[str, Any], float]] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                return None, f"第 {index} 条明细格式不正确"
            line = lines.get(self._as_int(item.get("line_id")))
            if line is None:
                return None, f"第 {index} 条明细不属于本租赁单"
            try:
                amount = float(item.get("金额"))
            except (TypeError, ValueError):
                return None, f"第 {index} 条明细金额不是数字"
            if amount <= 0:
                return None, f"第 {index} 条明细金额必须大于 0"
            total_amount += amount
            planned.append((line, amount))

        if kind == "押金退款":
            # 校验：已退 + 本次不能超过各设备的应退押金。
            existing_refunded = {
                int(payment["line_id"]): float(payment["金额"])
                for payment in self._payments(lease_id)
                if payment.get("种类") == "押金退款"
            }
            for line, amount in planned:
                line_refund = self._line_refundable(lease_id, [line])
                if existing_refunded.get(int(line["id"]), 0.0) + amount > line_refund + 0.005:
                    return None, f"设备 {line['设备编号']} 累计退款将超过应退押金 {line_refund:.2f}"

        voucher = str(values.get("凭证号") or "").strip()
        seq = 0
        for line, amount in planned:
            seq += 1
            key = f"pay:{token}:{seq}" if token else f"pay:{lease_id}:{kind}:{line['id']}:{paid_date}:{amount}"
            if self._payment_exists(key):
                continue
            self._append_payment(
                lease_id, int(line["id"]), kind, money(amount), paid_date,
                voucher or key, f"{lease['租赁单号']}{kind}", key,
            )
        self._refresh_status(lease)
        return self._lease_view(lease), f"已登记{kind} {money(total_amount):.2f} 元"

    def _line_refundable(self, lease_id: int, lines: list[dict[str, Any]]) -> float:
        ids = {int(line["id"]) for line in lines}
        return money(-sum(
            float(bill["金额"]) for bill in self._bills(lease_id)
            if int(bill["line_id"]) in ids and bill.get("类型") in DEPOSIT_FAMILY
        ))

    # ------------------------------------------------------------------ 内部工具

    def _lines(self, lease_id: int) -> list[dict[str, Any]]:
        rows = [row for row in store.rows(LINE_TABLE) if int(row["lease_id"]) == lease_id]
        return sorted(rows, key=lambda row: int(row["行号"]))

    def _bills(self, lease_id: int) -> list[dict[str, Any]]:
        return [row for row in store.rows(BILL_TABLE) if int(row["lease_id"]) == lease_id]

    def _payments(self, lease_id: int) -> list[dict[str, Any]]:
        return [row for row in store.rows(PAYMENT_TABLE) if int(row["lease_id"]) == lease_id]

    def _lease_view(self, lease: dict[str, Any]) -> dict[str, Any]:
        view = dict(lease)
        lines = self._lines(int(lease["id"]))
        bills = self._bills(int(lease["id"]))
        payments = self._payments(int(lease["id"]))
        view.update(self._summary(lease=lease, lines=lines, bills=bills, payments=payments))
        return view

    def _refresh_status(self, lease: dict[str, Any]) -> None:
        lines = self._lines(int(lease["id"]))
        total = len(lines)
        returned = sum(1 for line in lines if line.get("实退日期"))
        settled = sum(1 for line in lines if line.get("settled"))
        if total == 0 or returned == 0:
            status = STATUS_RENTING
        elif returned < total:
            status = STATUS_PARTIAL
        elif settled >= total:
            status = STATUS_SETTLED
        else:
            status = STATUS_RETURNED
        lease["status"] = status
        lease["pending"] = status != STATUS_SETTLED
        summary = self._summary(lease=lease, lines=lines, bills=self._bills(int(lease["id"])),
                                payments=self._payments(int(lease["id"])))
        lease["abnormal"] = summary["未收租金"] > 0 or summary["未退押金"] > 0

    def _append_bill(
        self,
        lease: dict[str, Any],
        line: dict[str, Any],
        bill_type: str,
        amount: float,
        period_start: date,
        period_end: date,
        bill_date: date,
        batch_no: str,
        key: str,
        remark: str,
    ) -> dict[str, Any]:
        bill_id = self._next_id(BILL_TABLE)
        bill = {
            "id": bill_id,
            "单据编号": f"BILL-{bill_id:05d}",
            "lease_id": int(lease["id"]),
            "line_id": int(line["id"]),
            "租赁单号": lease["租赁单号"],
            "设备编号": line["设备编号"],
            "设备名称": line["设备名称"],
            "类型": bill_type,
            "账期起": period_start.isoformat(),
            "账期止": period_end.isoformat(),
            "金额": money(amount),
            "方向": "应退" if amount < 0 or bill_type in DEPOSIT_FAMILY else "应收",
            "入账状态": "已入账",
            "版本": int(lease.get("变更次数", 0)),
            "来源批次": batch_no,
            "账单日期": bill_date.isoformat(),
            "幂等键": key,
            "说明": remark,
        }
        store.rows(BILL_TABLE).append(bill)
        return bill

    def _append_payment(
        self,
        lease_id: int,
        line_id: int,
        kind: str,
        amount: float,
        paid_date: date,
        voucher: str,
        remark: str,
        key: str,
    ) -> dict[str, Any]:
        payment = {
            "id": self._next_id(PAYMENT_TABLE),
            "单据编号": f"PAY-{self._next_id(PAYMENT_TABLE):05d}",
            "lease_id": lease_id,
            "line_id": line_id,
            "种类": kind,
            "金额": money(amount),
            "日期": paid_date.isoformat(),
            "凭证号": voucher,
            "说明": remark,
            "幂等键": key,
        }
        store.rows(PAYMENT_TABLE).append(payment)
        return payment

    def _next_batch_no(self, batch_date: date) -> str:
        prefix = f"SETTLE-{batch_date.strftime('%Y%m%d')}-"
        same_day = [
            row for row in store.rows(BILL_TABLE)
            if str(row.get("来源批次", "")).startswith(prefix)
        ]
        seq = len({row["来源批次"] for row in same_day}) + 1
        return f"{prefix}{seq:02d}"

    def _get_bill(self, key: str) -> dict[str, Any] | None:
        return next((row for row in store.rows(BILL_TABLE) if row.get("幂等键") == key), None)

    def _payment_exists(self, key: str) -> bool:
        return any(row.get("幂等键") == key for row in store.rows(PAYMENT_TABLE))

    @staticmethod
    def _as_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1

    def _next_id(self, table: str) -> int:
        return max((int(row.get("id", 0)) for row in store.rows(table)), default=0) + 1
