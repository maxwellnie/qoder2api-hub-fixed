"""qoder_scheduler.py —— 后台定时调度器 (Scheduler)

负责常驻后台自动执行：
1. 每日签到 (Daily Checkin)：按**官方窗口**整点触发——UTC+8 每日 10:00（上游
   额度/活动名额在此刻轮转）与 22:00 各一轮；启动时若窗口已过（例如 10:20
   才打开软件）立即补一次本轮领取。
2. 福利包巡检：签到巡检时顺带刷新额度与套餐快照。
3. Token 保活 (Keepalive)：每日 22:00 集中刷新；另在任意巡检中顺带刷新剩余
   寿命不足 4 小时的凭证（drt- / jrt- 按 token 前缀路由，PAT 兜底）。
4. 状态持久化与看板展示：暴露状态、执行记录、支持手动立即触发与开关切换。

状态落盘：enabled / last_run_time / next_run_time / logs、已触发的窗口
(fired_windows)「最近一次发起领取的窗口」(last_claim_window) 写入
<账号目录>/scheduler/state.json。
  · 放在账号目录的**子目录**里：AccountPool.load() 只扫顶层 *.json
    （qoder_accounts.py:1441-1460），子目录不会被误当成账号；
  · accounts/* 已被 .gitignore 忽略，无需新增忽略规则。

为什么窗口按 UTC+8 10:00 对齐（历史缺陷）：上游「每日 10:00（UTC+8）刷新」
是**跨日**窗口（今天 10:00 → 明天 10:00）。旧排程把签到放在 09:00 与 21:00、
且用**日历日期**判断"今天领过没有"，于是：
  · 09:00 那一轮永远在窗口轮转之前，只能拿到上一轮的"今日已领取"；
  · 那一轮还会把"当日已补签"标记用掉（mark-before-act），当天 10:00 窗口
    真正打开后（用户打开软件的时刻）反而不再发起领取 —— 用户实测"当天 10:00
    打开软件不触发自动签到"。
现在判定统一为「**本轮窗口**」：can_checkin() 用窗口起点而非日历日期，
排程窗口对齐 10:00/22:00，漏过的窗口在启动巡检里补一次（每轮最多一次）。
"""
import datetime
import json
import os
import re
import sys
import threading
import time

import qoder_tasks
from qoder_tasks import set_logger, run_batch_checkin, run_keepalive

# 状态文件相对账号目录的子路径（避免被 AccountPool 读取为账号 JSON）
STATE_SUBDIR = "scheduler"
STATE_FILE_NAME = "state.json"

# 巡回来由：启动自动 / 整点排程 / 手动触发
CYCLE_STARTUP = "startup"
CYCLE_HOUR = "hour"
CYCLE_MANUAL = "manual"


class Scheduler(object):
    def __init__(self, pool, state_dir=None):
        self.pool = pool
        # _fired 必须在 _apply_schedule 之前就存在（它会读旧键做分类清理）。
        # 两类键：排程点 epoch（上游 issue #70）与窗口 key
        # "YYYY-MM-DD@HH:00"（我方窗口口径），互认但不混类型。
        self._fired = {}
        # issue #70：排程可配置（默认仍是 09:00 / 21:00 签到、22:00 保活）。
        # 旧实现靠「每分钟轮询 cur_min == 0」命中整点：容器时区与宿主机不同、
        # 或上一轮巡检跑过 1 分钟，就会**整点漏掉**——用户看到的就是「签到无效」。
        self.checkin_times = []          # [(hour, minute), ...]
        self.keepalive_times = []
        self.cron = ""                   # 可选：5 段 cron，覆盖 checkin_times
        self.jitter_secs = 8             # 账号之间的随机间隔上限（秒）
        self._apply_schedule({})
        self._stop_event = threading.Event()
        self._thread = None
        self._run_lock = threading.Lock()
        # 已触发过的窗口 key（"YYYY-MM-DD@HH:00"）：内存 + 落盘双份，进程重启
        # 不会把同一个整点窗口重放一遍（后者是崩溃/重启场景的兜底）。
        # 状态目录：优先显式传入（测试）→ 账号池目录 → 环境变量 → 脚本同级 accounts/
        self.state_dir = (state_dir or getattr(pool, "dir", None)
                          or os.environ.get("ACCOUNTS_DIR")
                          or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "accounts"))
        self._state_lock = threading.Lock()
        st = self._load_state()
        self.enabled = bool(st.get("enabled", True))   # 看板开关重启后仍生效
        self._apply_schedule(st)                        # issue #70：排程配置持久化
        self.last_run_time = st.get("last_run_time") or None
        self.next_run_time = None
        self.logs = [str(x) for x in (st.get("logs") or [])][-60:]
        # 「最近一次发起本轮领取的窗口」：同一窗口内（含进程重启）最多补领一次。
        # 旧字段名 startup_claim_date 保留（只读回退），看板字段名不变。
        self.last_claim_window = str(
            st.get("last_claim_window")
            or st.get("startup_claim_date") or "")
        self.last_cycle_date = str(st.get("last_cycle_date") or "")
        for item in st.get("fired_windows") or []:
            key = str(item)
            if "@" in key and len(key) >= 16:
                self._fired[key] = 0.0     # 重启后视为已触发（不重放）
        self._calc_next_fire()
        # 把任务层失败（死端点、上游结构变化）也打进看板日志。
        set_logger(self.log)
        if st:
            self.log("调度器状态已从 %s 恢复（enabled=%s，上次运行 %s）"
                     % (self._state_path(), self.enabled,
                        self.last_run_time or "-"))

    # -- 排程解析（issue #70） ----------------------------------------------
    @staticmethod
    def _parse_times(value, fallback):
        """"09:00,21:30" -> [(9,0),(21,30)]；非法项忽略，全非法用 fallback。"""
        out = []
        for chunk in str(value or "").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            m = re.match(r"^(\d{1,2}):(\d{2})$", chunk)
            if not m:
                continue
            h, mi = int(m.group(1)), int(m.group(2))
            if 0 <= h <= 23 and 0 <= mi <= 59:
                out.append((h, mi))
        return sorted(set(out)) or list(fallback)

    @staticmethod
    def _fmt_times(times):
        return ",".join("%02d:%02d" % t for t in times)

    @staticmethod
    def _cron_field(spec, lo, hi):
        """单段 cron -> set(int)；非法返回 None。支持 * / a-b / a,b / */n / a-b/n。"""
        out = set()
        for part in str(spec or "").split(","):
            part = part.strip()
            if not part:
                continue
            step = 1
            has_step = "/" in part
            if has_step:
                part, _, s_step = part.partition("/")
                try:
                    step = max(1, int(s_step))
                except Exception:
                    return None
            if part == "*":
                rng = range(lo, hi + 1)
            elif "-" in part:
                a, _, b = part.partition("-")
                try:
                    a, b = int(a), int(b)
                except Exception:
                    return None
                if a > b:
                    a, b = b, a
                rng = range(max(lo, a), min(hi, b) + 1)
            else:
                try:
                    v = int(part)
                except Exception:
                    return None
                if v < lo or v > hi:
                    return None
                # 关键：单个值只是「这一个」。只有显式 step（写成 9/6）才展开
                # 成「从 9 到上限每 6」——否则 "9,21" 会退化成 9..23 全命中。
                rng = range(v, hi + 1) if has_step else [v]
            out.update(x for x in rng if (x - lo) % step == 0)
        return out or None

    def _cron_fields(self):
        parts = str(self.cron or "").split()
        if len(parts) != 5:
            return None
        minute = self._cron_field(parts[0], 0, 59)
        hour = self._cron_field(parts[1], 0, 23)
        dom = self._cron_field(parts[2], 1, 31)
        mon = self._cron_field(parts[3], 1, 12)
        dow = self._cron_field(parts[4], 0, 6)
        if None in (minute, hour, dom, mon, dow):
            return None
        return minute, hour, dom, mon, dow

    def _cron_matches(self, tm):
        f = self._cron_fields()
        if not f:
            return False
        minute, hour, dom, mon, dow = f
        if tm.tm_min not in minute or tm.tm_hour not in hour:
            return False
        if tm.tm_mon not in mon:
            return False
        if tm.tm_mday not in dom:
            return False
        return ((tm.tm_wday + 1) % 7) in dow     # Python 0=周一 -> cron 0=周日

    def _occurrences(self, day_offset):
        """某一天的所有排程时刻 -> [(kind, label, ts)]。"""
        base = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        base += day_offset * 86400
        out = []
        if self.cron:
            start = time.localtime(base)
            for i in range(24 * 60):
                ts = base + i * 60
                tm = time.localtime(ts)
                if tm.tm_mday != start.tm_mday and i > 0:
                    break
                if self._cron_matches(tm):
                    out.append((CYCLE_HOUR, "cron %02d:%02d" % (tm.tm_hour, tm.tm_min),
                                ts))
        else:
            for (h, mi) in self.checkin_times:
                ts = base + h * 3600 + mi * 60
                out.append((CYCLE_HOUR, "签到期 %02d:%02d" % (h, mi), ts))
        for (h, mi) in self.keepalive_times:
            ts = base + h * 3600 + mi * 60
            out.append((CYCLE_HOUR, "保活期 %02d:%02d" % (h, mi), ts))
        return out

    def _next_target(self):
        """下一个应触发但还没触发的排程时刻 -> (kind, label, ts) 或 None。"""
        now = time.time()
        cands = []
        for off in (-1, 0, 1):
            cands.extend(self._occurrences(off))
        cands.sort(key=lambda x: x[2])
        for kind, label, ts in cands:
            if ts > now:
                break
            if ts in self._fired or self._window_key_for_ts(ts) in self._fired:
                continue
            if now - ts > 3 * 3600:      # 超过 3 小时的历史点不再补跑
                self._fired[ts] = True
                continue
            return kind, label, ts
        return None

    def _apply_schedule(self, st):
        # 默认 10:00（UTC+8 官方轮转整点）：与 qoder_accounts.can_checkin() 的
        # 「窗口日」口径对齐——上游默认的 09:00/21:00 是日历日口径，早于轮转点，
        # 那两轮会在窗口未开时白跑一次领取请求。
        self.checkin_times = self._parse_times(st.get("checkin_times"),
                                              [(10, 0)])
        self.keepalive_times = self._parse_times(st.get("keepalive_times"),
                                                [(22, 0)])
        self.cron = str(st.get("cron") or "").strip()
        if self.cron and not self._cron_fields():
            self.log("排程 cron 表达式无效，已忽略: %s" % self.cron)
            self.cron = ""
        try:
            self.jitter_secs = max(0, min(60, int(st.get("jitter_secs") or 8)))
        except Exception:
            self.jitter_secs = 8
        # 只清上游排程点键（epoch），保留我方窗口键
        # （YYYY-MM-DD@HH:00）：否则面板改一次排程就把本轮窗口已补领过的
        # 记忆抹掉，进程重启后会把同一轮窗口重放一遍
        # （_mark_fired 的落盘兜底随之失效）。
        _win_keys = [k for k in self._fired
                     if isinstance(k, str) and '@' in k]
        self._fired = {}
        for _k in _win_keys:
            self._fired[_k] = 0.0
        self._calc_next_fire()

    def set_schedule(self, checkin_times=None, keepalive_times=None,
                     cron=None, jitter_secs=None):
        """面板改排程（issue #70）：校验后套用并落盘。"""
        st = {
            "checkin_times": checkin_times if checkin_times is not None
            else self._fmt_times(self.checkin_times),
            "keepalive_times": keepalive_times if keepalive_times is not None
            else self._fmt_times(self.keepalive_times),
            "cron": cron if cron is not None else self.cron,
            "jitter_secs": jitter_secs if jitter_secs is not None
            else self.jitter_secs,
        }
        self._apply_schedule(st)
        self._save_state()
        return self.status()

    # -- 状态持久化 ---------------------------------------------------------
    def _state_path(self):
        return os.path.join(self.state_dir, STATE_SUBDIR, STATE_FILE_NAME)

    def _load_state(self):
        """读状态文件；不存在/损坏都返回 {}（按全新进程处理）。"""
        try:
            with open(self._state_path(), encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_state(self):
        """原子落盘（tmp + os.replace，同账号文件的写法）。失败只写 stderr。

        注意：本函数**不得**调用 self.log()，否则与 log→_save_state 形成递归。
        """
        with self._state_lock:
            try:
                path = self._state_path()
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".tmp"
                data = {
                    "enabled": bool(self.enabled),
                    "checkin_times": self._fmt_times(self.checkin_times),
                    "keepalive_times": self._fmt_times(self.keepalive_times),
                    "cron": self.cron,
                    "jitter_secs": self.jitter_secs,
                    "last_run_time": self.last_run_time,
                    "next_run_time": self.next_run_time,
                    "startup_claim_date": self.last_claim_window,
                    "last_claim_window": self.last_claim_window,
                    "last_cycle_date": self.last_cycle_date,
                    "fired_windows": sorted(
                        k for k in self._fired
                        if isinstance(k, str) and '@' in k)[-8:],
                    "logs": self.logs[-60:],
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
            except Exception as exc:
                try:
                    sys.stderr.write("[scheduler] 状态落盘失败: %s\n" % exc)
                    sys.stderr.flush()
                except Exception:
                    pass

    def log(self, msg):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = "[%s] %s" % (ts, msg)
        self.logs.append(entry)
        if len(self.logs) > 60:
            self.logs = self.logs[-60:]
        # 日志即状态：顺带落盘，看板开关/上次运行时间在重启后仍然可见
        self._save_state()
        try:
            import qoder_proxy
            qoder_proxy.add_log_entry("[调度器] %s" % msg, tag="scheduler")
        except Exception:
            pass

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self.log("后台定时调度器已启动（排程 %s 签到 · %s 保活，北京时间 UTC+8）"
                 % ("/".join("%d:00" % h for h in self.checkin_hours),
                    "/".join("%d:00" % h for h in self.keepalive_hours)))

    def stop(self):
        self._stop_event.set()
        self.log("后台定时调度器已暂停")

    # -- 排程窗口 -----------------------------------------------------------
    @staticmethod
    def _window_start_epoch(day, hour):
        """某个 (日期, 整点) 窗口的起点 epoch。用 datetime 构造而非 time.mktime：
        mktime 走被替换过的 time.localtime，判定不可复现（排程必须可复现）。"""
        d = datetime.datetime(day[0], day[1], day[2], hour, 0, 0)
        return d.timestamp()

    @staticmethod
    def _window_key(tm, hour):
        return "%04d-%02d-%02d@%02d:00" % (tm.tm_year, tm.tm_mon, tm.tm_mday,
                                           hour)

    @classmethod
    def _window_key_for_ts(cls, ts):
        """排程点 ts 所属的窗口 key（cron 10:30 归 10:00 那一轮）。"""
        tm = time.localtime(ts)
        return cls._window_key(tm, tm.tm_hour)

    # issue #70 之后 *_times 可由面板配置，窗口判定仍按整点：
    # 这里从配置实时派生，改排程后窗口语义自动跟上。
    @property
    def checkin_hours(self):
        """签到整点（checkin_times 的小时部分，升序去重）。"""
        return sorted({h for h, _m in self.checkin_times})

    @property
    def keepalive_hours(self):
        """保活整点（keepalive_times 的小时部分，升序去重）。"""
        return sorted({h for h, _m in self.keepalive_times})

    @property
    def all_hours(self):
        """全部排程整点 = 签到并集保活。"""
        return sorted(set(self.checkin_hours) | set(self.keepalive_hours))

    def _due_windows(self, now=None):
        """需要立刻执行的排程窗口 [((年,月,日), 小时), ...]（升序）。

        候选 = 今天与昨天各自的每个排程整点；命中条件分两类，**各管各的窗口**：
          · 签到整点（checkin_hours）：已到点且「本轮窗口还没轮转」——即
            UTC+8 已过 10:00、未到次日 10:00（窗口开着才可能真的领到；否则
            只能拿到上一轮的"今日已领取"）。
          · 保活整点（keepalive_hours）：已到点且仍在**同一天**（当天 23:xx
            仍值得补做 22:00 的集中保活）。
        当天窗口不论过了多久都补做；跨日只补**仍在轮转中的签到窗口**
        （见 _active_claim_window_day）。返回的窗口此刻**一律已经到点**。
        """
        now = time.time() if now is None else float(now)
        now_tm = time.localtime(now)
        today = (now_tm.tm_year, now_tm.tm_mon, now_tm.tm_mday)
        y_tm = time.localtime(now - 86400)
        yesterday = (y_tm.tm_year, y_tm.tm_mon, y_tm.tm_mday)
        active_claim_day = self._active_claim_window_day(now)
        out = []
        for day, tm in ((yesterday, y_tm), (today, now_tm)):
            for hour in self.all_hours:
                start = self._window_start_epoch(day, hour)
                if now < start:
                    continue               # 还没到点
                if self._fired.get(self._window_key(tm, hour)):
                    continue               # 已触发过（内存或重启前落盘）
                if day == today:
                    # 当天窗口一律补做（刚过整点 / 启动晚了几分钟 / 错过很久）
                    out.append((day, hour))
                elif hour in self.checkin_hours and day == active_claim_day:
                    out.append((day, hour))        # 跨日补偿：本轮窗口仍是那一天的
        return out

    @staticmethod
    def _active_claim_window_day(now=None):
        """本轮**已开窗**的领取窗口属于哪一天（返回 (年,月,日)）。

        窗口按 UTC+8 每日 10:00 轮转：当天 10:00 之后 → 当天；10:00 之前 →
        仍是前一天开的那一轮（**前一天 10:00** 才是"现在有效"的领取窗口）。
        排程补领据此区分「昨天没做的保活」（不做）与「跨日仍在开的签到窗口」
        （要做）——例如 20:00 打开软件，本轮窗口是**昨天 10:00** 开的那个。
        """
        now = time.time() if now is None else float(now)
        try:
            import qoder_accounts
            _, day = qoder_accounts.current_checkin_window(now)
            y, m, d = (int(x) for x in day.split("-"))
            return (y, m, d)
        except Exception:
            return None

    def _mark_fired(self, window):
        day, hour = window
        self._fired[self._window_key(
            time.struct_time((day[0], day[1], day[2], hour, 0, 0, 0, 0, -1)),
            hour)] = time.time()
        # 窗口键与排程点键（epoch）分开裁剪：混在一起 sorted() 会 TypeError
        _wins = sorted(k for k in self._fired
                       if isinstance(k, str) and '@' in k)
        for key in _wins[:-8]:
            self._fired.pop(key, None)
        _pts = sorted(k for k in self._fired
                      if not (isinstance(k, str) and '@' in k))
        for key in _pts[:-32]:
            self._fired.pop(key, None)

    def _run_window(self, window):
        """执行一个排程窗口：先标记"已触发"（内存 + 落盘），再跑巡检。

        标记先行有两个作用：① 同一轮窗口不会被重复执行（不管是因为主循环
        每个 tick 都重算 due，还是进程重启后重算）；② 启动补领与整点排程
        命中同一轮窗口时，后者不会把同一批账号再打扰一遍。
        """
        day, hour = window
        if self._fired.get(self._window_key(
                time.struct_time((day[0], day[1], day[2], hour, 0, 0, 0, 0, -1)),
                hour)):
            return                     # 已触发过（内存或重启前落盘）：不重放
        self._mark_fired(window)
        checkin_part = hour in self.checkin_hours
        keepalive_part = hour in self.keepalive_hours
        reason = "整点排程命中 (%d:00%s%s)" % (
            hour,
            " 签到" if checkin_part else "",
            " 保活" if keepalive_part else "")
        try:
            self._execute_cycle(reason, CYCLE_HOUR, hour=hour, scheduled=True)
        except Exception as exc:
            self.log("排程执行异常: %s" % exc)

    def _run_loop(self):
        # 启动后稍等主服务就绪，然后先执行一次「启动巡检」：窗口已过则补做本轮
        time.sleep(10)
        try:
            self._execute_cycle("启动初次初始化巡检", CYCLE_STARTUP)
        except Exception as exc:
            self.log("初次巡检异常: %s" % exc)

        # issue #70：不再靠「每分钟轮询 cur_min == 0」。旧写法有两个必漏点：
        #   1) 容器时区与宿主机不同 -> 命中的是 UTC 的 9 点，不是用户本地 9 点；
        #   2) 上一轮巡检跑超过 1 分钟 -> 整点那一分钟整个跳过，当天就没了。
        # 现在改成「到点即补」：每 15s 检查一次「有没有已过点但还没触发的排程」，
        # 最多回补 3 小时内的漏点（进程重启也接得上）。
        while not self._stop_event.is_set():
            if self.enabled:
                due = self._next_target()
                if due is not None:
                    kind, label, ts = due
                    self._fired[ts] = True
                    self._pending_focus = label
                    try:
                        self._execute_cycle("整点排程命中 (%s)" % label, kind)
                    except Exception as exc:
                        self.log("排程执行异常: %s" % exc)
            self._stop_event.wait(15)

    def _calc_next_fire(self):
        now = time.time()
        cands = []
        for off in (-1, 0, 1, 2):
            cands.extend(self._occurrences(off))
        future = [ts for _k, _l, ts in cands if ts > now + 1]
        if future:
            self.next_run_time = time.strftime("%Y-%m-%d %H:%M:%S",
                                              time.localtime(min(future)))
        else:
            self.next_run_time = None

    def trigger_now(self):
        """手动立即触发一次调度检查（不受"本轮补领一次"闸门限制）。"""
        if self._run_lock.locked():
            return {"ok": False, "msg": "已有巡检正在执行，请稍候再试"}
        threading.Thread(target=self._execute_cycle,
                         args=("手动立即触发", CYCLE_MANUAL),
                         daemon=True).start()
        return {"ok": True, "msg": "已触发后台调度执行"}

    def _execute_cycle(self, trigger_reason="周期巡检", cycle_kind=CYCLE_HOUR,
                       hour=None, scheduled=False):
        if not self._run_lock.acquire(blocking=False):
            self.log("跳过本次巡检 (%s)：上一轮仍在执行" % trigger_reason)
            return
        try:
            self._run_cycle(trigger_reason, cycle_kind, hour=hour,
                            scheduled=scheduled)
        finally:
            self._run_lock.release()

    def _allow_complement_checkin(self, cycle_kind):
        """启动巡检的补领闸门：**同一轮窗口最多补领一次**（其余巡回来由不受限）。

        为什么需要：启动巡检会对 can_checkin() 为真的账号发真实领取请求，
        进程反复重启就会反复领取。这里做 mark-before-act——先把本轮窗口标记
        落盘，再去做领取；即使随后进程崩溃/被杀，重启后也不再重放。
        can_checkin()（按**窗口起点**判定，见 qoder_accounts.current_checkin_window）
        依赖的 per-account lastCheckin 是第二道闸：本轮真正领到之后，即使闸门
        因故再开，也不会重复 POST。
        """
        if cycle_kind != CYCLE_STARTUP:
            return True
        window = self._current_window_key()
        if not window:
            return True                    # 时钟/时区取不到 → 不做无依据的拦截
        if self.last_claim_window == window:
            return False
        self.last_claim_window = window
        self._save_state()
        return True

    def _current_window_key(self):
        """当前「本轮签到窗口」的 key（"YYYY-MM-DD@10:00"，UTC+8 口径）。"""
        try:
            import qoder_accounts
            start, day = qoder_accounts.current_checkin_window()
            return "%s@%02d:00" % (day, time.localtime(start).tm_hour)
        except Exception:
            return ""

    def _run_cycle(self, trigger_reason="周期巡检", cycle_kind=CYCLE_HOUR,
                   hour=None, scheduled=False):
        self.last_run_time = time.strftime("%Y-%m-%d %H:%M:%S")
        self.last_cycle_date = time.strftime("%Y-%m-%d")
        self.log("开始执行任务 (%s)..." % trigger_reason)
        if not self.pool or not self.pool.accounts:
            self.log("暂无可用的活跃账号，跳过本次巡检")
            self._save_state()
            return

        # 本轮已到点且按窗口口径该处理的窗口（[((年,月,日), 小时), ...]）：
        # 1) 启动巡检据此把补做过的窗口当场标记，主循环醒来后不会再按整点排程
        #    把同一轮执行一遍；2) hour 未指定时用它兜底，让手动触发与上游整点
        #    排程也按最近的窗口判断该签到还是该保活。
        # （合并上游 issue #70 判定块时这行被连带删丢，启动巡检于是每次必
        #  NameError，被 _run_loop 的 try 吞成一条日志，启动补领静默失效。）
        due = self._due_windows()
        if hour is None and due:
            hour = due[0][1]
        cur_hour = time.localtime().tm_hour
        focus = getattr(self, "_pending_focus", "") or ""
        self._pending_focus = ""
        # issue #70：排程可配置后，靠标签判断本轮该做什么（cron 只覆盖签到）。
        if focus.startswith("保活期"):
            keepalive_due, checkin_due = True, False
        elif focus.startswith(("签到期", "cron")):
            keepalive_due, checkin_due = False, True
        else:
            _h = hour if hour is not None else cur_hour
            keepalive_due = _h in {h for h, _m in self.keepalive_times}
            checkin_due = _h in {h for h, _m in self.checkin_times}
        allow_complement = self._allow_complement_checkin(cycle_kind)

        # 1. Token 保活：整点 22:00 全量刷新；其余巡检只刷新临近过期的
        force = keepalive_due
        ka = run_keepalive(self.pool, force=force)
        self.log("Token 保活：刷新 %d 个，失败 %d 个%s"
                 % (ka["refreshed"], ka["failed"],
                    "（22:00 集中保活）" if force else "（临近过期）"))
        for line in ka["logs"]:
            if line.startswith("!"):
                self.log(line)

        # 2. 每日签到：只有三种来由会真的发起领取——
        #      ① 整点签到窗口（排程自己命中的 10:00，上游此刻才轮转）；
        #      ② 启动巡检的本轮补领（每轮窗口最多一次，见 _allow_complement_checkin）；
        #      ③ 看板手动触发（用户显式要求，不受闸门限制）。
        #    注意：启动恰好落在 10:xx **不算**①——否则"启动补领"这个一次性的
        #    闸门会被绕过，进程反复重启就变成反复领取（历史缺陷）。
        #    其余巡检只做保活与额度快照。per-account can_checkin() 是第二道闸：
        #    按**窗口起点**判定，本轮真领到之后不会再进 pending。
        #    能力运行时探测：接口不存在的区域由 run_checkin 给出原因并跳过。
        # #25 a：本轮失效凭证的账号**整体排除**在后续步骤之外。
        # 理由：跳过只是"不再打上游"，但一轮里若已用失效 token 拿到过退化响应，
        # 那次响应会被写进账本（把健康余额覆盖成 0）；所以这里在**源头**把这些
        # 账号从签到与额度快照里去掉 —— 它们的余额等下一次成功刷新再恢复。
        # 两类来源：① 刚才保活明确失败的 uid；② token 已过期（含没被保活覆盖的）。
        stale_uids = set(ka.get("failed_uids") or [])
        now_ts = time.time()
        for a in (self.pool.accounts if self.pool else []):
            exp = getattr(a, "expires_at", 0) or 0
            if a.access_token and exp and exp <= now_ts:
                stale_uids.add(a.uid)
        if stale_uids:
            self.log("跳过 %d 个失效凭证账号（本轮不签到、不刷额度）：%s"
                     % (len(stale_uids),
                        "、".join(sorted(str(u)[:8] for u in stale_uids))))
        targets = [a for a in self.pool.accounts
                   if a.enabled and a.access_token and a.uid not in stale_uids]
        if cycle_kind == CYCLE_STARTUP:
            # 启动巡检会补做"此刻已到点的窗口"（例如 10:20 打开软件补 10:00
            # 那一轮）。这里把这些窗口当场标记为已触发，避免主循环醒来后又
            # 按整点排程把同一轮再执行一遍（同一轮窗口不重复打扰上游）。
            for win in due:
                self._mark_fired(win)
            self._save_state()
        may_claim = ((cycle_kind == CYCLE_MANUAL) or allow_complement
                     or (scheduled and checkin_due))
        if may_claim and targets:
            pending = [a for a in targets if a.can_checkin()]
        else:
            pending = []               # 已补领过（或未到窗口）：不发起领取
        checkin_count = 0
        earned = 0
        if pending:
            self.log("本轮窗口待领取 %d 个账号，执行自动签到..." % len(pending))
            res = run_batch_checkin(pending, gap=1.0, inter_gap=1.0)
            checkin_count = res["accounts_count"]
            earned = res.get("credit_added") or 0
            for line in res["logs"]:
                if line.startswith("✓") or line.startswith("!"):
                    self.log(line)
        elif targets:
            self.log("本轮窗口内 %d 个账号均已领取（窗口自 UTC+8 每日 10:00 起算）；"
                     "如仍想强制重试可在看板手动触发" % len(targets))
        if cycle_kind == CYCLE_STARTUP:
            # 启动补领只做一次：把结果说清楚，避免看板以为"每次启动都会领"
            self.log("（本轮窗口 %s 的启动补领已记录）"
                     % (self.last_claim_window or "-"))

        # 3. 刷新额度快照（看板积分卡片依赖）
        for acc in targets:
            try:
                acc.fetch_credits()
            except Exception:
                pass
            time.sleep(0.5)

        self.log("巡检完成：Token 保活 %d 个，签到 %d 个，本次新增积分 +%d"
                 % (ka["refreshed"], checkin_count, earned))
        self._save_state()

    def status(self):
        return {
            "enabled": self.enabled,
            "checkin_times": self._fmt_times(self.checkin_times),
            "keepalive_times": self._fmt_times(self.keepalive_times),
            "cron": self.cron,
            "jitter_secs": self.jitter_secs,
            "timezone": time.strftime("%Z %z"),
            "mode": "排程：签到 %s · 保活 %s%s"
                    % (self._fmt_times(self.checkin_times),
                       self._fmt_times(self.keepalive_times),
                       (" · cron %s" % self.cron) if self.cron else ""),
            "mode_cn": "排程：每日签到 %s · Token 保活 %s"
                         % (self._fmt_times(self.checkin_times),
                            self._fmt_times(self.keepalive_times)),
            "mode_intl": "排程：每日签到 %s · Token 保活 %s"
                           % (self._fmt_times(self.checkin_times),
                              self._fmt_times(self.keepalive_times)),
            "last_run_time": self.last_run_time or "尚未运行",
            "next_run_time": self.next_run_time or "待调度",
            "startup_claim_date": self.last_claim_window or "",
            "last_claim_window": self.last_claim_window or "",
            "current_claim_window": self._current_window_key(),
            "last_cycle_date": self.last_cycle_date or "",
            "state_file": self._state_path(),
            "logs": self.logs[-20:],
        }
