"""只追加的事件账本。

每条记录携带 ``seq`` 与前一条记录的哈希，形成可校验的哈希链：
任何删除、改写或乱序都会在重放时被发现（``IntegrityError``）。

持久化使用单行 JSON（JSONL），追加后 ``fsync``，进程崩溃最多丢失
尚未落盘的最后一条；已确认的回执一定可回放。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Iterable

from .clock import now_ms
from .errors import IntegrityError

GENESIS_HASH = "0" * 64
_ENCODING = "utf-8"


def canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode(_ENCODING)).hexdigest()


class EventStore:
    """文件型只追加事件存储。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []
        self._load()

    # ---- 读取 -------------------------------------------------------

    @property
    def seq(self) -> int:
        return len(self._records)

    def records(self) -> tuple[dict[str, Any], ...]:
        """返回当前全部记录（快照元组，防止外部改写）。"""
        return tuple(self._records)

    def _load(self) -> None:
        if not self.path.exists():
            self.path.touch()
            return
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding=_ENCODING) as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise IntegrityError(
                        f"第{line_no}行事件无法解析", details={"line": line_no}
                    ) from exc
                self._verify_record(record, records, line_no)
                records.append(record)
        self._records = records

    @staticmethod
    def _verify_record(
        record: dict[str, Any], previous: list[dict[str, Any]], line_no: int
    ) -> None:
        required = {"seq", "ts", "type", "data", "meta", "prev", "hash"}
        if not isinstance(record, dict) or set(record) != required:
            raise IntegrityError(f"第{line_no}行事件字段不完整", details={"line": line_no})
        expected_seq = len(previous) + 1
        if record["seq"] != expected_seq:
            raise IntegrityError(
                f"第{line_no}行序号断裂：期望{expected_seq}，实际{record['seq']}",
                details={"line": line_no, "expected": expected_seq, "actual": record["seq"]},
            )
        expected_prev = previous[-1]["hash"] if previous else GENESIS_HASH
        if record["prev"] != expected_prev:
            raise IntegrityError(
                f"第{line_no}行前序哈希不匹配", details={"line": line_no}
            )
        expected_hash = canonical_hash(
            {
                "seq": record["seq"],
                "ts": record["ts"],
                "type": record["type"],
                "data": record["data"],
                "meta": record["meta"],
                "prev": record["prev"],
            }
        )
        if record["hash"] != expected_hash:
            raise IntegrityError(f"第{line_no}行内容哈希不匹配", details={"line": line_no})

    # ---- 写入 -------------------------------------------------------

    def append(
        self,
        event_type: str,
        data: dict[str, Any],
        meta: dict[str, Any] | None = None,
        *,
        ts: int | None = None,
    ) -> dict[str, Any]:
        """在锁内追加事件并落盘，返回完整记录。"""
        meta = dict(meta or {})
        with self._lock:
            record = self._build_record(event_type, data, meta, ts)
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            # O_APPEND 保证单条写入原子边界；fsync 保证重启后仍在。
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line.encode(_ENCODING))
                os.fsync(fd)
            finally:
                os.close(fd)
            self._records.append(record)
            return dict(record)

    def _build_record(
        self,
        event_type: str,
        data: dict[str, Any],
        meta: dict[str, Any],
        ts: int | None,
    ) -> dict[str, Any]:
        seq = len(self._records) + 1
        prev = self._records[-1]["hash"] if self._records else GENESIS_HASH
        record: dict[str, Any] = {
            "seq": seq,
            "ts": ts if ts is not None else meta.get("occurred_at") or now_ms(),
            "type": event_type,
            "data": data,
            "meta": meta,
            "prev": prev,
        }
        record["hash"] = canonical_hash(record)
        return record

    def replay_into(self, sink: Any) -> None:
        """把全部事件依次投递给具备 ``apply(event)`` 的状态对象。"""
        for record in self._records:
            sink.apply(record)

    def iter_after(self, seq: int) -> Iterable[dict[str, Any]]:
        yield from self._records[seq:]
