"""T40：``core/security.py`` 的 JWT 签发 / 校验护栏。

背景（事实 F-31）：``core/security.py`` 是全部鉴权的信任根，但此前只有 T02 的
**配置期**密钥校验（``tests/core/test_security_jwt.py``）—— 签发 / 校验路径零覆盖。
本文件补齐这段空白。

覆盖范围：

1. 签发 → 校验往返（``sub`` / ``username`` 透传，含无 ``username`` 的情形）；
2. 篡改签名 / 篡改载荷 / 无 ``sub`` / 非 JWT 串 → 校验失败（返回 ``None``，不抛异常）；
3. 过期 Token → 校验失败（用**负** ``expires_delta`` 构造，不引入 ``freezegun``）；
4. 用另一密钥签发的 Token → 校验失败；
5. ``get_current_user_required`` 在缺失 / 格式错误 ``Authorization`` 头下的行为
   （缺失、非 Bearer 方案、Bearer 但非 JWT → 401），以及有效 Token 下的
   放行 / 未知用户 401 / 已禁用用户 403。

**刻意不重复** T02 的配置期用例（密钥缺失 / 为空 / 过短 / 沿用历史默认值 → 导入即失败）：
那四个情形由 ``tests/core/test_security_jwt.py`` 独家维护，两处并存只会带来双份维护成本。

全部用例不连数据库：``get_user_by_id`` 由 monkeypatch 接管；``get_db`` 产出的 Session
是惰性的，401 / 403 路径不会触发任何查询。因此本文件在 T27 的默认口径
（``-m "not needs_infra"``）下可直接运行。

另有 2 例（缺 ``Authorization`` 头 / 垃圾 Token → 401 + 固定文案）与
``tests/router/test_router_auth.py`` 的「17 个端点各发一次无 Token 请求」覆盖同一事实，
但粒度不同 —— 本文件验的是**依赖单元**，那份验的是**路由挂载**，故两处并存而非二选一。
"""

import types
from datetime import datetime, timedelta

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from core import security
from router import auth_router
from router.auth_router import get_current_user_required

# 明显假值，长度满足 security.MIN_SECRET_KEY_LENGTH；绝不使用任何真实密钥
OTHER_SIGNING_KEY = "test-only-other-signing-key-" + "y" * 32

# auth_router 抛出的固定文案
CREDENTIALS_DETAIL = "无法验证凭据"
DISABLED_DETAIL = "用户已被禁用"

FAKE_USER_ID = "11111111-1111-1111-1111-111111111111"


def _fake_user(active: bool = True, user_id: str = FAKE_USER_ID):
    """只需 ``id`` / ``is_active`` 两个属性，无需构造真实 ORM 实体。"""
    return types.SimpleNamespace(id=user_id, is_active=active)


def _protected_client(monkeypatch, user):
    """挂一个最小受保护端点，并把 ``get_user_by_id`` 换成返回 ``user`` 的替身。

    注意：FastAPI 0.141 下不能把 ``APIRouter`` 直接交给 ``TestClient``，
    需经最小 ``FastAPI`` 应用挂载（与 ``tests/router/test_router_auth.py`` 同一写法）。
    """
    app = FastAPI()

    @app.get("/protected")
    async def protected(current=Depends(get_current_user_required)):
        return {"user_id": str(current.id)}

    # 替身按 user_id 匹配才返回用户 —— 于是「查询键确实取自 token 的 sub」也被断言覆盖。
    # 若不分青红皂白返回 user，即使被测代码传错字段（例如 username）测试仍会绿。
    def _lookup(db, user_id):
        return user if user is not None and user_id == str(user.id) else None

    monkeypatch.setattr(auth_router, "get_user_by_id", _lookup)
    return TestClient(app)


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- 1) 签发 → 校验往返 ---------------------------------------------------------

def test_roundtrip_preserves_subject_and_username():
    token = security.create_access_token({"sub": "u-1", "username": "alice"})

    data = security.decode_token(token)

    assert data is not None
    assert data.user_id == "u-1"
    assert data.username == "alice"


def test_roundtrip_without_username_yields_none_username():
    token = security.create_access_token({"sub": "u-2"})

    data = security.decode_token(token)

    assert data is not None
    assert data.user_id == "u-2"
    assert data.username is None


# --- 2) 篡改 / 畸形输入 ---------------------------------------------------------

def test_tampered_signature_is_rejected():
    token = security.create_access_token({"sub": "u-1"})
    header, payload, signature = token.split(".")

    # ⚠️ 必须改**中间**字符，不能改末位 —— 这是 §4 门禁（第二轮）实测出来的坑：
    # 签名是 32 字节 HMAC，base64url（无填充）后是 **43 个字符**，末位字符只承载
    # **4 个有效比特**（可出现的取值只有 A/E/I/M/Q/U/Y/c/g/k/o/s/w/0/4/8 这 16 个）。
    # 因此「把末位换成高 4 位相同的另一个字符」（例如把 'Y' 换成 'a'，二者高 4 位都是 0110）
    # 解码后**字节完全相同**，签名依然有效 —— 篡改是个空操作。
    # 实测：改末位时 **6.20%（248/4000）** 的 token 篡改无效，使本用例偶发假红。
    # 中间字符承载 6 个有效比特，换成任何不同字符都必然改变解码字节。
    mid = len(signature) // 2
    flipped = "A" if signature[mid] != "A" else "B"

    tampered = ".".join([header, payload, signature[:mid] + flipped + signature[mid + 1:]])

    assert tampered != token
    assert security.decode_token(tampered) is None


def test_tampered_signature_is_rejected_for_many_tokens():
    """同一件事做 200 次：**每一次**都必须被拒（把「偶发假红」变成确定性）。

    原实现改末位字符，只在末位恰好是 'Y' 时才真正改变字节（约 1/16），其余情况下
    「篡改」是空操作、验签照过（实测 6.20%，248/4000）。本用例用 200 个不同 token
    覆盖各种末位取值，保证「改中间字符」这条修法在任何取值下都成立。
    """
    for i in range(200):
        token = security.create_access_token({"sub": f"u-{i}"})
        header, payload, signature = token.split(".")
        mid = len(signature) // 2
        flipped = "A" if signature[mid] != "A" else "B"
        tampered = ".".join([header, payload, signature[:mid] + flipped + signature[mid + 1:]])

        assert security.decode_token(tampered) is None, f"第 {i} 个 token 的签名篡改未被检出"


def test_tampered_payload_is_rejected():
    token = security.create_access_token({"sub": "u-1"})
    header, payload, signature = token.split(".")
    flipped = "A" if payload[0] != "A" else "B"

    tampered = ".".join([header, flipped + payload[1:], signature])

    assert security.decode_token(tampered) is None


def test_token_without_subject_is_rejected():
    """没有 ``sub`` 的 Token 无法定位用户，必须判失败而不是返回空 TokenData。"""
    token = security.create_access_token({"username": "alice"})

    assert security.decode_token(token) is None


@pytest.mark.parametrize("bad", ["", "not-a-jwt", "a.b.c", "Bearer xyz"])
def test_malformed_token_is_rejected(bad):
    """畸形输入一律返回 ``None`` —— 不得把 JWTError 泄漏给调用方。"""
    assert security.decode_token(bad) is None


# --- 3) 有效期 -----------------------------------------------------------------

def test_expired_token_is_rejected():
    """用负的 expires_delta 构造过期 Token，避免引入 freezegun 依赖。"""
    token = security.create_access_token(
        {"sub": "u-1"}, expires_delta=timedelta(minutes=-5)
    )

    assert security.decode_token(token) is None


def test_unexpired_token_is_accepted():
    """对照用例：正的 expires_delta 应当通过，证明上面的失败来自过期而非其它原因。"""
    token = security.create_access_token(
        {"sub": "u-1"}, expires_delta=timedelta(minutes=5)
    )

    assert security.decode_token(token) is not None


# --- 4) 密钥不匹配 -------------------------------------------------------------

def test_token_signed_with_another_key_is_rejected():
    forged = jwt.encode(
        {"sub": "u-1", "exp": datetime.utcnow() + timedelta(minutes=5)},
        OTHER_SIGNING_KEY,
        algorithm=security.ALGORITHM,
    )

    assert security.decode_token(forged) is None


# --- 5) Authorization 头与当前用户的解析 ---------------------------------------

def test_missing_authorization_header_is_rejected(monkeypatch):
    """无 Authorization 头 → 401 且文案为鉴权依赖的固定值。"""
    client = _protected_client(monkeypatch, _fake_user())

    resp = client.get("/protected")

    assert resp.status_code == 401
    assert resp.json()["detail"] == CREDENTIALS_DETAIL


def test_non_bearer_scheme_is_rejected(monkeypatch):
    """非 Bearer 方案（如 Basic）→ 401。"""
    client = _protected_client(monkeypatch, _fake_user())

    resp = client.get("/protected", headers={"Authorization": "Basic dXNlcjpwYXNz"})

    assert resp.status_code == 401
    assert resp.json()["detail"] == CREDENTIALS_DETAIL


def test_bearer_with_garbage_token_is_rejected(monkeypatch):
    """Bearer 后的内容不是合法 JWT → 401。"""
    client = _protected_client(monkeypatch, _fake_user())

    resp = client.get("/protected", headers=_auth_header("not-a-jwt"))

    assert resp.status_code == 401
    assert resp.json()["detail"] == CREDENTIALS_DETAIL


def test_valid_token_with_active_user_is_accepted(monkeypatch):
    """正常路径：有效 Token + 启用用户 → 200，且拿到的是 sub 对应的用户。"""
    user = _fake_user()
    client = _protected_client(monkeypatch, user)
    token = security.create_access_token({"sub": str(user.id)})

    resp = client.get("/protected", headers=_auth_header(token))

    assert resp.status_code == 200
    assert resp.json()["user_id"] == str(user.id)


def test_valid_token_for_unknown_user_is_rejected(monkeypatch):
    """Token 合法但用户已被删除 → 401。"""
    client = _protected_client(monkeypatch, None)
    token = security.create_access_token({"sub": "u-404"})

    resp = client.get("/protected", headers=_auth_header(token))

    assert resp.status_code == 401
    assert resp.json()["detail"] == CREDENTIALS_DETAIL


def test_valid_token_for_disabled_user_returns_403(monkeypatch):
    """用户被禁用 → 403（与「凭据无效」的 401 区分开，便于前端给出正确提示）。"""
    user = _fake_user(active=False)
    client = _protected_client(monkeypatch, user)
    token = security.create_access_token({"sub": str(user.id)})

    resp = client.get("/protected", headers=_auth_header(token))

    assert resp.status_code == 403
    assert resp.json()["detail"] == DISABLED_DETAIL
