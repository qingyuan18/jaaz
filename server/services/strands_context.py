"""
Strands上下文管理器
用于在工具调用中传递session_id, canvas_id等上下文信息
同时支持存储会话级的意图识别结果（例如 kontext_model）供工具兜底读取
"""
import contextvars
from typing import Dict, Any, Optional

# 创建上下文变量
_session_context: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    'session_context',
    default={}
)


def set_session_context(
    session_id: str,
    canvas_id: str,
    model_info: Optional[Dict[str, Any]] = None,
    tool_call_id: Optional[str] = None,
    user_id: Optional[str] = None
):
    """设置会话上下文"""
    # Try to get user_id from user context if not provided
    if not user_id:
        try:
            from services.user_context import get_current_user_id
            user_id = get_current_user_id()
        except:
            user_id = None

    context = {
        'session_id': session_id,
        'canvas_id': canvas_id,
        'user_id': user_id,
        'model_info': model_info or {},
        'tool_call_id': tool_call_id,
        # 意图识别结果占位（后续可通过 set_intention_result 写入）
        'intention': {}
    }
    _session_context.set(context)


def get_session_context() -> Dict[str, Any]:
    """获取当前会话上下文"""
    return _session_context.get({})


def _update_context(update: Dict[str, Any]):
    ctx = dict(get_session_context())
    ctx.update(update)
    _session_context.set(ctx)


def set_intention_result(result: Dict[str, Any]):
    """设置本次会话的意图识别结果，例如 {"mode": "single", "kontext_model": "flux-kontext"}"""
    if not isinstance(result, dict):
        return
    ctx = dict(get_session_context())
    ctx['intention'] = result
    _session_context.set(ctx)


def get_intention_result(default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """获取当前会话的意图识别结果"""
    return dict(get_session_context().get('intention') or (default or {}))


def get_session_id() -> str:
    """获取当前session_id"""
    return get_session_context().get('session_id', '')


def get_canvas_id() -> str:
    """获取当前canvas_id"""
    return get_session_context().get('canvas_id', '')


def get_user_id() -> str:
    """获取当前user_id"""
    return get_session_context().get('user_id', '')


def get_model_info() -> Dict[str, Any]:
    """获取当前模型信息"""
    return get_session_context().get('model_info', {})


def get_tool_call_id() -> str:
    """获取当前tool_call_id"""
    return get_session_context().get('tool_call_id', '')


def get_image_model() -> Dict[str, Any]:
    """获取图像模型信息"""
    model_info = get_model_info()
    return model_info.get('image', {})


class SessionContextManager:
    """会话上下文管理器"""

    def __init__(self, session_id: str, canvas_id: str, model_info: Optional[Dict[str, Any]] = None, user_id: Optional[str] = None):
        self.session_id = session_id
        self.canvas_id = canvas_id
        self.model_info = model_info or {}
        self.user_id = user_id
        self.previous_context = None

    def __enter__(self):
        self.previous_context = get_session_context()
        set_session_context(self.session_id, self.canvas_id, self.model_info, user_id=self.user_id)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.previous_context is not None:
            _session_context.set(self.previous_context)
        else:
            _session_context.set({})
