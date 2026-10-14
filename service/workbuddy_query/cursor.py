"""不透明、防篡改的分页游标：只编码偏移量，服务端不保存分页会话。

游标用查询 token 签名，入口原样回传即可；被改写或过期则报 QUERY_FAILED。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

from service.workbuddy_query.models import QUERY_FAILED, QueryError


def _sign(body: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body.encode("ascii"),
                    hashlib.sha256).hexdigest()[:16]


def encode_cursor(offset: int, secret: str) -> str:
    raw = json.dumps({"o": int(offset)}, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"{body}.{_sign(body, secret)}"


def decode_cursor(cursor: str, secret: str) -> int:
    try:
        body, signature = cursor.rsplit(".", 1)
        if not hmac.compare_digest(signature, _sign(body, secret)):
            raise ValueError("bad cursor signature")
        padding = "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body + padding).decode("utf-8"))
        offset = int(payload["o"])
        if offset < 0:
            raise ValueError("negative offset")
        return offset
    except (ValueError, KeyError, TypeError) as exc:
        raise QueryError(QUERY_FAILED) from exc
