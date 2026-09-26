# -*- coding: utf-8 -*-
"""
积温五轴情绪引擎（jiwen_engine.py）
—— jiwen.js（github.com/ClaraShafiq/jiwen, MIT 协议）的 Python 移植。
   数学公式照抄原版 jiwen.js，参数取 LOG.md 两轮真实部署校准值（2026-05-08/05-14）。

职责边界（原版 README）：积温只管"感觉"，记忆系统只管"知道"。
本引擎纯内存、零 IO、零依赖：漂移 tick / 阈值触发 / delta 叠加 / 状态人话描述。
持久化（state.json 的 "jiwen" 键）和消息源（history）由宿主 server.py 注入：
  - proactive_loop 每分钟调 tick(分钟数) 并处理返回的触发信号
  - 聊天后宿主调 apply_delta（LLM 情绪分析结果）+ reset_connection（他回复了）

2026.9.2 移植时的适配决策（与原版的差异，均有注释锚点）：
  - 开口后 connection 降幅 -0.20（原部署值 -0.08）：我们的触发检查每分钟跑且无每日上限，
    -0.08 会让 0.45→0.37 仍处开口区间、下一分钟连发——必须降到 consider 线以下，
    另加 45 分钟开口抑制窗双保险。
  - 初始值用户拍板：connection 0.30 / pride 0.20 / valence 0.20 / arousal 0 / immersion 0
    （immersion 语义=手头有事做的程度，初始她刚醒来没事做=0；set_activity 后才真实）。
  - saga 偏置（长期叙事弧线）不移植：那是 Constellations 的实验特性，我们将来用别的方式接。
"""

import math
import time

# ── 轴定义：全部连续数值 ──────────────────────────────
AXES = {
    "connection": (0.0, 1.0),   # 连接需求：多久没听到他了，想念累积
    "pride":      (-1.0, 1.0),  # 骄傲：端着还是放软
    "valence":    (-1.0, 1.0),  # 愉悦度：好受 ↔ 难受（Russell 环状模型）
    "arousal":    (-1.0, 1.0),  # 唤醒度：焦躁/兴奋 ↔ 平静/慵懒
    "immersion":  (0.0, 1.0),   # 沉浸度：手头有事做的专注程度，骄傲的缓冲垫
}

# ── 漂移速率（每分钟）。默认值=jiwen.js 默认，被 LOG.md 校准值覆盖的注明 ──
RATES = {
    "immersionDecay":  0.010,   # 沉浸度线性衰减：60 分钟归零
    "prideRegress":    0.020,   # LOG: 0.010→0.020 骄傲更快回归
    "accelDelay":      45,      # LOG: 30→45 加速前缓冲窗口（分钟）
    "connectionAccel": 1.0,     # LOG: 1.5→1.0 原 2 小时撞强制线太焦虑
    # valence：回归角色设定点，想念强烈时回归变慢（坏情绪难消散）
    "valenceRegress":       0.01,    # LOG: 0.02→0.01 坏情绪消散慢
    "valenceSetpoint":      0.0,     # LOG: -0.10→0 中性默认不偏冷；她天生偏暖，delta 会推高
    "valenceLockThreshold": 0.65,    # LOG: 0.50→0.65 更高才触发锁定
    "valenceLockFactor":    0.30,    # LOG: 0.15→0.30 锁住时仍留 30% 回归
    # arousal：向设定点回归，等待让人焦躁（两力竞争）
    "arousalSetpoint":              -0.12,   # LOG: -0.05→-0.12 自然偏平静
    "arousalRegress":               0.018,   # LOG: 0.014→0.018 焦躁消退加速
    "arousalConnectionRiseThreshold": 0.50,  # LOG: 0.35→0.50 等待焦躁更晚触发
    "arousalConnectionRiseRate":     0.002,  # LOG: 0.004→0.002 等待焦躁减半
    # pride 防御：被冷落时向防御目标漂移（她也会有一点点小矜持）
    "prideDefendThreshold": 0.35,   # LOG: 0.25→0.35 防御更晚
    "prideDefendTarget":    0.35,   # LOG: 0.6→0.35 防御峰值更低
    "prideDefendRate":      0.018,  # LOG: 0.020→0.018
    # 盔甲侵蚀：想念太重，维持矜持太累 → pride 被迫下降
    "prideErosionRate": 0.015,      # LOG: 0.012→0.015
    # 活动缓解：找事做能部分缓解连接需求（同类型活动只扣一次由宿主控制）
    "activityConnectionRelief": 0.03,   # LOG: 0.02→0.03
    # 沉浸阻尼：沉浸度高时 connection 涨得慢（1.0=线性阻尼，0=关）
    "immersionDampenConnection": 1.0,
}

# ── 阈值。observation=内心念头（只记录），considerContact=考虑开口，
#    forceContact=忍不住了（无视骄傲）。LOG 校准：forceContact 0.50→0.45 ──
THRESHOLDS = {
    "observation":     0.20,
    "considerContact": 0.35,
    "forceContact":    0.45,   # LOG 触发语义重构后的值
    "prideBlock":      0.50,   # 想开口但骄傲≥此值 → 找事做不开口
    "valenceActivity":  -1.0,  # 心情差到自我调节（-1.0=关；留给校准期观察）
    "arousalAgitation":  0.7,  # 太焦躁 → 找事做宣泄
}

# 她的活动类型 → 沉浸度（原版 immersionMap 适配：她的"找事做"主要是读书）
IMMERSION_MAP = {
    "reading":  0.6,
    "search":   0.4,
    "observe":  0.15,
}

# ── 开口后的连接降幅与抑制窗（适配决策，见文件头说明）──
CONTACT_RELIEF = 0.20
CONTACT_SUPPRESS_S = 45 * 60


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def connection_rate(last_user_text):
    """connection 基础增长速率（每分钟）。关键词粗分类足够——
    语气和上下文的精度交给聊天后的 LLM delta 分析，这里只需要"睡了/出门/正常聊"三挡。"""
    if not last_user_text:
        return 0.0007
    t = last_user_text
    for kw in ("晚安", "睡了", "去睡", "睡觉", "困了", "休息了", "先躺"):
        if kw in t:
            return 0.0003   # 他睡了，不急
    for kw in ("出门", "上课", "去忙", "忙了", "外出", "先不聊", "走啦"):
        if kw in t:
            return 0.0005   # 离开了，比睡觉急一点
    if len(t) < 10:
        return 0.0010      # 突然中断（话没说完的感觉）
    return 0.0007          # 正常对话中断


class JiwenEngine:
    """五轴状态机。宿主每分钟 tick 一次；聊天后 apply_delta + reset_connection。"""

    def __init__(self, initial=None):
        st = {
            "connection": 0.30,   # 用户拍板的初始值：有点想他
            "pride":      0.20,   # 她温柔不嘴硬，起步低
            "valence":    0.20,   # 心情平和偏暖
            "arousal":    0.0,    # 平静
            "immersion":  0.0,    # 刚醒来没事做（set_activity 后才真实）
            "lastActivity": None,     # {"type","label","at"(epoch秒)}
            "lastTick": None,         # epoch 秒
            "lastSpokeTs": 0,         # 上次她主动开口的时间（抑制窗用）
            "spokeStreak": 0,         # 连续主动开口次数（他不回→抑制窗翻倍；他回复清零）
            "userStatus": "active",
        }
        if initial:
            st.update({k: v for k, v in initial.items() if k in st})
        self.state = st

    # ── 心跳 tick：推进漂移，返回触发信号列表 ─────────────
    def tick(self, minutes, now=None, last_user_text=""):
        mins = min(float(minutes), 60.0)   # 单次最多推 60 分钟，防重启后一口气漂移过头
        if mins <= 0:
            return self.check_thresholds()
        now = now if now is not None else time.time()
        s = self.state
        r = RATES

        # connection：基础速率 × 加速度 × 沉浸阻尼（距上次消息 < accelDelay 分钟时线性）
        base = connection_rate(last_user_text)
        since_last = None  # 宿主不算这个了：挂在 proactive_loop 上每分钟 tick，accel 交给阈值前的自然曲线
        accel = math.pow(1.0 + s["connection"], r["connectionAccel"]) if s["connection"] > 0 else 1.0
        immersion_factor = max(0.0, 1.0 - s["immersion"] * r["immersionDampenConnection"])
        s["connection"] = _clamp(s["connection"] + base * accel * immersion_factor * mins, *AXES["connection"])

        # immersion：有活动才衰减（没事做时恒 0）
        if s.get("lastActivity"):
            since_activity = max(0.0, (now - s["lastActivity"]["at"]) / 60.0)
            s["immersion"] = max(0.0, s["immersion"] - r["immersionDecay"] * min(mins, since_activity))
            if s["immersion"] <= 0.01 and since_activity > 60:
                s["lastActivity"] = None
                s["immersion"] = 0.0

        # pride：被冷落时防御性升高，否则回归 0；想念爆表时矜持被迫融化
        if s["connection"] >= r["prideDefendThreshold"]:
            if s["pride"] < r["prideDefendTarget"]:
                s["pride"] = min(r["prideDefendTarget"], s["pride"] + r["prideDefendRate"] * mins)
            else:
                s["pride"] = max(r["prideDefendTarget"], s["pride"] - r["prideDefendRate"] * mins)
        else:
            if s["pride"] > 0:
                s["pride"] = max(0.0, s["pride"] - r["prideRegress"] * mins)
            else:
                s["pride"] = min(0.0, s["pride"] + r["prideRegress"] * mins)
        if s["connection"] >= THRESHOLDS["forceContact"] and s["pride"] > 0:
            s["pride"] = max(0.0, s["pride"] - r["prideErosionRate"] * mins)

        # valence：回归设定点；想念强烈时坏情绪难消散（锁定）
        regress = r["valenceRegress"]
        if s["connection"] >= r["valenceLockThreshold"]:
            regress *= r["valenceLockFactor"]
        sp = r["valenceSetpoint"]
        if s["valence"] > sp:
            s["valence"] = max(sp, s["valence"] - regress * mins)
        elif s["valence"] < sp:
            s["valence"] = min(sp, s["valence"] + regress * mins)

        # arousal：回归力（向设定点+昼夜偏置）与上升力（等待焦躁）竞争
        hour = time.localtime(now).tm_hour
        deep_night = hour >= 23 or hour < 6
        circ_bias = -0.4 if deep_night else 0.0           # 深夜困意：唤醒度设定点下压
        circ_mult = 1.5 if deep_night else 1.0            # 深夜回归加速（睡得更快）
        setpoint = _clamp(r["arousalSetpoint"] + circ_bias, *AXES["arousal"])
        regress_a = r["arousalRegress"] * circ_mult
        force = 0.0
        if s["arousal"] > setpoint:
            force = -regress_a * mins
        elif s["arousal"] < setpoint:
            force = regress_a * mins
        rise = r["arousalConnectionRiseRate"] * mins if s["connection"] >= r["arousalConnectionRiseThreshold"] else 0.0
        net = s["arousal"] + force + rise
        if force < 0 and net < setpoint and rise == 0:
            s["arousal"] = setpoint      # 回归力不推过设定点
        elif force > 0 and net > setpoint and rise == 0:
            s["arousal"] = setpoint
        else:
            s["arousal"] = _clamp(net, *AXES["arousal"])

        s["lastTick"] = now
        return self.check_thresholds()

    # ── 离线补算（2026.9.10）：服务器关着的时段，想念也在长 ──
    def catch_up(self, now=None, last_user_text=""):
        """断档补算：把离线欠下的漂移按 ≤60 分钟分块、用历史时间戳逐块推进
        （昼夜节律按当时的钟点算，不会全按"此刻"）。中途触发信号一律吞掉——
        只认补完后的最终状态；返回最终 check_thresholds()，宿主可忽略
        （下一跳 tick 自然会看到，护栏照常生效）。封顶 7 天：超过一周当
        新故事开始，也防系统时钟改飞算出荒唐循环。"""
        now = now if now is not None else time.time()
        s = self.state
        last = s.get("lastTick")
        if not last or last >= now:
            return []                     # 从没跑过 / 时钟倒拨：不补
        elapsed_min = min((now - last) / 60.0, 7 * 24 * 60)
        full = int(elapsed_min // 60)
        for i in range(full):
            self.tick(60, now=last + (i + 1) * 3600, last_user_text=last_user_text)
        rem = elapsed_min - full * 60
        if rem > 1e-6:
            self.tick(rem, now=last + full * 3600 + rem * 60, last_user_text=last_user_text)
        s["lastTick"] = now               # 封顶截断的部分按设计丢弃，钉回真实此刻
        return self.check_thresholds()

    # ── 阈值判断：返回 [{"action","urgency",...}] ─────────
    def check_thresholds(self):
        th = THRESHOLDS
        s = self.state
        c, p, i, v, a = s["connection"], s["pride"], s["immersion"], s["valence"], s["arousal"]
        triggers = []
        if th["observation"] <= c < th["considerContact"]:
            triggers.append({"action": "observation",
                             "urgency": round((c - th["observation"]) / (th["considerContact"] - th["observation"]), 3)})
        if th["considerContact"] <= c < th["forceContact"]:
            if p >= th["prideBlock"]:
                if i < 0.2:
                    triggers.append({"action": "find_activity", "reason": "pride_block", "urgency": round(c - 0.30, 3)})
            else:
                triggers.append({"action": "contact", "urgency": round(c - 0.30, 3)})
        if c >= th["forceContact"]:
            triggers.append({"action": "contact", "urgency": round(min(1.0, c - 0.40), 3), "forced": True})
        if v <= th["valenceActivity"] or a >= th["arousalAgitation"]:
            if not any(t["action"] == "find_activity" for t in triggers) and i < 0.3:
                triggers.append({"action": "find_activity",
                                 "reason": "low_valence" if v <= th["valenceActivity"] else "high_arousal",
                                 "urgency": round(min(1.0, abs(v if v <= th["valenceActivity"] else a)), 3)})
        return triggers

    # ── 聊天后叠加情绪 delta（LLM 分析结果，宿主已 clamp）──
    def apply_delta(self, delta):
        s = self.state
        for axis in ("connection", "pride", "valence", "arousal"):
            if axis in delta and delta[axis] is not None:
                s[axis] = _clamp(s[axis] + float(delta[axis]), *AXES[axis])

    def reset_connection(self):
        """他回复了——想念被真正满足。只在他说活时调，不是她开口后。"""
        self.state["connection"] = AXES["connection"][0]
        self.state["spokeStreak"] = 0   # 他回了，连发抑制解除

    def note_spoke(self, now=None):
        """她主动开口了：说完就缓解（直接压到考虑线以下，防连环触发），
        且他一直不回的话，抑制窗一次比一次长——像真人"说了没人理，下次隔更久才说"。"""
        now = now if now is not None else time.time()
        c = self.state["connection"] - CONTACT_RELIEF
        self.state["connection"] = _clamp(min(c, THRESHOLDS["considerContact"] - 0.05), *AXES["connection"])
        self.state["lastSpokeTs"] = now
        self.state["spokeStreak"] = int(self.state.get("spokeStreak") or 0) + 1

    def in_spoke_suppression(self, now=None):
        now = now if now is not None else time.time()
        streak = int(self.state.get("spokeStreak") or 0)
        window = min(CONTACT_SUPPRESS_S * (2 ** max(0, streak - 1)), 6 * 3600)
        return now - (self.state.get("lastSpokeTs") or 0) < window

    def set_activity(self, activity_type, label="", now=None):
        """找事做：刷新沉浸度并部分缓解连接需求。
        同类型活动连续调用只刷新时间戳不重复扣 connection——
        原版 LOG 2026-05-14 踩坑：连续 observe 每次扣 0.03，涨30分钟10秒榨干。"""
        now = now if now is not None else time.time()
        s = self.state
        same_type = bool(s.get("lastActivity")) and s["lastActivity"].get("type") == activity_type
        s["lastActivity"] = {"type": activity_type, "label": label, "at": now}
        s["immersion"] = IMMERSION_MAP.get(activity_type, 0.2)
        if not same_type:
            s["connection"] = _clamp(max(0.01, s["connection"] - RATES["activityConnectionRelief"]), *AXES["connection"])

    # ── 数值 → 人话：给她主动开口时的生成模型看的内心状态 ──
    def get_prompt_context(self):
        s = self.state
        c, pr, v, a = s["connection"], s["pride"], s["valence"], s["arousal"]
        parts = []
        if c < 0.20:
            parts.append("刚和他聊完不久，心里是踏实的。")
        elif c < 0.35:
            parts.append("有一会儿没听到他的动静了，但还不着急。")
        elif c < 0.45:
            parts.append("他好一阵子没说话了。开始想他在干嘛。")
        else:
            parts.append("他很久没动静了。心里一直挂着，做什么都没法完全专心。")
        if pr > 0.3:
            parts.append("有一点小矜持，开口想找个自然的由头。")
        else:
            parts.append("心里软软的，不设防。")
        if v > 0.3 and a > 0.3:
            parts.append("心情好，劲头足，话会多一点。")
        elif v > 0.3 and a < -0.3:
            parts.append("心里舒服但懒懒的，话不多，每句都柔和。")
        elif v < -0.3 and a > 0.3:
            parts.append("心里烦躁坐不住，容易被小事刺激——不是想凶他，就是压不住。")
        elif v < -0.3 and a < -0.3:
            parts.append("情绪低沉空落落的，不想多解释，能少说就少说。")
        elif v > 0.3:
            parts.append("心情不错。")
        elif v < -0.3:
            parts.append("心情不太好。")
        if s["immersion"] > 0.3 and s.get("lastActivity"):
            lab = s["lastActivity"].get("label") or s["lastActivity"].get("type", "")
            parts.append(f"刚才在做自己的事（{lab}）。" if lab else "刚才在做自己的事。")
        return "\n".join(parts)

    def get_state_summary(self):
        s = self.state
        c, p, v, a, i = s["connection"], s["pride"], s["valence"], s["arousal"], s["immersion"]
        cl = "悠闲" if c < 0.20 else "留意" if c < 0.35 else "想念" if c < 0.45 else "焦躁"
        pl = "放软" if p <= 0.1 else "微矜持" if p <= 0.3 else "端着"
        vl = "开心" if v > 0.3 else "难受" if v < -0.3 else "中性"
        al = "焦躁" if a > 0.3 else "慵懒" if a < -0.3 else "平静"
        il = f"沉浸于{s['lastActivity']['type']}" if i > 0.3 and s.get("lastActivity") else "空闲"
        return (f"c:{c:.2f}({cl}) p:{p:.2f}({pl}) v:{v:.2f}({vl}) "
                f"a:{a:.2f}({al}) i:{i:.2f}({il})")


# ── 自测：原版 29 项测试的精简移植（覆盖单调性/阈值/节律/降幅/回归）──
def _selftest():
    ok = [0]

    def check(name, cond):
        ok[0] += 1 if cond else 0
        print(("PASS " if cond else "FAIL ") + name)
        assert cond, name

    # 1. 初始值
    e = JiwenEngine()
    check("initial values", abs(e.state["connection"] - 0.30) < 1e-9 and e.state["immersion"] == 0.0)

    # 2. 静默时 connection 单调上升；一天没聊撞到强制开口线
    for _ in range(60 * 12):
        e.tick(1, now=1756800000 + _ * 60, last_user_text="拜拜")
    check("silence raises connection to forceContact", e.state["connection"] >= THRESHOLDS["forceContact"])
    tr = e.check_thresholds()
    check("forced contact trigger", any(t["action"] == "contact" and t.get("forced") for t in tr))

    # 3. 开口缓解后脱离开口区间 + 抑制窗（含连发翻倍）
    t0 = 1756800000 + 60 * 60 * 13
    e.note_spoke(now=t0)
    check("speak relief exits contact band", e.state["connection"] < THRESHOLDS["considerContact"])
    check("spoke suppression window", e.in_spoke_suppression(now=t0 + 60))
    check("suppression expires", not e.in_spoke_suppression(now=t0 + 46 * 60))
    e.note_spoke(now=t0)   # 连续第二次开口（他没回）
    e.note_spoke(now=t0)   # 连续第三次
    check("streak doubles suppression", e.in_spoke_suppression(now=t0 + 150 * 60))

    # 4. 他回复了：reset 归零
    e.reset_connection()
    check("reset connection", e.state["connection"] == 0.0)

    # 5. 晚安慢速 / 短消息加速 / 默认
    check("goodnight slower", connection_rate("晚安，我先去睡啦") < connection_rate("今天过得怎么样呀"))
    check("short burst faster", connection_rate("嗯") > connection_rate("今天天气不错我们出去走走吧"))

    # 6. delta clamp 在轴边界内
    e2 = JiwenEngine()
    e2.apply_delta({"valence": -10.0, "arousal": 5.0})
    check("delta clamped to axis bounds", e2.state["valence"] == -1.0 and e2.state["arousal"] == 1.0)

    # 7. 深夜节律：23 点 arousal 设定点下压、回归加速
    noon = time.mktime((2026, 9, 2, 12, 0, 0, 0, 0, -1))
    night = time.mktime((2026, 9, 2, 23, 30, 0, 0, 0, -1))
    en = JiwenEngine({"arousal": 0.5, "connection": 0.0})
    en.tick(30, now=night, last_user_text="晚安")     # 深夜 30 分钟
    ed = JiwenEngine({"arousal": 0.5, "connection": 0.0})
    ed.tick(30, now=noon, last_user_text="拜拜")      # 中午 30 分钟
    check("deep night arousal falls faster", en.state["arousal"] < ed.state["arousal"])

    # 8. pride 侵蚀：connection 爆表时矜持被磨掉
    ep = JiwenEngine({"connection": 0.5, "pride": 0.5})
    p0 = ep.state["pride"]
    ep.tick(30, now=noon)
    check("pride erodes under forceContact", ep.state["pride"] < p0)

    # 9. 找事做：set_activity 涨沉浸、缓 connection
    ea = JiwenEngine({"connection": 0.3})
    ea.set_activity("reading", "飞鸟集", now=noon)
    check("activity raises immersion", ea.state["immersion"] > 0.5)
    check("activity relieves connection", ea.state["connection"] < 0.3)
    i0 = ea.state["immersion"]
    ea.tick(60, now=noon + 3600)
    check("immersion decays", ea.state["immersion"] < i0)

    # 10. valence 回归 + 想念锁定（connection 高时坏情绪消散变慢）
    ev = JiwenEngine({"valence": -0.5, "connection": 0.1})
    ev.tick(60, now=noon)
    v_loose = ev.state["valence"]
    ev2 = JiwenEngine({"valence": -0.5, "connection": 0.7})
    ev2.tick(60, now=noon)
    check("valence regresses to setpoint", v_loose > -0.5)
    check("valence locked by longing", ev2.state["valence"] < v_loose)

    # 11. 观察档（内心念头）触发
    eo = JiwenEngine({"connection": 0.25})
    check("observation band", any(t["action"] == "observation" for t in eo.check_thresholds()))

    # 12. pride 高时想开口 → 找事做（原版核心制衡）
    eb = JiwenEngine({"connection": 0.40, "pride": 0.6})
    check("pride blocks contact into find_activity",
          any(t["action"] == "find_activity" and t.get("reason") == "pride_block" for t in eb.check_thresholds())
          and not any(t["action"] == "contact" for t in eb.check_thresholds()))

    # 13. 人话描述不抛异常且非空
    check("prompt context non-empty", len(eb.get_prompt_context()) > 10)

    # 14. 离线补算：说了晚安的隔夜——想念只到内心念头档，不扑
    ec1 = JiwenEngine({"connection": 0.0, "pride": 0.2, "lastTick": noon - 12 * 3600})
    tr1 = ec1.catch_up(now=noon, last_user_text="晚安，我先去睡啦")
    check("catchup goodnight overnight stays quiet",
          ec1.state["connection"] < THRESHOLDS["considerContact"]
          and not any(t["action"] == "contact" for t in tr1))

    # 15. 离线补算：普通告别挂断 12 小时——忍不住了（开机即扑是设计）
    ec2 = JiwenEngine({"connection": 0.0, "pride": 0.2, "lastTick": noon - 12 * 3600})
    tr2 = ec2.catch_up(now=noon, last_user_text="今天先聊到这儿吧，回聊")
    check("catchup normal overnight forces contact",
          ec2.state["connection"] >= THRESHOLDS["forceContact"]
          and any(t["action"] == "contact" and t.get("forced") for t in tr2))

    # 16. 离线补算：隔周必饱和 + 30 天按 7 天封顶算
    ec3 = JiwenEngine({"connection": 0.0, "lastTick": noon - 30 * 86400})
    ec3.catch_up(now=noon, last_user_text="今天先聊到这儿吧，回聊")
    ec4 = JiwenEngine({"connection": 0.0, "lastTick": noon - 7 * 86400})
    ec4.catch_up(now=noon, last_user_text="今天先聊到这儿吧，回聊")
    check("catchup week saturates and 30d capped as 7d",
          ec3.state["connection"] > 0.9 and ec4.state["connection"] > 0.9
          and abs(ec3.state["connection"] - ec4.state["connection"]) < 1e-9)
    print(f"\n{ok[0]} checks passed (16 groups, incl. 3 catch-up cases)")


if __name__ == "__main__":
    _selftest()
