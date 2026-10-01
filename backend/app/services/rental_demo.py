"""租赁结算演示数据：通过 RentalService 的正式动作生成台账。

不直接塞账单/收款，而是走 登记 → 退租 → 结算 → 变更 → 收退款 的完整流程，
保证示例页面上看到的数字全部由结算规则算出；重复启动只引导一次。
"""
from __future__ import annotations

from app.services.rental import RentalService

LEASES = [
    {
        "租赁单号": "RENT-2026-001",
        "承租方": "城东建工集团",
        "起租日": "2026-07-01",
        "约定到期日": "2026-09-30",
        "押金规则": "全额退还",
        "立即收押金": True,
        "devices": [
            {"设备编号": "CRAN-RENT-01", "设备名称": "汽车吊 25t", "月租金": 12000, "押金": 20000},
            {"设备编号": "FORK-RENT-02", "设备名称": "内燃叉车 3t", "月租金": 4000, "押金": 10000},
        ],
    },
    {
        "租赁单号": "RENT-2026-002",
        "承租方": "宏远物流有限公司",
        "起租日": "2026-08-15",
        "约定到期日": "2026-11-15",
        "押金规则": "扣10%押金",
        "立即收押金": True,
        "devices": [
            {"设备编号": "FORK-RENT-03", "设备名称": "电动叉车 2t", "月租金": 3500, "押金": 8000},
            {"设备编号": "FORK-RENT-04", "设备名称": "电动叉车 2t", "月租金": 3500, "押金": 8000},
            {"设备编号": "ELEV-RENT-05", "设备名称": "施工升降机 SC200", "月租金": 9000, "押金": 15000},
        ],
    },
    {
        "租赁单号": "RENT-2026-003",
        "承租方": "沿江路桥项目部",
        "起租日": "2026-06-15",
        "约定到期日": "2026-08-31",
        "押金规则": "全额退还",
        "立即收押金": True,
        "devices": [
            {"设备编号": "CRAN-RENT-06", "设备名称": "重型履带吊 150t", "月租金": 30000, "押金": 20000},
        ],
    },
]


def bootstrap_demo(service: RentalService) -> None:
    from app.store import store

    if store.rows("rental_leases"):
        return

    lease_ids: dict[str, int] = {}
    for payload in LEASES:
        view, missing, _duplicated = service.create_lease(payload)
        if missing:
            raise RuntimeError(f"租赁演示数据初始化失败：{missing}")
        lease_ids[payload["租赁单号"]] = int(view["id"])

    lease_a = lease_ids["RENT-2026-001"]
    lease_b = lease_ids["RENT-2026-002"]
    lease_c = lease_ids["RENT-2026-003"]

    def lines_of(lease_id: int) -> list[dict]:
        detail = service.get_lease(lease_id)
        return detail["lines"] if detail else []

    # A：两台 09-12 同日退租（未逾期），09-15 结算。
    service.register_returns(
        lease_a,
        [{"line_id": line["id"]} for line in lines_of(lease_a)],
        "2026-09-12",
    )
    service.settle(lease_a, "2026-09-15")
    a_lines = lines_of(lease_a)
    service.register_payment(lease_a, {"种类": "rent", "日期": "2026-09-15",
                                       "明细": [{"line_id": a_lines[0]["id"], "金额": 28800}]})
    service.register_payment(lease_a, {"种类": "rent", "日期": "2026-09-15",
                                       "明细": [{"line_id": a_lines[1]["id"], "金额": 7600}]})
    service.register_payment(lease_a, {"种类": "refund", "日期": "2026-09-16",
                                       "明细": [{"line_id": a_lines[0]["id"], "金额": 20000}]})

    # B：约好同日退租，实际只退了两台；09-20 只结这两台，第三台留在下一批。
    b_lines = lines_of(lease_b)
    service.register_returns(
        lease_b,
        [{"line_id": b_lines[0]["id"]}, {"line_id": b_lines[1]["id"]}],
        "2026-09-20",
    )
    service.settle(lease_b, "2026-09-21")
    service.register_payment(lease_b, {"种类": "rent", "日期": "2026-09-21",
                                       "明细": [{"line_id": b_lines[0]["id"], "金额": 4083.33}]})
    service.register_payment(lease_b, {"种类": "refund", "日期": "2026-09-22",
                                       "明细": [{"line_id": b_lines[0]["id"], "金额": 7200}]})

    # C：09-05 退租并逾期 5 天 → 按全额退还规则扣逾期；
    # 09-10 双方改约定（月租降到 29000、押金规则改为扣10%）→ 原账单保留，出调整账单。
    c_line = lines_of(lease_c)[0]
    service.register_returns(lease_c, [{"line_id": c_line["id"]}], "2026-09-05")
    service.settle(lease_c, "2026-09-06")
    service.register_payment(lease_c, {"种类": "rent", "日期": "2026-09-06",
                                       "明细": [{"line_id": c_line["id"], "金额": 20000}]})
    service.amend_lease(lease_c, {
        "变更日": "2026-09-10",
        "押金规则": "扣10%押金",
        "明细": [{"line_id": c_line["id"], "月租金": 29000}],
        "说明": "承租方提出续租谈判，月租降至 29000，押金按扣 10% 重算",
        "client_token": "demo-amend-003",
    })
