"""T07 回归测试：数据库口令必填，且明文口令不得出现在受版本控制的文件里。

背景（事实 F-07）：`docker-compose.yml` 把 `POSTGRES_PASSWORD: postgres123` 与 MinIO 的
`minioadmin/minioadmin` 明文写进受版本控制的文件；应用侧 `core/database.py` 又给了同一个
弱口令当默认值 —— compose 一旦忘记注入变量，应用会**静默**连上同一个弱口令的库。

执行期范围扩张：全仓同类扫描还发现 `backend/docker-compose-base.yml`、`backend/.env.example`、
历史文档曾携带该明文口令，本测试确保当前配置与 README 不再出现该弱口令。

本测试只读文件 + 调纯函数，不依赖任何基础设施。
"""

import pathlib

import pytest

import core.database as database

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent

# 本票要清除的明文口令（历史 weak 值）
WEAK_SECRETS = ("postgres123", "minioadmin")

COMPOSE_FILES = (
    REPO_ROOT / "docker-compose.yml",
    BACKEND_DIR / "docker-compose-base.yml",
)

# 除 compose 外，还扫这两个「会告诉用户口令是什么」的受控文件：
# README 与启动脚本。它们同样不该把明文口令复制出来。
DOC_FILES = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "start-services.sh",
    BACKEND_DIR / ".env.example",
)


def test_require_env_raises_and_names_the_variable(monkeypatch):
    """缺失时必须显式失败，并在报错里点名变量 —— 否则排查者不知道要配什么。"""
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
        database._require_env("POSTGRES_PASSWORD")


def test_require_env_returns_value_when_present(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "some-strong-password")

    assert database._require_env("POSTGRES_PASSWORD") == "some-strong-password"


def test_database_module_no_longer_defaults_the_password():
    """结构断言：`os.getenv("POSTGRES_PASSWORD", <默认值>)` 的写法必须彻底消失。"""
    source = (BACKEND_DIR / "app" / "core" / "database.py").read_text(encoding="utf-8")

    for weak in WEAK_SECRETS:
        assert weak not in source, f"database.py 仍含明文口令 {weak!r}"
    # 有意保留源码字符串断言：本测试的验收口径本身就是「源码里不得出现弱口令 /
    # 必须存在导入期校验调用」（ticket T07 验收 = grep 类命令的 pytest 化），
    # 不是对运行时行为的断言，与第 2 批 findings 中鉴权测试改结构化断言的处置不同型。
    assert "POSTGRES_PASSWORD = _require_env(" in source


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_compose_files_have_no_plaintext_secrets(path):
    content = path.read_text(encoding="utf-8")

    for weak in WEAK_SECRETS:
        assert weak not in content, f"{path.name} 仍含明文口令 {weak!r}"

    # MinIO 变量必须由环境注入，而不是写死
    assert "${MINIO_ROOT_USER}" in content
    assert "${MINIO_ROOT_PASSWORD}" in content


def test_root_compose_injects_postgres_password():
    """根 compose 有 postgres 服务；口令必须走环境变量。"""
    content = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "${POSTGRES_PASSWORD}" in content


def test_base_compose_needs_no_postgres_variable():
    """`backend/docker-compose-base.yml` 只起 redis / milvus 等，不含 postgres 服务。"""
    content = (BACKEND_DIR / "docker-compose-base.yml").read_text(encoding="utf-8")

    assert "postgres:" not in content
    assert "${POSTGRES_PASSWORD}" not in content


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_milvus_gets_the_same_minio_credentials(path):
    """轮换 MinIO 口令后，Milvus 必须拿到同一组凭据，否则向量库会静默不可写。"""
    content = path.read_text(encoding="utf-8")

    assert "${MINIO_ROOT_USER}" in content
    assert "${MINIO_ROOT_PASSWORD}" in content
    assert "MINIO_ACCESS_KEY_ID" in content
    assert "MINIO_SECRET_ACCESS_KEY" in content


def test_root_env_example_documents_compose_variables():
    content = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "POSTGRES_PASSWORD=" in content
    assert "MINIO_ROOT_USER=" in content
    assert "MINIO_ROOT_PASSWORD=" in content


def test_backend_env_example_blanked_the_weak_password():
    content = (BACKEND_DIR / ".env.example").read_text(encoding="utf-8")

    for weak in WEAK_SECRETS:
        assert weak not in content, f"backend/.env.example 仍含明文口令 {weak!r}"
    assert "POSTGRES_PASSWORD=\n" in content


@pytest.mark.parametrize("path", DOC_FILES, ids=lambda p: p.name)
def test_docs_and_scripts_do_not_repeat_the_plaintext_password(path):
    """README / 启动脚本会把口令「抄」给用户看，同样不该出现明文弱口令。"""
    content = path.read_text(encoding="utf-8")

    for weak in WEAK_SECRETS:
        assert weak not in content, f"{path.name} 仍含明文口令 {weak!r}"


def test_top_level_env_is_gitignored():
    """顶层 `.env` 必须被忽略 —— 否则用户照抄模板就会把真口令提交上去。"""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    lines = {line.strip() for line in gitignore.splitlines()}

    assert ".env" in lines
