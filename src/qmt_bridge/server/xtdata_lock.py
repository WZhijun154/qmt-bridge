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

由于所有 /api/* 请求本质上是排队串行执行的，本模块同时维护"当前正在
执行"和"排队中"的请求列表，供日志和 /api/meta/queue_status 端点使用，
方便判断服务是否卡住、卡在哪个请求上。
"""

import asyncio
import logging
import time

logger = logging.getLogger("qmt_bridge")

# 全局异步锁，确保同一时刻只有一个请求/任务调用 xtdata
xtdata_lock = asyncio.Lock()

# /api/* 中无需串行化的前缀（不调用 xtdata 的端点，如通知接口）。
# queue_status 本身也不参与串行化，否则在服务被占满时反而查不了状态。
NO_LOCK_PREFIXES: tuple[str, ...] = ("/api/notify", "/api/meta/queue_status")

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

# 长时间占用串行锁时，每隔多久打一次"仍在执行"心跳日志（秒）。
HEARTBEAT_INTERVAL = 20.0

# ── 排队 / 执行状态跟踪（供日志和 /api/meta/queue_status 使用） ──────
#
# 事件循环单线程执行，以下模块级状态只会被这一个协程栈访问，无需额外加锁。
_current: dict | None = None
_queue: list[dict] = []


def get_queue_status() -> dict:
    """返回当前串行锁的执行状态与排队情况，供 /api/meta/queue_status 使用。"""
    now = time.monotonic()
    current = None
    if _current is not None:
        current = {
            "method": _current["method"],
            "path": _current["path"],
            "elapsed_seconds": round(now - _current["start"], 1),
        }
    queue = [
        {
            "method": q["method"],
            "path": q["path"],
            "waited_seconds": round(now - q["enqueued"], 1),
        }
        for q in _queue
    ]
    return {"current": current, "queue": queue, "queue_length": len(queue)}


class _RequestTracker:
    """跟踪单个请求在串行锁前的排队状态和获得锁后的执行状态。

    统一给 XtdataSerializerMiddleware 的两条分支（下载类直通 / 带超时）复用，
    使得"现在在跑什么、排了多少个"在任意时刻都能通过日志或
    /api/meta/queue_status 端点看到。
    """

    def __init__(self, method: str, path: str) -> None:
        self.method = method
        self.path = path
        self._entry = {"method": method, "path": path, "enqueued": time.monotonic()}
        self._heartbeat_task: asyncio.Task | None = None

    async def wait_and_acquire(self) -> None:
        global _current
        _queue.append(self._entry)
        if len(_queue) > 1 or _current is not None:
            current_desc = (
                f"{_current['method']} {_current['path']}" if _current is not None else "无"
            )
            logger.info(
                "请求排队: %s %s（排队 %d 个，当前执行: %s）",
                self.method, self.path, len(_queue), current_desc,
            )

        await xtdata_lock.acquire()

        _queue.remove(self._entry)
        start = time.monotonic()
        _current = {"method": self.method, "path": self.path, "start": start}
        logger.info(
            "开始执行: %s %s（等待 %.1fs）",
            self.method, self.path, start - self._entry["enqueued"],
        )
        self._heartbeat_task = asyncio.create_task(self._heartbeat(start))

    async def _heartbeat(self, start: float) -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                logger.info(
                    "仍在执行: %s %s（已耗时 %.0fs，排队中 %d 个）",
                    self.method, self.path, time.monotonic() - start, len(_queue),
                )
        except asyncio.CancelledError:
            pass

    def release(self) -> None:
        global _current
        xtdata_lock.release()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
        elapsed = time.monotonic() - _current["start"] if _current else 0.0
        logger.info("执行完成: %s %s（耗时 %.1fs）", self.method, self.path, elapsed)
        _current = None


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
    - NO_LOCK_PREFIXES 中的路径（如 /api/notify、/api/meta/queue_status）直通，
      不参与串行化。
    - NO_TIMEOUT_PREFIXES 中的路径（如 /api/download）参与串行化，
      但不受 XTDATA_CALL_TIMEOUT 限制（下载任务本身耗时可能较长）。
    - 其余路径若单次调用超过 XTDATA_CALL_TIMEOUT 仍未完成，视为 xtdata 挂起，
      主动释放锁并返回超时响应，避免拖垮整个服务。
    - 所有参与串行化的请求都会记录排队/执行状态，见 get_queue_status()。
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

        method = scope.get("method", "")

        if path.startswith(NO_TIMEOUT_PREFIXES):
            tracker = _RequestTracker(method, path)
            await tracker.wait_and_acquire()
            try:
                await self.app(scope, receive, send)
            finally:
                tracker.release()
            return

        await self._call_with_timeout(scope, receive, send, path, method)

    async def _call_with_timeout(self, scope, receive, send, path: str, method: str) -> None:
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

        tracker = _RequestTracker(method, path)
        await tracker.wait_and_acquire()
        app_task = asyncio.ensure_future(self.app(scope, receive, _guarded_send))
        try:
            done, _ = await asyncio.wait({app_task}, timeout=XTDATA_CALL_TIMEOUT)
        finally:
            tracker.release()

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
