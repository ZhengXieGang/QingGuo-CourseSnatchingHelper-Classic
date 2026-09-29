#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
青果选课平台自动驱动脚本（配合同目录 app.py 使用，只操作本地 127.0.0.1 接口）

设计原则：学校服务器被挤爆是常态，脚本不做"试几次就放弃"。
只要还在选课时段内，就一轮一轮地重试下去，直到选上或到你的总时限。

用法示例:
  python3 qk_auto.py probe                       # 只登录+拉课程列表，打印候选（不选课）
  python3 qk_auto.py run --name 高等数学         # 持久抢课：失败就重来，直到成功或到 --max-total
  python3 qk_auto.py run --name 高等数学 --dry-run    # 走完流程但最后不启动抢课
  python3 qk_auto.py status                      # 查看状态
  python3 qk_auto.py stop                        # 停止抢课
"""
import argparse
import html
import json
import os
import re
import subprocess
import sys
import time

import tempfile

import requests

BASE = os.environ.get("QK_BASE", "http://127.0.0.1:5087")
HERE = os.path.dirname(os.path.abspath(__file__))
APP_PY = os.path.join(HERE, "app.py")
XQ = os.environ.get("QK_SEL_XQ", "3")  # 校区筛选值（取自教务系统页面下拉）

# 风控很敏感（阈值约 5 次请求 / 20 秒）。列表轮询一次 = 2 个学校请求，
# 所以 15 秒是最快的安全节奏；抢课循环间隔默认 12 秒（每次迭代 2 个请求）。
# 临时产物（课程/班级列表、日志）的落盘目录
WORKDIR = os.environ.get("QK_WORKDIR") or os.path.join(tempfile.gettempdir(), "qk")

POLL_LIST_SEC = 15.0
POLL_LOG_SEC = 3.0
# 拿不到列表 / 拉不到班级时的重试退避（服务器拥堵时别把节奏压得太紧）
RETRY_BACKOFF_SEC = 30.0
RETRY_BACKOFF_MAX = 300.0


class FatalSetupError(Exception):
    """配置或歧义类问题：重试没有意义，直接结束并交给人工处理。"""


class Backoff:
    """指数退避：连续失败越多次，重试间隔越长（上限 RETRY_BACKOFF_MAX 秒）。

    固定短间隔反复重试，在服务器本就吃力时会雪上加霜，也更容易触发防刷锁定
    （一次重新登录就是好几个请求）。有进展时调用 reset() 回到基准间隔。
    """

    def __init__(self, base=RETRY_BACKOFF_SEC, cap=RETRY_BACKOFF_MAX):
        self.base, self.cap, self.cur = base, cap, base

    def reset(self):
        self.cur = self.base

    def sleep(self, reason=""):
        log(f"退避 {self.cur:.0f}s 后重试" + (f"（{reason}）" if reason else ""))
        time.sleep(self.cur)
        self.cur = min(self.cap, self.cur * 2)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_creds():
    """从 app.py 的 PRESET_ACCOUNTS 读账号，避免密码出现两处。"""
    src = open(APP_PY, encoding="utf-8").read()
    m = re.search(r'PRESET_ACCOUNTS\s*=\s*\[\s*\{(.*?)\}', src, re.S)
    if not m:
        raise SystemExit("未能在 app.py 中找到 PRESET_ACCOUNTS")
    body = m.group(1)
    u = re.search(r'"username"\s*:\s*"([^"]+)"', body)
    p = re.search(r'"password"\s*:\s*"([^"]+)"', body)
    if not (u and p):
        raise SystemExit("PRESET_ACCOUNTS 解析失败")
    return u.group(1), p.group(1)


def api(path, payload=None, timeout=180):
    url = BASE + path
    r = requests.post(url, json=payload or {}, timeout=timeout) if payload is not None \
        else requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def dump(obj, name):
    os.makedirs(WORKDIR, exist_ok=True)
    path = os.path.join(WORKDIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    log(f"已保存 {path}")
    return path


def ensure_server(restart=True):
    """确认本地工具在跑；不通则尝试自己拉起来。返回 True/False。"""
    for attempt in (1, 2):
        try:
            api("/api/state", timeout=10)
            return True
        except Exception as e:
            log(f"本地工具无响应（第{attempt}次）: {type(e).__name__}")
            if attempt == 1:
                time.sleep(3)
    if not restart:
        return False
    log("尝试重新启动本地工具 app.py ...")
    try:
        env = dict(os.environ, BROWSER="/bin/true", QK_BIND_HOST="127.0.0.1", QK_BIND_PORT="5087")
        subprocess.Popen([sys.executable, APP_PY], cwd=HERE, env=env,
                         stdout=open(os.path.join(WORKDIR, "server.log"), "a"), stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        log(f"启动失败: {e}")
        return False
    for _ in range(10):
        time.sleep(2)
        try:
            api("/api/state", timeout=10)
            log("本地工具已重新启动")
            return True
        except Exception:
            pass
    log("本地工具仍无响应")
    return False


def do_login():
    u, p = load_creds()
    try:
        d = api("/api/login", {"username": u, "password": p})
    except Exception as e:
        log(f"登录请求异常: {e}")
        return False
    log(f"登录 {u}: {d.get('msg')}")
    return bool(d.get("ok"))


def list_courses_once():
    return api("/api/courses", {"sel_xq": XQ})


def wait_for_courses(deadline):
    """等到课程列表可用（学校开放前返回 not_started）。返回 courses 或 None。"""
    attempt = 0
    backoff = POLL_LIST_SEC
    while time.time() < deadline:
        attempt += 1
        try:
            d = list_courses_once()
        except Exception as e:
            log(f"[{attempt}] 拉取课程异常: {e}；{backoff:.0f}s 后重试")
            time.sleep(backoff)
            continue
        st = d.get("status")
        if st == "ok":
            cs = d.get("courses", [])
            log(f"[{attempt}] 课程列表已可用，共 {len(cs)} 门")
            if cs:
                return cs
            log("列表为空（服务器可能只返回了半截页面），继续重试")
        else:
            log(f"[{attempt}] 列表未就绪 status={st} msg={d.get('msg', '')}")
        time.sleep(backoff)
    return None


def find_course(courses, name):
    """
    定位目标课程。返回课程 dict 或 None（None = 本轮先不干活，外层退避后重试）。
    命中多门可选课程时抛 FatalSetupError：宁可不抢，也不能猜着抢错课。
    """
    hits = [c for c in courses if (c.get("name") or "").strip() == name] \
        or [c for c in courses if name in (c.get("name") or "")]
    if not hits:
        sample = "、".join((c.get("name") or "?") for c in courses[:12])
        log(f"列表里没有《{name}》。本次列表前 12 门：{sample}")
        return None
    avail = [h for h in hits if not h.get("disabled")]
    if not avail:
        log(f"《{name}》在列表中但不可选（disabled）——若确实是本学期已选上，则无需再抢")
        return None
    if len(avail) > 1:
        lines = "\n".join(
            f'    {i}. {h.get("name")}  [类别={h.get("category")} 学分={h.get("credit")} '
            f'教师={h.get("teacher") or "-"}]' for i, h in enumerate(avail))
        raise FatalSetupError(
            f"《{name}》匹配到 {len(avail)} 门可选课程，无法判断要抢哪一门，"
            f"已停止（不猜着选）。请用更完整、唯一的课程名重跑：\n{lines}")
    return avail[0]


def fetch_classes(course):
    """返回 (classes, rate_limit_wait_sec)；失败返回 (None, 0)。"""
    # 班级弹窗（stu_xszx_chooseskbj.aspx）要的是页面上"选择"链接里的 id 参数，
    # 即 look_value（形如 <学年>|0|<课程代码>|0|<类型>|||<班级序号>）；复选框的 value 是另一回事。
    value = course.get("look_value") or course.get("value")
    try:
        d = api("/api/classes", {"value": value, "skbjval": course.get("existing_class") or "", "xq": XQ})
    except Exception as e:
        log(f"拉班级请求异常: {e}")
        return None, 0
    if d.get("rate_limited"):
        wait_s = int(d.get("wait_minutes") or 2) * 60
        log(f"拉班级触发风控：{d.get('msg')}，等 {wait_s}s")
        return None, wait_s
    if not d.get("ok"):
        log(f"拉班级失败: {d.get('msg')}")
        return None, 0
    classes = d.get("classes") or []
    return (classes or None), 0


def build_target(course, cls):
    return {
        "course_code": course.get("code"),
        "course_name": course.get("name"),
        "class_id": cls.get("class_id"),
        "class_name": f'{cls.get("class_id")} {cls.get("teacher") or ""}'.strip(),
        "radio_value": cls.get("radio_value") or "",
        "class_page_value": cls.get("course_value") or course.get("look_value") or course.get("value"),
        "full_course_value": course.get("value"),
        "class_skbjval": cls.get("skbjval") or course.get("existing_class") or "",
        "alt_class_id": cls.get("alt_class_id") or "",
        "xq": cls.get("xq") or XQ,
    }


def already_selected(class_id):
    """独立复核：查退选报表页，看该班级是否已在已选列表里。"""
    try:
        d = api("/api/verify", {"class_id": class_id})
        return bool(d.get("ok")), d.get("msg")
    except Exception as e:
        return False, f"verify 异常: {e}"


def watch(deadline):
    """监视抢课循环。返回 True=成功, False=循环已停但未成功, None=超时仍在跑。"""
    # 从当前日志末尾开始跟，避免把工具启动以来的历史日志整个重放一遍
    since = 0
    try:
        existing = api("/api/logs?since=0")
        if existing:
            since = max(e.get("id", 0) for e in existing)
    except Exception:
        pass
    while time.time() < deadline:
        try:
            for e in api(f"/api/logs?since={since}"):
                since = max(since, e.get("id", 0))
                log(f"  {e.get('level', '')} {strip_tags(e.get('msg', ''))}")
            s = api("/api/state")
        except Exception as e:
            log(f"监视异常: {e}")
            time.sleep(POLL_LOG_SEC)
            continue
        if s.get("snatch_success"):
            log(f"★ 选课成功: {s.get('snatch_result')}")
            return True
        if not s.get("snatch_running"):
            log(f"抢课循环已停止（未成功）。phase={s.get('snatch_phase')}")
            return False
        time.sleep(POLL_LOG_SEC)
    return None


def cmd_probe(args):
    if not ensure_server():
        return 1
    if not do_login():
        return 1
    d = list_courses_once()
    log(f"status={d.get('status')}")
    cs = d.get("courses", [])
    if cs:
        dump(cs, "probe_courses.json")
        log(f"共 {len(cs)} 门课程；前 40 门：")
        for c in cs[:40]:
            log(f'  {c["name"]}  类别={c.get("category")} 学分={c.get("credit")} '
                f'disabled={c.get("disabled")} value={c.get("value")}')
    return 0


def cmd_status(args):
    s = api("/api/state")
    for k in ("logged_in", "username", "snatch_running", "snatch_success", "snatch_result",
              "snatch_phase", "snatch_interval", "target_capacity_live", "req_count_20s", "target"):
        log(f"{k} = {s.get(k)}")
    return 0


def cmd_stop(args):
    log(api("/api/snatch/stop", {}))
    return 0


def cmd_run(args):
    deadline = time.time() + args.max_total
    log(f"总时限 {args.max_total}s（到 {time.strftime('%H:%M:%S', time.localtime(deadline))}）；"
        f"目标《{args.name}》；抢课间隔 {args.interval}s")

    round_no = 0
    bo = Backoff()
    while time.time() < deadline:
        round_no += 1
        log(f"===== 第 {round_no} 轮 =====")

        if not ensure_server():
            bo.sleep("本地工具起不来")
            continue

        if not do_login():
            bo.sleep("登录失败，服务器可能正忙")
            continue

        if not args.no_stop_existing:
            try:
                if api("/api/state").get("snatch_running"):
                    log("检测到已有抢课任务在跑，先停止")
                    api("/api/snatch/stop", {})
                    time.sleep(1)
            except Exception:
                pass

        # --- 1. 等列表可用 ---
        courses = wait_for_courses(min(deadline, time.time() + args.wait_list))
        if courses is None:
            log("本轮仍未拿到课程列表，重新开始下一轮")
            bo.sleep("列表始终不可用")
            continue
        dump(courses, "courses.json")
        bo.reset()          # 能拿到列表就算有进展，重置退避

        # --- 2. 定位目标课程 ---
        try:
            course = find_course(courses, args.name)
        except FatalSetupError as e:
            log(f"致命问题，已停止：{e}")
            return 3
        if course is None:
            bo.sleep("本轮定位不到目标课程（列表可能不完整）")
            continue
        log("目标课程: " + json.dumps(course, ensure_ascii=False))

        # --- 3. 拉班级 ---
        classes, rate_wait = fetch_classes(course)
        while classes is None and time.time() < deadline:
            if rate_wait:
                time.sleep(rate_wait)
            else:
                bo.sleep("拉不到班级列表")
            classes, rate_wait = fetch_classes(course)
        if classes is None:
            log("始终拉不到班级列表，重新开始下一轮")
            continue
        dump(classes, "classes.json")
        for c in classes:
            log(f'  班级 class_id={c.get("class_id")} 教师={c.get("teacher")} '
                f'人数={c.get("capacity")!r} radio={c.get("radio_value")!r}')
        cls = classes[0]

        # --- 4. 设置目标并启动 ---
        target = build_target(course, cls)
        log("选课目标: " + json.dumps(target, ensure_ascii=False))
        try:
            api("/api/target", {"target": target, "sel_xq": XQ, "interval": args.interval,
                               "verify_after": True, "measure_business_latency": False,
                               "dry_run": bool(args.dry_run)})
        except Exception as e:
            bo.sleep(f"设置目标失败: {e}")
            continue

        try:
            log("启动抢课: " + json.dumps(api("/api/snatch/start", {}), ensure_ascii=False))
        except Exception as e:
            bo.sleep(f"启动抢课失败: {e}")
            continue

        if args.dry_run:
            # 演练模式由工具侧实现：走完 查询 → 班级弹窗 → 构造 id 的完整链路，
            # 在真正提交前停下并打印将发送的表单字段。
            watch(min(deadline, time.time() + 300))
            log("演练结束：未发送任何选课提交请求。"
                f"完整表单见 {WORKDIR}/run.log，也可 GET /api/submit_history 查看。")
            return 0

        state = watch(min(deadline, time.time() + args.watch))
        if state is True:
            ok, msg = already_selected(cls.get("class_id"))
            log(f"独立复核: ok={ok} msg={msg}")
            log(f"成功。course={course.get('name')} class={cls.get('class_id')} "
                f"teacher={cls.get('teacher')} schedule={cls.get('schedule')}")
            return 0 if ok else 0  # 循环已报成功即视为达成，复核结果如实打印
        if state is None:
            log("本轮监视超时，但循环可能仍在运行；继续监视/等待总时限")
            continue
        # state is False：循环停了但没成功
        bo.sleep("抢课循环停止且未成功，准备重新登录后重来")

    log("已到总时限仍未确认成功（选课时段可能已结束）")
    return 2


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe"); p.set_defaults(func=cmd_probe)
    p = sub.add_parser("status"); p.set_defaults(func=cmd_status)
    p = sub.add_parser("stop"); p.set_defaults(func=cmd_stop)

    r = sub.add_parser("run")
    r.add_argument("--name", required=True,
                   help="要抢的课程名（支持部分匹配；匹配到多门会拒绝执行，不会瞎猜）")
    r.add_argument("--interval", type=float, default=12.0,
                   help="抢课循环间隔秒（每次迭代=2 个学校请求，建议 >=12 以避开风控）")
    r.add_argument("--max-total", type=int, default=25200,
                   help="整个流程的总时限秒，默认 7 小时（覆盖 10:00-17:00 选课时段）")
    r.add_argument("--wait-list", type=int, default=1800,
                   help="单轮等课程列表开放的最长秒数，默认 30 分钟")
    r.add_argument("--watch", type=int, default=3600,
                   help="单次抢课循环的监视秒数，默认 1 小时；未成功会自动进入下一轮")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--no-stop-existing", action="store_true")
    r.set_defaults(func=cmd_run)

    args = ap.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
