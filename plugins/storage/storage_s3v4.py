"""AWS Signature Version 4 签名器（对象存储类别共享模块，**纯标准库、纯函数**）。

从 `plugins/cloud/aws/plugin.py` 的同名实现参数化抽取而来（逐字节校验过 AWS 官方测试向量）。
放在类别目录且带 ``storage_`` 前缀的理由见 ``storage_common.py`` 抬头。

只做签名：给定 method / host / path / query / payload / 凭据 / region，产出**要发的完整头**
（含 ``Authorization`` 与 ``x-amz-content-sha256``）。URL 由调用方按同一 host/path/query 拼出，
保证「发出去的」与「签出来的」逐字节一致。
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
from typing import Any
from urllib.parse import quote

ALGORITHM = "AWS4-HMAC-SHA256"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()   # GET 空体，S3 的每个请求都要带这个值


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def uri_encode(value: Any) -> str:
    """SigV4 要求的 RFC3986 编码：``-_.~`` 不编码，其余（含斜杠、空格）一律百分号编码。"""
    return quote(str(value), safe="-_.~")


def canonical_query(params: dict | None) -> str:
    """规范查询串：按 key 升序 + RFC3986 编码。**必须自己拼**——httpx 的 ``params=`` 会按自己
    的规则排序与编码，拼出来未必与这里一致，签名就对不上。URL 也直接由本函数结果拼成。
    """
    if not params:
        return ""
    return "&".join(
        f"{uri_encode(key)}={uri_encode(value)}"
        for key, value in sorted(params.items(), key=lambda item: str(item[0]))
    )


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, datestamp: str, region: str, service: str) -> bytes:
    """HMAC 逐级派生：AWS4+SK → 日期 → 区域 → 服务 → aws4_request。"""
    key = _hmac(("AWS4" + secret_key).encode("utf-8"), datestamp)
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, "aws4_request")


def _canonical_request(method: str, path: str, query: str, headers: list[tuple[str, str]],
                       payload_hash: str) -> tuple[str, str]:
    items = sorted(headers, key=lambda item: item[0])
    canonical_headers = "".join(f"{name}:{value}\n" for name, value in items)
    signed_headers = ";".join(name for name, _ in items)
    canonical_request = "\n".join(
        [method, path, query, canonical_headers, signed_headers, payload_hash]
    )
    return canonical_request, signed_headers


def sign(*, method: str, host: str, path: str, query: str = "", payload: bytes = b"",
         access_key: str, secret_key: str, session_token: str = "",
         region: str, service: str = "s3", now: datetime.datetime | None = None,
         extra_headers: tuple = ()) -> dict:
    """返回本次请求要发的完整头（含 ``Authorization``）。

    ``host`` **必须含端口**（如 ``127.0.0.1:9000``）——httpx 会用 URL 的 netloc 生成 Host 头，
    我们签同一个值，天然一致。``x-amz-content-sha256`` 由本函数按 ``payload`` 自动加进签名与返回头。
    """
    now = now or utcnow()
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(payload).hexdigest()

    headers: tuple = (("x-amz-content-sha256", payload_hash),) + tuple(extra_headers)
    to_sign: list[tuple[str, str]] = [("host", host), ("x-amz-date", amzdate)]
    to_sign.extend((str(name).lower(), str(value)) for name, value in headers)
    if session_token:
        to_sign.append(("x-amz-security-token", session_token))

    canonical_request, signed_headers = _canonical_request(method, path, query, to_sign, payload_hash)
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [ALGORITHM, amzdate, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()]
    )
    signature = hmac.new(
        _signing_key(secret_key, datestamp, region, service),
        string_to_sign.encode("utf-8"), hashlib.sha256,
    ).hexdigest()

    out: dict[str, str] = {str(name): str(value) for name, value in headers}
    out["X-Amz-Date"] = amzdate
    out["Authorization"] = (
        f"{ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    if session_token:
        out["X-Amz-Security-Token"] = session_token
    return out
