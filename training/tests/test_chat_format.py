from src.train.chat_format import format_trajectory_messages, load_openai_tools


def test_preserves_standard_function_call_shape():
    messages = format_trajectory_messages([
        {"role": "system", "content": "route"},
        {"role": "user", "content": "查市场"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "web_search", "arguments": "{\"query\":\"市场\"}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
    ])
    assert messages[2]["tool_calls"][0]["function"]["name"] == "web_search"
    assert messages[3]["tool_call_id"] == "call_1"


def test_canonical_schema_converts_to_openai_tools():
    tools = load_openai_tools("config/deepresearch_tools.v1.json")
    assert {item["function"]["name"] for item in tools} >= {"web_search", "local_rag"}
