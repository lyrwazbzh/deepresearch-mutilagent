"""T59（P-13）回归测试：compose 必须给 backend 注入**容器内可达**的连接目标，
且不得因此把密钥写进受版本控制的 `docker-compose.yml`。

背景：`env_file: ./backend/.env` 把**宿主机导向**的 `.env` 原样注入容器，容器内的
`POSTGRES_HOST` / `REDIS_HOST` / `MILVUS_HOST` 因此仍是 `localhost` —— 在容器里那是
容器自身而非 compose 服务名，连接必被拒：

    psycopg2.OperationalError: connection to server at "localhost", port 5432 failed:
    Connection refused

修法是在 `backend` 服务上加 `environment` 覆盖块（compose 的优先级 environment > env_file），
且**只覆盖非密钥的连接目标**。本测试同时锁住「覆盖存在」与「没顺手把密钥搬进来」两件事。

只读文件 + 解析 YAML，不依赖 Docker / Postgres / Redis / Milvus。
"""

import pathlib
import re

import pytest
import yaml

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
COMPOSE = REPO_ROOT / "docker-compose.yml"

# 键 = 环境变量名（app 里真正 getenv 的名字），值 = 期望指向的 compose 服务名
CONTAINER_HOST_OVERRIDES = {
    "POSTGRES_HOST": "postgres",
    "REDIS_HOST": "redis",
    "MILVUS_HOST": "milvus",
}

# environment 里出现这些词即说明密钥被搬进了受版本控制的文件
SECRET_HINTS = ("PASSWORD", "SECRET", "TOKEN", "CREDENTIAL", "_KEY")


def _as_env_map(env) -> dict:
    """compose 的 `environment` 允许 mapping 或 `KEY=value` 列表两种写法，统一成 dict。"""
    if env is None:
        return {}
    if isinstance(env, dict):
        return {str(k): "" if v is None else str(v) for k, v in env.items()}
    parsed = {}
    for entry in env:
        key, _, value = str(entry).partition("=")
        parsed[key] = value
    return parsed


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def backend_service(compose) -> dict:
    return compose["services"]["backend"]


def test_backend_overrides_container_hosts(backend_service):
    """三个宿主导向的主机名必须被改写成 compose 服务名。"""
    env = _as_env_map(backend_service.get("environment"))

    for var, expected in CONTAINER_HOST_OVERRIDES.items():
        assert env.get(var) == expected, (
            f"{var} 应为 {expected!r}（compose 服务名），实际 {env.get(var)!r} —— "
            "缺失时容器内会退回 env_file 里的 localhost，连接必被拒"
        )


def test_override_targets_are_declared_services(compose):
    """覆盖值必须指向本文件里真实存在的服务，否则等于换了个连不上的名字。"""
    for var, service in CONTAINER_HOST_OVERRIDES.items():
        assert service in compose["services"], f"{var}={service} 指向不存在的服务"


def test_override_variable_names_match_the_app_code(backend_service):
    """键名必须与 app 里真正 `os.getenv` 的名字一致 —— 否则覆盖是静默空操作。"""
    env = _as_env_map(backend_service.get("environment"))
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in (BACKEND_DIR / "app").rglob("*.py")
    )
    read_names = set(re.findall(r"""os\.getenv\(\s*["']([A-Z0-9_]+)["']""", source))

    assert env, "backend.environment 为空，P-13 的覆盖未生效"
    for var in env:
        assert var in read_names, (
            f"compose 覆盖了 {var}，但 app/ 下没有任何 os.getenv({var!r}) —— 拼错了变量名"
        )


def test_no_secret_is_written_into_compose(backend_service):
    """票面约束：密钥只能走 env_file，不得落进本文件。"""
    env = _as_env_map(backend_service.get("environment"))

    leaked = [var for var in env if any(hint in var.upper() for hint in SECRET_HINTS)]
    assert not leaked, f"以下疑似密钥被写进了 docker-compose.yml：{leaked}"


def test_secrets_still_come_from_env_file(backend_service):
    """覆盖块不能把 env_file 顶掉 —— 密钥仍需从中注入。"""
    env_file = backend_service.get("env_file")
    files = env_file if isinstance(env_file, list) else [env_file]

    assert "./backend/.env" in files, (
        f"backend.env_file 应仍指向 ./backend/.env，实际 {env_file!r} —— "
        "否则密钥（POSTGRES_PASSWORD / JWT_SECRET_KEY 等）无处注入"
    )
