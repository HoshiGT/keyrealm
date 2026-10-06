"""Google 服务账号（Service Account）JWT 断言的协议级工具模块。

云类别下 `cloud/gcp` 与 `cloud/firebase` 两个子插件共用本模块：把服务账号的
**邮箱 + 私钥**换成一枚 OAuth2 access_token，是 Google 全家（GCP / Firebase / FCM…）
**完全一致**的一步，因此单独成模块，避免两处各写一份 RS256 签名。

**输入不是 JSON**：一份服务账号 JSON 里真正有信息量的只有邮箱与私钥，其余
（项目号、客户端 ID、各种 URL）要么是常量、要么只用于展示。所以字段按「一行一项」
拆平——邮箱 / 私钥 / 密钥 ID / 项目 ID 各占一个单行输入框；私钥在单行框里会被压掉
换行，`normalize_sa` 负责把它还原成合法 PEM。

这里只放**协议本身**（归一化输入 / 签发 JWT / 换 token），不含任何服务级判定：
「什么状态码算有效」写在各自 plugin.py 里。

依赖 `cryptography`（见 requirements.txt）。缺失时不抛 ImportError 冒泡，而是抬
`GoogleSAError` 给调用方展示——打包环境里少了这个包也能给出人话提示。

本模块完全独立，不导入 backend 包。
"""
from __future__ import annotations

import base64
import json
import re
import time

import httpx

from cloud_common import make_client

# 换 token 用云平台全权限 scope：服务账号（含 Firebase adminsdk 默认的 Editor 角色）都能批
DEFAULT_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
TOKEN_URI_FALLBACK = "https://oauth2.googleapis.com/token"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"

# 与 manifest 一一对应的扁平字段；真正决定能否认证的只有邮箱与私钥两项
SA_FIELDS = ("client_email", "private_key", "private_key_id", "project_id")
REQUIRED_FIELDS = (("client_email", "服务账号邮箱"), ("private_key", "私钥（PEM）"))


class GoogleSAError(Exception):
    """本地错误（JSON 不合法、缺依赖等），``str(exc)`` 可直接展示给用户。"""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# 不用 \1 反向引用组 1：组 1 可以不参与匹配（普通 PRIVATE KEY），而 Python 里
# 未参与匹配的组做反向引用会直接匹配失败——那会把最常见的 PRIVATE KEY 全判成「不是 PEM」。
_PEM_FLAT = re.compile(r"-{5}BEGIN(RSA|ENCRYPTED)?PRIVATEKEY-{5}(.+?)-{5}END(?:RSA|ENCRYPTED)?PRIVATEKEY-{5}")


def normalize_pem(raw) -> str:
    """把「单行输入框里压过的私钥」还原成合法 PEM。

    单行输入框吃不下换行，而 PEM 恰恰靠 BEGIN / END 两行定界——粘贴进单行框后必然变成
    一条长串。这里不猜用户是哪种粘贴形态：**先把全部空白（含 header 内的空格）抹掉**，
    多行 / 换行变空格 / 换行被吃掉 / 字面 ``\\n`` 四种形态于是收敛到同一个扁平串，再统一
    按 64 列折行重建。不这么处理的话，最惨的形态连 ``BEGIN PRIVATE KEY`` 里的空格都没了
    （``-----BEGINPRIVATEKEY-----``），按原样匹配必然漏认。
    """
    text = str(raw or "")
    # 字面 \n / \r\n（从 json 文件里拷 private_key 的值就是这种）与真实换行，统一成换行
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    flat = "".join(text.split())
    match = _PEM_FLAT.search(flat)
    if not match:
        if text.strip().startswith("{"):
            raise GoogleSAError(
                "private_key 看起来是整段 JSON——这里只要 JSON 里 private_key 字段的**值**"
                "（以 -----BEGIN PRIVATE KEY----- 开头的那串）"
            )
        raise GoogleSAError("private_key 不是 PEM 私钥（应以 -----BEGIN PRIVATE KEY----- 开头）")
    # 私钥**只占这一栏**：除空白外前后不该再有别的东西。放行「粘整段 JSON」看着好用，
    # 实则会把别的字段一并存进私钥里、与上面几栏重复且可能对不上，故明确拒绝。
    if match.span() != (0, len(flat)):
        raise GoogleSAError(
            "private_key 前后多了内容——这里只填 JSON 里 private_key 字段的值"
            "（-----BEGIN PRIVATE KEY----- 起、-----END PRIVATE KEY----- 止），"
            "其余字段请填到各自的输入框"
        )
    label = f"{match.group(1) or ''} PRIVATE KEY".strip()
    body = match.group(2)
    if not body:
        raise GoogleSAError("private_key 的 PEM 主体为空")
    wrapped = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN {label}-----\n{wrapped}\n-----END {label}-----\n"


def normalize_sa(data) -> dict:
    """把扁平的单行字段拼成签名要用的 sa 字典；不合法时抬 ``GoogleSAError``。

    宽松处：``private_key_id`` / ``project_id`` 都可缺——前者只是 JWT 头里的 ``kid``，
    后者只用于补充探测项目信息，都不影响能否换到 token。
    """
    data = data or {}
    sa = {key: str(data.get(key) or "").strip() for key in SA_FIELDS}
    missing = [label for key, label in REQUIRED_FIELDS if not sa.get(key)]
    if missing:
        raise GoogleSAError("缺少必填字段: " + "、".join(missing))
    email = sa["client_email"]
    if "@" not in email or email.startswith("@"):
        raise GoogleSAError(f"client_email 不像服务账号邮箱（实际 {email}）")
    sa["private_key"] = normalize_pem(sa["private_key"])
    sa["type"] = "service_account"
    sa["token_uri"] = TOKEN_URI_FALLBACK
    return sa


def build_assertion(sa: dict, scope: str = DEFAULT_SCOPE, lifetime: int = 3600) -> str:
    """签发 RS256 的 JWT 断言（header.claims.signature）。"""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的友好提示
        raise GoogleSAError("缺少 cryptography 依赖，无法做 RS256 签名") from exc

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    if str(sa.get("private_key_id") or "").strip():
        header["kid"] = sa["private_key_id"]
    claims = {
        "iss": sa["client_email"],
        "scope": scope,
        "aud": str(sa.get("token_uri") or TOKEN_URI_FALLBACK),
        "iat": now,
        "exp": now + lifetime,
    }
    segment = ".".join(
        _b64url(json.dumps(part, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        for part in (header, claims)
    )
    try:
        key = serialization.load_pem_private_key(str(sa["private_key"]).encode("utf-8"), password=None)
    except Exception as exc:  # noqa: BLE001 - 各种 PEM 解析失败统一提示
        raise GoogleSAError(f"private_key 无法解析（{type(exc).__name__}）") from exc
    signature = key.sign(segment.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{segment}.{_b64url(signature)}"


def token_for(sa: dict, scope: str = DEFAULT_SCOPE) -> tuple[int, dict, str]:
    """用 JWT 断言换 access_token，返回 ``(HTTP 状态码, JSON 载荷, 正文摘要)``。

    只负责发请求、解析 JSON，不判断有效与否——状态码含义交给调用方（Firebase 与
    GCP 的判据相同，但展示信息不同）。
    """
    assertion = build_assertion(sa, scope)
    url = str(sa.get("token_uri") or TOKEN_URI_FALLBACK)
    with make_client() as client:
        resp = client.post(
            url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": GRANT_TYPE, "assertion": assertion},
        )
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001 - 非 JSON 错误页
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    snippet = " ".join((resp.text or "").split())[:240]
    return resp.status_code, payload, snippet


def token_error_detail(payload: dict, snippet: str = "") -> str:
    """把 Google 的 OAuth2 错误信封拍成一行可读文本。"""
    if payload:
        error = str(payload.get("error") or "").strip()
        description = str(payload.get("error_description") or "").strip()
        if error and description:
            return f"{error}: {description}"
        if error:
            return error
    return snippet


# token 端点回 ``invalid_grant`` 时，只有描述命中这些短语才敢判「这组凭据已失效」：
#   account not found    → 服务账号已被删除（doc 11 里 9 把 Firebase SA 的实测回包）
#   Invalid JWT Signature→ 私钥被轮换/吊销，与 client_email 对不上
# 其余 invalid_grant（时钟偏移、issuer 格式）说明密钥本身没问题，只能算「拿不准」，
# 否则会把「本机时间不对」误报成凭据失效。
INVALID_GRANT_HINTS = ("account not found", "invalid jwt signature")


def classify_token_error(status: int, payload: dict, snippet: str = "") -> tuple[str, str]:
    """把「换 token 失败」归类成 ``(VerifyResult.status, 说明文本)``。

    这是 OAuth2 错误信封的通用语义，与具体服务无关，故与 ``token_for`` 同放此处。
    """
    detail = token_error_detail(payload, snippet)
    error = str(payload.get("error") or "").strip()
    if error.lower() == "invalid_grant" and any(
        hint in detail.lower() for hint in INVALID_GRANT_HINTS
    ):
        return "invalid", f"服务账号已失效（{detail}）" if detail else "服务账号已失效"
    suffix = f"：{detail}" if detail else ""
    return "error", f"无法确认服务账号状态 (HTTP {status}){suffix}"


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def get_json(url: str, token: str) -> tuple[int, dict]:
    """带 Bearer 的只读 GET，返回 ``(状态码, JSON 载荷)``；网络异常由调用方兜底。"""
    with make_client() as client:
        resp = client.get(url, headers=bearer(token))
    try:
        payload = resp.json()
    except (ValueError, httpx.HTTPError):
        payload = {}
    return resp.status_code, payload if isinstance(payload, dict) else {}
