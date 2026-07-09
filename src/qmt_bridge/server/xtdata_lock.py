"""xtdata 请求级串行化模块。

xtquant 的 C 扩展不是线程安全的。FastAPI 的同步路由处理函数在线程池中
并发执行，多个请求同时调用 xtdata.* 会导致内部 BSON 序列化出现数据竞争，
触发 ``Assertion failed: u < 1000000`` 崩溃。

本模块通过 HTTP 中间件 + asyncio.Lock 实现请求级串行化：
- 同一时刻只允许一个 HTTP 请求进入路由处理函数
- 不修改 xtdata 模块本身，避免内部互调死锁风险
- 后台调度器的基础下载任务也通过同一把锁串行化

asyncio.Lock 在事件循环层面工作，持锁期间线程池中的 xtdata 调用正常执行，
释锁后下一个请求才进入处理函数，从而保证 xtdata 不被并发调用。

使用纯 ASGI 中间件（而非 BaseHTTPMiddleware），在服务关闭时排队请求
能立即取消退出，不会产生大量 CancelledError。
"""

import asyncio
import logging

logger = logging.getLogger("qmt_bridge")

# 全局异步锁，确保同一时刻只有一个请求/任务调用 xtdata
xtdata_lock = asyncio.Lock()

# /api/* 中无需串行化的前缀（不调用 xtdata 的端点，如通知接口）
NO_LOCK_PREFIXES: tuple[str, ...] = ("/api/notify",)

# 批量下载类接口耗时可能长达数分钟（downloader.py 内部已按只处理超时），
# 不适用下面的全局调用超时，仍按原有方式独占持锁直至完成。
NO_TIMEOUT_PREFIXES: tuple[str, ...] = ("/api/download",)

# 单次 xtdata 调用最长占用串行锁的时间（秒）。
#
# 已知问题（见 CLAUDE.md「已知问题与排障手册」）：QMT 服务端进程状态损坏时，
# xtdata.get_local_data() / get_market_data_ex() 等接口可能永久挂起且不抛异常。
# Starlette 默认以不可取消（cancellable=False）的方式在线程池中运行同步路由
# 函数，事件循环层面的 asyncio.wait_for/cancel 无法真正打断挂起的调用。
#
# 因此这里不依赖取消语义，而是主动放弃等待并释放锁：一旦某次调用超过此时间
# 仍未返回，就判定 xtdata 已进入不可用状态，向客户端返回超时错误并释放锁，
# 避免后续所有 /api/* 请求排队等待一个永远不会释放的锁（等同于整个服务瘫痪）。
# 代价是：若挂起的调用之后真的恢复运行，理论上可能与新请求短暂并发调用
# xtdata——但这一风险明显小于"一次挂起拖垮整个服务"，且该场景本身已提示
# QMT 客户端需要重启（同一损坏的服务端进程通常后续调用也会挂起/出错）。
XTDATA_CALL_TIMEOUT = 60.0


async def _send_timeout_response(send) -> None:
    """向客户端发送 504 超时响应（原始 ASGI 消息）。"""
    await send(
        {
            "type": "http.response.start",
            "status": 504,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": (
                b'{"detail":"xtdata call timed out on server side, '
                b'xtquant/miniQMT may need a restart"}'
            ),
        }
    )


class XtdataSerializerMiddleware:
    """纯 ASGI 中间件：串行化调用 xtdata 的 HTTP 请求。

    通过 asyncio.Lock 保证同一时刻只有一个请求的同步处理函数在线程池中执行。
    - 仅拦截 HTTP 请求，WebSocket 不受影响。
    - 仅锁 /api/* 路径；/docs、/openapi.json 等静态端点直通。
    - NO_LOCK_PREFIXES 中的路径（如 /api/notify）直通，不参与串行化。
    - NO_TIMEOUT_PREFIXES 中的路径（如 /api/download）参与串行化，
      但不受 XTDATA_CALL_TIMEOUT 限制（下载任务本身耗时可能较长）。
    - 其余路径若单次调用超过 XTDATA_CALL_TIMEOUT 仍未完成，视为 xtdata 挂起，
      主动释放锁并返回超时响应，避免拖垮整个服务。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith("/api/") or path.startswith(NO_LOCK_PREFIXES):
            await self.app(scope, receive, send)
            return

        if path.startswith(NO_TIMEOUT_PREFIXES):
            async with xtdata_lock:
                await self.app(scope, receive, send)
            return

        await self._call_with_timeout(scope, receive, send, path)

    async def _call_with_timeout(self, scope, receive, send, path: str) -> None:
        # 超时后丢弃的调用可能仍在后台线程中运行，之后若真的完成，
        # 不能再往（可能已被复用给下一个请求的）连接上发送 ASGI 消息，
        # 否则会破坏连接协议状态，这里用一个哨兵屏蔽超时之后的延迟消息。
        response_sent = False

        async def _guarded_send(message):
            if response_sent:
                logger.warning(
                    "xtdata 调用超时后收到延迟的 ASGI 消息，已丢弃: %s",
                    message.get("type"),
                )
                return
            await send(message)

        await xtdata_lock.acquire()
        app_task = asyncio.ensure_future(self.app(scope, receive, _guarded_send))
        try:
            done, _ = await asyncio.wait({app_task}, timeout=XTDATA_CALL_TIMEOUT)
        finally:
            xtdata_lock.release()

        if app_task in done:
            app_task.result()  # 正常完成时为 None；若内部抛出异常则在此重新抛出
            return

        response_sent = True
        logger.error(
            "xtdata 调用超时 (>%.0fs)，已释放串行锁避免后续请求被永久阻塞，"
            "请尽快检查/重启 QMT 客户端（参见 CLAUDE.md 已知问题）: %s",
            XTDATA_CALL_TIMEOUT,
            path,
        )
        await _send_timeout_response(send)

        def _log_late_exception(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.error("xtdata 超时调用之后延迟抛出异常 (%s): %s", path, exc)

        app_task.add_done_callback(_log_late_exception)
