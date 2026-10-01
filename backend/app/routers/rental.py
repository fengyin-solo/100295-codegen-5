"""租赁结算接口：租赁登记、租期变更、退租登记、批次结算、账单与台账概览。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload
from app.services.rental import RentalError, RentalService

router = APIRouter(prefix="/api/rental", tags=["租赁结算"])

service = RentalService()


@router.get("/overview")
def rental_overview() -> dict[str, Any]:
    """租赁概览：在租台数、应收/已收全部随明细实时重算。"""
    return service.overview()


@router.get("/ledger")
def rental_ledger() -> dict[str, Any]:
    """租赁台账：账单流水 + 与概览同源的应收/已收汇总。"""
    return service.ledger()


@router.get("/leases")
def list_leases(
    keyword: str | None = Query(default=None, description="按租赁单号或承租方检索"),
) -> dict[str, Any]:
    """租赁单列表（含设备明细与单级汇总）。"""
    items = service.list_leases(keyword=keyword)
    return {"items": items, "total": len(items)}


@router.get("/leases/{lease_id}")
def lease_detail(lease_id: int) -> dict[str, Any]:
    """单张租赁单详情：设备明细逐台展开，账单全量带出。"""
    try:
        return service.lease_detail(lease_id)
    except RentalError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/items/{item_id}")
def item_detail(item_id: int) -> dict[str, Any]:
    """单台设备明细（含账单与押金退还测算）。"""
    try:
        return service.item_detail(item_id)
    except RentalError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/leases", response_model=ActionResult)
def register_lease(payload: EntryPayload) -> ActionResult:
    """登记租赁单与设备租期、租金、押金。

    租赁单号即幂等键：同一份单子重复提交返回原单，只入账一次。
    """
    try:
        entry, message, created = service.register_lease(payload.values)
    except RentalError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/items/{item_id}/change-term", response_model=ActionResult)
def change_term(item_id: int, payload: EntryPayload) -> ActionResult:
    """中途改租期：原账单保留原值，按最后一次约定重算已出账期。"""
    try:
        entry, message = service.change_term(item_id, payload.values)
    except RentalError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/returns", response_model=ActionResult)
def register_return(payload: EntryPayload) -> ActionResult:
    """登记一批设备同一天退租（只记事实，不出账）。"""
    try:
        entries, message = service.register_return(payload.values)
    except RentalError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=message, entry={"items": entries})


@router.post("/settle", response_model=ActionResult)
def settle(payload: EntryPayload) -> ActionResult:
    """批次结算：对已退租设备逐台生成月租并入账，未退租的留下一批。"""
    try:
        result = service.settle(payload.values)
    except RentalError as exc:
        return ActionResult(ok=False, message=str(exc))
    count = len(result["生成租金账单"])
    message = f"本批入账租金账单 {count} 张；跳过 {len(result['跳过'])} 台（未退租的留下一批）"
    return ActionResult(ok=True, message=message, entry=result)


@router.get("/bills")
def list_bills(
    lease_id: int | None = None,
    item_id: int | None = None,
    bill_type: str | None = Query(default=None, description="租金/租金重算/押金/押金退还"),
    include_superseded: bool = True,
) -> dict[str, Any]:
    """账单流水查询，默认连已冲销的历史账单一起带出（原值可追溯）。"""
    items = service.list_bills(
        lease_id=lease_id,
        item_id=item_id,
        bill_type=bill_type,
        include_superseded=include_superseded,
    )
    return {"items": items, "total": len(items)}


@router.post("/bills/{bill_id}/payments", response_model=ActionResult)
def receive_payment(bill_id: int, payload: EntryPayload) -> ActionResult:
    """对账单登记收款（押金退还单则为登记退款）。"""
    try:
        result = service.receive_payment(bill_id, payload.values)
    except RentalError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=result["message"], entry=result)
