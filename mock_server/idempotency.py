"""幂等键机制。

要防的线上事故：**网络超时导致的重复提交。**
客户端 POST 创建工单，服务端已经落库，但响应在网络上丢了。客户端重试，
于是同一个业务动作产生了两条工单——车间照着两个工单各领一次料，
多出来的库存和产能消耗要月底对账才发现。

实现（这是生产系统里的标准做法，不是玩具）：
1. 客户端给每次"业务动作"生成一个唯一键，放在 `Idempotency-Key` 头里；
2. 服务端把 (idem_key, scope) 插进唯一索引表。插入成功 = 第一次来，真干活；
3. 插入撞唯一索引 = 重放。此时：
   - 已 DONE 且请求内容指纹一致 -> 直接回放上次的响应体（客户端看到 200/201，语义一致）
   - 已 DONE 但内容指纹不同 -> 409 IDEMPOTENCY_KEY_CONFLICT（复用键传了别的数据，
     这是客户端 bug，必须报错而不是"帮你执行一次"）
   - 还 IN_PROGRESS -> 409 IDEMPOTENCY_IN_PROGRESS（并发的同一请求，直接拒绝比等待更安全）
4. 干完活，把响应体写回该行并置 DONE。

注意"先占坑再干活"的顺序：如果反过来（先干活再写幂等键），
两个并发请求会同时通过检查、各干一次，幂等就白做了。
"""

from __future__ import annotations

import json

from fastapi import Request, Response

from mock_server.db import write_tx
from mock_server.errors import IdempotencyConflict, IdempotencyInProgress
from mock_server.util import now_iso, stable_hash

IDEMPOTENCY_HEADER = "Idempotency-Key"

# 只有"会创建/改变业务数据"的接口需要幂等。GET 天然幂等，不需要键。
IDEMPOTENT_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def get_idem_key(request: Request) -> str | None:
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if key is None:
        return None
    key = key.strip()
    if not key:
        return None
    if len(key) > 128:
        # 超长键直接截断会让不同请求撞到一起，所以宁可报错
        from mock_server.errors import ValidationFailed

        raise ValidationFailed(f"{IDEMPOTENCY_HEADER} 长度不能超过 128 字符")
    return key


def fingerprint(payload) -> str:
    return stable_hash(payload)


class IdempotencyGuard:
    """配合 with 语句使用的幂等守卫。用法见各 router。

    成功时：`guard.replay_response` 为 None -> 正常执行 -> 调用 `guard.store_response()`。
    重放时：把缓存的响应直接抛出 `IdempotentReplay`，由上层转成 JSONResponse。
    """

    def __init__(self, request: Request, scope: str, payload):
        self.key = get_idem_key(request)
        self.scope = scope
        self.request_hash = fingerprint(payload)
        self.replay: tuple[int, dict] | None = None
        self._claimed = False

    def __enter__(self) -> "IdempotencyGuard":
        if self.key is None:
            return self
        now = now_iso()
        try:
            with write_tx() as conn:
                conn.execute(
                    "INSERT INTO idempotency_keys (idem_key, scope, request_hash, state,"
                    " created_at, updated_at) VALUES (?, ?, ?, 'IN_PROGRESS', ?, ?)",
                    (self.key, self.scope, self.request_hash, now, now),
                )
            self._claimed = True
        except Exception as exc:  # sqlite3.IntegrityError：撞唯一索引
            if "UNIQUE" not in str(exc).upper():
                raise
            self._handle_duplicate()
        return self

    def _handle_duplicate(self) -> None:
        from mock_server.db import read_tx

        with read_tx() as conn:
            row = conn.execute(
                "SELECT * FROM idempotency_keys WHERE idem_key = ? AND scope = ?",
                (self.key, self.scope),
            ).fetchone()

        # 唯一索引保证了这一行必然存在；如果读不到，说明刚刚被清理，重试即可
        if row is None:
            raise IdempotencyInProgress("幂等记录状态异常，请重试")

        if row["request_hash"] != self.request_hash:
            raise IdempotencyConflict(
                "同一个 Idempotency-Key 被用于了不同的请求内容",
                idempotency_key=self.key,
                scope=self.scope,
            )
        if row["state"] != "DONE":
            raise IdempotencyInProgress(
                "相同请求正在处理中，请勿重复提交",
                idempotency_key=self.key,
            )
        self.replay = (row["response_code"], json.loads(row["response_body"]))

    def store_response(self, status_code: int, body: dict) -> None:
        """把响应体落库，后续重放直接返回它，客户端拿到完全一致的响应。"""
        if not self._claimed or self.key is None:
            return
        with write_tx() as conn:
            conn.execute(
                "UPDATE idempotency_keys SET state='DONE', response_code=?,"
                " response_body=?, updated_at=? WHERE idem_key=? AND scope=?",
                (status_code, json.dumps(body, ensure_ascii=False), now_iso(), self.key, self.scope),
            )

    def __exit__(self, exc_type, exc, tb) -> bool:
        # 干活动失败时把占坑记录删掉，否则客户端换数据重试会被误判成"键冲突"。
        # 这是很多人写幂等时会漏掉的一步，也是"幂等键把接口彻底卡死"的根因。
        if exc_type is not None and self._claimed and self.key is not None:
            try:
                with write_tx() as conn:
                    conn.execute(
                        "DELETE FROM idempotency_keys WHERE idem_key=? AND scope=? AND state='IN_PROGRESS'",
                        (self.key, self.scope),
                    )
            except Exception:  # noqa: BLE001 - 清理失败不能掩盖原始异常
                pass
        return False


def replay_or_none(guard: IdempotencyGuard) -> Response | None:
    """重放命中时构造响应；否则返回 None 表示继续正常处理。"""
    if guard.replay is None:
        return None
    status_code, body = guard.replay
    return Response(
        content=json.dumps(body, ensure_ascii=False),
        status_code=status_code,
        media_type="application/json",
        # 明确告诉调用方"这是重放"，便于排查；客户端可据此做指标统计
        headers={"X-Idempotent-Replay": "true"},
    )
