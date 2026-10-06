"""
事件落库（SQLite，标准库自带）。

用 (device_host, serialNo) 做主键去重：
设备的 serialNo 是全局单调递增的事件流水号，frontSerialNo 指向上一条，
两者配合既能去重，也能发现丢事件（缺口）从而触发对账。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    device      TEXT NOT NULL,
    serial_no   INTEGER NOT NULL,
    front_no    INTEGER,
    time        TEXT,
    received_at REAL NOT NULL,
    live        INTEGER NOT NULL DEFAULT 0,
    major       INTEGER,
    minor       INTEGER,
    event_name  TEXT,
    method      TEXT,
    actor_kind  TEXT,
    person      TEXT,
    person_name TEXT,
    card_no     TEXT,
    door_no     INTEGER,
    remote_host TEXT,
    net_user    TEXT,
    configured_verify_mode TEXT,
    code_verified INTEGER DEFAULT 0,
    picture     BLOB,
    picture_type TEXT,
    raw         TEXT,
    PRIMARY KEY (device, serial_no)
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(time DESC);
CREATE INDEX IF NOT EXISTS idx_events_received ON events(received_at DESC);

CREATE TABLE IF NOT EXISTS door_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          REAL NOT NULL,
    door        INTEGER,
    ok          INTEGER,
    detail      TEXT,
    source      TEXT
);
"""


class Store:
    def __init__(self, path: str | Path, device: str):
        self.path = str(path)
        self.device = device
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # -- 事件 -------------------------------------------------------------

    def last_serial(self) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(serial_no) AS s FROM events WHERE device=?", (self.device,)
            ).fetchone()
        return row["s"] if row and row["s"] is not None else None

    def insert_event(self, rec: dict, raw: dict, picture: bytes | None = None,
                     picture_type: str | None = None) -> bool:
        """插入事件。已存在（同 serialNo）返回 False。"""
        if rec.get("serialNo") is None:
            return False
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO events (device, serial_no, front_no, time, received_at, live,
                           major, minor, event_name, method, actor_kind, person, person_name,
                           card_no, door_no, remote_host, net_user, configured_verify_mode,
                           code_verified, picture, picture_type, raw)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        self.device, rec["serialNo"], rec.get("frontSerialNo"), rec.get("time"),
                        time.time(), 1 if rec.get("live") else 0,
                        rec.get("major"), rec.get("minor"), rec.get("eventName"),
                        rec.get("method"), rec.get("actorKind"), rec.get("person"),
                        rec.get("personName"), rec.get("cardNo"), rec.get("doorNo"),
                        rec.get("remoteHost"), rec.get("netUser"),
                        rec.get("configuredVerifyMode"), 1 if rec.get("codeVerified") else 0,
                        picture, picture_type,
                        json.dumps(raw, ensure_ascii=False),
                    ),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def attach_picture(self, serial_no: int, picture: bytes, picture_type: str) -> bool:
        """把随后到达的抓拍图挂到刚插入的那条事件上。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE events SET picture=?, picture_type=? WHERE device=? AND serial_no=?",
                (picture, picture_type, self.device, serial_no),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def recent_events(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT serial_no, time, received_at, live, major, minor, event_name,
                          method, actor_kind, person, person_name, card_no, door_no,
                          remote_host, code_verified,
                          (picture IS NOT NULL) AS has_picture
                   FROM events WHERE device=?
                   ORDER BY received_at DESC LIMIT ?""",
                (self.device, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_picture(self, serial_no: int) -> tuple[bytes | None, str | None]:
        with self._lock:
            row = self._conn.execute(
                "SELECT picture, picture_type FROM events WHERE device=? AND serial_no=?",
                (self.device, serial_no),
            ).fetchone()
        if not row or row["picture"] is None:
            return None, None
        return row["picture"], row["picture_type"]

    # -- 开门动作留痕 -----------------------------------------------------

    def log_door_action(self, door: int, ok: bool, detail: str, source: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO door_actions (at, door, ok, detail, source) VALUES (?,?,?,?,?)",
                (time.time(), door, 1 if ok else 0, detail, source),
            )
            self._conn.commit()

    def recent_door_actions(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM door_actions ORDER BY at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
            live = self._conn.execute("SELECT COUNT(*) c FROM events WHERE live=1").fetchone()["c"]
            pics = self._conn.execute(
                "SELECT COUNT(*) c FROM events WHERE picture IS NOT NULL").fetchone()["c"]
        return {"total": total, "live": live, "pictures": pics}
