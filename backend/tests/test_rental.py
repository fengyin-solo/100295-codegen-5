"""租赁结算业务口径测试：只用标准库，.venv/bin/python -m unittest 即可跑。

覆盖用户提出的每条规则：
* 同一份租赁单重复提交只入账一次（幂等键 = 租赁单号）；
* 一批设备同日退租，按设备逐台生成月租，已退租才入账，未退租留下一批；
* 跨月租期按自然月拆账（整月按月租、不足月按当月天数折算）；
* 押金按约定算法退（全额 / 扣欠费 / 扣欠费及违约金）；
* 中途改租期：原账单保留原值，按最后一次约定重算已出账期，押金单同步重开；
* 台账页与概览从同一份账单取应收/已收，在租台数随明细重算；
* 收款/退款不能超过未结余额。
"""
from __future__ import annotations

import unittest
from decimal import Decimal

from app.services.rental import RentalError, RentalService
from app.services.rental_billing import money
from app.services.rental_store import rental_store


class RentalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        rental_store.reload_seed()
        self.svc = RentalService()

    def _register(self, no: str, renter: str, *items: dict) -> dict:
        entry, _, created = self.svc.register_lease({
            "租赁单号": no, "承租方": renter, "设备明细": list(items),
        })
        self.assertTrue(created)
        return entry

    def _item(self, lease_entry: dict, code: str) -> dict:
        return next(item for item in lease_entry["设备明细"] if item["设备编号"] == code)

    @staticmethod
    def _item_payload(code: str, name: str, start: str, end: str, rent: str,
                      deposit: str, algorithm: str = "全额退", ratio: str = "0") -> dict:
        return {
            "设备编号": code, "设备名称": name,
            "起租日期": start, "到期日期": end,
            "月租金额": rent, "押金金额": deposit,
            "押金算法": algorithm, "违约金比例": ratio,
        }

    # ---- 幂等 -----------------------------------------------------------
    def test_duplicate_submission_only_books_once(self) -> None:
        leases_before = len(rental_store.leases)
        bills_before = len(rental_store.bills)
        entry, msg, created = self.svc.register_lease({
            "租赁单号": "ZL-2026-0901", "承租方": "华东建工集团",
            "设备明细": [self._item_payload("X", "X", "2026-09-01", "2026-10-01", "1", "1")],
        })
        self.assertFalse(created)
        self.assertEqual(len(rental_store.leases), leases_before)
        self.assertEqual(len(rental_store.bills), bills_before)
        self.assertEqual(entry["id"], 1)
        self.assertIn("重复提交未重复入账", msg)

    def test_new_lease_books_deposit_but_not_rent(self) -> None:
        entry = self._register(
            "ZL-T1", "测试承租方",
            self._item_payload("T-1", "电焊机", "2026-09-01", "2026-11-30", "2000", "2000", "扣欠费"),
        )
        item = entry["设备明细"][0]
        bills = rental_store.item_bills(item["id"])
        self.assertEqual([b["账单类型"] for b in bills], ["押金"])
        self.assertEqual(bills[0]["已收金额"], Decimal("2000.00"))

    # ---- 批次结算 -------------------------------------------------------
    def test_batch_settlement_only_books_returned_devices(self) -> None:
        lease = self._register(
            "ZL-T2", "测试承租方",
            self._item_payload("T-1", "焊机", "2026-09-01", "2026-11-30", "2000", "2000"),
            self._item_payload("T-2", "切割机", "2026-09-10", "2026-12-09", "3000", "3000"),
        )
        other = self._register(
            "ZL-T3", "测试承租方2",
            self._item_payload("T-3", "水泵", "2026-09-01", "2026-12-01", "1000", "1000"),
        )
        t1, t2 = self._item(lease, "T-1"), self._item(lease, "T-2")
        t3 = self._item(other, "T-3")
        self.svc.register_return({"设备明细IDs": [t1["id"], t2["id"]], "退租日期": "2026-09-30"})

        res = self.svc.settle({"结算日期": "2026-10-01",
                               "设备明细IDs": [t1["id"], t2["id"], t3["id"]]})
        self.assertEqual({x["id"] for x in res["本次入账设备"]}, {t1["id"], t2["id"]})
        self.assertTrue(any("尚未退租" in s for s in res["跳过"]))

        rent_t1 = [b for b in res["生成租金账单"] if b["设备明细ID"] == t1["id"]]
        rent_t2 = [b for b in res["生成租金账单"] if b["设备明细ID"] == t2["id"]]
        self.assertEqual(money(rent_t1[0]["金额"]), Decimal("2000.00"))  # 整月
        self.assertEqual(money(rent_t2[0]["金额"]), Decimal("2100.00"))  # 21/30 天
        self.assertTrue(res["押金退还单"])

    def test_second_batch_picks_up_unreturned_device_without_double_booking(self) -> None:
        lease = self._register(
            "ZL-T4", "测试承租方",
            self._item_payload("T-1", "焊机", "2026-09-01", "2026-12-31", "2000", "2000"),
        )
        t1 = self._item(lease, "T-1")
        other = self._register(
            "ZL-T5", "测试承租方2",
            self._item_payload("T-3", "水泵", "2026-09-01", "2026-12-01", "1000", "1000"),
        )
        t3 = self._item(other, "T-3")
        self.svc.register_return({"设备明细IDs": [t1["id"]], "退租日期": "2026-09-30"})

        first = self.svc.settle({"结算日期": "2026-10-01", "设备明细IDs": [t1["id"], t3["id"]]})
        self.assertEqual({x["id"] for x in first["本次入账设备"]}, {t1["id"]})

        # 再结一次：已入账的不重复
        again = self.svc.settle({"结算日期": "2026-10-01", "设备明细IDs": [t1["id"], t3["id"]]})
        self.assertEqual(again["生成租金账单"], [])

        # 下一批：T3 退租后，把从未出过账的 9 月与 10/1-10/20 一起补
        self.svc.register_return({"设备明细IDs": [t3["id"]], "退租日期": "2026-10-20"})
        second = self.svc.settle({"结算日期": "2026-10-21", "设备明细IDs": [t3["id"]]})
        months = sorted((b["账期月份"], money(b["金额"])) for b in second["生成租金账单"])
        self.assertEqual(months, [("2026-09", Decimal("1000.00")),
                                  ("2026-10", Decimal("645.16"))])

    def test_cross_month_rent_is_split_per_calendar_month(self) -> None:
        lease = self._register(
            "ZL-T6", "测试承租方",
            self._item_payload("T-4", "吊车", "2026-09-10", "2026-12-09", "3000", "3000"),
        )
        t4 = self._item(lease, "T-4")
        self.svc.register_return({"设备明细IDs": [t4["id"]], "退租日期": "2026-11-05"})
        res = self.svc.settle({"结算日期": "2026-11-06", "设备明细IDs": [t4["id"]]})
        months = [(b["账期月份"], money(b["金额"])) for b in res["生成租金账单"]]
        self.assertEqual(months, [
            ("2026-09", Decimal("2100.00")),
            ("2026-10", Decimal("3000.00")),
            ("2026-11", Decimal("500.00")),
        ])

    # ---- 改租重算 -------------------------------------------------------
    def test_term_change_keeps_original_and_recalculates(self) -> None:
        lease = self._register(
            "ZL-T7", "测试承租方",
            self._item_payload("T-1", "焊机", "2026-09-01", "2026-12-31", "2000", "2000"),
        )
        t1 = self._item(lease, "T-1")
        self.svc.register_return({"设备明细IDs": [t1["id"]], "退租日期": "2026-09-30"})
        self.svc.settle({"结算日期": "2026-10-01", "设备明细IDs": [t1["id"]]})

        old = next(b for b in rental_store.item_bills(t1["id"]) if b["账单类型"] == "租金")
        old_id, old_amount = old["id"], old["金额"]

        detail, msg = self.svc.change_term(t1["id"], {
            "新起租日期": "2026-09-01", "新到期日期": "2026-11-15",
            "新月租金额": "3000", "变更原因": "补充协议加价",
        })
        old_after = next(b for b in rental_store.bills if b["id"] == old_id)
        self.assertEqual(old_after["金额"], old_amount)       # 原值保留
        self.assertEqual(old_after["状态"], "已冲销")
        revised = [b for b in rental_store.item_bills(t1["id"])
                   if b["状态"] == "有效" and b["账单类型"] == "租金重算"]
        self.assertEqual(len(revised), 1)
        self.assertEqual(money(revised[0]["金额"]), Decimal("3000.00"))
        self.assertEqual(revised[0]["版本号"], 2)
        self.assertEqual(revised[0]["关联账单ID"], old_id)
        self.assertEqual(len(rental_store.find_item(t1["id"])["变更记录"]), 1)
        self.assertIn("原账单保留原值", msg)

        # 重算后应收取新版本：3000 而不是原 2000
        self.assertEqual(money(detail["租金应收"]), Decimal("3000.00"))

    def test_term_change_lower_rent_keeps_overpayment_as_prepaid(self) -> None:
        """月租调低且账期已全额收款：多缴不消失，形成预收并随押金退回。"""
        lease = self._register(
            "ZL-T7B", "测试承租方",
            self._item_payload("T-9", "焊机", "2026-09-01", "2026-12-31",
                               "2000", "2000", "全额退"),
        )
        item = self._item(lease, "T-9")
        self.svc.register_return({"设备明细IDs": [item["id"]], "退租日期": "2026-09-30"})
        res = self.svc.settle({"结算日期": "2026-10-01", "设备明细IDs": [item["id"]]})
        bill_id = res["生成租金账单"][0]["id"]
        self.svc.receive_payment(bill_id, {"金额": "2000"})

        # 改租降价：9 月应收变 1500
        detail, _ = self.svc.change_term(item["id"], {
            "新起租日期": "2026-09-01", "新到期日期": "2026-12-31", "新月租金额": "1500",
        })
        self.assertEqual(money(detail["租金应收"]), Decimal("1500.00"))
        self.assertEqual(money(detail["租金已收"]), Decimal("2000.00"))  # 实收 2000 不丢
        self.assertEqual(money(detail["租金预收"]), Decimal("500.00"))
        self.assertEqual(money(detail["租金未收"]), Decimal("0.00"))
        preview = detail["押金测算"]
        self.assertEqual(money(preview["预收租金"]), Decimal("500.00"))
        self.assertEqual(money(preview["应退押金"]), Decimal("2500.00"))  # 押金 2000 + 预收 500

        # 押金退还单已被改租流程重开，金额为 -2500；原单冲销原值保留
        refunds = [b for b in detail["账单"] if b["账单类型"] == "押金退还"]
        self.assertTrue(any(b["状态"] == "已冲销" for b in refunds))
        live = next(b for b in refunds if b["状态"] == "有效")
        self.assertEqual(money(live["金额"]), Decimal("-2500.00"))

    def test_term_change_shortens_period_and_recalculates_existing_bills(self) -> None:
        lease = self._register(
            "ZL-T8", "测试承租方",
            self._item_payload("T-1", "挖机", "2026-05-01", "2026-08-31", "8000", "16000", "扣欠费"),
        )
        t1 = self._item(lease, "T-1")
        self.svc.register_return({"设备明细IDs": [t1["id"]], "退租日期": "2026-08-15"})
        self.svc.settle({"结算日期": "2026-08-16", "设备明细IDs": [t1["id"]]})
        # 改租：提前到 8 月 10 日，月租 9000（与种子 EQ-201 同场景）
        detail, _ = self.svc.change_term(t1["id"], {
            "新起租日期": "2026-05-01", "新到期日期": "2026-08-10",
            "新月租金额": "9000",
        })
        amounts = sorted(
            (b["账期月份"], money(b["金额"]))
            for b in detail["账单"] if b["状态"] == "有效" and b["账单类型"] == "租金重算"
        )
        self.assertEqual(amounts, [
            ("2026-05", Decimal("9000.00")),
            ("2026-06", Decimal("9000.00")),
            ("2026-07", Decimal("9000.00")),
            ("2026-08", Decimal("2903.23")),
        ])
        # 原 8 月单 3870.97 原样保留但已冲销
        old_aug = next(b for b in rental_store.item_bills(t1["id"])
                       if b["账单类型"] == "租金" and b["账期月份"] == "2026-08")
        self.assertEqual(old_aug["金额"], Decimal("3870.97"))
        self.assertEqual(old_aug["状态"], "已冲销")

    # ---- 押金算法 -------------------------------------------------------
    def test_deposit_algorithms(self) -> None:
        # 扣欠费：欠费超过押金，应退 0
        lease = self._register(
            "ZL-T9", "测试承租方",
            self._item_payload("T-1", "焊机", "2026-09-01", "2026-11-30",
                               "2000", "2000", "扣欠费"),
        )
        t1 = self._item(lease, "T-1")
        self.svc.register_return({"设备明细IDs": [t1["id"]], "退租日期": "2026-09-30"})
        self.svc.settle({"结算日期": "2026-10-01", "设备明细IDs": [t1["id"]]})
        self.assertEqual(money(self.svc.item_detail(t1["id"])["押金测算"]["应退押金"]),
                         Decimal("0.00"))

        # 扣欠费及违约金（提前退租）：欠费 10000 + 违约金 5000，押金 30000 -> 退 15000
        lease2 = self._register(
            "ZL-T10", "测试承租方2",
            self._item_payload("T-5", "钻机", "2026-09-01", "2026-12-31",
                               "10000", "30000", "扣欠费及违约金", "0.5"),
        )
        t5 = self._item(lease2, "T-5")
        self.svc.register_return({"设备明细IDs": [t5["id"]], "退租日期": "2026-09-30"})
        self.svc.settle({"结算日期": "2026-10-01", "设备明细IDs": [t5["id"]]})
        preview = self.svc.item_detail(t5["id"])["押金测算"]
        self.assertEqual(money(preview["违约金"]), Decimal("5000.00"))
        self.assertEqual(money(preview["应退押金"]), Decimal("15000.00"))

        # 正常到期即使选了违约金算法也不计违约金
        lease3 = self._register(
            "ZL-T11", "测试承租方3",
            self._item_payload("T-6", "焊机", "2026-09-01", "2026-09-30",
                               "10000", "30000", "扣欠费及违约金", "0.5"),
        )
        t6 = self._item(lease3, "T-6")
        self.svc.register_return({"设备明细IDs": [t6["id"]], "退租日期": "2026-09-30"})
        self.svc.settle({"结算日期": "2026-10-01", "设备明细IDs": [t6["id"]]})
        preview = self.svc.item_detail(t6["id"])["押金测算"]
        self.assertEqual(money(preview["违约金"]), Decimal("0.00"))

    # ---- 台账/概览同源 --------------------------------------------------
    def test_ledger_and_overview_share_one_source(self) -> None:
        lease = self._register(
            "ZL-T12", "测试承租方",
            self._item_payload("T-1", "焊机", "2026-09-01", "2026-12-31", "2000", "2000"),
            self._item_payload("T-2", "焊机", "2026-09-01", "2026-12-31", "2000", "2000"),
        )
        ledger_before = self.svc.ledger()
        overview_before = self.svc.overview()
        self.assertEqual(
            money(str(next(c["value"] for c in overview_before["cards"]
                           if c["label"] == "在租台数"))),
            Decimal(str(ledger_before["在租台数"])),
        )

        t1 = self._item(lease, "T-1")
        self.svc.register_return({"设备明细IDs": [t1["id"]], "退租日期": "2026-09-30"})
        ledger_after = self.svc.ledger()
        overview_after = self.svc.overview()
        on_hire_after = next(c["value"] for c in overview_after["cards"]
                             if c["label"] == "在租台数")
        self.assertEqual(on_hire_after, ledger_after["在租台数"])
        self.assertEqual(ledger_before["在租台数"] - 1, ledger_after["在租台数"])
        due_in_overview = next(c["value"] for c in overview_after["cards"]
                               if c["label"] == "租金应收（元）")
        self.assertAlmostEqual(due_in_overview, ledger_after["汇总"]["租金应收"], places=2)

    # ---- 收款 -----------------------------------------------------------
    def test_overpayment_is_rejected(self) -> None:
        lease = self._register(
            "ZL-T13", "测试承租方",
            self._item_payload("T-1", "焊机", "2026-09-01", "2026-12-31", "2000", "2000"),
        )
        t1 = self._item(lease, "T-1")
        self.svc.register_return({"设备明细IDs": [t1["id"]], "退租日期": "2026-09-30"})
        res = self.svc.settle({"结算日期": "2026-10-01", "设备明细IDs": [t1["id"]]})
        bill_id = res["生成租金账单"][0]["id"]
        with self.assertRaises(RentalError):
            self.svc.receive_payment(bill_id, {"金额": "99999"})
        result = self.svc.receive_payment(bill_id, {"金额": "2000"})
        self.assertEqual(result["剩余未结"], 0.0)

    def test_invalid_inputs_are_rejected(self) -> None:
        with self.assertRaises(RentalError):
            self.svc.register_lease({"承租方": "无单号"})
        with self.assertRaises(RentalError):
            self.svc.register_lease({
                "租赁单号": "ZL-BAD", "承租方": "承租方",
                "设备明细": [self._item_payload("B", "B", "2026-12-01", "2026-09-01", "1", "1")],
            })
        with self.assertRaises(RentalError):
            self.svc.register_lease({
                "租赁单号": "ZL-BAD2", "承租方": "承租方",
                "设备明细": [self._item_payload("B", "B", "2026-09-01", "2026-12-01", "1", "1",
                                               algorithm="随便写")],
            })


if __name__ == "__main__":
    unittest.main()
