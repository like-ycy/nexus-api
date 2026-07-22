"""项目统一异常层次定义。"""


class NexusApiError(Exception):
    """Nexus API 基础异常。"""


class StartupError(NexusApiError):
    """启动异常。"""


class TransportError(NexusApiError):
    """网络传输异常。"""
