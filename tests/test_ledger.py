"""事件账本哈希链与重放完整性测试。"""

from __future__ import annotations

import json

from src.disaster_relief.errors import IntegrityError
from src.disaster_relief.state import State
from src.disaster_relief.store import EventStore

try:
    from ._harness import ServiceTestCase
except ImportError:  # 以 tests 为发现目录运行时
    from _harness import ServiceTestCase


class LedgerIntegrityTest(ServiceTestCase):
    def _ledger_lines(self) -> list[str]:
        return (self.dir / "ledger.jsonl").read_text(encoding="utf-8").strip().splitlines()

    def test_events_form_hash_chain(self) -> None:
        self.seed_site("S1")
        self.cmd("receive_person", {"person_id": "P1", "site_id": "S1"},
                 role="rescue_worker")
        records = self.app.store.records()
        prev = "0" * 64
        for record in records:
            self.assertEqual(record["prev"], prev)
            self.assertEqual(len(record["hash"]), 64)
            prev = record["hash"]

    def test_tampered_content_is_detected_on_reload(self) -> None:
        self.seed_site("S1")
        self.cmd("receive_person", {"person_id": "P1", "site_id": "S1"},
                 role="rescue_worker")
        lines = self._ledger_lines()
        record = json.loads(lines[0])
        record["data"]["capacity"] = 9999
        lines[0] = json.dumps(record, ensure_ascii=False)
        (self.dir / "ledger.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(IntegrityError):
            EventStore(self.dir / "ledger.jsonl")

    def test_missing_line_breaks_seq(self) -> None:
        self.seed_site("S1")
        self.cmd("receive_person", {"person_id": "P1", "site_id": "S1"},
                 role="rescue_worker")
        lines = self._ledger_lines()
        (self.dir / "ledger.jsonl").write_text(lines[1] + "\n", encoding="utf-8")
        with self.assertRaises(IntegrityError):
            EventStore(self.dir / "ledger.jsonl")

    def test_replay_reaches_same_state(self) -> None:
        self.seed_site("S1", capacity=5)
        for pid in ("P1", "P2", "P3"):
            self.cmd("receive_person", {"person_id": pid, "site_id": "S1"},
                     role="rescue_worker")
        state = State()
        self.app.store.replay_into(state)
        self.assertEqual(state.seq, self.app.commands.state.seq)
        self.assertEqual(state.site_occupancy("S1"), 3)
        self.assertEqual(sorted(state.persons), ["P1", "P2", "P3"])

    def test_out_of_order_apply_is_rejected(self) -> None:
        self.seed_site("S1")
        self.cmd("receive_person", {"person_id": "P1", "site_id": "S1"},
                 role="rescue_worker")
        records = self.app.store.records()
        state = State()
        # 直接投喂 seq=2（缺少 seq=1）应被折叠器拒绝
        with self.assertRaises(IntegrityError):
            state.apply(records[1])
