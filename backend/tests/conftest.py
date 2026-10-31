import importlib
import os
import sys
import types
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("ENV", "test")

# 自 T02 起 core.security 在**导入期**校验 JWT_SECRET_KEY，而 core/__init__.py 会导入 security，
# 于是任何 `import core.<anything>`（含与鉴权无关的模块）都要求该变量存在。
# 这里给测试环境一个固定占位值；T02 自身的用例会用 monkeypatch 先清掉它，再断言「缺失即失败」。
os.environ.setdefault(
    "JWT_SECRET_KEY", "test-only-jwt-secret-key-do-not-use-in-production-0123456789"
)

# 自 T07 起 core.database 在**导入期**要求 POSTGRES_PASSWORD 存在（移除 weak 默认值）。
# 大量模块（含与数据库无关的 router）都会 `from core.database import get_db`，
# 于是无基础设施的单元测试也需要一个测试占位口令。注意这只影响导入期校验 ——
# engine 是惰性建连的，不会真的去连库。
os.environ.setdefault("POSTGRES_PASSWORD", "test-only-postgres-password")


def _register_namespace_package(name: str, path: Path) -> types.ModuleType:
    package = types.ModuleType(name)
    package.__path__ = [str(path)]
    sys.modules.setdefault(name, package)
    return sys.modules[name]


_register_namespace_package("service", APP_DIR / "service")
_register_namespace_package("core", APP_DIR / "core")
_register_namespace_package("models", APP_DIR / "models")
_register_namespace_package("router", APP_DIR / "router")
_register_namespace_package(
    "service.deep_research_v2",
    APP_DIR / "service" / "deep_research_v2",
)
_register_namespace_package(
    "service.deep_research_v2.agents",
    APP_DIR / "service" / "deep_research_v2" / "agents",
)


# --- 让占位包能顶替真实的 `service` 包 ------------------------------------------
# 上面的占位包**故意不执行** `service/__init__.py`（它会连带拉起 langchain /
# dashscope 等重型依赖，实测单次导入 >35s）。但这带来一个副作用：
# `router/document_router.py` 等模块用 `from service import DocumentService, ServiceConfig`
# 取**顶层名字**，而占位包里没有这些名字，于是 router 测试在收集阶段直接 ImportError。
#
# 修法：把 router 实际用到的顶层名字，从对应的**轻量子模块**取出来挂到占位包上。
# 代价接近 0（config / web_search_service / session_service / dr_g 约 0.01s，
# document_service 约 1s、chat_service 约 1.7s），且不再要求测试拉起整条重型链。
# 若后续 router 用到更多顶层名字，在此按同一方式追加即可。
_service = sys.modules["service"]

# 顶层名字 → 定义它的轻量子模块（对应 `service/__init__.py` 里的 `from .X import Y`）。
_SERVICE_PUBLIC_NAMES = {
    "config": ("ServiceConfig",),
    "document_service": ("DocumentService",),
    "web_search_service": ("WebSearchService",),
    "chat_service": ("ChatService",),
    "session_service": ("SessionService",),
    # T64：`router/research_router.py` 顶部 `from service import ResearchService, ServiceConfig`。
    # `ResearchService` 定义在 V1 ReAct 路线模块 `service/dr_g.py`（§8 明令保留、不得删除）。
    # 取的是**真实类**而非替身：dr_g 只依赖 stdlib + openai + requests + core.serialization，
    # 脱离 `service/__init__.py` 单独导入实测 0.01s，比重型链便宜得多。
    "dr_g": ("ResearchService",),
}

for _submodule, _exported_names in _SERVICE_PUBLIC_NAMES.items():
    _module = importlib.import_module(f"service.{_submodule}")
    for _exported_name in _exported_names:
        setattr(_service, _exported_name, getattr(_module, _exported_name))


# --- 重型协作者替身 -------------------------------------------------------------
# `router/document_router.py` 在**模块级**导入 `service.docmind_service`，而该模块实测
# 导入耗时约 48s（拉起 docmind / llama-index 重依赖）。鉴权与路由装配类测试不会真正执行
# 文档解析，因此这里注入轻量替身，把验收压到秒级。
# 替身故意抛 NotImplementedError（而不是伪造成功），以便任何真实的解析调用立刻暴露，
# 而不是被静默吞掉当成「测试通过」。
# ⚠️ 将来若有测试需要真实的 docmind 行为，请在该测试内先 `del sys.modules[...]` 再导入。
if "service.docmind_service" not in sys.modules:
    _docmind_stub = types.ModuleType("service.docmind_service")

    def process_document_with_docmind(*args, **kwargs):
        raise NotImplementedError(
            "tests/conftest.py 注入了 service.docmind_service 替身；"
            "本测试不应真正执行文档解析。如需真实行为请先移除该替身。"
        )

    _docmind_stub.process_document_with_docmind = process_document_with_docmind
    sys.modules["service.docmind_service"] = _docmind_stub
    _service.docmind_service = _docmind_stub


# --- T64：`service.deep_research_v2.service` 替身 ---------------------------------
# `router/research_router.py` 在**模块级**导入 `service.deep_research_v2.service`。该模块本身
# 没问题，但它 `from .agents import ChiefArchitect` 取**包级属性**，而上面的占位包
# （`service.deep_research_v2.agents`）**故意不执行 __init__**，因此这个真实模块在测试环境里
# **本来就无法导入**（实测报 `cannot import name 'ChiefArchitect' from
# 'service.deep_research_v2.agents'`）。
#
# 于是 T64 之前，鉴权的目录级全量回归锁根本覆盖不到 `research_router`（它是 12 个路由模块里
# 唯一一个 import 不进来的，10 个端点因此无回归）。这里按 docmind 的同一取舍补一个替身：
# 只提供 `research_router` 需要的类名，且**任何真实调用都抛 NotImplementedError**，
# 以便「测试其实没走真服务」这件事在第一次真实调用时立刻暴露，而不是静默变绿。
# ⚠️ 需要**真实** service 的测试必须自己回到真实模块 —— 按 conftest 既有做法先
# `sys.modules.pop(...)` 再导入。`tests/service/deep_research_v2/test_service_observability.py`
# 就是这样做的（它要 monkeypatch 真实模块的 get_config，拿到替身会直接 AttributeError）。
if "service.deep_research_v2.service" not in sys.modules:
    _v2_service_stub = types.ModuleType("service.deep_research_v2.service")

    class DeepResearchV2Service:
        def __init__(self, *args, **kwargs):
            raise NotImplementedError(
                "tests/conftest.py 注入了 service.deep_research_v2.service 替身；"
                "本测试不应真正执行 V2 深度研究流程。如需真实行为请先移除该替身。"
            )

    _v2_service_stub.DeepResearchV2Service = DeepResearchV2Service
    sys.modules["service.deep_research_v2.service"] = _v2_service_stub
    sys.modules["service.deep_research_v2"].service = _v2_service_stub
