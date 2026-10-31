"""Service exports loaded lazily so independent subpackages stay testable."""

from __future__ import annotations

from importlib import import_module


_EXPORTS = {
    "DocumentService": ("document_service", "DocumentService"),
    "ServiceConfig": ("config", "ServiceConfig"),
    "WebSearchService": ("web_search_service", "WebSearchService"),
    "ChatService": ("chat_service", "ChatService"),
    "SessionService": ("session_service", "SessionService"),
    "PolicySearchService": ("policy_search_service", "PolicySearchService"),
    "ResearchService": ("dr_g", "ResearchService"),
    "ReActController": ("react_controller", "ReActController"),
    "create_default_tools": ("react_controller", "create_default_tools"),
    "ToolExecutor": ("tool_executor", "ToolExecutor"),
    "create_tool_executor": ("tool_executor", "create_tool_executor"),
    "Text2SQLService": ("text2sql_service", "Text2SQLService"),
    "create_text2sql_service": ("text2sql_service", "create_text2sql_service"),
    "SmartDataAnalyzer": ("smart_analyzer", "SmartDataAnalyzer"),
    "create_smart_analyzer": ("smart_analyzer", "create_smart_analyzer"),
    "ChartGenerator": ("chart_generator", "ChartGenerator"),
    "create_chart_generator": ("chart_generator", "create_chart_generator"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute)
    globals()[name] = value
    return value
