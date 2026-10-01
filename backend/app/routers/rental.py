"""租赁结算接口：租赁台账、退租、分批结算、租期变更与收退款登记。

台账页的应收/已收与概览在租台数都取自 RentalService.summary() 这一份口径。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult, PageResult
from app.services.rental import DEPOSIT_RULES, RentalService

router = APIRouter(prefix="/api/rental", tags=["租赁结算"])
service = RentalService()


class LeaseCreate(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class BatchPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


@router.get("/summary")
def rental_summary() -> dict[str, Any]:
    """租赁概览/台账页共用的汇总：在租台数随明细实时重算，应收已收只此一份口径。"""
    return service.summary()


@router.get("/rules")
def deposit_rules() -> dict[str, Any]:
    """返回登记与变更时可选的押金约定算法。"""
    return {
        "rules": [
            {"name": name, "ratio": meta[0], "desc": meta[1]}
            for name, meta in DEPOSIT_RULES.items()
        ],
        "overdue_daily_rate": 0.005,
    }


@router.get("/leases", response_model=PageResult[dict])
def list_leases(
    keyword: str | None = Query(default=None, description="按租赁单号或承租方检索"),
    status: str | None = Query(default=None, description="在租、部分退租、已退租、已结清"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """租赁台账列表；每行内嵌该单的应收、已收、应退、已退（与汇总同源）。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_leases(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/leases/{lease_id}")
def get_lease(lease_id: int) -> dict[str, Any]:
    """租赁单全貌：设备明细、账单、收退款、变更记录与该单汇总。"""
    detail = service.get_lease(lease_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"租赁单 {lease_id} 不存在")
    return detail


@router.post("/leases", response_model=ActionResult)
def create_lease(payload: LeaseCreate) -> ActionResult:
    """登记租赁单；同一租赁单号重复提交只入账一次。"""
    lease, missing, duplicated = service.create_lease(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段或取值非法：{'、'.join(missing)}")
    if duplicated:
        return ActionResult(ok=True, message="同一份租赁单重复提交，已跳过（幂等，只入账一次）", entry=lease)
    return ActionResult(ok=True, message="租赁单已登记", entry=lease)


@router.post("/leases/{lease_id}/returns", response_model=ActionResult)
def register_returns(lease_id: int, payload: BatchPayload) -> ActionResult:
    """一批设备登记同一天退租；未选择的设备继续在租。"""
    values = payload.values
    lease, message = service.register_returns(
        lease_id, values.get("items") or [], values.get("退租日期")
    )
    if lease is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=lease)


@router.post("/leases/{lease_id}/settle", response_model=ActionResult)
def settle_lease(lease_id: int, payload: BatchPayload) -> ActionResult:
    """按设备逐台生成月租并入账；只结已退租的，未退租留到下一批，重复结算不重复出账。"""
    lease, message = service.settle(lease_id, (payload.values or {}).get("结算日期"))
    if lease is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=lease)


@router.post("/leases/{lease_id}/amend", response_model=ActionResult)
def amend_lease(lease_id: int, payload: BatchPayload) -> ActionResult:
    """中途改租期/租金/押金约定：已出账单保留原值，差额以调整账单重算。"""
    lease, message = service.amend_lease(lease_id, payload.values)
    if lease is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=lease)


@router.post("/leases/{lease_id}/payments", response_model=ActionResult)
def register_payment(lease_id: int, payload: BatchPayload) -> ActionResult:
    """登记收租、收押金、退押金；client_token 保证同一笔不重复入账。"""
    lease, message = service.register_payment(lease_id, payload.values)
    if lease is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=lease)


@router.get("/bills")
def list_bills(
    lease_id: int | None = Query(default=None),
    line_id: int | None = Query(default=None),
) -> dict[str, Any]:
    """账单流水：可按租赁单或设备行过滤，供对账核对原值与调整。"""
    items = service.list_bills(lease_id=lease_id, line_id=line_id)
    return {"total": len(items), "items": items}


@router.get("/payments")
def list_payments(lease_id: int | None = Query(default=None)) -> dict[str, Any]:
    """收退款流水。"""
    items = service.list_payments(lease_id=lease_id)
    return {"total": len(items), "items": items}


@router.get("/export")
def export_leases() -> dict[str, Any]:
    """导出租赁台账全量清单。"""
    items, total = service.list_leases(page=1, size=10000)
    return {"module": "rental", "total": total, "items": items, "summary": service.summary()}
