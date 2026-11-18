"""流内 SSE 信封排队（403/10605）的 visible 可见化回归。

上游在 HTTP 200 建流后把 10605 排队当 SSE 信封投递；此前 chat / responses
走静默 time.sleep + reopen，客户端在 ~93s 内看不到任何排队提示。本套件验证
流内路径复用 visible_queue_acquire（qoder_queue 进度帧 + 中文超时帧），且不
改变 fast 模式与默认轮次上限。
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])

_SRC = io.open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()

print("[visible-envelope] 源码守卫与默认语义")
check("新增流内排队可见化辅助函数", "def _reopen_with_visible_queue(" in _SRC)
for _flavor in ("chat", "responses"):
    check("%s 流内重开走可见化 helper" % _flavor,
          ('_reopen_with_visible_queue(' in _SRC and
           ('self, "%s", exc' % _flavor) in _SRC))
check("默认轮次上限不变（QUEUE_MAX_ROUNDS=3）", P.QUEUE_MAX_ROUNDS == 3)
check("should_retry_envelope 默认判定不变",
      [P.should_retry_envelope(P.UpstreamStatus(403, '{"code":"10605","isQueued":true,"retryAfterSeconds":30}'), 0, a)
       for a in range(5)] == [True, True, True, False, False])


def _handler(cap):
    h = object.__new__(P.Handler)
    h.headers = {}
    h.realm_used = ""
    h.realm_reason = ""
    h._sse_begin = lambda: cap.update(kind="sse", begun=True)
    h._sse_end = lambda: cap.update(ended=True)
    h._sse_write = lambda frame: cap.setdefault("frames", []).append(
        frame.decode("utf-8", "replace") if isinstance(frame, bytes) else str(frame))
    return h


print("[visible-envelope] helper 行为：visible 排队发进度帧 + 中文超时帧")
_EXC = P.UpstreamStatus(403, '{"code":"10605","isQueued":true,"retryAfterSeconds":30}')
_CAP = {}
_H = _handler(_CAP)
_SEEN = {"seed": None, "reopen_calls": 0}


def _reopen_ok():
    _SEEN["reopen_calls"] += 1
    return ("upstream", "account", "body")


def _fake_acquire(seed, reopen, emit, budget_secs=None, started=None):
    _SEEN["seed"] = (seed.status, seed.retry_after)
    emit("queue", {"queued": True, "waited_seconds": 7, "wait_seconds": 30,
                   "budget_seconds": 600, "message": "排队中"})
    return reopen()


_REAL = P.visible_queue_acquire
P.visible_queue_acquire = _fake_acquire
try:
    _res, _handled = P._reopen_with_visible_queue(_H, "chat", _EXC, _reopen_ok, model="Kimi-K3.1-Pro")
finally:
    P.visible_queue_acquire = _REAL
_FRAMES = "".join(_CAP.get("frames") or [])
check("返回 handled=True 且带回上游结果", _handled is True and _res and _res[0] == "upstream")
check("seed 是 503 + retry_after=30", _SEEN["seed"] == (503, 30), _SEEN["seed"])
check("调用了一次 SSE begin", _CAP.get("kind") == "sse", _CAP.get("kind"))
check("发了 qoder_queue 帧", "qoder_queue" in _FRAMES and "排队中" in _FRAMES, _FRAMES[:200])
check("帧里带模型名", "Kimi-K3.1-Pro" in _FRAMES)

print("[visible-envelope] 非 visible / 非排队：不接管")
_CAP2 = {}
_H2 = _handler(_CAP2)
_OLD = P.QUEUE_MODE
P.QUEUE_MODE = "fast"
try:
    _r2, _h2 = P._reopen_with_visible_queue(_H2, "chat", _EXC, _reopen_ok)
finally:
    P.QUEUE_MODE = _OLD
check("fast 模式不接管（仍走原静默重试）", _h2 is False and not _CAP2.get("frames"))
_CAP3 = {}
_H3 = _handler(_CAP3)
_r3, _h3 = P._reopen_with_visible_queue(_H3, "chat", P.UpstreamStatus(403, "普通 403"), _reopen_ok)
check("非排队信封不接管", _h3 is False and not _CAP3.get("frames"))
check("非排队时未调用 helper 内的 sse_begin", not _CAP3.get("begun"))

finish()
