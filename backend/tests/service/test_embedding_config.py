"""embedding 供应商可配置化（T49 解除阻塞）的单元测试。

背景：`generate_embedding` 此前把供应商（api_key / base_url / model）写死为 DashScope，
且**无条件**向供应商传 `dimensions`。实测（2026-09-14）硅基流动 BAAI/bge-m3：
- 固定 1024 维（与 `milvus_service.vector_dim=1024` 一致）；
- **不接受 dimensions 参数**（HTTP 400，code=20015）。

本文件锁死三件事，全部用桩替身，**不发真实网络请求**：
1. 供应商三元组由 `EMBEDDING_*` 环境变量驱动，缺省回退 `DASHSCOPE_*`；
2. 未设置 `EMBEDDING_DIMENSIONS` 时**不传** dimensions（bge-m3 必需）；
   设置了则透传为 int（DashScope v4 需要它）；
3. 两个密钥都缺失时返回 `None`（显式缺失，而非带空鉴权发请求）。
"""

import types

import pytest

import service.embedding_service as es


class _FakeEmbeddings:
    def __init__(self, sink):
        self._sink = sink

    def create(self, **kwargs):
        self._sink.append(kwargs)
        # 真实 API 语义：返回条数 == 输入条数；用下标区分以便断言批量结果的对位
        inp = kwargs["input"]
        n = len(inp) if isinstance(inp, list) else 1
        data = [types.SimpleNamespace(embedding=[float(i)] * 3) for i in range(n)]
        return types.SimpleNamespace(data=data)


class _FakeClient:
    """记录构造参数与每次 create 调用的 kwargs。"""

    created = []
    calls = []

    def __init__(self, **kwargs):
        _FakeClient.created.append(kwargs)
        self.embeddings = _FakeEmbeddings(_FakeClient.calls)


ENV_KEYS = (
    "EMBEDDING_API_KEY", "EMBEDDING_BASE_URL", "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSIONS", "DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """隔离真实环境（含 load_dotenv 读到的 backend/.env），逐条断言才有判别力。"""
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    _FakeClient.created.clear()
    _FakeClient.calls.clear()
    yield
    _FakeClient.created.clear()
    _FakeClient.calls.clear()


@pytest.fixture(autouse=True)
def _fake_openai(monkeypatch):
    monkeypatch.setattr(es, "OpenAI", _FakeClient)


def test_env_driven_provider_and_no_dimensions(monkeypatch):
    """硅基流动场景：三元组来自 EMBEDDING_*，且不传 dimensions。"""
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-test-sf")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://api.siliconflow.cn/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-m3")

    out = es.generate_embedding("华电科工 电力工程")

    assert out == [0.0, 0.0, 0.0]
    assert _FakeClient.created == [{
        "api_key": "sk-test-sf",
        "base_url": "https://api.siliconflow.cn/v1",
    }]
    (kwargs,) = _FakeClient.calls
    assert kwargs["model"] == "BAAI/bge-m3"
    assert "dimensions" not in kwargs  # bge-m3 对该参数直接 400
    assert kwargs["encoding_format"] == "float"
    assert kwargs["input"] == "华电科工 电力工程"


def test_dimensions_passed_only_when_configured(monkeypatch):
    """设置 EMBEDDING_DIMENSIONS → 透传为 int（DashScope v4 需要它）。"""
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-x")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "1024")

    es.generate_embedding("文本")

    (kwargs,) = _FakeClient.calls
    assert kwargs["dimensions"] == 1024
    assert isinstance(kwargs["dimensions"], int)


def test_explicit_args_win_over_env(monkeypatch):
    """显式实参优先于环境变量（函数既有语义，不得被本次改动破坏）。"""
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-env")

    es.generate_embedding("文本", api_key="sk-arg", model_name="text-embedding-v4")

    # base_url 未显式给出时仍会被解析为 DashScope 缺省值（既有行为）
    assert _FakeClient.created == [{
        "api_key": "sk-arg",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    }]
    (kwargs,) = _FakeClient.calls
    assert kwargs["model"] == "text-embedding-v4"


def test_fallback_to_dashscope_and_default_model(monkeypatch):
    """未配置 EMBEDDING_* 时回退 DASHSCOPE_*，模型缺省 text-embedding-v4（旧行为不变）。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dash")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

    es.generate_embedding("文本")

    assert _FakeClient.created == [{
        "api_key": "sk-dash",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    }]
    (kwargs,) = _FakeClient.calls
    assert kwargs["model"] == "text-embedding-v4"
    # 旧行为里 dimensions 无条件传；现在**缺省不传** —— DashScope v4 的 1024 维
    # 需显式设 EMBEDDING_DIMENSIONS=1024，故此处断言「不传」而非「传 1024」。
    assert "dimensions" not in kwargs


def test_batch_input_also_omits_dimensions(monkeypatch):
    """列表输入走批量分支，同样不传 dimensions（docmind 入库主路径）。"""
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-x")
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-m3")

    out = es.generate_embedding(["a", "b", "c"])

    assert out == [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0], [2.0, 2.0, 2.0]]
    (kwargs,) = _FakeClient.calls
    assert kwargs["input"] == ["a", "b", "c"]
    assert "dimensions" not in kwargs


def test_missing_both_keys_returns_none(monkeypatch):
    """两套密钥都缺失 → 返回 None，且**不构造客户端**（不发请求）。"""
    out = es.generate_embedding("文本")

    assert out is None
    assert _FakeClient.created == []
    assert _FakeClient.calls == []
