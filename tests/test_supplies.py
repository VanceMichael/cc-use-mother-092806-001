"""物资守恒、退回/补发、重复回执幂等测试。"""

from __future__ import annotations

from src.disaster_relief.errors import ConflictError
from src.disaster_relief.state import SUP_RETURNED, SUP_REISSUED

try:
    from ._harness import ServiceTestCase
except ImportError:  # 以 tests 为发现目录运行时
    from _harness import ServiceTestCase


class SuppliesTest(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed_site("S1")
        self.cmd("receive_person", {"person_id": "P1", "site_id": "S1"}, role="rescue_worker")
        self.cmd("register_supply_item",
                 {"item_id": "RICE", "name": "米", "unit": "袋"})

    def _allocate(self, qty: int) -> None:
        self.cmd("allocate_supplies",
                 {"site_id": "S1", "item_id": "RICE", "quantity": qty})

    def test_distribution_debits_available_and_cannot_overshoot(self) -> None:
        self._allocate(10)
        self.cmd("distribute_supplies", {
            "site_id": "S1", "person_id": "P1", "item_id": "RICE", "quantity": 4,
        }, role="site_worker")
        self.assertEqual(self.app.commands.state.allocation_available("S1", "RICE"), 6)
        with self.assertRaises(ConflictError):
            self.cmd("distribute_supplies", {
                "site_id": "S1", "person_id": "P1", "item_id": "RICE", "quantity": 7,
            }, role="site_worker")

    def test_return_restores_stock_and_cannot_be_redistributed_twice(self) -> None:
        self._allocate(10)
        dist = self.cmd("distribute_supplies", {
            "site_id": "S1", "person_id": "P1", "item_id": "RICE", "quantity": 4,
        }, role="site_worker")
        self.cmd("return_supplies",
                 {"distribution_id": dist["distribution_id"], "quantity": 4,
                  "reason": "重复领取"},
                 role="site_worker")
        # 已发出的物资只能通过退回恢复库存：可用量回到 10
        self.assertEqual(self.app.commands.state.allocation_available("S1", "RICE"), 10)
        record = self.app.commands.state.distributions[dist["distribution_id"]]
        self.assertEqual(record["status"], SUP_RETURNED)
        # 同一单不能退两次
        with self.assertRaises(ConflictError):
            self.cmd("return_supplies",
                     {"distribution_id": dist["distribution_id"]},
                     role="site_worker")

    def test_reissue_creates_linked_new_record_and_debits_stock(self) -> None:
        self._allocate(10)
        dist = self.cmd("distribute_supplies", {
            "site_id": "S1", "person_id": "P1", "item_id": "RICE", "quantity": 4,
        }, role="site_worker")
        reissue = self.cmd("reissue_supplies", {
            "original_distribution_id": dist["distribution_id"],
            "quantity": 4, "reason": "物资受潮",
        }, role="site_worker")
        new_id = reissue["events"][0]["data"]["new_distribution_id"]
        new_record = self.app.commands.state.distributions[new_id]
        self.assertEqual(new_record["status"], SUP_REISSUED)
        self.assertEqual(new_record["related_id"], dist["distribution_id"])
        # 原单保持已发放，补发新单再次扣减：10 - 4 - 4 = 2
        self.assertEqual(self.app.commands.state.allocation_available("S1", "RICE"), 2)

    def test_duplicate_receipt_with_command_id_returns_original_decision(self) -> None:
        self._allocate(10)
        payload = {"site_id": "S1", "person_id": "P1", "item_id": "RICE", "quantity": 3}
        first = self.cmd("distribute_supplies", payload, role="site_worker",
                         command_id="CMD-1")
        duplicate = self.cmd("distribute_supplies", payload, role="site_worker",
                             command_id="CMD-1")
        # 重复回执返回原决定，不产生新事件、不再次扣库存
        self.assertTrue(duplicate.get("replayed"))
        self.assertEqual(duplicate["seq"], first["seq"])
        self.assertEqual(self.app.commands.state.allocation_available("S1", "RICE"), 7)

    def test_duplicate_replay_survives_restart(self) -> None:
        self._allocate(10)
        payload = {"site_id": "S1", "person_id": "P1", "item_id": "RICE", "quantity": 3}
        first = self.cmd("distribute_supplies", payload, role="site_worker",
                         command_id="CMD-42")
        app2 = self.restart_app()
        duplicate = app2.commands.execute(
            "distribute_supplies", payload,
            role="site_worker", command_id="CMD-42",
        )
        self.assertTrue(duplicate.get("replayed"))
        self.assertEqual(duplicate["seq"], first["seq"])

    def test_distribution_is_visible_in_person_history(self) -> None:
        self._allocate(5)
        dist = self.cmd("distribute_supplies", {
            "site_id": "S1", "person_id": "P1", "item_id": "RICE", "quantity": 2,
        }, role="site_worker")
        history = self.app.queries.person_distributions("P1")
        self.assertEqual([d["distribution_id"] for d in history], [dist["distribution_id"]])
