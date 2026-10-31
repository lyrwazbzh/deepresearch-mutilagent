# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""安全相关：密码哈希、JWT Token"""
import os
import bcrypt
from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from pydantic import BaseModel

# JWT 配置
ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))

# 密钥最小长度；短于此长度视为可被暴力破解的弱值
MIN_SECRET_KEY_LENGTH = 32

# 历史版本把这些值当作默认密钥写死在源码里，等同于公开已知值，必须持续拒绝
KNOWN_WEAK_SECRET_KEYS = frozenset({
    "your-super-secret-key-change-in-production",
})

_GENERATE_HINT = 'python -c "import secrets; print(secrets.token_urlsafe(48))"'


def validate_secret_key() -> str:
    """读取并校验 ``JWT_SECRET_KEY``，不合格立即失败。

    刻意**不提供任何默认值**：一旦兜底成公开可知的字符串，任何人都能伪造合法 Token。
    本函数在模块导入期即执行，因此「密钥缺失/过弱」会让应用在**启动阶段**就终止，
    而不是等到第一次签发 Token 才暴露。
    """
    value = os.environ.get("JWT_SECRET_KEY")
    if not value:
        raise RuntimeError(
            "缺少必需的环境变量 JWT_SECRET_KEY。请在 backend/.env 中配置强随机值，"
            f"生成方式：{_GENERATE_HINT}"
        )
    if value in KNOWN_WEAK_SECRET_KEYS:
        raise RuntimeError(
            "JWT_SECRET_KEY 使用了公开已知的弱值（历史默认值），必须替换为强随机值，"
            f"生成方式：{_GENERATE_HINT}"
        )
    if len(value) < MIN_SECRET_KEY_LENGTH:
        raise RuntimeError(
            f"JWT_SECRET_KEY 长度不足（当前 {len(value)}，要求 ≥ {MIN_SECRET_KEY_LENGTH}）。"
            f"弱密钥可被暴力破解，请改用强随机值，生成方式：{_GENERATE_HINT}"
        )
    return value


SECRET_KEY = validate_secret_key()


class Token(BaseModel):
    """Token 响应模型"""
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    """Token 数据模型"""
    user_id: Optional[str] = None
    username: Optional[str] = None


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码"""
    return bcrypt.checkpw(
        plain_password.encode('utf-8'),
        hashed_password.encode('utf-8')
    )


def get_password_hash(password: str) -> str:
    """生成密码哈希"""
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode('utf-8'), salt)
    return hashed.decode('utf-8')


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """创建访问 Token"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def decode_token(token: str) -> Optional[TokenData]:
    """解码 Token"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        username: str = payload.get("username")
        if user_id is None:
            return None
        return TokenData(user_id=user_id, username=username)
    except JWTError:
        return None
