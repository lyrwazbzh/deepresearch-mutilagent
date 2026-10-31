"""T72 回归：`POST /chat/completion/v3` 的**附件归属校验**（IDOR：越权把他人附件正文送进模型上下文）。

## 缺陷

`attachment_router` 的归属校验在 T55 已修好（四个入口统一 JOIN `ChatSession` 过滤
`user_id`），但 `chat_router.py` 的 `/completion/v3` 里还有一处**同型**的裸查：
遍历 `request.attachment_ids` 后只按 id 取附件 ——

    att = db.query(ChatAttachment).filter(ChatAttachment.id == att_uuid).first()

**无 join、无 owner 判断**。后果比「多读一行」更重：该附件的 `content_text` 会被拼进
`enhanced_question` **送进模型上下文**，于是任何已登录用户凭一个 UUID 即可让别人的
文档正文进入自己的对话（并把内容回显给自己）。

## 为什么此前没被发现

T55 的源码锁（`test_no_bare_attachment_lookup_remains`）作用域是 `attachment_router.py`
**单文件** —— 与终审 §4 中-1 的裸 `os.remove` 家族复发原因完全相同（那个锁已由 T63
提升为目录级）。本票把附件查询也提升为**目录级**，并把 T55 的 fake DB 提为共享模块。

## 三层验证，均**不依赖基础设施**（Postgres / Redis / Milvus 一概不碰）

1. **行为层** —— 共享替身 `FakeDB`（其 `_matches` **真的执行** WHERE 约束）+ 注入
   stub 服务调用端点：非本人附件的正文**没有**进入模型上下文，而本人的**有**
   （正向对照，防「一刀切过滤」也能变绿）；
2. **结构层（目录级）** —— `app/router/*.py` 中任何**已注册端点**只要按
   `ChatAttachment.id` 查附件，就必须带上归属 join 与 `current_user` 过滤；
3. **依赖层** —— 该端点必须显式拿到 `current_user`（拿不到就谈不上校验归属）。
"""

import importlib
import inspect
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import get_db
from router import chat_router as cr
from router.auth_router import get_current_user_required
from _attachment_ownership_fakes import FakeDB, Row, make_attachment, make_session

OWNER_ID = uuid4()
INTRUDER_ID = uuid4()
OWNER_SESSION_ID = uuid4()
INTRUDER_SESSION_ID = uuid4()
OWNER_ATTACHMENT_ID = uuid4()

# 一个足够独特的哨兵串：只要它出现在模型上下文里，就说明该附件的正文被读出来了
OWNER_SECRET = "OWNER-PRIVATE-NOTE-7f3a1c"


# --------------------------------------------------------------------------- stub 服务


class _StubChatService:
    """最小 `ChatService` 替身：只记录**最终交给模型的问题**。

    「附件有没有被读出来」这个事实，唯一可观测的下游就是 `get_chat_completion` 收到的
    `question`（端点会把附件正文拼进 `enhanced_question`）。
    """

    def __init__(self) -> None:
        self.questions: List[str] = []

    def retrieve_from_web(self, question: str) -> List[Dict[str, Any]]:
        raise AssertionError("本用例不开网络检索，不应走到这里")

    def rerank_documents(self, question: str, documents: List[Dict[str, Any]]):
        return documents

    def get_chat_completion(self, session_id, question, retrieved_content) -> Iterator[str]:
        self.questions.append(question)
        yield "data: stub-chunk\n\n"


class _StubSessionService:
    def get_session(self, session_id):
        return {"session_id": session_id}

    def create_session(self):
        return {"session_id": str(uuid4())}


def _make_db() -> FakeDB:
    """OWNER 的会话 + 一条带正文的附件；另有 INTRUDER 的会话（制造真实越权场景）。"""
    sessions = [
        make_session(id=OWNER_SESSION_ID, user_id=OWNER_ID, title="owner session"),
        make_session(
            id=INTRUDER_SESSION_ID, user_id=INTRUDER_ID, title="intruder session"
        ),
    ]
    attachments = [
        make_attachment(
            id=OWNER_ATTACHMENT_ID,
            session_id=OWNER_SESSION_ID,
            filename="owner-secret.txt",
            content_text=OWNER_SECRET,
        ),
    ]
    return FakeDB(sessions=sessions, attachments=attachments)


def _client(db, user_id, chat_service) -> TestClient:
    app = FastAPI()
    app.include_router(cr.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_required] = lambda: Row(id=user_id)
    app.dependency_overrides[cr.get_services] = lambda: {
        "chat_service": chat_service,
        "session_service": _StubSessionService(),
    }
    return TestClient(app)


def _ask_whoever(client, attachment_id):
    """发一次 v3 请求：关掉知识库与网络检索，把变量收敛到「附件」这一项。"""
    return client.post(
        "/chat/completion/v3",
        json={
            "question": "帮我总结这份材料",
            "attachment_ids": [str(attachment_id)],
            "search_knowledge": False,
            "search_web": False,
        },
    )


# --------------------------------------------------------------------------- 1) 行为层


def test_owner_attachment_reaches_the_model_context():
    """正向对照：**本人**的附件正文必须进上下文。

    没有这一条，「把附件全部过滤掉」这种过度修复也会让下面的越权断言变绿 ——
    那等于把功能改坏来通过测试。
    """
    chat = _StubChatService()
    response = _ask_whoever(_client(_make_db(), OWNER_ID, chat), OWNER_ATTACHMENT_ID)

    assert response.status_code == 200, response.text
    assert chat.questions, "端点没有把问题交给 chat_service，用例失去观测点"
    assert OWNER_SECRET in chat.questions[0], (
        "本人附件的正文没有进模型上下文 —— 附件功能被改坏（或归属过滤过严）"
    )


def test_intruder_cannot_inject_someone_elses_attachment_into_context():
    """核心断言：非本人凭 UUID 不得把他人附件正文送进自己的模型上下文。"""
    chat = _StubChatService()
    response = _ask_whoever(_client(_make_db(), INTRUDER_ID, chat), OWNER_ATTACHMENT_ID)

    # 静默跳过（不区分「不存在」与「无权限」），但请求本身仍然是成功的对话请求
    assert response.status_code == 200, response.text
    assert chat.questions, "端点没有把问题交给 chat_service，用例失去观测点"
    assert OWNER_SECRET not in chat.questions[0], (
        "非本人附件的正文进了模型上下文 —— `/completion/v3` 缺归属校验"
        "（T72 / §4 补审；与 T55 修好的 attachment_router 同型）"
    )


def test_unknown_attachment_id_is_silently_skipped():
    """不存在的 id：静默跳过、不报错（保持既有行为，且不泄漏「该 UUID 是否存在」）。"""
    chat = _StubChatService()
    response = _ask_whoever(_client(_make_db(), OWNER_ID, chat), uuid4())

    assert response.status_code == 200, response.text
    assert OWNER_SECRET not in chat.questions[0]


# --------------------------------------------------------------------------- 2) 结构层（目录级）

# ⚠️ 目录必须从**具体模块**推导：`app/router/` 没有 `__init__.py`，`router` 是命名空间包，
#    `importlib.import_module("router").__file__` 为 None（T64 踩过）。
ROUTER_DIR = Path(cr.__file__).parent
ALL_ROUTER_MODULES = sorted(
    path.stem for path in ROUTER_DIR.glob("*.py") if path.stem != "__init__"
)

_LOOKUP_RE = re.compile(r"query\(\s*ChatAttachment\s*\)")
_BY_ID_RE = re.compile(r"ChatAttachment\.id\s*==")
_OWNERSHIP_RE = re.compile(r"ChatSession\.user_id\s*==\s*current_user\.id")
_JOIN_RE = re.compile(r"\.join\(\s*ChatSession")


def _endpoint_sources():
    """枚举 `app/router/*.py` 里**已注册端点**的 (模块名, 路由, 源码)。

    ⚠️ 只扫**端点**（`route.endpoint`），不扫模块全文：`attachment_router.process_attachment`
    是**后台任务**、运行在请求之外（授权发生在上传时、且没有 `current_user` 可用），
    它按 id 裸查是**正确**的 —— 扫全文会误报，从而逼出一个「白名单豁免」，
    而豁免名单正是这类锁腐烂的开始。
    """
    for stem in ALL_ROUTER_MODULES:
        try:
            module = importlib.import_module(f"router.{stem}")
        except Exception:  # noqa: BLE001 - 不可导入的模块由 T64 的鉴权锁负责报警
            continue
        router = getattr(module, "router", None)
        if router is None:
            continue
        seen = set()
        for route in router.routes:
            func = getattr(route, "endpoint", None)
            if func is None or func in seen:
                continue
            seen.add(func)
            yield stem, route, inspect.getsource(func)


def test_no_endpoint_looks_up_attachments_by_id_without_ownership_filter():
    """🔒 **目录级**锁：端点按 `ChatAttachment.id` 查附件时，必须带上归属过滤。

    作用域为什么必须是目录级：T55 的同类锁只罩 `attachment_router.py` **单文件**，
    而 `/completion/v3` 长在 `chat_router.py` 里 —— 与 T63 修的 `os.remove` 家族
    是同一个结构性原因（那个锁已提升为目录级，本锁对齐）。

    判据刻意限定为「**按 id** 查」：`get_session_attachments` 按 `session_id` 查、
    且会话归属已在同一端点内先行校验，不属本锁范围（否则会误报，进而被豁免掉）。
    """
    offenders: List[str] = []
    scanned = 0

    for stem, route, src in _endpoint_sources():
        if not _LOOKUP_RE.search(src) or not _BY_ID_RE.search(src):
            continue
        scanned += 1
        missing = []
        if not _JOIN_RE.search(src):
            missing.append(".join(ChatSession, …)")
        if not _OWNERSHIP_RE.search(src):
            missing.append("ChatSession.user_id == current_user.id")
        if missing:
            methods = sorted(route.methods - {"HEAD", "OPTIONS"})
            offenders.append(f"{stem}: {methods} {route.path} 缺 {' / '.join(missing)}")

    assert not offenders, (
        f"以下端点按 id 查附件、却没有归属过滤：{offenders}。"
        "非本人凭 UUID 即可读到他人附件正文（T55 / T72 同型缺陷），"
        "请改成 `join(ChatSession, ChatAttachment.session_id == ChatSession.id)` "
        "并过滤 `ChatSession.user_id == current_user.id`"
    )

    # 反恒真：本锁必须真的扫到东西，否则「0 个 offender」毫无意义
    # （端点被改名 / 判据被改坏时，这里先红，而不是让锁静默退化成摆设）。
    assert scanned >= 3, (
        f"本锁只扫到 {scanned} 个「按 id 查附件」的端点（预期 ≥3）—— "
        "判据或端点结构可能已变，请复核是否仍有判别力"
    )


# --------------------------------------------------------------------------- 3) 依赖层


def test_v3_endpoint_declares_current_user():
    """该端点必须显式拿到 `current_user` —— 拿不到，就谈不上校验归属。

    只挂 router 级 `dependencies=[…]` 是不够的：依赖的**返回值**不会进入路径操作函数，
    函数体里根本拿不到用户身份（这正是 T55 之前 `attachment_router` 的半修复形态）。
    """
    body = inspect.getsource(cr.chat_completion_with_attachments)

    assert "current_user: User = Depends(get_current_user_required)" in body, (
        "`/completion/v3` 未声明 current_user 依赖，函数体拿不到当前用户"
    )
