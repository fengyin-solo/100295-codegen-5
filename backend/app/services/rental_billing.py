"""租赁计费引擎：自然月拆账、批次结算、押金退还与改租重算。

所有金额计算只在这一层发生，台账汇总与概览都直接读账单表，保证
「同一份数据」。账单一经生成金额不再改写；改租通过「冲销原账单 +
按新约定重开同账期账单（版本号 +1）」表达，原行原值保留。
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterator

from app.services.rental_store import money, rental_store

CENT = Decimal("0.01")

# 押金退还算法代码
DEPOSIT_FULL = "全额退"
DEPOSIT_DEDUCT = "扣欠费"
DEPOSIT_DEDUCT_PENALTY = "扣欠费及违约金"
DEPOSIT_ALGORITHMS = [DEPOSIT_FULL, DEPOSIT_DEDUCT, DEPOSIT_DEDUCT_PENALTY]


def parse_date(value: Any) -> date | None:
    """容忍 date / 'YYYY-MM-DD' / 空值，解析失败抛 ValueError 由上层提示。"""
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    return datetime.strptime(text, "%Y-%m-%d").date()


def month_first(day: date) -> date:
    return day.replace(day=1)


def month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def next_month_first(day: date) -> date:
    if day.month == 12:
        return date(day.year + 1, 1, 1)
    return day.replace(month=day.month + 1, day=1)


def month_segments(start: date, end: date) -> Iterator[tuple[date, date, bool]]:
    """把 [start, end] 按自然月切成 (段起, 段止, 是否整月)。"""
    cursor = month_first(start)
    while cursor <= end:
        seg_start = max(start, cursor)
        seg_end = min(end, month_end(cursor))
        is_full = seg_start == cursor and seg_end == month_end(cursor)
        yield seg_start, seg_end, is_full
        cursor = next_month_first(cursor)


def segment_amount(
    seg_start: date,
    seg_end: date,
    is_full: bool,
    monthly_rent: Decimal,
) -> Decimal:
    """整月收月租，不足月按所在自然月天数折算（含首尾当天）。"""
    monthly_rent = money(monthly_rent)
    if is_full:
        return monthly_rent
    month_days = calendar.monthrange(seg_start.year, seg_start.month)[1]
    used_days = (seg_end - seg_start).days + 1
    return (monthly_rent * Decimal(used_days) / Decimal(month_days)).quantize(
        CENT, rounding=ROUND_HALF_UP
    )


def _live_rent_bills(item_id: int) -> list[dict[str, Any]]:
    return [
        bill
        for bill in rental_store.item_bills(item_id)
        if bill["状态"] == "有效" and bill["账单类型"] in ("租金", "租金重算")
    ]


def billed_through(item_id: int) -> date | None:
    """该设备已入账租金覆盖到的最后一天（取有效账单最大结束日）。"""
    ends = [bill["结束日期"] for bill in _live_rent_bills(item_id)]
    return max(ends) if ends else None


def generate_rent_bills(
    item: dict[str, Any],
    period_end: date,
    *,
    batch_no: str,
    bill_type: str = "租金",
    base_version: int | None = None,
    change_seq: int | None = None,
    remarks: str = "",
) -> list[dict[str, Any]]:
    """从已入账截止日的次日起，到 period_end 为止，逐自然月生成租金账单。

    已出过账的区间不会重复生成，天然支撑「分多批结算」。
    """
    start = parse_date(item["起租日期"])
    covered = billed_through(int(item["id"]))
    period_start = start if covered is None else covered + timedelta(days=1)
    if period_end < period_start:
        return []
    if covered is None and period_end < start:
        return []

    created: list[dict[str, Any]] = []
    for seg_start, seg_end, is_full in month_segments(period_start, period_end):
        amount = segment_amount(seg_start, seg_end, is_full, money(item["月租金额"]))
        bill = rental_store.add_bill({
            "租赁单ID": item["租赁单ID"],
            "设备明细ID": item["id"],
            "账单类型": bill_type,
            "起始日期": seg_start,
            "结束日期": seg_end,
            "账期月份": f"{seg_start.year:04d}-{seg_start.month:02d}",
            "金额": amount,
            "已收金额": Decimal("0.00"),
            "状态": "有效",
            "版本号": 1 if base_version is None else base_version,
            "变更序号": change_seq,
            "批次号": batch_no,
            "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "关联账单ID": None,
            "备注": remarks,
        })
        created.append(bill)
    return created


def settle_batch(
    cutoff: date,
    item_ids: list[int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """批次退租结算。

    只挑「实际退租日期不晚于结算日」的设备逐台出月租；未退租的原样跳过，
    留到下一批。返回 (生成的账单, 本次结算的设备, 跳过原因列表)。
    """
    items = rental_store.items
    if item_ids is not None:
        wanted = set(item_ids)
        items = [row for row in items if int(row["id"]) in wanted]

    created_bills: list[dict[str, Any]] = []
    settled_items: list[dict[str, Any]] = []
    skipped: list[str] = []
    batch_no = f"JS-{cutoff.strftime('%Y%m%d')}-{datetime.now().strftime('%H%M%S')}"

    for item in sorted(items, key=lambda row: int(row["id"])):
        returned_at = item.get("实际退租日期")
        if not returned_at:
            skipped.append(f"设备 {item['设备编号']}：尚未退租，留待下一批结算")
            continue
        returned_at = parse_date(returned_at)
        if returned_at > cutoff:
            skipped.append(f"设备 {item['设备编号']}：退租日 {returned_at} 晚于结算日，本批不结")
            continue
        covered = billed_through(int(item["id"]))
        if covered is not None and covered >= returned_at:
            skipped.append(f"设备 {item['设备编号']}：租金已结至 {covered}，无需重复入账")
            continue
        bills = generate_rent_bills(item, returned_at, batch_no=batch_no)
        created_bills.extend(bills)
        settled_items.append(item)
    return created_bills, settled_items, skipped


def recalc_on_term_change(
    item: dict[str, Any],
    change_seq: int,
) -> list[dict[str, Any]]:
    """改租后按最后一次约定重算「已出账」的账期。

    做法：把当前有效租金账单冲销（金额原值不动，仅状态置为已冲销），
    再按新约定重开同账期账单（版本号 = 原版本 + 1）。新租期不再覆盖的
    尾段账期冲销后以 0 元重开单占位，保留可追溯的重算痕迹。
    """
    item_id = int(item["id"])
    new_start = parse_date(item["起租日期"])
    new_end = parse_date(item["到期日期"])
    batch_no = f"CG-{datetime.now().strftime('%Y%m%d%H%M%S')}-{item_id}"

    live_bills = [
        bill for bill in rental_store.item_bills(item_id)
        if bill["状态"] == "有效" and bill["账单类型"] in ("租金", "租金重算")
    ]
    created: list[dict[str, Any]] = []
    for old in sorted(live_bills, key=lambda row: (row["起始日期"], int(row["id"]))):
        old["状态"] = "已冲销"
        seg_start, seg_end = old["起始日期"], old["结束日期"]
        if seg_end < new_start or seg_start > new_end:
            amount = Decimal("0.00")
            clip_start, clip_end = seg_start, seg_end
            remark = "租期调整后该账期不在约定租期内，冲销为 0"
        else:
            clip_start = max(seg_start, new_start)
            clip_end = min(seg_end, new_end)
            amount = _recalc_segment(item, clip_start, clip_end, seg_start, seg_end)
            remark = f"租期第 {change_seq} 次变更后重算"
        created.append(rental_store.add_bill({
            "租赁单ID": item["租赁单ID"],
            "设备明细ID": item_id,
            "账单类型": "租金重算",
            "起始日期": clip_start,
            "结束日期": clip_end,
            "账期月份": old["账期月份"],
            "金额": amount,
            # 已收按实际收款原样结转：月租调低时多缴部分形成预收，
            # 不能截断（否则客户的钱在台账里凭空消失）。
            "已收金额": money(old["已收金额"]),
            "状态": "有效",
            "版本号": int(old["版本号"]) + 1,
            "变更序号": change_seq,
            "批次号": batch_no,
            "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "关联账单ID": old["id"],
            "备注": remark,
        }))
    return created


def _recalc_segment(
    item: dict[str, Any],
    clip_start: date,
    clip_end: date,
    seg_start: date,
    seg_end: date,
) -> Decimal:
    """重算单个账期：账期整体落在新租期内仍按整月/不足月规则走。"""
    # 原账期本身是整月，且新租期没有切到它的边界，沿用整月价
    boundary_changed = clip_start != seg_start or clip_end != seg_end
    is_full = not boundary_changed and seg_start == month_first(seg_start) and seg_end == month_end(seg_start)
    return segment_amount(clip_start, clip_end, is_full, money(item["月租金额"]))


def deposit_refund_preview(item: dict[str, Any]) -> dict[str, Any]:
    """按设备当前约定的押金算法试算应退押金（不落账）。

    租金已收大于应收时形成「预收租金」（如改租降价后多缴），无论选哪种
    算法都应随押金一并退回——那本来就是承租方的钱。
    """
    deposit = money(item["押金金额"])
    rent_due, rent_paid = rent_balance(item)
    arrears = max(Decimal("0.00"), rent_due - rent_paid)
    prepaid = max(Decimal("0.00"), rent_paid - rent_due)
    algorithm = item.get("押金算法") or DEPOSIT_FULL
    penalty = Decimal("0.00")
    basis = ""

    if algorithm == DEPOSIT_FULL:
        deposit_back = deposit
        basis = "约定到期全额退还押金"
    elif algorithm == DEPOSIT_DEDUCT:
        deposit_back = max(Decimal("0.00"), deposit - arrears)
        basis = f"扣除未结清租金 {arrears} 后退还"
    elif algorithm == DEPOSIT_DEDUCT_PENALTY:
        returned_at = parse_date(item.get("实际退租日期"))
        planned_end = parse_date(item["到期日期"])
        if returned_at is not None and returned_at < planned_end:
            ratio = Decimal(str(item.get("违约金比例") or 0))
            penalty = (money(item["月租金额"]) * ratio).quantize(CENT, rounding=ROUND_HALF_UP)
            basis = f"提前退租：扣除欠费 {arrears} 与违约金 {penalty}（月租×{ratio}）"
        else:
            basis = f"正常到期：扣除欠费 {arrears}，不计违约金"
        deposit_back = max(Decimal("0.00"), deposit - arrears - penalty)
    else:
        deposit_back = deposit
        basis = "未指定算法，默认全额退"

    refund = deposit_back + prepaid
    if prepaid > 0:
        basis += f"；另退回预收租金 {prepaid}"
    return {
        "设备明细ID": item["id"],
        "押金金额": deposit,
        "欠费金额": arrears,
        "预收租金": prepaid.quantize(CENT),
        "违约金": penalty,
        "应退押金": refund.quantize(CENT),
        "押金算法": algorithm,
        "计算说明": basis,
    }


def rent_balance(item: dict[str, Any]) -> tuple[Decimal, Decimal]:
    """该设备有效租金账单的 (应收, 已收)。重算单已收以冲销后口径为准。"""
    bills = [
        bill for bill in rental_store.item_bills(int(item["id"]))
        if bill["状态"] == "有效" and bill["账单类型"] in ("租金", "租金重算")
    ]
    due = sum((money(bill["金额"]) for bill in bills), Decimal("0.00"))
    paid = sum((money(bill["已收金额"]) for bill in bills), Decimal("0.00"))
    return due.quantize(CENT), paid.quantize(CENT)


def _make_bill(
    item: dict[str, Any],
    bill_type: str,
    day: date,
    amount: Decimal,
    *,
    batch_no: str,
    remarks: str = "",
    version: int = 1,
    change_seq: int | None = None,
    related_id: int | None = None,
) -> dict[str, Any]:
    return rental_store.add_bill({
        "租赁单ID": item["租赁单ID"],
        "设备明细ID": item["id"],
        "账单类型": bill_type,
        "起始日期": day,
        "结束日期": day,
        "账期月份": f"{day.year:04d}-{day.month:02d}",
        "金额": money(amount),
        "已收金额": Decimal("0.00"),
        "状态": "有效",
        "版本号": version,
        "变更序号": change_seq,
        "批次号": batch_no,
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "关联账单ID": related_id,
        "备注": remarks,
    })


def create_deposit_bill(item: dict[str, Any], *, batch_no: str) -> dict[str, Any]:
    """设备交租时收押金：登记即视为押金到账（已收 = 应收）。"""
    bill = _make_bill(
        item, "押金", parse_date(item["起租日期"]), money(item["押金金额"]),
        batch_no=batch_no, remarks="设备交租登记，押金到账",
    )
    bill["已收金额"] = money(bill["金额"])
    return bill


def find_refund_bill(item_id: int) -> dict[str, Any] | None:
    rows = [
        bill for bill in rental_store.item_bills(item_id)
        if bill["状态"] == "有效" and bill["账单类型"] == "押金退还"
    ]
    return rows[-1] if rows else None


def supersede_refund_bill(
    item: dict[str, Any],
    *,
    batch_no: str,
    change_seq: int,
) -> dict[str, Any] | None:
    """改租后按最新押金算法重开押金退还单；原单冲销、原值保留。

    已经把钱退清的押金单不再重开（钱已出账，靠新单追加没有业务意义）。
    """
    old = find_refund_bill(int(item["id"]))
    if old is None:
        return None
    if money(old["已收金额"]) <= money(old["金额"]):
        # 负账单：已收 == 金额 代表退清
        return None
    old["状态"] = "已冲销"
    preview = deposit_refund_preview(item)
    bill = _make_bill(
        item, "押金退还", parse_date(item["实际退租日期"]), -preview["应退押金"],
        batch_no=batch_no,
        version=int(old["版本号"]) + 1,
        change_seq=change_seq,
        related_id=int(old["id"]),
        remarks=f"租期第 {change_seq} 次变更后按最新约定重算应退押金；{preview['计算说明']}",
    )
    return bill


def create_refund_bill(
    item: dict[str, Any],
    *,
    batch_no: str,
    remarks: str = "退租结算，按约定押金算法生成应退押金",
) -> dict[str, Any] | None:
    """生成押金退还单（金额为负表示应退）。已存在时幂等返回原单。"""
    existing = find_refund_bill(int(item["id"]))
    if existing is not None:
        return existing
    preview = deposit_refund_preview(item)
    refund = preview["应退押金"]
    bill = _make_bill(
        item, "押金退还", parse_date(item["实际退租日期"]), -refund,
        batch_no=batch_no, remarks=f"{remarks}；{preview['计算说明']}",
    )
    return bill


def apply_payment(
    bill: dict[str, Any],
    amount: Decimal,
    *,
    at: str | None = None,
) -> tuple[Decimal, Decimal]:
    """对账单登记一笔收款（退还单则为一笔退款）。

    返回 (本次核销金额, 核销后剩余应收/应退)；超过未结余额的部分不接收，
    防止一笔账单被多收。
    """
    amount = money(amount)
    if amount < 0:
        raise ValueError("核销金额不能为负")
    if bill["状态"] != "有效":
        raise ValueError("账单已冲销，不能再登记收款")

    due = money(bill["金额"])
    paid = money(bill["已收金额"])
    if due >= 0:
        outstanding = due - paid
        if amount > outstanding:
            raise ValueError(f"本次收款 {amount} 超过未结余额 {outstanding}，请改按未结金额登记")
        bill["已收金额"] = paid + amount
    else:
        # 负账单：应退押金，已收用负数表示已退
        outstanding = paid - due  # 正数 = 还没退的
        if amount > outstanding:
            raise ValueError(f"本次退款 {amount} 超过待退余额 {outstanding}，请改按待退金额登记")
        bill["已收金额"] = paid - amount

    bill["最近收款时间"] = at or datetime.now().strftime("%Y-%m-%d %H:%M")
    applied = amount.quantize(CENT)
    left = (due - money(bill["已收金额"]) if due >= 0 else money(bill["已收金额"]) - due)
    return applied, left.quantize(CENT)
