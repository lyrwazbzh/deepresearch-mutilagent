"""回归测试：**全部 12 个路由模块**的端点都不得匿名可达（T64 起为目录级全覆盖）。

## 沿革：三次都漏在「判据作用域 < 缺陷散布面」

- 事实 F-03：`document_router` / `chat_router` / `search_router` / `news_router` 原先**没有任何
  鉴权依赖**（未登录即可上传检索文档、创建会话、发起补全、消耗第三方搜索配额），T04 / T05 修复；
  `attachment_router` 是全仓最后一个用 `get_current_user`（**可选**认证）的路由（未登录即可读取
  任意会话的附件清单、取详情、删附件及其落盘文件），由 §4 第一轮总门禁补入、T55 修掉。
- **T64（本版）**：前两次都是「发现一个补一个」，因为本文件的 `ROUTER_MODULES` 一直是**硬编码清单**
  —— 所以每次都能再从缝里漏掉一个（`attachment_router` 就是这么漏的，与 `document_router` 的
  清理缺陷从单文件源码锁里漏出去同型）。本版改为**目录级枚举 + 三分类穷尽**：
  `app/router/*.py` 即真相源，新增模块不登记就会让 `test_every_router_module_is_classified` 直接失败。

## 被钉住的事实基线（T64 实测，68 个端点）

| 分类 | 模块数 | 端点数 | 机制 |
|------|--------|--------|------|
| `ROUTER_LEVEL_AUTH_MODULES` | 5 | 21（全部受保护） | `APIRouter(dependencies=[Depends(get_current_user_required)])`，新增端点自动受保护 |
| `ENDPOINT_LEVEL_AUTH_MODULES` | 7 | 47（44 受保护 + 3 匿名） | 每个端点签名里 `Depends(get_current_user_required)`（历史写法，由穷尽性锁兜住） |

合计 12 个模块 / 68 个端点，其中 **65 个必须要求鉴权**，3 个匿名端点全部落在 `auth_router`
（注册 / 登录 / 取 token —— **拿 token 不能要求先有 token**，公开是设计要求），逐个登记在
`ANONYMOUS_ENDPOINTS` 并写明理由。

`get_current_user`（**可选**认证）当前在全仓端点的依赖链上**出现 0 次** ——
`test_no_endpoint_uses_optional_auth` 把这条钉死，防止 T55 那一类缺陷以「顺手复用可选依赖」的形式复发。

「是否需要保留匿名端点」的判定依据（已实测，见 `tickets.md` T05）：前端**没有任何匿名调用方**
—— `frontend/src/router/routes.tsx` 里除 `/login` 外所有页面（含 `/404`）都在 `AuthGuard`
子树内；`/search/web` 在 `frontend/src` 中命中 0 处。

## 「全覆盖」是靠什么保证的

目录级枚举只是把模块**列全**；真正致命的是「某个模块**导入失败**，于是它的端点从判据里静默消失」
—— 那等于回到硬编码清单的老路，只是失败得更隐蔽。所以导入循环**不吞异常**，失败原因逐条收进
`_IMPORT_ERRORS`，由 `test_every_router_module_is_runtime_importable` 断言为空。

T64 施工时这条锁立刻起作用：`research_router` 是 12 个模块里**唯一**导入不进来的（conftest 的
`service` 占位包缺 `ResearchService`、`deep_research_v2.agents` 占位包不执行 `__init__` 致
`service.deep_research_v2.service` 连带不可导入），其 10 个端点**从来没有被任何鉴权回归覆盖过**。
修法是给共享 conftest 补两处占位（见该文件的 T64 注释，实测总代价 ~0.1s），本文件因此覆盖到 **12/12**。

## 判据的三层

1. **静态层**（结构化对象，不做源码字符串匹配）：直接读 `APIRouter.dependencies` 与
   `route.dependant.dependencies`。源码匹配会被格式化（换行 / 尾逗号）打翻，且与真实依赖图不一致。
2. **行为层**：除三个匿名端点外，**每个端点**都实发一次不带 Token 的请求，断言 401 且
   detail 为鉴权依赖的固定文案 —— 确保 401 来自鉴权，而不是恰好某个环节也返回 401。
   反向亦钉住：三个匿名端点**不得**返回 401（否则登录链路被掐断，而所有 401 断言依然全绿）。
3. **顺序层**：鉴权必须是**第一个**被解析的依赖（`test_auth_is_the_first_dependency_resolved`）。
   这一层是 CI 逼出来的、也是本文件最有价值的一处收获：`research_router` 的两个 `/research/stream`
   把服务构造依赖写在了鉴权**之前**，于是**未登录请求也会真的构造服务** ——
   在有 `.env` 的开发机上侥幸仍是 401，在 CI 上直接 500。**「有没有挂鉴权」不等于
   「未登录一定被挡住」**，只有顺序也对了才是。
"""

import importlib
from pathlib import Path
from types import ModuleType
from typing import Dict, Iterable, List, Set, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from router.auth_router import get_current_user, get_current_user_required

# --------------------------------------------------------------------------- 目录级枚举
# 模块清单不再硬编码：`app/router/*.py` 即真相源，新增模块自动进入本文件的判据。
# ⚠️ 目录必须从**具体模块**推导：`app/router/` 没有 `__init__.py`，`router` 是命名空间包，
#    `importlib.import_module("router").__file__` 为 None（实测踩过）。
ROUTER_DIR = Path(importlib.import_module("router.auth_router").__file__).parent
ALL_ROUTER_MODULES: Set[str] = {
    path.stem for path in ROUTER_DIR.glob("*.py") if path.stem != "__init__"
}

# --------------------------------------------------------------------------- 三分类
# 鉴权挂在 router 级：新增端点自动受保护，是**首选**写法。
ROUTER_LEVEL_AUTH_MODULES: Set[str] = {
    "document_router",
    "chat_router",
    "search_router",
    "news_router",
    "attachment_router",
}

# 鉴权逐端点挂载：等价安全性，但没有「新增端点自动受保护」的性质，
# 因此由 test_no_unauthenticated_endpoint_outside_the_allowlist 逐端点兜住。
ENDPOINT_LEVEL_AUTH_MODULES: Set[str] = {
    "auth_router",
    "database_router",
    "knowledge_router",
    "memory_router",
    "observability_router",
    "research_router",
    "session_router",
}

# 唯一允许匿名的端点：(模块, 方法, 路径) -> 匿名理由。每个都要写明理由；无理由的匿名端点一律视为缺陷。
# 键用三元组而不是「模块 + "METHOD /path"」拼串 —— 后者要在断言里再 split 回来，
# 等于把三个字段硬塞进一个字符串（多一个空格就静默失配），而且读起来还要心算。
ANONYMOUS_ENDPOINTS: Dict[Tuple[str, str, str], str] = {
    ("auth_router", "POST", "/auth/register"): "注册入口：此时尚无凭据可带，匿名是设计要求",
    ("auth_router", "POST", "/auth/login"): "登录入口：同上",
    ("auth_router", "POST", "/auth/token"): "OAuth2 取 token 入口：同上",
}

# 端点数量护栏（全部 12 个模块）：数量变化时强制复核新增/删除的端点是否也应鉴权。
EXPECTED_ENDPOINT_COUNTS = {
    "document_router": 4,
    "chat_router": 4,
    "search_router": 1,
    "news_router": 8,
    "attachment_router": 4,
    "auth_router": 6,
    "database_router": 5,
    "knowledge_router": 9,
    "memory_router": 6,
    "observability_router": 4,
    "research_router": 10,
    "session_router": 7,
}

# 鉴权依赖抛出的固定文案（`router/auth_router.py` 的 credentials_exception）。
CREDENTIALS_DETAIL = "无法验证凭据"

# --------------------------------------------------------------------------- 运行时载入
# **不吞异常**：导入失败的模块会从所有参数化判据里静默消失（=回到硬编码清单的老路，
# 而且失败得更隐蔽），所以失败原因逐条收下来，由
# test_every_router_module_is_runtime_importable 断言为空。
_MODULES: Dict[str, ModuleType] = {}
_IMPORT_ERRORS: Dict[str, str] = {}

for _module_name in sorted(ALL_ROUTER_MODULES):
    try:
        _MODULES[_module_name] = importlib.import_module(f"router.{_module_name}")
    except Exception as _exc:  # noqa: BLE001 - 失败原因需原样报给断言，不能在这里收敛
        _IMPORT_ERRORS[_module_name] = f"{type(_exc).__name__}: {_exc}"


def _loaded(names: Iterable[str]) -> List[str]:
    """从给定模块名里只挑出**成功导入**的那些。

    其余判据一律经由此处取模块名，好让「导入失败」这件事只由
    `test_every_router_module_is_runtime_importable` **一处**报错、并带上真实原因；
    否则会以 `KeyError: 'xxx_router'` 的形式炸在别的地方（实测：炸在参数化清单的收集期），
    把诊断信息盖掉。
    """
    return [name for name in names if name in _MODULES]


def _endpoints_of(module_name: str) -> List[Tuple[str, str]]:
    """产出该模块的 (HTTP 方法, 路径)，忽略自动生成的 HEAD / OPTIONS。

    只应在**已成功导入**的模块上调用（调用方均经 `_loaded` 过滤）。
    """
    return [
        (method, route.path)
        for route in _MODULES[module_name].router.routes
        for method in sorted(route.methods - {"HEAD", "OPTIONS"})
    ]


def _all_endpoints() -> Iterable[Tuple[str, str, str]]:
    """产出全部已载入模块的 (模块名, 方法, 路径)。

    穷尽性扫描类用例一律走这里：省得每个用例各抄一遍两层循环，抄漏一层就是一条假绿。
    """
    for module_name in _loaded(sorted(ALL_ROUTER_MODULES)):
        for method, path in _endpoints_of(module_name):
            yield module_name, method, path


def _dependency_calls(module_name: str, method: str, path: str) -> List[object]:
    """返回该**具体端点**依赖链上的被调函数（含 router 级与端点级）。

    必须同时匹配 method 与 path：`research_router` 里 `POST /research/stream` 与
    `GET /research/stream` 同路径不同方法，只按 path 取并集会让「其中一个漏鉴权」
    被另一个的依赖掩盖成假绿。
    """
    calls: List[object] = []
    for route in _MODULES[module_name].router.routes:
        if route.path == path and method in route.methods:
            calls.extend(dependency.call for dependency in route.dependant.dependencies)
    return calls


def _request_kwargs(method: str, path: str) -> Dict[str, object]:
    """构造请求体。

    POST 端点给一个**形态正确的最小请求体**（文档上传用 multipart，其余用空 JSON），
    这样「唯一的拦截原因是没带 Token」而不是参数形态不对。
    依赖解析先于请求体校验，所以即便空 JSON 也能拿到 401；这里仍按真实形态发送，
    避免测试与真实调用方式脱节。
    """
    if method != "POST":
        return {}
    if path.endswith("/upload"):
        return {"files": {"file": ("测试文件.pdf", b"%PDF-1.4 test", "application/pdf")}}
    return {"json": {}}


_CLIENTS: Dict[str, TestClient] = {}


def _client(module_name: str) -> TestClient:
    # 注意：FastAPI 0.141 下不能把 APIRouter 直接交给 TestClient
    # （会报 `AssertionError: fastapi_middleware_astack not found`），需经最小 FastAPI 应用挂载。
    #
    # 每个模块只建一次：本文件现在要发 130 次请求（65 个端点 × 有效/无效 Token），
    # 每次重建 FastAPI + TestClient 会把耗时抬到 18s 量级。不带 `with` 的 TestClient
    # 不触发 lifespan，请求之间无共享状态；被观测的端点要么在鉴权依赖处 401、要么在
    # 请求体校验处 422，都不会产生副作用，因此复用是安全的。
    if module_name not in _CLIENTS:
        app = FastAPI()
        app.include_router(_MODULES[module_name].router)
        _CLIENTS[module_name] = TestClient(app)
    return _CLIENTS[module_name]


# --------------------------------------------------------------------------- 分类穷尽性
def test_every_router_module_is_classified() -> None:
    """🔒 目录级锁：`app/router/*.py` 的每个模块都必须被显式归类。

    新增一个路由模块而不在本文件登记 → 本用例失败。这是 T64 的核心：
    `attachment_router` 当年就是从硬编码清单的缝里漏掉的（与 document_router 的
    清理缺陷从单文件源码锁里漏出去是同一类结构问题）。
    """
    classified = ROUTER_LEVEL_AUTH_MODULES | ENDPOINT_LEVEL_AUTH_MODULES
    unclassified = sorted(ALL_ROUTER_MODULES - classified)

    assert not unclassified, (
        f"以下路由模块未被本文件的鉴权判据覆盖：{unclassified}。"
        "请确认它们的端点是否需要鉴权，并登记到 ROUTER_LEVEL_AUTH_MODULES / "
        "ENDPOINT_LEVEL_AUTH_MODULES（若确需匿名，登记到 ANONYMOUS_ENDPOINTS 并写明理由）"
    )
    assert not (ROUTER_LEVEL_AUTH_MODULES & ENDPOINT_LEVEL_AUTH_MODULES), (
        "同一模块不得同时出现在两种鉴权挂载方式里"
    )


def test_classification_matches_endpoint_count_guard() -> None:
    """三张表必须互相对齐 —— 只改一张会在里失败，强制复核。"""
    classified = ROUTER_LEVEL_AUTH_MODULES | ENDPOINT_LEVEL_AUTH_MODULES

    assert classified == set(EXPECTED_ENDPOINT_COUNTS)
    assert classified == ALL_ROUTER_MODULES


def test_every_router_module_is_runtime_importable() -> None:
    """🔒 覆盖完整性：**每个**路由模块都必须在运行时真正导入进来。

    这条锁不是形式主义 —— T64 施工时它立刻抓到 `research_router`：它是 12 个模块里唯一
    导入失败的一个（`tests/conftest.py` 的占位包缺 `ResearchService` /
    `deep_research_v2.agents` 不执行 `__init__`），于是它的 **10 个端点从来没有被任何鉴权
    回归覆盖过**，而且失败方式是「从参数化清单里静默消失」—— 与硬编码清单是同一类结构性
    盲区，只是更隐蔽。

    因此：任何模块一旦导入失败，本用例必须红，而不是让它的端点悄悄退出判据范围。
    """
    assert not _IMPORT_ERRORS, (
        f"以下路由模块在测试环境无法导入：{sorted(_IMPORT_ERRORS)}。"
        f"它们的所有端点都不在本文件的判据范围内 —— 这是结构性盲区，必须修。"
        f"导入失败详情：{_IMPORT_ERRORS}。"
        "若是共享 conftest 的占位包缺名，按 tests/conftest.py 里既有的占位方式补齐"
        "（替身须抛 NotImplementedError，避免真实调用被静默吞掉）"
    )
    assert set(_MODULES) == ALL_ROUTER_MODULES, (
        f"只有 {len(_MODULES)}/{len(ALL_ROUTER_MODULES)} 个模块被载入"
    )


# --------------------------------------------------------------------------- 逐端点判据
def test_no_unauthenticated_endpoint_outside_the_allowlist() -> None:
    """🔒 穷尽性扫描：除 `ANONYMOUS_ENDPOINTS` 明列的三个端点外，**没有任何端点**可以不带鉴权。

    这条同时覆盖 router 级与端点级两种挂载方式，所以它不依赖「模块属于哪一类」这个判断 ——
    哪怕分类表被改错，只要真有端点漏鉴权，这里就会红。
    """
    offenders: List[str] = []

    for module_name, method, path in _all_endpoints():
        if (module_name, method, path) in ANONYMOUS_ENDPOINTS:
            continue
        calls = _dependency_calls(module_name, method, path)
        if get_current_user_required not in calls:
            offenders.append(f"{module_name}: {method} {path}")

    assert not offenders, (
        f"以下端点未要求鉴权、且不在 ANONYMOUS_ENDPOINTS 白名单里：{offenders}。"
        "要么补 Depends(get_current_user_required)，要么登记为匿名并写明理由"
    )


def test_auth_is_the_first_dependency_resolved() -> None:
    """🔒 鉴权必须是**第一个**被解析的依赖 —— 任何其它依赖都不得排在它前面。

    这条锁来自 CI 实测（本票开 PR 后第一次跑就红了）：`research_router` 的
    `POST /research/stream` 与 `GET /research/stream` 原先把
    `services = Depends(get_research_service)` 写在 `current_user` **之前**。
    FastAPI 按签名顺序**逐个**解析依赖、任一个抛异常就短路，于是
    **未登录请求也会真的构造 V1 研究服务**（`ResearchService(...)` 要读 `BOCHA_API_KEY`）。
    后果有二：

    1. 「无 Token 应 401」在有 `.env` 的开发机上侥幸成立，在 CI（无 `.env`）上变成 **500** ——
       也就是说「未登录一定被挡住」这件事**依赖于部署配置**，不再由代码保证；
    2. 把服务端配置状态泄漏给匿名调用方，并让匿名请求付出本不该发生的构造开销。

    修复方式是把鉴权参数挪到签名最前；本仓 65 个受保护端点现已全部满足该性质。
    新增端点若把别的依赖写在鉴权之前，这里会直接挡下。
    """
    offenders: List[str] = []

    for module_name in _loaded(sorted(ALL_ROUTER_MODULES)):
        for route in _MODULES[module_name].router.routes:
            calls = [dependency.call for dependency in route.dependant.dependencies]
            if get_current_user_required not in calls:
                continue  # 匿名端点由 ANONYMOUS_ENDPOINTS 与其自锁用例负责
            index = calls.index(get_current_user_required)
            if index != 0:
                earlier = [getattr(call, "__name__", repr(call)) for call in calls[:index]]
                methods = sorted(route.methods - {"HEAD", "OPTIONS"})
                offenders.append(f"{module_name}: {methods} {route.path} -> 先解析 {earlier}")

    assert not offenders, (
        f"以下端点的鉴权依赖不是第一个被解析的：{offenders}。"
        "FastAPI 按签名顺序逐个解析依赖且短路 —— 非鉴权依赖排在鉴权之前，"
        "意味着**未登录请求也会执行它**（可能抛 500、泄漏配置状态、白白付出构造开销），"
        "于是「有没有鉴权」不再等于「未登录一定被挡住」。"
        "请把 `current_user: User = Depends(get_current_user_required)` 挪到其它依赖之前"
    )


def test_no_endpoint_uses_optional_auth() -> None:
    """🔒 可选鉴权（`get_current_user`）不得出现在任何端点的依赖链上。

    T55 的缺陷形态正是「用了可选鉴权」——未登录时 `current_user=None` 静默通过，
    于是越权读取成为可能。当前全仓出现 **0** 次，这里把它钉死。
    """
    offenders: List[str] = []

    for module_name, method, path in _all_endpoints():
        if get_current_user in _dependency_calls(module_name, method, path):
            offenders.append(f"{module_name}: {method} {path}")

    assert not offenders, (
        f"以下端点使用了可选鉴权 get_current_user：{offenders} —— "
        "改为 get_current_user_required（必要时再叠加归属校验）"
    )


def test_anonymous_allowlist_has_no_stale_entries() -> None:
    """白名单本身也要被钉住：每个登记项必须**真实存在**且**确实匿名**。

    防两种腐烂：① 端点被删/改名，白名单里留着一条永不命中的僵尸条目；
    ② 端点后来被加上了鉴权，而白名单仍声称它匿名（掩盖了真实状态）。
    """
    for (module_name, method, path), reason in ANONYMOUS_ENDPOINTS.items():
        assert reason.strip(), f"{module_name} {method} {path} 的匿名理由不得为空"

        if module_name not in _MODULES:
            # 该模块未能导入 —— 这时「端点是否存在」无从判断，交给完整性护栏
            # （test_every_router_module_is_runtime_importable）统一报错，避免重复噪声。
            continue

        actual = _endpoints_of(module_name)

        assert (method, path) in actual, (
            f"ANONYMOUS_ENDPOINTS 里的 {module_name} {method} {path} 不存在（端点数 {len(actual)}）——"
            "端点已删除或改名，请同步白名单"
        )

        for call in _dependency_calls(module_name, method, path):
            assert call is not get_current_user_required, (
                f"{module_name} {method} {path} 现已要求鉴权，不应再留在匿名白名单里"
            )


def test_anonymous_endpoint_count_is_stable() -> None:
    """匿名端点数必须恰好是 3 个 —— 增加即需显式复核。"""
    assert len(ANONYMOUS_ENDPOINTS) == 3, (
        f"匿名端点由 3 变为 {len(ANONYMOUS_ENDPOINTS)}；每个新增的匿名端点都要有明确理由"
    )


# --------------------------------------------------------------------------- router 级判据
@pytest.mark.parametrize("module_name", _loaded(sorted(ROUTER_LEVEL_AUTH_MODULES)))
def test_auth_dependency_is_mounted_at_router_level(module_name: str) -> None:
    """锁定实现选择：依赖挂在 **router 级**，新增端点自动受保护。

    直接读 `APIRouter.dependencies`（结构化对象），而不是在源码里做字符串匹配 ——
    后者会被格式化（换行 / 尾逗号）轻易打翻，且与 `route.dependant.dependencies`
    的真实依赖图不一致。

    注意两种容器的形状不同：`APIRouter.dependencies` 装的是 `Depends`（取 `.dependency`），
    而 `route.dependant.dependencies` 装的是 `Dependant`（取 `.call`）。
    """
    mounted = [dependency.dependency for dependency in _MODULES[module_name].router.dependencies]

    assert any(
        call is get_current_user_required for call in mounted
    ), f"{module_name} 未在 router 级挂载鉴权依赖；改为逐端点挂载会让新增端点漏加鉴权"


@pytest.mark.parametrize("module_name", _loaded(sorted(ROUTER_LEVEL_AUTH_MODULES)))
def test_every_route_is_covered_by_the_router_dependency(module_name: str) -> None:
    """路由级依赖必须覆盖该 router 下的全部端点。"""
    routes: list = list(_MODULES[module_name].router.routes)

    assert routes, f"{module_name} 没有任何端点，断言失去意义"

    for route in routes:
        calls = [dependency.call for dependency in route.dependant.dependencies]
        assert get_current_user_required in calls, f"{route.path}（{module_name}）未被鉴权依赖覆盖"


@pytest.mark.parametrize(
    "module_name,expected",
    [(name, EXPECTED_ENDPOINT_COUNTS[name]) for name in _loaded(sorted(EXPECTED_ENDPOINT_COUNTS))],
)
def test_endpoint_count_is_expected(module_name: str, expected: int) -> None:
    """端点数量护栏：数量变化时提醒复核是否所有端点都应鉴权，并同步更新本断言。"""
    actual = len(_MODULES[module_name].router.routes)

    assert actual == expected, (
        f"{module_name} 端点数量由 {expected} 变为 {actual}；"
        "请确认新增/删除的端点是否也应鉴权，并更新 EXPECTED_ENDPOINT_COUNTS"
    )


# --------------------------------------------------------------------------- 行为层（真实请求）
PROTECTED_ENDPOINTS = [
    pytest.param(module_name, method, path, id=f"{module_name}-{method}-{path}")
    for module_name, method, path in _all_endpoints()
    if (module_name, method, path) not in ANONYMOUS_ENDPOINTS
]


@pytest.mark.parametrize("module_name,method,path", PROTECTED_ENDPOINTS)
def test_endpoint_without_token_returns_401(module_name: str, method: str, path: str) -> None:
    """未带 Token → 401，不得放行。"""
    response = _client(module_name).request(method, path, **_request_kwargs(method, path))

    assert response.status_code == 401, (
        f"{method} {path}（{module_name}）未带 Token 时返回 {response.status_code}，应为 401"
    )
    assert response.json()["detail"] == CREDENTIALS_DETAIL


@pytest.mark.parametrize("module_name,method,path", PROTECTED_ENDPOINTS)
def test_endpoint_with_invalid_token_returns_401(module_name: str, method: str, path: str) -> None:
    """伪造 / 损坏的 Token 同样必须被拒。"""
    response = _client(module_name).request(
        method,
        path,
        headers={"Authorization": "Bearer not-a-real-token"},
        **_request_kwargs(method, path),
    )

    assert response.status_code == 401
    assert response.json()["detail"] == CREDENTIALS_DETAIL


@pytest.mark.parametrize(
    "module_name,method,path",
    [
        pytest.param(module_name, method, path, id=f"{module_name}-{method}-{path}")
        for module_name, method, path in sorted(ANONYMOUS_ENDPOINTS)
        if module_name in _MODULES
    ],
)
def test_anonymous_endpoint_is_reachable_without_token(
    module_name: str, method: str, path: str
) -> None:
    """反向断言：三个匿名端点**不得**因为鉴权依赖而 401。

    只测「该拒的拒了」会漏掉另一类回归 —— 有人给 `auth_router` 挂上 router 级鉴权，
    于是整条登录链路 401，而所有 401 断言依然全绿。这里把「该放的」也钉住。
    """
    response = _client(module_name).request(method, path, **_request_kwargs(method, path))

    assert response.status_code != 401, (
        f"{method} {path}（{module_name}）是登记在案的匿名端点，却返回了 401 —— "
        "登录链路被鉴权依赖掐断"
    )
