"""共享测试替身：**附件归属校验**回归用例的最小 fake DB（T55 起，§4 补审 / T72 提升为共享）。

## 为什么会有这个模块

`_FakeDB` / `_Query` 最初写在 `test_attachment_ownership.py`（T55）里，作用域**只有那一个
文件**。于是 §4 补审 / T72 在 `chat_router.py` 的 `/completion/v3` 上发现**同型**缺陷
（只按 `id` 查附件、无 join、无归属过滤）时，这份带判别力的替身无法直接复用 ——
和「T55 的源码锁只罩单文件」是同一个结构性原因。

按本仓先例（T56 的 `remove_quietly`、T69 的 `build_fact_entry`：「第二处出现即提升为
公共归属地」）提升到此处，供所有需要断言「按**会话归属**过滤」的 router 用例复用。

## 判别力（本模块存在的唯一理由）

`Query._matches` **真的执行** WHERE 约束，并且**只认 `chat_sessions.user_id` 这一列**
才能解析归属 —— 于是「去掉归属过滤」会真的让非本人也查到行（= 修复前的漏洞行为），
使调用方的断言失败。这不是「断言某个过滤条件字符串存在」那种恒真写法。

## 不依赖基础设施

Postgres / Redis / Milvus 一律不碰：`FakeDB` 顶替 `get_db`，用户身份用
`dependency_overrides` 注入（不签发真 JWT）。
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy.sql.elements import BinaryExpression, BindParameter
from sqlalchemy.sql.schema import Column

# 注册 `User` 的关系目标（`User.knowledge_bases -> KnowledgeBase` 等）。conftest 为提速把
# `models` 注册成**命名空间占位包**（不执行 `models/__init__.py`），所以必须显式导入子模块；
# 否则构造 `ChatAttachment(...)` 触发 mapper 配置时会因解析不到 `KnowledgeBase`
# 抛 `InvalidRequestError`。
from models import chat as _chat_models  # noqa: F401
from models import knowledge as _knowledge_models  # noqa: F401
from models.chat import ChatAttachment, ChatSession

# 固定的 4 个 id 由调用方自取（`uuid4()`）；这里只提供「构造行」与「构造库」的工厂，
# 具体 id 与数据形状留给各用例，避免把某个文件的假设固化进共享模块。


class Row:
    """最小行对象：只提供属性访问（不碰 SQLAlchemy 元类）。"""

    def __init__(self, **fields):
        self.__dict__.update(fields)


def make_session(*, id=None, user_id, title="session"):
    """构造一条 `chat_sessions` 行。`user_id` 是归属解析的唯一依据（NOT NULL）。"""
    return Row(id=id or uuid4(), user_id=user_id, title=title)


def make_attachment(
    *,
    id=None,
    session_id,
    content_text=None,
    status="completed",
    filename="note.txt",
    file_size=12,
    **overrides,
):
    """构造一条 `chat_attachments` 行。`session_id` 必填（NOT NULL），归属经会话推得。"""
    fields = dict(
        id=id or uuid4(),
        session_id=session_id,
        message_id=None,
        user_id=None,  # nullable —— 归属**不**看这一列
        file_type="txt",
        filename=filename,
        file_size=file_size,
        file_path=None,
        content_text=content_text,
        status=status,
        error_message=None,
        created_at=datetime(2026, 9, 16, 12, 0, 0),
    )
    fields.update(overrides)
    return Row(**fields)


class Query:
    """极简查询对象：支持 `.join/.filter/.order_by/.first/.all`。

    `_matches` 是判别力的来源 —— 它会**真的解析** `Column == 值` 形态的条件：

    - `chat_sessions.user_id == <uid>` 会拿行所属会话的 user_id 去比；
    - 条件里**没有**该列时，行永远通过（复现修复前「裸查」的行为）。
    """

    def __init__(self, db, model):
        self._db = db
        self._model = model
        self._criteria = []
        self._joined_session = False

    def join(self, *_args, **_kwargs):
        self._joined_session = True
        return self

    def filter(self, *criteria):
        self._criteria.extend(criteria)
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    # --- 求值 ---

    def _owner_of(self, row):
        for session in self._db.sessions:
            if getattr(session, "id", None) == getattr(row, "session_id", None):
                return session.user_id
        return None

    def _matches(self, row):
        for criterion in self._criteria:
            if not isinstance(criterion, BinaryExpression):
                continue
            left, right = criterion.left, criterion.right
            if not isinstance(left, Column):
                continue
            value = (
                right.value
                if isinstance(right, BindParameter)
                else getattr(right, "value", right)
            )
            table = left.table.name
            if table == "chat_sessions" and left.key == "user_id":
                # 归属约束：查会话时直接比 `row.user_id`；
                # 查附件（join 过 sessions）时比「行所属会话的 user_id」。
                actual = (
                    getattr(row, "user_id", None)
                    if self._model is ChatSession
                    else self._owner_of(row)
                )
                if actual != value:
                    return False
            elif getattr(row, left.key, None) != value:
                return False
        return True

    def _rows(self):
        source = (
            self._db.sessions if self._model is ChatSession else self._db.attachments
        )
        return [row for row in source if self._matches(row)]

    def first(self):
        rows = self._rows()
        return rows[0] if rows else None

    def all(self):
        return list(self._rows())


class FakeDB:
    """顶替 `get_db`。只实现被测端点真正用到的那几个方法。"""

    def __init__(self, sessions=(), attachments=()):
        self.sessions = list(sessions)
        self.attachments = list(attachments)
        self.deleted = []
        self.added = []
        self.commits = 0

    def query(self, model):
        return Query(self, model)

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime(2026, 9, 16, 12, 0, 0)
        self.added.append(obj)
        if isinstance(obj, ChatAttachment):
            self.attachments.append(obj)

    def delete(self, obj):
        self.deleted.append(obj)

    def commit(self):
        self.commits += 1

    def refresh(self, _obj):
        return None

    def close(self):
        return None
