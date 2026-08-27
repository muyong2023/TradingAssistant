"""问答层测试：工具标签与进度描述。不调用真实 API。"""
from types import SimpleNamespace

from ta.chat import TOOL_LABELS, describe_tools


def message(*blocks):
    return SimpleNamespace(content=[SimpleNamespace(**b) for b in blocks])


def test_describes_local_tools():
    msg = message({"type": "tool_use", "name": "get_quote"},
                  {"type": "tool_use", "name": "get_indicators"})
    assert describe_tools(msg) == ["查询报价", "计算技术指标"]


def test_describes_server_tools():
    msg = message({"type": "server_tool_use", "name": "web_search"})
    assert describe_tools(msg) == ["搜索网页"]


def test_code_execution_gets_human_label():
    """联网搜索的动态过滤会以 code_execution 冒出来，
    不能把这个内部标识符直接摆到用户面前。"""
    msg = message({"type": "server_tool_use", "name": "code_execution"})
    assert describe_tools(msg) == ["整理搜索结果"]


def test_unknown_tool_falls_back_to_generic_label():
    msg = message({"type": "server_tool_use", "name": "some_future_tool"})
    assert describe_tools(msg) == ["处理数据"]


def test_deduplicates_repeated_tools():
    msg = message({"type": "tool_use", "name": "get_quote"},
                  {"type": "tool_use", "name": "get_quote"})
    assert describe_tools(msg) == ["查询报价"]


def test_ignores_text_and_thinking_blocks():
    msg = message({"type": "thinking", "thinking": "..."},
                  {"type": "text", "text": "回答"})
    assert describe_tools(msg) == []


def test_every_declared_tool_has_a_label():
    """新增工具时容易忘了配标签，这条测试会提醒。"""
    from ta.chat import TOOLS
    for tool in TOOLS:
        name = tool["name"] if isinstance(tool, dict) else getattr(tool, "name", None)
        if name:
            assert name in TOOL_LABELS, f"工具 {name} 缺少中文标签"
