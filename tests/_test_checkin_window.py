# -*- coding: utf-8 -*-
"""签到窗口套件：排程窗口 / 本轮窗口判定 / 启动补领闸门（10:00 bug 回归）。

    python tests/_test_checkin_window.py

背景（用户实测）：「当天 10:00 打开软件不触发自动签到」。根因两条，本套件各钉住：
  ① 上游「每日 10:00（UTC+8）刷新」是**跨日窗口**（今天 10:00 → 明天 10:00），
     而旧判定用**日历日期**：昨天 18:32 的 lastCheckin 落在今天日历日之外，
     但 09:10 的那一轮补领把它当成"今天已领"，10:00 窗口打开后不再发请求；
  ② 旧排程的签到整点是 09:00/21:00 —— 09:00 永远在窗口轮转之前，只能拿到
     上一轮的"今日已领取"，还会把"当日已补签"标记用掉。

判据一律拦在**纯函数 / 纯判定层**（current_checkin_window / can_checkin /
_due_windows / _allow_complement_checkin），不依赖网络、不依赖真实时钟：
时间通过 _clock() 打桩，日期一律「今天 = 2026-10-11」。
"""
import datetime
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
ACC_DIR = tempfile.mkdtemp(prefix="qd-cw-")
os.environ["ACCOUNTS_DIR"] = ACC_DIR

import qoder_accounts as A          # noqa: E402
import qoder_scheduler as S         # noqa: E402
import qoder_tasks as T             # noqa: E402

PASS = FAIL = 0
UTC8 = datetime.timezone(datetime.timedelta(hours=8))


def check(label, cond, extra=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %r" % (label, extra))


_REAL_TIME = time.time
_REAL_LOCALTIME = time.localtime
_REAL_STRFTIME = time.strftime
# datetime.strftime 内部走 time 模块 → 必须持有**原始** C 实现，否则自递归
_STRFTIME_STUB = time.strftime
_ISO_FMT = "%Y-%m-%d %H:%M:%S"


def _local_struct(seconds):
    """用 datetime 反推本地 struct_time（不经过被替换的 time.localtime）。"""
    d = datetime.datetime.fromtimestamp(float(seconds))
    return time.struct_time((d.year, d.month, d.day, d.hour, d.minute,
                             d.second, d.weekday(), 0, -1))


class _clock(object):
    """把 time.time / localtime / strftime 打桩到固定本地时刻。

    Windows 上 time.tzset 不存在，因此**只**打桩 time 模块（datetime 侧由
    辅助函数读同一份 dt），不依赖 TZ 环境变量，测试在任何时区下语义一致。
    """

    def __init__(self, dt):
        self.dt = dt
        self._epoch = dt.timestamp()
        self._tm = _local_struct(self._epoch)

    def __enter__(self):
        stub = self

        def fake_time():
            return stub._epoch

        def fake_localtime(seconds=None):
            if seconds is None:
                return stub._tm
            return _local_struct(seconds)

        def fake_strftime(fmt, tm=None):
            tm = stub._tm if tm is None else tm
            d = datetime.datetime(tm.tm_year, tm.tm_mon, tm.tm_mday,
                                  tm.tm_hour, tm.tm_min, tm.tm_sec)
            return d.isoformat(sep=" ")[:19] if fmt == _ISO_FMT \
                else _STRFTIME_STUB(fmt, tm)

        time.time = fake_time
        time.localtime = fake_localtime
        time.strftime = fake_strftime
        # qoder_accounts / qoder_scheduler / qoder_tasks 共享同一个 time 模块对象
        return self

    def __exit__(self, *exc):
        time.time = _REAL_TIME
        time.localtime = _REAL_LOCALTIME
        time.strftime = _REAL_STRFTIME
        return False


def at(dt):
    return _clock(dt)


def dt8(y=2026, mo=10, d=11, h=10, mi=0, s=0):
    """UTC+8 的本地时刻（本机就是 UTC+8，故 datetime 直接用本地语义）。"""
    return datetime.datetime(y, mo, d, h, mi, s)


def acc(uid="u1", last=None):
    a = A.Account({"uid": uid, "realm": "cn", "accessToken": "dt-x",
                   "nickname": uid}, path=None)
    a.enabled = True
    a.last_checkin = last
    return a


class FakePool(object):
    def __init__(self, accounts):
        self.accounts = accounts


def new_sched(accounts=None, state_dir=None):
    return S.Scheduler(FakePool(accounts if accounts is not None else [acc()]),
                       state_dir=state_dir or ACC_DIR)


print("[checkin-window] 窗口纯函数")
with at(dt8(h=9, mi=30)):
    check("09:30 新一轮还没开（窗口起点仍是前一天）",
          A.current_checkin_window()[1] == "2026-10-10")
    start, day = A.current_checkin_window()
    check("09:30 本轮窗口起点 = 前一天 10:00",
          datetime.datetime.fromtimestamp(start, UTC8).strftime("%Y-%m-%d %H:%M") == "2026-10-10 10:00", day)
    check("09:30 窗口日期 = 2026-10-10", day == "2026-10-10", day)
with at(dt8(h=10, mi=5)):
    check("10:05 窗口已轮转（起点换成当天 10:00）",
          A.current_checkin_window()[1] == "2026-10-11")
    start, day = A.current_checkin_window()
    check("10:05 本轮窗口起点 = 当天 10:00",
          datetime.datetime.fromtimestamp(start, UTC8).strftime("%Y-%m-%d %H:%M") == "2026-10-11 10:00", day)
    check("10:05 窗口日期 = 2026-10-11", day == "2026-10-11", day)
with at(dt8(h=18, mi=32)):
    check("18:32 仍是同一轮窗口（18:00 属 10:00 起算的那轮）",
          A.current_checkin_window()[1] == "2026-10-11")
with at(dt8(y=2026, mo=10, d=12, h=9, mi=0)):
    check("次日 09:00 仍算前一轮窗口", A.current_checkin_window()[1] == "2026-10-11")

print("[checkin-window] can_checkin 按本轮窗口判定")
with at(dt8(h=10, mi=0)):
    check("没领过 -> True", acc(last=None).can_checkin() is True)
    check("昨天 18:32 领过（上一轮）-> True（本轮该领）",
          acc(last="2026-10-10 18:32:21").can_checkin() is True)
    check("今天 10:05 真领到（本轮）-> False（同窗口不重复领）",
          acc(last="2026-10-11 10:05:00").can_checkin() is False)
    check("今天 23:59 领过（本轮）-> False",
          acc(last="2026-10-11 23:59:00").can_checkin() is False)
    check("时间戳畸形 -> fail-open True", acc(last="not-a-time").can_checkin() is True)
with at(dt8(h=9, mi=59)):
    check("09:59 时上一轮 18:32 已领 -> False（窗口未轮转）",
          acc(last="2026-10-10 18:32:21").can_checkin() is False)

print("[checkin-window] 排程窗口")
sc = new_sched()
check("签到整点对齐窗口轮转：仅 10:00（窗口打开的那一刻）",
      sc.checkin_hours == [10], sc.checkin_hours)
check("保活整点仍为 22:00", sc.keepalive_hours == [22])


def _due(kw):
    """独立实例 + 固定时钟：_fired 是实例状态，跨场景复用会互相污染。"""
    s = new_sched()
    with at(dt8(**kw)):
        return s._due_windows()


check("10:00:30 命中签到窗口",
      _due(dict(h=10, mi=0, s=30)) == [((2026, 10, 11), 10)],
      _due(dict(h=10, mi=0, s=30)))
check("09:00 没有签到整点（旧排程的假窗口）；只补**仍在轮转中**的上一轮窗口",
      _due(dict(h=9, mi=0, s=5)) == [((2026, 10, 10), 10)],
      _due(dict(h=9, mi=0, s=5)))
check("09:00 昨天 22:00 的保活窗口已过期 -> 不补做",
      ((2026, 10, 10), 22) not in _due(dict(h=9, mi=0, s=5)))
check("10:00 之后昨天的签到窗口不再补（本轮已换成今天那一轮）",
      _due(dict(h=11, mi=0, s=5)) == [((2026, 10, 11), 10)],
      _due(dict(h=11, mi=0, s=5)))
check("22:00 命中签到+保活同一窗口",
      _due(dict(h=22, mi=0, s=5)) == [((2026, 10, 11), 10), ((2026, 10, 11), 22)],
      _due(dict(h=22, mi=0, s=5)))
check("23:45 补做当天 22:00（当日窗口一律补）",
      _due(dict(h=23, mi=45)) == [((2026, 10, 11), 10), ((2026, 10, 11), 22)],
      _due(dict(h=23, mi=45)))
check("10:30 启动：窗口仍开着 -> 命中 10:00 窗口（补领）",
      _due(dict(h=10, mi=30)) == [((2026, 10, 11), 10)],
      _due(dict(h=10, mi=30)))
sc2 = new_sched()
sc2._mark_fired(((2026, 10, 11), 10))
with at(dt8(h=10, mi=30)):
    check("10:00 窗口已执行过 -> 不重复命中", sc2._due_windows() == [], sc2._due_windows())

print("[checkin-window] 启动补领闸门（每轮窗口最多一次）")
sdir = tempfile.mkdtemp(prefix="qd-cw-st-")
sc3 = new_sched(state_dir=sdir)
with at(dt8(h=10, mi=20)):
    check("第一次启动：闸门开", sc3._allow_complement_checkin(S.CYCLE_STARTUP) is True)
    check("同窗口再次启动：闸门关", sc3._allow_complement_checkin(S.CYCLE_STARTUP) is False)
    check("落盘标记 = 本轮窗口 key",
          sc3.last_claim_window == "2026-10-11@10:00", sc3.last_claim_window)
with at(dt8(y=2026, mo=10, d=12, h=10, mi=20)):
    check("次日新窗口：闸门重新打开",
          sc3._allow_complement_checkin(S.CYCLE_STARTUP) is True)
    check("落盘标记滚动到新窗口",
          sc3.last_claim_window == "2026-10-12@10:00", sc3.last_claim_window)
    check("重启后仍认这个窗口（内存 + 落盘双份）",
          new_sched(state_dir=sdir)._allow_complement_checkin(S.CYCLE_STARTUP) is False)
    check("手动触发不受闸门限制",
          new_sched(state_dir=sdir)._allow_complement_checkin(S.CYCLE_MANUAL) is True)
    check("整点排程不受闸门限制",
          new_sched(state_dir=sdir)._allow_complement_checkin(S.CYCLE_HOUR) is True)

print("[checkin-window] 启动补领真的会发起领取（网络以外全真）")


def new_cycle_sched(accounts):
    """每个场景独立实例：_fired / last_claim_window 都是实例（+落盘）状态。"""
    sc = new_sched(accounts, state_dir=tempfile.mkdtemp(prefix="qd-cw-cy-"))
    sc.calls = []
    def fake_batch(targets, gap=1.0, inter_gap=1.0):
        sc.calls.append([a.uid for a in targets])
        return {"ok": True,
                "logs": ["✓ [%s] 签到成功 +100 积分" % a.uid for a in targets],
                "credit_added": 100 * len(targets),
                "accounts_count": len(targets)}
    S.run_batch_checkin = fake_batch          # 打桩调度器命名空间的绑定
    S.run_keepalive = lambda pool, force=False, threshold_seconds=None: {
        "refreshed": 0, "failed": 0,
        "logs": ["所有账号 Token 均未临近过期，无需刷新"]}
    return sc


NEW = lambda: acc("u-new", last="2026-10-10 18:32:21")    # 上一轮领过
DONE = lambda: acc("u-done", last="2026-10-11 10:01:00")  # 本轮已领
sc4 = new_cycle_sched([NEW(), DONE()])
with at(dt8(h=10, mi=20)):
    sc4._execute_cycle("启动初次初始化巡检", S.CYCLE_STARTUP)
check("10:20 启动：只对本轮未领账号发起领取",
      sc4.calls == [["u-new"]], sc4.calls)

sc5 = new_cycle_sched([NEW(), DONE()])
with at(dt8(h=10, mi=20)):
    sc5._execute_cycle("启动初次初始化巡检", S.CYCLE_STARTUP)
with at(dt8(h=10, mi=25)):
    sc5._execute_cycle("启动初次初始化巡检", S.CYCLE_STARTUP)
check("同窗口再次启动：闸门关，不重复领取", sc5.calls == [["u-new"]], sc5.calls)
# 重启后（**同一窗口内**）新实例仍要认这条闸门。时钟必须打回 10:25：
# 闸门判据 _current_window_key() 读的是"当前窗口"，若让它在真实时钟下跑，
# 取到的会是"今天"（运行日），与状态里的测试日期 2026-10-11 不等 → 闸门必然
# 重开。那是测试自己的时钟依赖，不是产品行为（曾因此长期假红）。
with at(dt8(h=10, mi=25)):
    check("闸门状态落盘（重启后仍认）",
          S.Scheduler(FakePool([NEW()]), state_dir=sc5.state_dir)
            ._allow_complement_checkin(S.CYCLE_STARTUP) is False)

sc6 = new_cycle_sched([NEW(), DONE()])
with at(dt8(h=10, mi=0, s=5)):
    sc6._execute_cycle("整点排程命中 (10:00 签到)", S.CYCLE_HOUR, hour=10)
check("10:00 整点窗口：只对**本轮未领**的账号领取（已是上一轮时间戳的不重复要）",
      sc6.calls == [["u-new"]], sc6.calls)

sc7 = new_cycle_sched([NEW()])
with at(dt8(h=9, mi=50)):
    sc7._execute_cycle("启动初次初始化巡检", S.CYCLE_STARTUP)
check("09:50 启动（窗口未轮转）：不发领取请求", sc7.calls == [], sc7.calls)
with at(dt8(h=10, mi=0, s=40)):
    sc7._execute_cycle("整点排程命中 (10:00 签到)", S.CYCLE_HOUR, hour=10)
check("跑着不动等到 10:00：窗口轮转即自动领取", sc7.calls == [["u-new"]], sc7.calls)

sc8 = new_cycle_sched([NEW()])
with at(dt8(h=10, mi=40)):
    sc8._execute_cycle("启动初次初始化巡检", S.CYCLE_STARTUP)
check("10:40 打开软件（窗口已过 40 分钟）：仍然补领本轮", sc8.calls == [["u-new"]], sc8.calls)

print("[checkin-window] 用户实测时间线重放：09:10 打开软件那天，10:20 再打开时才轮转")


def _local_epoch(y, mo, d, h, mi, s):
    """本机时区下该时刻的 epoch（epoch 与时钟打桩共用同一份本地语义）。"""
    return datetime.datetime(y, mo, d, h, mi, s).timestamp()


def _stamped_start_of_window():
    return _local_epoch(2026, 10, 10, 18, 32, 21)


TL = new_cycle_sched([acc("u-old", last="2026-10-10 18:32:21")])   # 上一轮领过
TL.pool.accounts[0].last_checkin_epoch = _stamped_start_of_window()

# 阶段 ①：当天 09:10 打开软件（窗口还没轮转 —— 旧版本在这里把"当日补签"
#          标记用掉，之后 10:00 窗口打开也不再发请求）
with at(dt8(h=9, mi=10, s=40)):
    TL._execute_cycle("启动初次初始化巡检", S.CYCLE_STARTUP)
check("阶段① 09:10 启动：窗口未轮转，不发领取请求",
      TL.calls == [], TL.calls)
check("阶段① 闸门已记录当时那一轮窗口（属于上一轮）",
      TL.last_claim_window == "2026-10-10@10:00", TL.last_claim_window)

# 阶段 ②：10:20 再次打开（上游窗口已在 10:00 轮转 —— 必须补领本轮）
with at(dt8(h=10, mi=20, s=5)):
    TL._execute_cycle("启动初次初始化巡检", S.CYCLE_STARTUP)
check("阶段② 10:20 打开软件：本轮窗口已轮转 -> 补领",
      TL.calls == [["u-old"]], TL.calls)
check("阶段② 闸门滚动到新窗口",
      TL.last_claim_window == "2026-10-11@10:00", TL.last_claim_window)

# 阶段 ③：同一天再重启两次（不得重复领取）
with at(dt8(h=10, mi=40, s=0)):
    TL._execute_cycle("启动初次初始化巡检", S.CYCLE_STARTUP)
with at(dt8(h=14, mi=0, s=0)):
    TL._execute_cycle("启动初次初始化巡检", S.CYCLE_STARTUP)
check("阶段③ 同窗口内重启不再重复领取", TL.calls == [["u-old"]], TL.calls)

# 阶段 ④：次日 10:00 排程命中（新窗口，重新领）
with at(dt8(y=2026, mo=10, d=12, h=10, mi=0, s=20)):
    TL._execute_cycle("整点排程命中 (10:00 签到)", S.CYCLE_HOUR, hour=10,
                      scheduled=True)
check("阶段④ 次日 10:00 排程：新窗口重新领取",
      TL.calls == [["u-old"], ["u-old"]], TL.calls)

print("[checkin-window] 启动补领与整点排程不重复执行同一轮")


def _epoch(y, mo, d, h, mi, s):
    return datetime.datetime(y, mo, d, h, mi, s).timestamp()


def _stamped(y, mo, d, h, mi, s):
    return datetime.datetime(y, mo, d, h, mi, s).strftime("%Y-%m-%d %H:%M:%S")


sc9 = new_cycle_sched([NEW()])
# 模拟 upstream 真发放：单轮内第二次对同一账号发起领取即算越界
_issued9 = set()
def _once(targets, gap=1.0, inter_gap=1.0):
    dup = [a.uid for a in targets if a.uid in _issued9]
    sc9.calls.append([a.uid for a in targets])
    for a in targets:
        _issued9.add(a.uid)
        a.last_checkin = _stamped(2026, 10, 11, 10, 20, 0)   # 网关签到后落章
    return {"ok": not dup, "dup": dup,
            "logs": [], "credit_added": 0, "accounts_count": len(targets)}
S.run_batch_checkin = _once

with at(dt8(h=10, mi=20, s=0)):
    sc9._run_cycle("启动补领", S.CYCLE_STARTUP)          # 启动：补领本轮
    sc9._run_cycle("整点排程命中 (10:00 签到)", S.CYCLE_HOUR, hour=10, scheduled=True)
check("启动补领后，主循环不再对同一轮重放（只有一次真实领取）",
      sc9.calls == [["u-new"]], sc9.calls)
sc10 = new_cycle_sched([NEW()])
S.run_batch_checkin = (lambda calls: (lambda targets, gap=1.0, inter_gap=1.0:
    (calls.append([a.uid for a in targets]) or
     {"ok": True, "logs": [], "credit_added": 0,
      "accounts_count": len(targets)})))(sc10.calls)
with at(dt8(h=10, mi=20, s=0)):
    sc10._run_window(((2026, 10, 11), 10))    # 主循环走的就是这条路径
    sc10._run_window(((2026, 10, 11), 10))    # 同一窗口再 tick 一次
check("主循环同一窗口只执行一次（第二次已被标 fired）",
      sc10.calls == [["u-new"]], sc10.calls)

print("[checkin-window] 旧状态迁移（09:10 启动过一次的机器）")
sdir2 = tempfile.mkdtemp(prefix="qd-cw-st3-")
os.makedirs(os.path.join(sdir2, S.STATE_SUBDIR), exist_ok=True)
with open(os.path.join(sdir2, S.STATE_SUBDIR, S.STATE_FILE_NAME), "w",
          encoding="utf-8") as fh:
    json.dump({"enabled": True, "startup_claim_date": "2026-10-11",
               "last_run_time": "2026-10-11 09:11:03"}, fh)
sc5 = new_sched(state_dir=sdir2)
with at(dt8(h=10, mi=20)):
    check("旧格式（纯日期）不等于新窗口 key -> 闸门开，10:20 仍会补领",
          sc5._allow_complement_checkin(S.CYCLE_STARTUP) is True)
    check("补领后标记升级为新窗口 key",
          sc5.last_claim_window == "2026-10-11@10:00", sc5.last_claim_window)

print("[checkin-window] 看板文案随排程走")
st = sc.status()
check("状态文案 = 10:00 签到（窗口轮转）+ 22:00 保活",
      "10:00" in st["mode_cn"] and "22:00" in st["mode_cn"], st["mode_cn"])
check("状态暴露当前窗口供排障",
      st["current_claim_window"] == sc._current_window_key(),
      st["current_claim_window"])

shutil.rmtree(ACC_DIR, ignore_errors=True)
shutil.rmtree(sdir, ignore_errors=True)
print()
print("SUMMARY: TOTAL %d checks, %d failed" % (PASS + FAIL, FAIL))
print("RESULT: %s" % ("PASS" if FAIL == 0 else "FAIL"))
sys.exit(1 if FAIL else 0)
