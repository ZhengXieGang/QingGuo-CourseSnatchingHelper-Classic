from flask import Flask, request, Response, redirect, render_template_string, jsonify
import requests as _requests
import hashlib
import os
import urllib.parse
import urllib3
import time
import re
import threading
from collections import OrderedDict, deque
from bs4 import BeautifulSoup

urllib3.disable_warnings()

# 预设账号密码（可配置多个）
PRESET_ACCOUNTS = [
    {"username": "", "password": ""},
    # 添加更多账号...
]

# ==========================================
#              常量配置
# ==========================================

# 学校基本信息 (提取自传统版)
SCHOOL_CODE = "11451"
SCHOOL_HOST = "https://jw.example.edu.cn"
JWWEB_BASE = "https://jw.example.edu.cn/jwweb"

# 教务系统URL路径
URL_DEFAULT = "/Default.aspx"
URL_LOGIN_HOME = "/_data/home_login.aspx"
URL_VALIDATE_CODE = "/sys/ValidateCode.ashx"
URL_MAIN_FRAME = "/MAINFRM.aspx"
URL_LOGOUT = "/sys/Logout.aspx"
URL_MAIN_TOOLS = "/SYS/Main_tools.aspx"

# 选课相关URL路径
URL_COURSE_SELECT = "/wsxk/stu_xszx.aspx"
URL_COURSE_REPORT = "/wsxk/stu_xszx_rpt.aspx"
URL_CLASS_CHOOSE = "/wsxk/stu_xszx_chooseskbj.aspx"
URL_WITHDRAW_RESULT = "/wsxk/stu_txjg_rpt.aspx"

# 登录字段名
# 提示: 探测到以下潜在隐藏字段 (若自动识别失败可尝试): pcInfo, typeName, dsdsdsdsdxcxdfgfg, fgfggfdgtyuuyyuuckjg, txt_mm_expression, txt_mm_length, txt_mm_userzh, txt_mm_lxpd
LOGIN_FIELD_VIEWSTATE = "__VIEWSTATE"
LOGIN_FIELD_EVENTVALIDATION = "__EVENTVALIDATION"
LOGIN_FIELD_PCINFO = "pcInfo"
LOGIN_FIELD_USERNAME = "txt_asmcdefsddsd"
LOGIN_FIELD_PASSWORD = "txt_pewerwedsdfsdff"
LOGIN_FIELD_USERTYPE = "typeName"
# ===========================================================================


# User Agent
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")



# ==========================================
#             延迟与频率限制配置
# ==========================================
# 网络请求超时时长 (连接超时时长, 读取超时时长)
REQ_TIMEOUT = (30.0, 60.0)

# 前端防刷限制时间集中管理 (单位：毫秒)
DELAY_COURSE_REFRESH_MS = 5000  # 刷新课程列表冷却时间
DELAY_CLASS_FETCH_MS = 4000     # 拉取班级详情冷却时间
POLL_LOGS_MS = 2000             # 日志轮询间隔
POLL_STATE_MS = 1000             # 状态轮询间隔
POLL_PING_MS = 30000            # 延迟检测轮询间隔

# ==========================================
#             关键词和字段配置
# ==========================================
# 登录相关关键词
LOGIN_SUCCESS_KEYWORDS = ['frmbody', 'Main_banner', 'MAINFRM']
LOGIN_FAIL_KEYWORDS = ['不正确', '不存在', '密码', '错误', '失败', '已锁定', '冻结']
SESSION_INVALID_KEYWORDS = ['重新', '无权', 'login_home']

# 选课相关关键词
SNATCH_SUCCESS_KEYWORDS = ['正选成功', '选课成功', '操作成功', '已完成', '成功', '重复', '已选']
SNATCH_FAIL_KEYWORDS = ['人数已满', '已满', '冲突', '失败', '不允许', '尚未开始', '非正选时间',
                        '无权访问', '超出', '错误', '超过5次', '锁定', '等待', '出错']
RATE_LIMIT_KEYWORDS = ['刷新频率超过', '已经被锁定', '频率超过', '等待']
NOT_STARTED_KEYWORDS = ['尚未开始', '非正选时间']
SNATCH_STOP_KEYWORDS = ['重复', '无权', '冲突', '出错']



# ==========================================
#              Flask
# ==========================================
app = Flask(__name__)
app.secret_key = "secure-qk-v6-2026-dynamic-static-key"

# 屏蔽内部轮询路由的日志输出，避免命令行刷屏
import logging
class _QuietFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        # 屏蔽高频的内部轮询 GET 请求日志
        if any(p in msg for p in ['/api/state', '/api/log', '/api/ping']):
            return False
        return True
logging.getLogger('werkzeug').addFilter(_QuietFilter())

# ==========================================
#           帮助函数
# ==========================================
def build_url(path):
    """构建完整的教务系统 URL"""
    return f"{JWWEB_BASE}{path}"

def build_request_headers(referer=None, extra_headers=None):
    """为单次请求构造 headers，避免并发修改全局 Session headers。"""
    headers = {}
    if referer:
        if referer.startswith(("http://", "https://")):
            headers["Referer"] = referer
        else:
            headers["Referer"] = build_url(referer)
    if extra_headers:
        headers.update(extra_headers)
    return headers or None

def record_latency(ms, kind="request"):
    """记录延迟
    Args:
        ms: 延迟毫秒数
        kind: 延迟类型 - "network"(纯网络延迟) 或 "request"(业务请求耗时)
    """
    with state_lock:
        if kind == "network":
            app_state["network_latency"] = ms
            app_state["last_latency"] = ms
        else:
            app_state["last_latency"] = ms

def should_measure_business_latency():
    with state_lock:
        return bool(app_state.get("measure_business_latency", False))


def latency_suffix(ms):
    if ms is None or ms < 0:
        return ""
    return f" <span class='latency-note'>({ms}ms)</span>"


# ==========================================
#           共享 Session
# ==========================================
DEFAULT_SESSION_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
session_lock = threading.RLock()


def create_session():
    session = _requests.Session()
    adapter = _requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=1)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.verify = False
    session.headers.update(DEFAULT_SESSION_HEADERS)
    return session


SESSION = create_session()


def session_request(method, url, **kwargs):
    with session_lock:
        return SESSION.request(method, url, **kwargs)


def session_get(url, **kwargs):
    return session_request("GET", url, **kwargs)


def session_post(url, **kwargs):
    return session_request("POST", url, **kwargs)


def session_head(url, **kwargs):
    return session_request("HEAD", url, **kwargs)


def reset_session():
    global SESSION
    with session_lock:
        old_session = SESSION
        SESSION = create_session()
    old_session.close()

# ==========================================
#         全局状态
# ==========================================
state_lock = threading.Lock()
app_state = {
    "logged_in": False,
    "username": "",
    "session_id": "",
    # 抢课状态
    "snatch_running": False,
    "snatch_success": False,
    "snatch_result": "",
    # 选课目标（用户从列表中勾选后设置）
    "target": None,  # {"course_code","course_name","class_id","class_name","chk_value"}
    "filter_params": {},  # 动态筛选参数
    "interval": 4,
    "verify_after": True,  # 选课后是否验证
    "last_latency": -1,    # 最后一次业务请求耗时 (ms)
    "measure_business_latency": False,  # 是否测量并展示业务请求耗时
    "network_latency": -1,  # 纯网络延迟 (ms) - 通过轻量级请求测量
    "session_expire_time": 0,  # Session 过期时间戳（每次请求学校时重置为 now + 20min）
    "snatch_phase": "idle",     # idle / waiting / requesting / request_done / verifying
    "snatch_interval": 0,       # 当前设置的额外等待间隔(秒)
    "snatch_phase_start": 0,    # 当前阶段开始的 time.time()
    "last_class_fetch_ms": -1,   # 最近一次拉取班级人数/班级列表耗时(ms)
    "snatch_request_ms": -1,    # 最近一次抢课提交请求耗时(ms)
    "req_history": [],          # 滑动窗口内的请求时间戳历史
    "manual_logout": False,     # 是否由用户主动挂断
    "last_request_time": 0,     # 最后一次向学校发送请求的时间戳（防风控用）
    "target_capacity_live": "",  # 抢课过程中实时更新的目标班级人数（用于前端无感更新）
    "rate_limit_active": False,
    "rate_limit_until": 0,
}

# 日志
log_queue = deque(maxlen=500)
log_id_counter = [0]


def format_net_err(e: Exception) -> str:
    err_str = str(e)
    if "10051" in err_str or "unreachable" in err_str:
        return "网络已断开，或系统服务器不可达"
    if "10060" in err_str or "Timeout" in err_str or "timeout" in err_str or "Read timed out" in err_str:
        return "连接教务系统超时。服务器目前极度拥堵或您的网络信号差"
    if "ConnectionError" in err_str or "Max retries exceeded" in err_str or "10054" in err_str:
        return "连接教务系统失败或被服务器防火墙切断 (Connection Failed)"
    return err_str

def _track_activity(count_req=False):
    """统一更新 session 过期时间戳。若 count_req=True 则递增请求历史记录。"""
    with state_lock:
        now = time.time()
        app_state["session_expire_time"] = now + SESSION_TTL_SECONDS
        app_state["last_request_time"] = now  # 记录最后一次请求时间（防风控用）
        if count_req:
            app_state["req_history"].append(now)
            # 清理 20 秒以外的记录
            app_state["req_history"] = [t for t in app_state["req_history"] if now - t <= 20]


LOG_ICON_MAP = {
    '::network::': '<svg t="1772774152585" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="22326" data-darkreader-inline-fill=""  ><path d="M640 213.333333h85.333333a85.333333 85.333333 0 0 1 85.333334 85.333334v348.586666a128.042667 128.042667 0 1 1-85.333334 0V298.666667h-85.333333v128l-192-170.666667L640 85.333333v128zM213.333333 376.746667a128.042667 128.042667 0 1 1 85.333334 0v270.506666a128.042667 128.042667 0 1 1-85.333334 0V376.746667z" p-id="22327"></path></svg>',
    '::auth::': '<svg t="1772773285700" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="5630" data-darkreader-inline-fill=""  ><path d="M128 469.312h408.96L387.712 320 448 259.648 700.352 512 448 764.352 387.648 704l149.376-149.312H128V469.312zM597.312 832h213.376V192H597.312V106.688H896v810.624H597.312V832z" p-id="5631"></path></svg>',
    '::bye::': '<svg t="1772773408400" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="7570" data-darkreader-inline-fill=""  ><path d="M316.309333 175.424a42.666667 42.666667 0 1 1 39.189334 75.818667A341.248 341.248 0 0 0 170.666667 554.666667c0 188.522667 152.810667 341.333333 341.333333 341.333333s341.333333-152.810667 341.333333-341.333333a341.248 341.248 0 0 0-184.832-303.424 42.666667 42.666667 0 1 1 39.189334-75.818667A426.581333 426.581333 0 0 1 938.666667 554.666667c0 235.648-191.018667 426.666667-426.666667 426.666666S85.333333 790.314667 85.333333 554.666667c0-161.28 90.282667-306.496 230.976-379.242667zM469.333333 85.333333a42.666667 42.666667 0 1 1 85.333334 0v426.666667a42.666667 42.666667 0 1 1-85.333334 0V85.333333z" fill="" p-id="7571"></path></svg>',
    '::teacher::': '<svg t="1772773474330" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="8987" data-darkreader-inline-fill=""  ><path d="M128 640a42.666667 42.666667 0 0 1 0-85.333333h768a42.666667 42.666667 0 0 1 0 85.333333h-42.666667v213.333333a85.333333 85.333333 0 0 1-85.333333 85.333334H256a85.333333 85.333333 0 0 1-85.333333-85.333334v-213.333333H128z m724.266667-276.266667a42.666667 42.666667 0 0 1 0 60.330667L764.373333 512H316.416a213.376 213.376 0 0 1 367.829333-40.533333l107.690667-107.690667a42.666667 42.666667 0 0 1 60.330667 0zM512 85.333333a128 128 0 1 1 0 256 128 128 0 0 1 0-256z" fill="currentColor" p-id="8988"  data-darkreader-inline-fill=""></path></svg>',
    '::schedule::': '<svg t="1772773595295" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="9985" data-darkreader-inline-fill=""  ><path d="M810.666667 128h-42.666667V42.666667h-85.333333v85.333333H341.333333V42.666667h-85.333333v85.333333h-42.666667c-47.146667 0-84.906667 38.186667-84.906666 85.333333L128 810.666667c0 47.146667 38.186667 85.333333 85.333333 85.333333h597.333334c47.146667 0 85.333333-38.186667 85.333333-85.333333V213.333333c0-47.146667-38.186667-85.333333-85.333333-85.333333z m0 682.666667H213.333333V341.333333h597.333334v469.333334zM298.666667 426.666667h213.333333v213.333333H298.666667z" fill="currentColor" p-id="9986"  data-darkreader-inline-fill=""></path></svg>',
    '::capacity::': '<svg t="1772773659430" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="11105" data-darkreader-inline-fill=""  ><path d="M682.666667 469.333333c70.613333 0 127.573333-57.386667 127.573333-128s-56.96-128-127.573333-128c-70.613333 0-128 57.386667-128 128s57.386667 128 128 128z m-341.333334 0c70.613333 0 127.573333-57.386667 127.573334-128s-56.96-128-127.573334-128c-70.613333 0-128 57.386667-128 128s57.386667 128 128 128z m0 85.333334c-99.626667 0-298.666667 49.92-298.666666 149.333333v106.666667h597.333333v-106.666667c0-99.413333-199.04-149.333333-298.666667-149.333333z m341.333334 0c-12.373333 0-26.24 0.853333-41.173334 2.346666C690.986667 592.64 725.333333 640.64 725.333333 704v106.666667h256v-106.666667c0-99.413333-199.04-149.333333-298.666666-149.333333z" p-id="11106"></path></svg>',
    '::course::': '<svg t="1772773786831" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="12079" data-darkreader-inline-fill=""  ><path d="M292.571429 358.144a18.285714 18.285714 0 0 0 31.232 12.909714l65.536-65.536a18.285714 18.285714 0 0 1 25.892571 0l65.536 65.536A18.285714 18.285714 0 0 0 512 358.144V73.142857h329.142857c40.228571 0 73.142857 32.914286 73.142857 73.142857v731.428572c0 40.228571-32.914286 73.142857-73.142857 73.142857H182.857143c-40.228571 0-73.142857-32.914286-73.142857-73.142857V146.285714c0-40.228571 32.914286-73.142857 73.142857-73.142857h109.714286z" fill="currentColor" p-id="12080"  data-darkreader-inline-fill=""></path></svg>',
    '::panic::': '<svg t="1772773913347" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="13070" data-darkreader-inline-fill=""  ><path d="M934.4 770.133333L605.866667 181.333333C586.666667 147.2 550.4 128 512 128s-74.666667 21.333333-93.866667 53.333333L89.6 770.133333c-19.2 34.133333-19.2 76.8 0 110.933334S145.066667 938.666667 183.466667 938.666667h657.066666c40.533333 0 74.666667-21.333333 93.866667-57.6 19.2-34.133333 19.2-76.8 0-110.933334zM480 362.666667c0-17.066667 14.933333-32 32-32s29.866667 12.8 32 29.866666V640c0 17.066667-14.933333 32-32 32s-29.866667-12.8-32-29.866667V362.666667zM512 832c-23.466667 0-42.666667-19.2-42.666667-42.666667s19.2-42.666667 42.666667-42.666666 42.666667 19.2 42.666667 42.666666-19.2 42.666667-42.666667 42.666667z" fill="currentColor" p-id="13071"  data-darkreader-inline-fill=""></path></svg>',
    '::success::': '<svg t="1772774016483" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="18607" data-darkreader-inline-fill=""  ><path d="M512 967.0656c-251.392 0-455.0656-203.776-455.0656-455.0656S260.608 56.9344 512 56.9344s455.0656 203.776 455.0656 455.0656S763.392 967.0656 512 967.0656zM289.8944 515.3792l140.9024 131.3792c10.3424 9.6256 26.8288 10.0352 36.9664 0.7168L791.04 346.112c6.7584-6.3488 7.2704-15.872 0.8192-21.9136l-7.3728-6.9632c-5.9392-5.5296-16.5888-5.9392-23.3472-0.8192L459.8784 544.5632c-5.632 4.3008-15.9744 4.7104-22.016 1.024l-113.9712-71.0656c-7.168-4.4032-17.2032-2.8672-22.6304 3.4816L288.1536 493.568c-5.632 6.656-4.608 15.872 1.7408 21.8112z" fill="currentColor" p-id="18608"  data-darkreader-inline-fill=""></path></svg>',
    '::lock::': '<svg t="1772774050587" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="18753" data-darkreader-inline-fill=""  ><path d="M704 192h160v736H160V192h160.064v64H704zM311.616 537.28l-45.312 45.248L447.36 763.52l316.8-316.8-45.312-45.184L447.36 673.024zM384 192V96h256v96z" p-id="18754"></path></svg>',
    '::folder::': '<svg t="1772774407310" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="27298" data-darkreader-inline-fill=""  ><path d="M170.666667 469.333333h256a42.666667 42.666667 0 0 0 42.666666-42.666666V170.666667a42.666667 42.666667 0 0 0-42.666666-42.666667H170.666667a42.666667 42.666667 0 0 0-42.666667 42.666667v256a42.666667 42.666667 0 0 0 42.666667 42.666666z m426.666666 0h256a42.666667 42.666667 0 0 0 42.666667-42.666666V170.666667a42.666667 42.666667 0 0 0-42.666667-42.666667h-256a42.666667 42.666667 0 0 0-42.666666 42.666667v256a42.666667 42.666667 0 0 0 42.666666 42.666666zM170.666667 896h256a42.666667 42.666667 0 0 0 42.666666-42.666667v-256a42.666667 42.666667 0 0 0-42.666666-42.666666H170.666667a42.666667 42.666667 0 0 0-42.666667 42.666666v256a42.666667 42.666667 0 0 0 42.666667 42.666667z m554.666666 0c94.122667 0 170.666667-76.544 170.666667-170.666667s-76.544-170.666667-170.666667-170.666666-170.666667 76.544-170.666666 170.666666 76.544 170.666667 170.666666 170.666667z" p-id="27299"></path></svg>',
    '::retry::': '<svg t="1772774479074" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="29113" data-darkreader-inline-fill=""  ><path d="M936.432 603.424q0 2.848-0.576 4-36.576 153.152-153.152 248.288t-273.152 95.136q-83.424 0-161.44-31.424t-139.136-89.728l-73.728 73.728q-10.848 10.848-25.728 10.848t-25.728-10.848-10.848-25.728l0-256q0-14.848 10.848-25.728t25.728-10.848l256 0q14.848 0 25.728 10.848t10.848 25.728-10.848 25.728l-78.272 78.272q40.576 37.728 92 58.272t106.848 20.576q76.576 0 142.848-37.152t106.272-102.272q6.272-9.728 30.272-66.848 4.576-13.152 17.152-13.152l109.728 0q7.424 0 12.864 5.44t5.44 12.864zM950.736 146.272l0 256q0 14.848-10.848 25.728t-25.728 10.848l-256 0q-14.848 0-25.728-10.848t-10.848-25.728 10.848-25.728l78.848-78.848q-84.576-78.272-199.424-78.272-76.576 0-142.848 37.152t-106.272 102.272q-6.272 9.728-30.272 66.848-4.576 13.152-17.152 13.152l-113.728 0q-7.424 0-12.864-5.44t-5.44-12.864l0-4q37.152-153.152 154.272-248.288t274.272-95.136q83.424 0 162.272 31.712t140 89.44l74.272-73.728q10.848-10.848 25.728-10.848t25.728 10.848 10.848 25.728z" p-id="29114"></path></svg>',
    '::search::': '<svg t="1772774606993" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="9138" data-darkreader-inline-fill=""  ><path d="M940.571 852.693L726.096 637.971c44.778-60.855 69.263-134.077 69.263-211.012 0-95.732-37.354-185.78-104.889-253.311-67.781-67.536-157.825-104.889-253.557-104.889s-185.78 37.354-253.311 104.889c-139.518 139.518-139.518 366.859 0 506.626 67.536 67.536 157.579 104.889 253.311 104.889 77.18 0 150.155-24.487 211.257-69.263l214.475 214.722c21.52 21.52 56.403 21.52 77.676 0 21.77-21.52 21.77-56.403 0.25-77.921zM252.866 611.256c-101.425-101.673-101.425-266.916 0-368.342 49.227-49.227 114.535-76.192 184.295-76.192 69.511 0 134.819 26.966 184.047 76.192 49.227 49.227 76.443 114.535 76.443 184.295 0 69.762-27.211 135.065-76.443 184.295-49.227 49.227-114.535 76.192-184.047 76.192-69.762-0.25-135.065-27.211-184.295-76.443z" p-id="9139"></path></svg>',
    '::noted::': '<svg t="1772774665361" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="11001" data-darkreader-inline-fill=""  ><path d="M853.333333 981.333333H170.666667c-23.466667 0-42.666667-19.2-42.666667-42.666666V85.333333c0-23.466667 19.2-42.666667 42.666667-42.666666h682.666666c23.466667 0 42.666667 19.2 42.666667 42.666666v853.333334c0 23.466667-19.2 42.666667-42.666667 42.666666z m-618.666666-85.333333h554.666666c12.8 0 21.333333-8.533333 21.333334-21.333333V149.333333c0-12.8-8.533333-21.333333-21.333334-21.333333H234.666667c-12.8 0-21.333333 8.533333-21.333334 21.333333v725.333334c0 12.8 8.533333 21.333333 21.333334 21.333333z" p-id="11002"></path><path d="M654.933333 334.933333H369.066667c-23.466667 0-42.666667-19.2-42.666667-42.666666s19.2-42.666667 42.666667-42.666667h283.733333c23.466667 0 42.666667 19.2 42.666667 42.666667 2.133333 23.466667-17.066667 42.666667-40.533334 42.666666zM654.933333 524.8H369.066667c-23.466667 0-42.666667-19.2-42.666667-42.666667s19.2-42.666667 42.666667-42.666666h283.733333c23.466667 0 42.666667 19.2 42.666667 42.666666 2.133333 23.466667-17.066667 42.666667-40.533334 42.666667zM654.933333 710.4H369.066667c-23.466667 0-42.666667-19.2-42.666667-42.666667s19.2-42.666667 42.666667-42.666666h283.733333c23.466667 0 42.666667 19.2 42.666667 42.666666 2.133333 23.466667-170.666667 42.666667-40.533334 42.666667z" p-id="11003"></path></svg>',
    '::config::': '<svg t="1772774712673" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="12134" data-darkreader-inline-fill=""  ><path d="M853.333333 362.666667v42.666666a21.333333 21.333333 0 0 1-21.333333 21.333334H810.666667v99.84a170.666667 170.666667 0 0 1-49.92 120.746666l-83.2 85.333334A128 128 0 0 1 597.333333 768v149.333333a21.333333 21.333333 0 0 1-21.333333 21.333334h-128a21.333333 21.333333 0 0 1-21.333333-21.333334V768a128 128 0 0 1-80.213334-36.693333l-83.2-85.333334A170.666667 170.666667 0 0 1 213.333333 526.506667V426.666667h-21.333333a21.333333 21.333333 0 0 1-21.333333-21.333334v-42.666666a21.333333 21.333333 0 0 1 21.333333-21.333334H298.666667V106.666667a21.333333 21.333333 0 0 1 21.333333-21.333334h42.666667a21.333333 21.333333 0 0 1 21.333333 21.333334V341.333333h256V106.666667a21.333333 21.333333 0 0 1 21.333333-21.333334h42.666667a21.333333 21.333333 0 0 1 21.333333 21.333334V341.333333h106.666667a21.333333 21.333333 0 0 1 21.333333 21.333334z" p-id="12135"></path></svg>',
    '::globe::': '<svg t="1772774745189" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="13122" data-darkreader-inline-fill=""  ><path d="M698.026667 597.333333C701.44 569.173333 704 541.013333 704 512 704 482.986667 701.44 454.826667 698.026667 426.666667L842.24 426.666667C849.066667 453.973333 853.333333 482.56 853.333333 512 853.333333 541.44 849.066667 570.026667 842.24 597.333333M622.506667 834.56C648.106667 787.2 667.733333 736 681.386667 682.666667L807.253333 682.666667C766.293333 753.066667 701.013333 807.68 622.506667 834.56M611.84 597.333333 412.16 597.333333C407.893333 569.173333 405.333333 541.013333 405.333333 512 405.333333 482.986667 407.893333 454.4 412.16 426.666667L611.84 426.666667C615.68 454.4 618.666667 482.986667 618.666667 512 618.666667 541.013333 615.68 569.173333 611.84 597.333333M512 851.626667C476.586667 800.426667 448 743.68 430.506667 682.666667L593.493333 682.666667C576 743.68 547.413333 800.426667 512 851.626667M341.333333 341.333333 216.746667 341.333333C257.28 270.506667 322.986667 215.893333 401.066667 189.44 375.466667 236.8 356.266667 288 341.333333 341.333333M216.746667 682.666667 341.333333 682.666667C356.266667 736 375.466667 787.2 401.066667 834.56 322.986667 807.68 257.28 753.066667 216.746667 682.666667M181.76 597.333333C174.933333 570.026667 170.666667 541.44 170.666667 512 170.666667 482.56 174.933333 453.973333 181.76 426.666667L325.973333 426.666667C322.56 454.826667 320 482.986667 320 512 320 541.013333 322.56 569.173333 325.973333 597.333333M512 171.946667C547.413333 223.146667 576 280.32 593.493333 341.333333L430.506667 341.333333C448 280.32 476.586667 223.146667 512 171.946667M807.253333 341.333333 681.386667 341.333333C667.733333 288 648.106667 236.8 622.506667 189.44 701.013333 216.32 766.293333 270.506667 807.253333 341.333333M512 85.333333C276.053333 85.333333 85.333333 277.333333 85.333333 512 85.333333 747.52 276.48 938.666667 512 938.666667 747.52 938.666667 938.666667 747.52 938.666667 512 938.666667 276.48 747.52 85.333333 512 85.333333Z" p-id="13123"></path></svg>',
    '::time::': '<svg t="1772774793169" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="15231" data-darkreader-inline-fill=""  ><path d="M810.666667 314.453333l48.213333-48.213333a21.333333 21.333333 0 0 0 0-29.866667l-30.293333-30.293333a21.333333 21.333333 0 0 0-30.293334 0L750.933333 256A384 384 0 0 0 597.333333 180.48V128a42.666667 42.666667 0 0 0-42.666666-42.666667h-85.333334a42.666667 42.666667 0 0 0-42.666666 42.666667v52.48A384 384 0 0 0 128 554.666667a388.693333 388.693333 0 0 0 372.906667 384A384 384 0 0 0 810.666667 314.453333zM512 853.333333a298.666667 298.666667 0 1 1 298.666667-298.666666 298.666667 298.666667 0 0 1-298.666667 298.666666z m16.213333-512h-32.426666a21.333333 21.333333 0 0 0-21.333334 21.333334v213.333333a21.333333 21.333333 0 0 0 21.333334 21.333333h32.426666a21.333333 21.333333 0 0 0 21.333334-21.333333v-213.333333a21.333333 21.333333 0 0 0-21.333334-21.333334z" p-id="15232"></path></svg>',
    '::star::': '<svg t="1772774862301" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="18376" data-darkreader-inline-fill=""  ><path d="M548 101l104.1 221.5c5.9 12.6 17.9 21.2 31.6 23l243 30.5c34.1 4.3 47.7 46.3 22.7 69.7L770.8 613.1c-10.1 9.5-14.7 23.5-12 37.1l46 240.3c6.4 33.7-29.3 59.6-59.3 43.1l-214.6-118c-12.2-6.7-26.9-6.7-39.1 0l-214.6 118c-30.1 16.5-65.8-9.4-59.3-43.1l46-240.3c2.6-13.6-1.9-27.7-12.1-37.1L73.2 445.7c-25.1-23.5-11.4-65.4 22.7-69.7l243-30.5c13.8-1.7 25.8-10.4 31.6-23L474.7 101c14.5-31 58.7-31 73.3 0z" p-id="18377"></path></svg>',
    '::terminal::': '<svg t="1772774973409" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="19489" data-darkreader-inline-fill=""  ><path d="M853.333333 810.666667V298.666667H170.666667v512h682.666666m0-682.666667a85.333333 85.333333 0 0 1 85.333334 85.333333v597.333334a85.333333 85.333333 0 0 1-85.333334 85.333333H170.666667a85.333333 85.333333 0 0 1-85.333334-85.333333V213.333333a85.333333 85.333333 0 0 1 85.333334-85.333333h682.666666m-298.666666 597.333333v-85.333333h213.333333v85.333333h-213.333333m-145.92-170.666666L237.653333 384H358.4l140.8 140.8c16.64 16.64 16.64 43.946667 0 60.586667L359.253333 725.333333H238.506667l170.24-170.666666z" fill="" p-id="19490"></path></svg>',
    '::wait::': '<svg viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg"><path d="M856 64c22.091 0 40 17.909 40 40s-17.909 40-40 40h-26.273c-14.244 141.436-94.29 297.85-202.86 366.539l-0.71 0.44v3.652l0.705 0.442c107.891 68.869 187.804 224.251 202.65 364.928L856 880c22.091 0 40 17.909 40 40s-17.909 40-40 40H168c-22.091 0-40-17.909-40-40s17.909-40 40-40l26.459 0.001c14.704-141.014 94.054-296.803 201.896-365.421l3.285-2.055v-0.423l-3.282-2.056C288.183 441.104 208.484 285.1 194.269 144H168c-22.091 0-40-17.909-40-40s17.909-40 40-40h688zM477.567 695.579l-0.559 0.564L371.046 804.98a16 16 0 0 0-4.536 11.117c-0.025 8.836 7.119 16.02 15.955 16.044l18.764 0.043c36.185 0.065 70.62 0.018 103.304-0.14l19.75-0.101c35.982-0.165 74.913-0.191 116.792-0.08a16 16 0 0 0 11.263-4.594c6.212-6.11 6.379-16.046 0.44-22.362l-0.254-0.264L545.62 695.966c-0.243-0.246-0.488-0.49-0.735-0.731-18.805-18.308-48.777-18.092-67.318 0.344zM442.76 361.015c-8.837 0.015-15.988 7.19-15.974 16.027a16 16 0 0 0 4.535 11.134l46.407 47.672c18.49 18.996 48.88 19.405 67.876 0.914a48 48 0 0 0 0.74-0.737l46.77-47.552c6.196-6.3 6.112-16.43-0.188-22.627a16 16 0 0 0-11.245-4.593l-14.526 0.014h-4.98a9356.27 9356.27 0 0 1-44.885-0.108l-11.698-0.06c-19.937-0.092-40.881-0.12-62.832-0.084z" fill="currentColor"></path></svg>',
    '::warning::': '<svg t="1772773913347" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="13070" data-darkreader-inline-fill=""><path d="M934.4 770.133333L605.866667 181.333333C586.666667 147.2 550.4 128 512 128s-74.666667 21.333333-93.866667 53.333333L89.6 770.133333c-19.2 34.133333-19.2 76.8 0 110.933334S145.066667 938.666667 183.466667 938.666667h657.066666c40.533333 0 74.666667-21.333333 93.866667-57.6 19.2-34.133333 19.2-76.8 0-110.933334zM480 362.666667c0-17.066667 14.933333-32 32-32s29.866667 12.8 32 29.866666V640c0 17.066667-14.933333 32-32 32s-29.866667-12.8-32-29.866667V362.666667zM512 832c-23.466667 0-42.666667-19.2-42.666667-42.666667s19.2-42.666667 42.666667-42.666666 42.666667 19.2 42.666667 42.666666-19.2 42.666667-42.666667 42.666667z" fill="currentColor" p-id="13071" data-darkreader-inline-fill=""></path></svg>',
    '::clipboard::': '<svg t="1772902955936" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="10711" data-darkreader-inline-fill=""><path d="M425.984 726.016L768 384l-59.989333-59.989333-281.984 280.021333-109.994667-109.994667-59.989333 59.989334zM512 128c-24.021333 0-41.984 18.005333-41.984 41.984s18.005333 43.989333 41.984 43.989333 41.984-20.010667 41.984-43.989333S535.978667 128 512 128z m297.984 0C855.978667 128 896 168.021333 896 214.016v596.010667c0 45.994667-40.021333 86.016-86.016 86.016H213.973333c-45.994667 0-86.016-40.021333-86.016-86.016V214.016C127.957333 168.021333 167.978667 128 213.973333 128h178.005334C409.984 77.994667 455.978667 41.984 512 41.984s102.016 36.010667 120.021333 86.016h178.005334z" fill="currentColor" p-id="10712"></path></svg>',
    '::error::': '<svg t="1772904506837" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="17117" data-darkreader-inline-fill=""><path d="M513 98.2c-229.2 0-415 185.8-415 415s185.8 415 415 415 415-185.8 415-415c0-229.1-185.8-415-415-415z m173.4 522.6c18.3 18.4 18.3 48 0 66.1-9.1 9.1-21 13.7-33 13.7s-23.9-4.5-33-13.7L513 579.5 405.4 686.9c-9.1 9.1-21 13.7-33 13.7s-23.9-4.5-33-13.7c-18.3-18.4-18.3-48 0-66.1l107.4-107.4-107.4-107.5c-18.3-18.4-18.3-48 0-66.1 18.3-18.2 47.9-18.3 66.1 0L513 447.2l107.5-107.5c18.3-18.3 47.9-18.3 66.1 0 18.3 18.4 18.3 48 0 66.1L579.1 513.3l107.3 107.5z m0 0" fill="currentColor" p-id="6642"></path></svg>',
    '::pause::': '<svg t="1773332868844" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="5658" data-darkreader-inline-fill=""><path d="M768 832h-128c-35.392 0-64-28.608-64-64V256c0-35.392 28.608-64 64-64h128c35.392 0 64 28.608 64 64v512c0 35.392-28.608 64-64 64z m-384 0h-128c-35.392 0-64-28.608-64-64V256c0-35.392 28.608-64 64-64h128c35.392 0 64 28.608 64 64v512c0 35.392-28.608 64-64 64z" fill="currentColor" p-id="5659"></path></svg>'}

def push_log(msg, level="INFO"):
    """
    发送日志到队列以供前端拉取。
    后端改为完全静默模式，不再向标准输出/终端打印任何内容。
    """
    for e, svg_code in LOG_ICON_MAP.items():
        if e in msg:
            msg = msg.replace(e, f"<span class='ico'>{svg_code}</span>")
    log_id_counter[0] += 1
    entry = {"id": log_id_counter[0], "time": time.strftime("%H:%M:%S"), "level": level, "msg": msg}
    log_queue.append(entry)



# ==========================================
#     Session 有效性检测 + 保活
# ==========================================
SESSION_TTL_SECONDS = 20 * 60          # session 有效期 20 分钟
KEEPALIVE_INTERVAL_SECONDS = 15 * 60   # 每 15 分钟保活一次
CHECK_INTERVAL_SECONDS = 1


def check_session_alive():
    """检测当前 Session 是否仍然有效（未被挤掉/过期）。"""
    try:
        t0 = time.time()
        r = session_get(build_url(URL_MAIN_FRAME), timeout=REQ_TIMEOUT)
        record_latency(int((time.time() - t0) * 1000))
        _track_activity()
        if any(kw in r.text for kw in SESSION_INVALID_KEYWORDS):
            return False
        if any(kw in r.text or kw in r.url for kw in LOGIN_SUCCESS_KEYWORDS):
            return True
        return False
    except Exception:
        return False


def relogin_if_needed(reason="session expired"):
    with state_lock:
        if app_state.get("manual_logout"):
            return False, "用户已手动挂断"
        uname = app_state.get("username", "")
        pwd = app_state.get("password", "")
    if not uname or not pwd:
        return False, "缺少登录凭据"
    push_log(f"::warning:: 检测到会话失效({reason})，正在自动重新登录...", "WARN")
    ok, msg = do_login(uname, pwd)
    if ok:
        push_log("::success:: Session 自动重建成功", "SUCCESS")
        return True, msg
    push_log(f"::error:: Session 自动重建失败: {msg}", "ERROR")
    return False, msg


def keep_alive_loop():
    """后台 Session 检查 + 15分钟保活 + 失效自动重登 + 网络异常自动恢复"""
    _net_fail_count = 0
    _last_keepalive_time = 0
    while True:
        time.sleep(CHECK_INTERVAL_SECONDS)
        with state_lock:
            if app_state.get("snatch_running", False):
                continue
            if app_state.get("manual_logout"):
                continue
            is_logged_in = app_state.get("logged_in", False)
            expire_time = app_state.get("session_expire_time", 0)
            now = time.time()
            need_keepalive = is_logged_in and (now - _last_keepalive_time) >= KEEPALIVE_INTERVAL_SECONDS
            is_expired = is_logged_in and expire_time > 0 and now >= expire_time

        try:
            if is_logged_in:
                if need_keepalive:
                    if check_session_alive():
                        push_log("::time:: Session 已自动保活，倒计时已重置为 20 分钟")
                        _last_keepalive_time = now
                        _net_fail_count = 0
                    else:
                        relogin_if_needed("keepalive failed")
                        _last_keepalive_time = now
                        _net_fail_count = 0
                elif is_expired:
                    relogin_if_needed("ttl reached")
                    _last_keepalive_time = now
                    _net_fail_count = 0
            else:
                t0 = time.time()
                _requests.head(build_url(URL_LOGIN_HOME), timeout=REQ_TIMEOUT, verify=False, allow_redirects=True)
                record_latency(int((time.time() - t0) * 1000), kind="network")
                _net_fail_count = 0
        except Exception as e:
            _net_fail_count += 1
            err_str = str(e)
            is_network_err = any(kw in err_str for kw in ["NameResolution", "name resolution", "Temporary failure", "ConnectionError", "NewConnectionError", "MaxRetries", "Max retries", "10051", "10060"])
            if is_network_err:
                # 网络/DNS 临时故障，指数退避重试，不崩溃
                backoff = min(60, 5 * _net_fail_count)
                if _net_fail_count <= 3 or _net_fail_count % 10 == 0:
                    push_log(f"::warning:: 网络暂时不可用 (第{_net_fail_count}次)，{backoff}秒后重试: {format_net_err(e)}", "WARN")
                time.sleep(backoff)
            # 其他异常静默忽略

threading.Thread(target=keep_alive_loop, daemon=True, name="LatencyMonitor").start()


# ==========================================
#            协议登录
# ==========================================
login_lock = threading.Lock()

def do_login(username, password):
    with login_lock:
        return _do_login_inner(username, password)

def _do_login_inner(username, password):
    try:
        with session_lock:
            SESSION.cookies.clear()
        t0 = time.time()
        session_get(build_url(URL_DEFAULT), timeout=REQ_TIMEOUT)
        gate_url = build_url(URL_LOGIN_HOME)
        res = session_get(gate_url, timeout=REQ_TIMEOUT)
        record_latency(int((time.time() - t0) * 1000))
        _track_activity()
        res.encoding = 'gbk'
        soup = BeautifulSoup(res.text, 'html.parser')
        vs = soup.find('input', {'name': LOGIN_FIELD_VIEWSTATE})['value']
        ev = soup.find('input', {'name': LOGIN_FIELD_EVENTVALIDATION})['value']

        session_get(build_url(URL_VALIDATE_CODE) + f"?t=0.{int(time.time() * 1000)}", timeout=REQ_TIMEOUT)
        time.sleep(0.3)

        pwd_md5 = hashlib.md5(password.encode()).hexdigest().upper()[:30]
        final_hash = hashlib.md5((username + pwd_md5 + SCHOOL_CODE).encode()).hexdigest().upper()[:30]

        payload = OrderedDict([
            (LOGIN_FIELD_VIEWSTATE, vs), (LOGIN_FIELD_EVENTVALIDATION, ev),
            (LOGIN_FIELD_PCINFO, UA + "undefined5.0 (X11; Linux x86_64) AppleWebKit/537.36 SN:NULL"),
            ("txt_mm_expression", "14"), ("txt_mm_length", str(len(password))),
            ("txt_mm_userzh", "0"), (LOGIN_FIELD_USERTYPE, "学生"),
            ("dsdsdsdsdxcxdfgfg", final_hash),
            ("fgfggfdgtyuuyyuuckjg", ""), ("validcodestate", "0"),
            ("Sel_Type", "STU"), (LOGIN_FIELD_USERNAME, username),
            (LOGIN_FIELD_PASSWORD, ""), ("txt_psasas", "请输入密码"),
            ("btn_login", "登 录"),
        ])
        t0 = time.time()
        login_resp = session_post(
            gate_url,
            data=urllib.parse.urlencode(payload, encoding='gbk'),
            allow_redirects=True,
            timeout=REQ_TIMEOUT,
            headers=build_request_headers(gate_url, {
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": SCHOOL_HOST,
            })
        )
        record_latency(int((time.time() - t0) * 1000))
        _track_activity()
        login_resp.encoding = 'gbk'
        # 从响应 HTML 的 JS alert() 中精确提取错误消息
        alert_match = re.search(r"alert\(['\"](.+?)['\"]\)", login_resp.text)
        if alert_match:
            alert_msg = alert_match.group(1).strip()
            if any(kw in alert_msg for kw in LOGIN_FAIL_KEYWORDS):
                push_log(f"::error:: 登录失败: {alert_msg}", "ERROR")
                return False, alert_msg

        # 请求主页并验证是否真正进入了系统
        main_resp = session_get(build_url(URL_MAIN_FRAME), timeout=REQ_TIMEOUT)
        main_resp.encoding = 'gbk'

        if any(kw in main_resp.text for kw in SESSION_INVALID_KEYWORDS):
            push_log("::error:: 登录失败: 账号或密码不正确", "ERROR")
            return False, "账号或密码不正确"

        if any(kw in main_resp.text for kw in LOGIN_SUCCESS_KEYWORDS):
            sid = SESSION.cookies.get('ASP.NET_SessionId', '')
            now = time.time()
            with state_lock:
                app_state["logged_in"] = True
                app_state["username"] = username
                app_state["password"] = password  # 保存密码用于 session 过期后自动重新登录
                app_state["session_id"] = sid
                app_state["session_expire_time"] = now + SESSION_TTL_SECONDS
                app_state["manual_logout"] = False
            push_log(f"::success:: 登录成功!", "SUCCESS")
            return True, "登录成功"
        else:
            push_log("::error:: 登录失败: 无法验证登录状态", "ERROR")
            return False, "无法确认登录成功"
    except Exception as e:
        push_log(f"::error:: 登录异常: {format_net_err(e)}", "ERROR")
        return False, format_net_err(e)


# ==========================================
#    筛选选项动态拉取
# ==========================================
def fetch_filters():
    """从教务系统动态获取所有筛选维度（校区、类型等）。
    教务系统的筛选器来源于两个地方：
     1. 正选主页 stu_xszx.aspx 中的 <select> 标签（如 sel_lx）
     2. 正选主页 JS 中动态生成的 <select>（如 sel_xq 校区）
    返回: [{"name": "sel_xq", "label": "校区", "options": [{"value":"4","text":"北区"}, ...]}, ...]
    """
    push_log("::network:: 正在获取筛选选项...")
    zx_url = build_url(URL_COURSE_SELECT)
    r = session_get(zx_url, timeout=REQ_TIMEOUT, headers=build_request_headers(URL_MAIN_FRAME))
    r.encoding = 'gbk'
    html = r.text
    soup = BeautifulSoup(html, 'html.parser')

    filters = []

    # 1. 提取 HTML 中的 <select> 标签（如 sel_lx）
    for sel in soup.find_all('select'):
        name = sel.get('name', sel.get('id', ''))
        if not name:
            continue
        options = []
        for opt in sel.find_all('option'):
            val = opt.get('value', '')
            txt = opt.get_text(strip=True)
            if val and txt:  # 跳过空选项
                options.append({"value": val, "text": txt})
        if options:
            # 尝试从前面的标签获取中文名称
            label = name
            label_map = {"sel_lx": "类型", "sel_xq": "校区", "SelSpeciality": "专业",
                         "kclbmc": "课程类别", "kclbmc3": "类别三"}
            label = label_map.get(name, name)
            filters.append({"name": name, "label": label, "options": options})

    # 2. 从 JS 中提取动态生成的 <select>(如 sel_xq 校区)
    # 匹配模式: name=sel_xxx ... <option value=X>文字</option> ...
    js_selects = re.findall(
        r"<select\s[^>]*name=([\w]+)[^>]*>(.*?)</select>",
        html, re.DOTALL | re.IGNORECASE
    )
    existing_names = {f["name"] for f in filters}
    for sel_name, sel_body in js_selects:
        if sel_name in existing_names:
            continue
        opts = re.findall(r"<option\s+value=([^>\s]+?)>(.*?)</option>", sel_body, re.IGNORECASE)
        if opts:
            # 过滤掉 JS 变量拼接的占位符（value 含 + 号）和 Nothing 空选项
            options = [{"value": v.strip("'\""), "text": t} for v, t in opts
                       if '+' not in v and v.strip("'\"").lower() != 'nothing' and t.strip()]
            if not options:
                continue
            # 推断 label
            label_match = re.search(rf'[;\s]([\u4e00-\u9fff]+)[&\s]*<select[^>]*name={sel_name}', html)
            label = label_match.group(1) if label_match else sel_name
            label_map = {"sel_lx": "类型", "sel_xq": "校区", "SelSpeciality": "专业",
                         "kclbmc": "课程类别", "kclbmc3": "类别三"}
            label = label_map.get(sel_name, label)
            filters.append({"name": sel_name, "label": label, "options": options})
            existing_names.add(sel_name)

    push_log(f"::success:: 获取到 {len(filters)} 个筛选维度", "SUCCESS")
    return filters


# ==========================================
#    课程/班级数据拉取
# ==========================================
def fetch_course_list(filter_params=None):
    """
    拉取可选课程列表（从报表页表格行直接提取全部信息）。
    filter_params: 动态筛选参数字典，如 {"sel_xq": "4", "sel_lx": "0"}
    """
    if filter_params is None:
        filter_params = {}
    zx_url = build_url(URL_COURSE_SELECT)
    rpt_url = build_url(URL_COURSE_REPORT)

    push_log("::network:: 正在拉取课程列表...")

    # 1. 获取正选主页 VIEWSTATE
    r = session_get(zx_url, timeout=REQ_TIMEOUT, headers=build_request_headers(URL_MAIN_FRAME))
    r.encoding = 'gbk'
    soup_zx = BeautifulSoup(r.text, 'html.parser')
    vs = (soup_zx.find('input', {'name': LOGIN_FIELD_VIEWSTATE}) or {}).get('value', '')
    vsg = (soup_zx.find('input', {'name': '__VIEWSTATEGENERATOR'}) or {}).get('value', '')
    ev = (soup_zx.find('input', {'name': LOGIN_FIELD_EVENTVALIDATION}) or {}).get('value', '')

    # 2. POST 检索到报表页（动态合并筛选参数）
    sp = OrderedDict([
        (LOGIN_FIELD_VIEWSTATE, vs), ("__VIEWSTATEGENERATOR", vsg), (LOGIN_FIELD_EVENTVALIDATION, ev),
        ("SelSpeciality", ""), ("kc", ""), ("btn_search", "检索"),
    ])
    for k, v in filter_params.items():
        if k.startswith('sel') or k.startswith('Sel'):
            sp[k] = v
    t0 = time.time() if should_measure_business_latency() else None
    r = session_post(
        rpt_url,
        data=urllib.parse.urlencode(sp, encoding='gbk'),
        timeout=REQ_TIMEOUT,
        headers=build_request_headers(zx_url, {
            "Content-Type": "application/x-www-form-urlencoded",
        })
    )
    req_ms = int((time.time() - t0) * 1000) if t0 is not None else -1
    if t0 is not None:
        record_latency(req_ms)
    _track_activity(count_req=False)
    r.encoding = 'gbk'

    if any(kw in r.text for kw in NOT_STARTED_KEYWORDS):
        push_log(f"::wait:: 选课尚未开始{latency_suffix(req_ms)}", "WARN")
        return {"status": "not_started", "courses": []}

    if any(kw in r.text for kw in RATE_LIMIT_KEYWORDS):
        m = re.search(r'等待\s*(\d+)\s*分钟', r.text)
        wait_minutes = int(m.group(1)) if m else 2
        push_log(f"::warning:: 触发教务系统防刷策略，须等待{wait_minutes}分钟!", "ERROR")
        return {"status": "error", "msg": f"教务系统限流，已被锁定，请等待 {wait_minutes} 分钟后再拉取！"}

    soup_rpt = BeautifulSoup(r.text, 'html.parser')

    # 3. 从表格行中提取完整课程信息
    # 报表页表格列: [选定, 课程([代码]名称), 学分, 总学时, 类别, 考核方式, 查看]
    courses = []
    for tr in soup_rpt.find_all('tr'):
        cb = tr.find('input', {'type': 'checkbox'})
        if not cb:
            continue
        name_attr = cb.get('name', '')
        if not name_attr.startswith('chkKC'):
            continue

        value = cb.get('value', '')
        idx = name_attr.replace('chkKC', '')
        disabled = cb.get('disabled') is not None

        # 从 value 中提取课程代码
        code = value.split('|')[0] if value else idx

        # 从表格单元格提取完整信息
        tds = tr.find_all('td')
        td_texts = [td.get_text(strip=True) for td in tds]

        # 课程名: 格式 "[代码]课程名称"，去掉代码部分只留名称
        raw_name = td_texts[1] if len(td_texts) > 1 else ''
        course_name = re.sub(r'^\[.*?\]', '', raw_name).strip() or raw_name
        credit = td_texts[2] if len(td_texts) > 2 else ''      # 学分
        hours = td_texts[3] if len(td_texts) > 3 else ''        # 总学时
        category = td_texts[4] if len(td_texts) > 4 else ''     # 类别（公共课/必修课 等）
        exam_type = td_texts[5] if len(td_texts) > 5 else ''    # 考核方式（考试/考查）

        # chkSKBJ 隐藏字段（已选班级）
        skbj_node = soup_rpt.find('input', {'name': f'chkSKBJ{idx}'})
        existing_class = skbj_node.get('value', '') if skbj_node else ''

        # 查找"查看"或"选择"按钮 —— 未选课账号是"选择"，已选课账号是"查看"
        look_a = tr.find('a', string=re.compile(r'查看|选择'))
        look_value = value
        if look_a:
            # 尝试从 href 或 onclick 中提取 id 参数
            attr_str = str(look_a.get('href', '')) + str(look_a.get('onclick', ''))
            id_match = re.search(r"""[?&]id=([^&'"]+)""", attr_str)
            if id_match:
                look_value = id_match.group(1)
            else:
                look_value = look_a.get('value', value)

        courses.append({
            "index": idx,
            "code": code,
            "name": course_name,
            "value": value,
            "look_value": look_value,
            "disabled": disabled,
            "credit": credit,
            "hours": hours,
            "category": category,
            "exam_type": exam_type,
            "existing_class": existing_class,
        })

    if len(courses) == 0:
        push_log(f"::warning:: 未获取到课程，请检查网络、登录状态，尝试重新登录或稍后重试{latency_suffix(req_ms)}", "WARN")
    else:
        push_log(f"::course:: 共获取 {len(courses)} 门课程（{sum(1 for c in courses if not c['disabled'])} 门可选）{latency_suffix(req_ms)}", "SUCCESS")
    return {"status": "ok", "courses": courses}


def fetch_class_list(course_value, skbjval="", xq="", silent=False):
    """
    拉取指定课程的班级列表。
    返回: [{"class_id","class_num","radio_value","teacher","schedule","capacity","row_text"}, ...]
    silent: 是否静默模式（抢课中不输出日志）
    """
    if not silent:
        push_log(f"::network:: 正在拉取班级列表...")

    # 如果 course_value 包含 |，说明这是一个无需打开子班级弹窗的公选/直选课，强行发包将被校园网WAF拦截并断掉TCP(10051)
    if '|' in course_value:
        _cid = course_value.split('|')[2] if len(course_value.split('|')) > 2 else course_value.split('|')[0]
        return [{
            "class_id": _cid,
            "class_name": "（无子班级）",
            "teacher": "快捷直达通道",
            "schedule": "该类课程已略过班级选择步骤，点 Start 即可生效",
            "capacity": "",
            "radio_value": "",
            "course_value": course_value
        }]

    base_url = build_url(URL_CLASS_CHOOSE)

    def _do_fetch():
        try:
            t0 = time.time() if should_measure_business_latency() else None
            r = session_get(
                base_url,
                params={
                    "lx": "ZX",
                    "id": course_value,
                    "skbjval": skbjval,
                    "xq": xq
                },
                timeout=REQ_TIMEOUT,
                headers=build_request_headers(URL_COURSE_REPORT)
            )
            req_ms = int((time.time() - t0) * 1000) if t0 is not None else -1
            if t0 is not None:
                record_latency(req_ms)
            with state_lock:
                app_state["last_class_fetch_ms"] = req_ms
            _track_activity(count_req=True)
            r._req_ms = req_ms
        except Exception as e:
            push_log(f"拉取班级异常: {e}", "ERROR")
            return None

        r.encoding = 'gbk'
        return r

    r = _do_fetch()
    if r is None:
        return []

    # 检测 session 过期
    if any(kw in r.text for kw in ["无权", "重新登录"]):
        ok, msg = relogin_if_needed("fetch_class_list")
        if ok:
            push_log("::success:: 重新登录成功，重新拉取班级...", "SUCCESS")
            session_get(build_url(URL_COURSE_SELECT), timeout=REQ_TIMEOUT, headers=build_request_headers(URL_MAIN_FRAME))
            r = _do_fetch()
            if r is None:
                return []
        else:
            push_log(f"::error:: 重新登录失败: {msg}", "ERROR")
            return []

    if any(kw in r.text for kw in RATE_LIMIT_KEYWORDS):
        m = re.search(r'等待\s*(\d+)\s*分钟', r.text)
        wait_minutes = int(m.group(1)) if m else 2
        push_log(f"::warning:: 触发教务系统防刷策略，须等待{wait_minutes}分钟!", "ERROR")
        with state_lock:
            app_state["rate_limit_active"] = True
            app_state["rate_limit_until"] = time.time() + wait_minutes * 60
        return {"_rate_limited": True, "wait_minutes": wait_minutes}

    soup = BeautifulSoup(r.text, 'html.parser')

    # 提取没有嵌套表格的纯净行为候选行
    all_trs = [tr for tr in soup.find_all('tr') if not tr.find('table')]

    # 1. 动态表头识别：扫描前几行 tr 以建立列索引映射 (ColMap)
    col_map = {}
    header_rows = all_trs[:5] # 扫描前5行寻找表头特征
    for tr in header_rows:
        ths = tr.find_all(['td', 'th'], recursive=False)
        offset = 0
        for idx, th in enumerate(ths):
            text = th.get_text(strip=True)
            # 处理跨列情况 (colspan) 带来的索引偏移预测
            cspan = int(th.get('colspan', 1))

            if "教师" in text: col_map['teacher'] = offset
            elif "上课时间" in text or "时间" in text: col_map['schedule'] = offset
            elif "地点" in text: col_map['location'] = offset
            elif "限选" in text: col_map['limit'] = offset
            elif "已选" in text: col_map['used'] = offset
            elif "可选" in text or "余量" in text: col_map['left'] = offset
            elif "班级名称" in text: col_map['class_name'] = offset

            offset += cspan

    classes = []
    # 2. 遍历所有表格行进行数据提取
    for tr in all_trs:
        sel_input = tr.find('input', {'type': ['radio', 'checkbox']}, recursive=False)
        if not sel_input:
            # 兼容有些 input 包裹在 td 里的情况
            sel_inputs = tr.find_all('input', {'type': ['radio', 'checkbox']})
            if not sel_inputs or len(sel_inputs) > 2:
                continue
            sel_input = sel_inputs[0]
        else:
            if len(tr.find_all('input', {'type': ['radio', 'checkbox']}, recursive=False)) > 2:
                continue

        value = sel_input.get('value', '')
        if not value or '|' not in value:
            continue

        parts = value.split('|')
        class_id = parts[1] if len(parts) > 1 else (parts[0] if parts else '')
        class_num = class_id.split('-')[-1] if '-' in class_id else class_id

        tds = tr.find_all(['td', 'th'], recursive=False)
        cells = [td.get_text(strip=True) for td in tds]
        row_text = ' | '.join(cells)

        teacher, schedule, capacity, class_name, location = "", "", "", "", ""

        # 模式 A: 基于动态表头解析 (优先)
        if col_map:
            try:
                if 'teacher' in col_map and col_map['teacher'] < len(cells):
                    teacher = cells[col_map['teacher']]
                if 'schedule' in col_map and col_map['schedule'] < len(cells):
                    schedule = cells[col_map['schedule']]
                if 'location' in col_map and col_map['location'] < len(cells):
                    location = cells[col_map['location']]
                if 'class_name' in col_map and col_map['class_name'] < len(cells):
                    class_name = cells[col_map['class_name']]

                # 容量逻辑处理
                c_limit = cells[col_map['limit']] if 'limit' in col_map and col_map['limit'] < len(cells) else ""
                c_used = cells[col_map['used']] if 'used' in col_map and col_map['used'] < len(cells) else ""
                c_left = cells[col_map['left']] if 'left' in col_map and col_map['left'] < len(cells) else ""
                if c_limit and c_used:
                    capacity = f"{c_used}/{c_limit}"
                    if c_left: capacity += f" (余{c_left})"
            except Exception: pass

        # 模式 B: 特征锚点定位 (模式 A 缺失字段时的补全)
        if not schedule or not capacity:
            time_idx = -1
            for idx, cell in enumerate(cells):
                if ('周' in cell or '节' in cell) and len(cell) > 5:
                    time_idx = idx
                    if not schedule: schedule = cell
                    break

            if time_idx != -1:
                if not location and time_idx + 1 < len(cells):
                    cand_loc = cells[time_idx + 1].strip()
                    if cand_loc and "选定" not in cand_loc and "查看" not in cand_loc:
                        location = cand_loc
                if not capacity and time_idx >= 3:
                    c_l, c_u, c_r = cells[time_idx-3], cells[time_idx-2], cells[time_idx-1]
                    if c_l.isdigit() and c_u.isdigit():
                        capacity = f"{c_u}/{c_l}"
                        if c_r.isdigit(): capacity += f" (余{c_r})"
                if not teacher:
                    for cand_t in cells[:time_idx]:
                        if re.match(r'^[\u4e00-\u9fff]{2,4}$', cand_t.strip()) and cand_t.strip() not in ("选定", "查看", "限选", "可选"):
                            teacher = cand_t.strip()
                            break

        # 模式 C: 全局启发式兜底 (模式 A/B 均失效时)
        if not teacher or not capacity:
            for cell in cells:
                c_s = cell.strip()
                if not teacher and re.match(r'^[\u4e00-\u9fff]{2,4}$', c_s) and c_s not in ("选定", "查看", "已选", "限选", "可选"):
                    teacher = c_s
                elif not capacity and (re.match(r'^\d+/\d+$', c_s) or (c_s.isdigit() and int(c_s) > 20)):
                    capacity = c_s

        classes.append({
            "class_id": class_id, "class_num": class_num, "class_name": class_name,
            "location": location, "radio_value": value, "teacher": teacher,
            "schedule": schedule.strip(), "capacity": capacity, "row_text": row_text,
            "course_value": course_value, "skbjval": skbjval, "xq": xq,
        })

    # 去重
    unique_classes = []
    seen = set()
    for c in classes:
        if c['radio_value'] not in seen:
            seen.add(c['radio_value'])
            unique_classes.append(c)

    classes = unique_classes
    if not silent:
        push_log(f"::clipboard:: 共 {len(classes)} 个班级可选{latency_suffix(getattr(r, '_req_ms', -1))}", "SUCCESS")
    return classes


# ==========================================
#       选课验证（提交后确认）
# ==========================================
def verify_selection(target_class_id):
    """
    验证指定班级是否已选上。
    通过退选报表页，按班级ID精确匹配。
    返回: (已选上, 详情)
    """
    push_log(f"::search:: 正在验证选课结果 (班级={target_class_id})...")
    try:
        t0 = time.time() if should_measure_business_latency() else None
        r = session_get(build_url(URL_WITHDRAW_RESULT), timeout=REQ_TIMEOUT)
        req_ms = int((time.time() - t0) * 1000) if t0 is not None else -1
        if t0 is not None:
            record_latency(req_ms)
        r.encoding = 'gbk'

        soup = BeautifulSoup(r.text, 'html.parser')
        for cb in soup.find_all('input', {'type': 'checkbox'}):
            val = cb.get('value', '')
            # 退选 checkbox value 格式: 班级ID##token 或 班级ID1;班级ID2#课程码#token
            # 按班级ID精确匹配，避免子串误判
            matched_ids = val.split('#', 1)[0].split(';')
            if target_class_id in {x.strip() for x in matched_ids if x.strip()}:
                push_log(f"::success:: 验证通过！{target_class_id} 已在已选列表中{latency_suffix(req_ms)}", "SUCCESS")
                return True, f"班级 {target_class_id} 已确认选上"

        push_log(f"::warning:: 在已选列表中未找到 {target_class_id}{latency_suffix(req_ms)}", "WARN")
        return False, f"未在已选列表中找到 {target_class_id}"
    except Exception as e:
        push_log(f"::warning:: 验证异常: {e}", "ERROR")
        return False, str(e)


# ==========================================
#          选课提交 + 抢课引擎
# ==========================================
def submit_selection(strid, campus):
    """提交选课，返回 (成功, 消息, 请求耗时ms)"""
    url = build_url(URL_COURSE_REPORT) + "?func=1"
    payload = OrderedDict([
        ("id", strid), ("yxsjct", ""), ("sel_xq", campus),
        ("hid_ReturnStr", ""), ("hid_N", ""), ("txt_yzm", ""),
    ])
    t0 = time.time() if should_measure_business_latency() else None
    r = session_post(
        url,
        data=urllib.parse.urlencode(payload, encoding='gbk'),
        timeout=REQ_TIMEOUT,
        headers=build_request_headers(URL_COURSE_REPORT, {
            "Content-Type": "application/x-www-form-urlencoded",
        })
    )
    req_ms = int((time.time() - t0) * 1000) if t0 is not None else -1
    if t0 is not None:
        record_latency(req_ms)
    with state_lock:
        app_state["snatch_request_ms"] = req_ms
    _track_activity()
    r.encoding = 'gbk'
    text = r.text

    for kw in SNATCH_SUCCESS_KEYWORDS:
        if kw in text:
            return True, kw, req_ms

    matched = [k for k in SNATCH_FAIL_KEYWORDS if k in text]
    if matched:
        if '出错' in matched:
            with open('C:/Users/ZhengXG/.gemini/antigravity/scratch/error_dump.html', 'w', encoding='utf-8', errors='ignore') as f:
                f.write(text)
        return False, ', '.join(matched), req_ms
    return False, f"未知响应({len(text)}字节)", req_ms


def build_strid(class_id, chk_value):
    """构造正选提交的 id 参数: TTT,班级ID¤课程value"""
    return f"TTT,{class_id}\xa4{chk_value}"


def wait_with_stop(seconds):
    """可中断等待。返回 True 表示等待完成，False 表示已停止。"""
    whole = int(max(seconds, 0))
    for _ in range(whole):
        with state_lock:
            if not app_state["snatch_running"]:
                push_log("::pause:: 已停止", "WARN")
                app_state["snatch_phase"] = "idle"
                return False
        time.sleep(1)
    remain = max(seconds, 0) - whole
    if remain > 0:
        time.sleep(remain)
    return True


def snatch_loop():
    """抢课主循环 —— 收到回复后倒计时 + 风控自动等待恢复"""
    with state_lock:
        target = app_state.get("target")
        filter_params = app_state.get("filter_params", {})
        interval = app_state["interval"]  # 用户填写的间隔（秒）
        do_verify = app_state["verify_after"]
        app_state["snatch_interval"] = interval
        last_req_time = app_state.get("last_request_time", 0)

    if not target:
        push_log("::error:: 没有设置目标课程！", "ERROR")
        with state_lock:
            app_state["snatch_running"] = False
        return

    # === 防风控机制：检查距离上次请求的时间间隔 ===
    now = time.time()
    time_since_last = now - last_req_time if last_req_time > 0 else float('inf')

    if time_since_last < interval:
        cooldown = interval - time_since_last
        push_log(f"::warning:: 距离上次请求仅 {time_since_last:.1f}s，为防止风控，等待 {cooldown:.1f}s 冷却...", "WARN")
        app_state["snatch_phase"] = "waiting"
        app_state["snatch_phase_start"] = time.time()
        app_state["snatch_interval"] = cooldown

        # 分段等待，支持中途停止
        for _ in range(int(cooldown)):
            with state_lock:
                if not app_state["snatch_running"]:
                    push_log("::pause:: 冷却期间已停止", "WARN")
                    app_state["snatch_phase"] = "idle"
                    return
            time.sleep(1)

        # 等待剩余的小数部分
        remaining = cooldown - int(cooldown)
        if remaining > 0:
            time.sleep(remaining)

        push_log(f"::success:: 冷却完成，开始选课")

    t_class_id = target["class_id"]
    t_name = target.get("course_name", "?")
    t_class_name = target.get("class_name", t_class_id)
    current_radio_val = target.get('radio_value', '')
    class_page_value = target.get('class_page_value') or target.get('course_value') or target.get('look_value') or target.get('value', '')
    full_course_value = target.get('full_course_value') or class_page_value
    class_skbjval = target.get('class_skbjval', '')

    push_log(f"::success:: 开始选课，目标: {t_name} / {t_class_name}")
    attempt = 0

    while True:
        with state_lock:
            if not app_state["snatch_running"]:
                push_log("::pause:: 已停止", "WARN")
                app_state["snatch_phase"] = "idle"
                return

        attempt += 1
        try:
            # --- 1. 先发请求拉取班级列表 ---
            app_state["snatch_phase"] = "requesting"
            app_state["snatch_phase_start"] = time.time()

            xq = target.get('xq') or filter_params.get('sel_xq', '')
            if not class_page_value:
                raise Exception("缺少班级列表入口参数")

            fresh_classes = fetch_class_list(class_page_value, class_skbjval, xq, silent=True)
            class_fetch_ms = app_state.get("last_class_fetch_ms", -1)

            # --- 1.5 处理风控返回 ---
            if isinstance(fresh_classes, dict) and fresh_classes.get('_rate_limited'):
                wait_min = fresh_classes.get('wait_minutes', 2)
                wait_sec = wait_min * 60
                push_log(f"::warning:: [{attempt}] 自动等待 {wait_min} 分钟后继续...", "ERROR")
                app_state["snatch_phase"] = "waiting"
                app_state["snatch_phase_start"] = time.time()
                app_state["snatch_interval"] = wait_sec
                if not wait_with_stop(wait_sec):
                    return
                push_log(f"::success:: [{attempt}] 风控等待结束，继续抢课流程")
                continue

            if fresh_classes is None or len(fresh_classes) == 0:
                push_log(f"::warning:: [{attempt}] 获取班级列表失败或为空，{interval}s 后重试")
                app_state["snatch_phase"] = "waiting"
                app_state["snatch_phase_start"] = time.time()
                app_state["snatch_interval"] = interval
                if not wait_with_stop(max(interval, 1)):
                    return
                continue

            target_class_info = next((fc for fc in fresh_classes if fc.get('class_id') == t_class_id), None)
            if not target_class_info:
                push_log(f"::warning:: [{attempt}] 在最新班级列表中找不到目标班级 ({t_class_id}){latency_suffix(class_fetch_ms)}", "WARN")
                app_state["snatch_phase"] = "waiting"
                app_state["snatch_phase_start"] = time.time()
                app_state["snatch_interval"] = interval
                if not wait_with_stop(max(interval, 1)):
                    return
                continue

            # 获取最新 token
            if target_class_info.get('radio_value'):
                current_radio_val = target_class_info['radio_value']

            # 解析人数
            capacity_raw = target_class_info.get('capacity', '')

            # 更新实时人数到 app_state，供前端无感更新
            with state_lock:
                app_state["target_capacity_live"] = capacity_raw

            is_full = False
            is_valid_capacity = False
            parts = capacity_raw.split('/')
            if len(parts) >= 2:
                used = parts[0].strip()
                limit_part = parts[1].split('(')[0].strip()
                if used.isdigit() and limit_part.isdigit():
                    is_valid_capacity = True
                    if int(used) >= int(limit_part):
                        is_full = True

            if not is_valid_capacity:
                push_log(f"::warning:: [{attempt}] 无法解析班级人数 ({capacity_raw})，跳过", "WARN")

            # --- 2. 收到回复后，完整倒计时 interval 秒再发下一次请求 ---
            if is_full or not is_valid_capacity:
                if is_full:
                    push_log(f"::wait:: [{attempt}] 班级人数满 {capacity_raw}，{interval}s 后重试...{latency_suffix(class_fetch_ms)}")
                app_state["snatch_phase"] = "waiting"
                app_state["snatch_phase_start"] = time.time()
                app_state["snatch_interval"] = interval
                if not wait_with_stop(max(interval, 0.5)):
                    return
                continue

            # === 人数未满！立刻构造 strid 并提交 ===
            push_log(f"::success:: [{attempt}] 发现余量！({capacity_raw}) 准备发包...{latency_suffix(class_fetch_ms)}")

            if current_radio_val and '@' in current_radio_val:
                skbj_token = current_radio_val.split('@', 1)[1]
            elif target_class_info.get('existing_class'):
                skbj_token = target_class_info['existing_class']
            else:
                skbj_token = t_class_id

            strid = build_strid(skbj_token, full_course_value)
            push_log(f"::success:: [{attempt}] 使用最新Token提交: {t_name} → {t_class_id}")

            app_state["snatch_phase"] = "requesting"
            app_state["snatch_phase_start"] = time.time()
            ok, msg, req_ms = submit_selection(strid, xq)
            push_log(f"::time:: [{attempt}] 本次提交请求耗时{latency_suffix(req_ms)}")
            app_state["snatch_phase"] = "request_done"
            app_state["snatch_phase_start"] = time.time()

            # === 根据响应结果决定下一步 ===
            if ok:
                push_log(f"::success:: 服务器返回: {msg}", "SUCCESS")
                if do_verify:
                    app_state["snatch_phase"] = "verifying"
                    app_state["snatch_phase_start"] = time.time()
                    v_ok, v_msg = verify_selection(t_class_id)
                    if v_ok:
                        push_log(f"::success:: {t_name}/{t_class_name} 选课成功并通过验证！", "SUCCESS")
                    else:
                        push_log(f"::warning:: 服务器返回成功但首次验证未通过: {v_msg}，等待 5 秒后重试...", "WARN")
                        if not wait_with_stop(5):
                            return
                        app_state["snatch_phase_start"] = time.time()
                        v_ok2, _ = verify_selection(t_class_id)
                        if v_ok2:
                            push_log(f"::success:: {t_name}/{t_class_name} 选课成功并通过二次验证！", "SUCCESS")
                        else:
                            push_log(f"::warning:: 二次验证仍未通过，可能未成功，退回监控池继续尝试...", "WARN")
                            continue
                else:
                    push_log(f"::success:: {t_name}/{t_class_name} 选课成功！", "SUCCESS")

                with state_lock:
                    app_state["snatch_success"] = True
                    app_state["snatch_result"] = f"{t_name} / {t_class_name}"
                    app_state["snatch_running"] = False
                    app_state["snatch_phase"] = "idle"
                return
            else:
                push_log(f"::error:: [{attempt}] {msg}", "WARN")
                # 检查提交响应中是否也触发了风控
                if any(k in msg for k in RATE_LIMIT_KEYWORDS):
                    m = re.search(r'(\d+)\s*分钟', msg)
                    wait_min = int(m.group(1)) if m else 2
                    wait_sec = wait_min * 60
                    push_log(f"::warning:: [{attempt}] 触发风控，自动等待 {wait_min} 分钟后继续...", "ERROR")
                    app_state["snatch_phase"] = "waiting"
                    app_state["snatch_phase_start"] = time.time()
                    app_state["snatch_interval"] = wait_sec
                    if not wait_with_stop(wait_sec):
                        return
                    push_log(f"::success:: [{attempt}] 风控等待结束，继续抢课流程")
                    continue

                if any(k in msg for k in SNATCH_STOP_KEYWORDS):
                    push_log(f"::error:: 触发硬性阻挡 ({msg})，已自动中止。", "ERROR")
                    with state_lock:
                        app_state["snatch_running"] = False
                        app_state["snatch_phase"] = "idle"
                    return
                # 一般失败，倒计时用户设定间隔后重试
                app_state["snatch_phase"] = "waiting"
                app_state["snatch_phase_start"] = time.time()
                app_state["snatch_interval"] = interval
                if not wait_with_stop(max(interval, 0.5)):
                    return

        except Exception as e:
            err_str = str(e)
            is_net = any(kw in err_str for kw in ["NameResolution", "ConnectionError", "MaxRetries", "Max retries", "Temporary failure", "10051", "10060"])
            push_log(f"::warning:: [{attempt}] 请求发生异常: {format_net_err(e)}", "ERROR")
            if is_net:
                push_log(f"::warning:: [{attempt}] 网络连接不稳，自动等待 30 秒后重试...", "WARN")
                app_state["snatch_phase"] = "waiting"
                app_state["snatch_phase_start"] = time.time()
                app_state["snatch_interval"] = 30
                if not wait_with_stop(30):
                    return
                continue

            app_state["snatch_phase"] = "waiting"
            app_state["snatch_phase_start"] = time.time()
            app_state["snatch_interval"] = interval
            if not wait_with_stop(max(interval, 1)):
                return




# ==========================================
#       反向代理
# ==========================================
def normalize_redirect(location):
    location = location.replace(f"{SCHOOL_HOST}/jwweb/", "/jw/")
    location = location.replace(f"{SCHOOL_HOST}/", "/jw/")
    if location.startswith('/jwweb/'):
        location = '/jw/' + location[len('/jwweb/'):]
    return location


@app.route('/jw/', defaults={'path': ''}, methods=['GET', 'POST'])
@app.route('/jw/<path:path>', methods=['GET', 'POST'])
def reverse_proxy(path):
    target_url = f"{JWWEB_BASE}/{path}"
    if request.query_string:
        target_url += f"?{request.query_string.decode()}"
    fwd_headers = {"User-Agent": UA, "Referer": build_url(URL_COURSE_SELECT),
                   "Accept": request.headers.get('Accept', '*/*')}
    if 'Content-Type' in request.headers:
        fwd_headers['Content-Type'] = request.headers['Content-Type']
    try:
        if request.method == 'POST':
            resp = session_post(target_url, data=request.get_data(),
                                headers=fwd_headers, timeout=REQ_TIMEOUT, allow_redirects=False, verify=False)
        else:
            resp = session_get(target_url, headers=fwd_headers, timeout=REQ_TIMEOUT, allow_redirects=False, verify=False)
        if resp.status_code in (301, 302, 303, 307):
            return redirect(normalize_redirect(resp.headers.get('Location', '')), code=resp.status_code)
        ct = resp.headers.get('Content-Type', '')
        content = resp.content
        if 'text/html' in ct:
            text = content.decode('gbk', errors='ignore')
            text = text.replace(f'{SCHOOL_HOST}/jwweb/', '/jw/')
            text = text.replace(f'{SCHOOL_HOST}/', '/jw/')
            text = text.replace('"/jwweb/', '"/jw/')
            text = text.replace("'/jwweb/", "'/jw/")
            text = text.replace('top.location', '//top.location')
            return Response(text.encode('gbk', errors='ignore'), status=resp.status_code,
                            content_type='text/html; charset=gb2312')
        return Response(content, status=resp.status_code, content_type=ct)
    except Exception as e:
        return f"<h3>代理错误</h3><p>{e}</p>", 502


# ==========================================
#              API 路由
# ==========================================
@app.route('/api/login', methods=['POST'])
def api_login():
    d = request.get_json()
    ok, msg = do_login(d.get('username', ''), d.get('password', ''))
    return jsonify({"ok": ok, "msg": msg})


@app.route('/api/logout', methods=['POST'])
def api_logout():
    logout_url = build_url(URL_LOGOUT)
    logout_headers = {
        "Referer": build_url(URL_MAIN_TOOLS),
        "Upgrade-Insecure-Requests": "1",
    }
    with state_lock:
        was_logged_in = app_state["logged_in"]
        app_state["manual_logout"] = True
        app_state["logged_in"] = False
        app_state["snatch_running"] = False
        app_state["username"] = ""
        app_state["password"] = ""
        app_state["session_id"] = ""
        app_state["session_expire_time"] = 0
        app_state["target"] = None

    if was_logged_in:
        try:
            session_get(logout_url, headers=logout_headers, timeout=REQ_TIMEOUT, allow_redirects=True)
            push_log(f"::bye:: 已成功注销")
        except Exception as e:
            push_log(f"::warning:: 正常注销失败，继续本地销毁会话: {e}", "WARN")

    reset_session()

    return jsonify({"ok": True})

@app.route('/api/preset_accounts')
def api_preset_accounts():
    """返回预设账号列表（仅用户名，不返回密码）"""
    return jsonify({"accounts": [acc["username"] for acc in PRESET_ACCOUNTS]})

@app.route('/api/get_password', methods=['POST'])
def api_get_password():
    """根据用户名返回对应密码"""
    d = request.get_json() or {}
    username = d.get("username", "")
    for acc in PRESET_ACCOUNTS:
        if acc["username"] == username:
            return jsonify({"ok": True, "password": acc["password"]})
    return jsonify({"ok": False})

@app.route('/api/state')
def api_state():
    now = time.time()
    with state_lock:
        t = app_state.get("target")
        expire_time = app_state.get("session_expire_time", 0)
        # 请求频率统计：最近 20 秒请求次数（用于贴近风控阈值 5 次/20秒）
        valid_reqs = [t for t in app_state.get("req_history", []) if now - t <= 20]
        app_state["req_history"] = valid_reqs
        req_count_20s = len(valid_reqs)
        rate_limit_until = app_state.get("rate_limit_until", 0)
        rate_limit_active = bool(app_state.get("rate_limit_active", False) and rate_limit_until > now)
        if app_state.get("rate_limit_active", False) and rate_limit_until <= now:
            app_state["rate_limit_active"] = False
            app_state["rate_limit_until"] = 0
            rate_limit_active = False
            rate_limit_until = 0
        return jsonify({
            "logged_in": app_state["logged_in"],
            "username": app_state["username"],
            "snatch_running": app_state["snatch_running"],
            "snatch_success": app_state["snatch_success"],
            "snatch_result": app_state["snatch_result"],
            "target": t,
            "verify_after": app_state["verify_after"],
            "measure_business_latency": app_state.get("measure_business_latency", False),
            "session_expire_time": expire_time,  # 返回过期时间戳，前端自己算剩余
            "snatch_phase": app_state.get("snatch_phase", "idle"),
            "snatch_interval": app_state.get("snatch_interval", 0),
            "snatch_phase_start": app_state.get("snatch_phase_start", 0),
            "rate_limit_active": rate_limit_active,
            "rate_limit_until": rate_limit_until,
            "snatch_request_ms": app_state.get("snatch_request_ms", -1),
            "server_time": now,
            "req_count_20s": req_count_20s,
            "target_capacity_live": app_state.get("target_capacity_live", ""),  # 实时班级人数
        })


@app.route('/api/filters', methods=['POST'])
def api_filters():
    """动态获取筛选维度（校区、类型等）"""
    try:
        filters = fetch_filters()
        return jsonify({"ok": True, "filters": filters})
    except Exception as e:
        push_log(f"获取筛选选项失败: {e}", "ERROR")
        return jsonify({"ok": False, "msg": str(e)})


@app.route('/api/ping')
def api_ping():
    """返回纯网络延迟，用于反映网络质量"""
    with state_lock:
        network_ms = app_state.get("network_latency", -1)
    return jsonify({
        "ok": True,
        "ms": network_ms,
        "type": "network"  # 明确标识这是网络延迟
    })


@app.route('/api/logs')
def api_logs():
    since = int(request.args.get('since', 0))
    return jsonify([e for e in log_queue if e['id'] > since])


@app.route('/api/courses', methods=['POST'])
def api_courses():
    """拉取课程列表（筛选参数动态传入）"""
    d = request.get_json() or {}
    # 提取所有 sel_ 筛选参数
    filter_params = {k: v for k, v in d.items() if k.startswith('sel') or k.startswith('Sel')}
    try:
        result = fetch_course_list(filter_params)
        return jsonify(result)
    except Exception as e:
        push_log(f"::error:: 拉取课程失败: {format_net_err(e)}", "ERROR")
        return jsonify({"status": "error", "msg": format_net_err(e)})


@app.route('/api/classes', methods=['POST'])
def api_classes():
    """拉取指定课程的班级列表"""
    d = request.get_json() or {}
    value = d.get('value', '')
    skbjval = d.get('skbjval', '')
    xq = d.get('xq', '')
    try:
        classes = fetch_class_list(value, skbjval, xq)
        if isinstance(classes, dict) and classes.get('_rate_limited'):
            wait_minutes = int(classes.get('wait_minutes', 2) or 2)
            return jsonify({
                "ok": False,
                "rate_limited": True,
                "wait_minutes": wait_minutes,
                "msg": f"触发风控，请等待 {wait_minutes} 分钟后再试"
            })
        return jsonify({"ok": True, "classes": classes})
    except Exception as e:
        friendly_msg = format_net_err(e)
        push_log(f"::error:: 拉取班级失败: {friendly_msg}", "ERROR")
        return jsonify({"ok": False, "msg": friendly_msg})


@app.route('/api/target', methods=['POST'])
def api_set_target():
    """设置选课目标（用户从列表中选好后调用）"""
    d = request.get_json() or {}
    with state_lock:
        app_state["target"] = d.get("target")
        # 动态存储所有筛选参数
        app_state["filter_params"] = {k: v for k, v in d.items() if k.startswith('sel') or k.startswith('Sel')}
        app_state["interval"] = float(d.get("interval", 0.3))
        app_state["verify_after"] = d.get("verify_after", True)
        app_state["measure_business_latency"] = d.get("measure_business_latency", False)
    return jsonify({"ok": True})


@app.route('/api/snatch/start', methods=['POST'])
def api_snatch_start():
    with state_lock:
        if app_state["snatch_running"]:
            return jsonify({"ok": False, "msg": "已在运行"})
        if not app_state["logged_in"]:
            return jsonify({"ok": False, "msg": "请先登录"})
        if not app_state.get("target"):
            return jsonify({"ok": False, "msg": "请先选择目标课程和班级"})

        # 立即设置为运行状态，防止重复启动（在锁内完成所有操作）
        app_state["snatch_running"] = True
        app_state["snatch_success"] = False
        app_state["snatch_phase"] = "idle"
        app_state["snatch_result"] = ""

        # 在锁内启动线程，确保状态设置和线程启动是原子操作
        threading.Thread(target=snatch_loop, daemon=True, name="Snatcher").start()

    return jsonify({"ok": True})


@app.route('/api/snatch/stop', methods=['POST'])
def api_snatch_stop():
    with state_lock:
        app_state["snatch_running"] = False
        app_state["snatch_phase"] = "idle"
    # 日志由 snatch_loop 中打印，避免重复
    return jsonify({"ok": True})


@app.route('/api/verify', methods=['POST'])
def api_verify():
    """手动验证某个班级是否已选上"""
    d = request.get_json() or {}
    class_id = d.get('class_id', '')
    if not class_id:
        return jsonify({"ok": False, "msg": "缺少 class_id"})
    ok, msg = verify_selection(class_id)
    return jsonify({"ok": ok, "msg": msg})


# ==========================================
#            前端页面
# ==========================================
PANEL_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>青果选课平台-Beta</title>
<style>
:root {
  --bg:#0a0e1a; --card:#111827; --card2:#1a2332;
  --border:#1e2d3d; --border2:#2a3a4a;
  --text:#e2e8f0; --text2:#94a3b8; --text3:#64748b;
  --accent:#3b82f6; --accent2:#60a5fa;
  --green:#22c55e; --green-bg:#052e16;
  --red:#ef4444; --orange:#f59e0b;
  --radius:12px;
}
*{box-sizing:border-box;margin:0;padding:0}
/* 统一滚动条美化 */
::-webkit-scrollbar { width:6px; height:6px; }
::-webkit-scrollbar-track { background:transparent; }
::-webkit-scrollbar-thumb { background-color:var(--border2); border-radius:10px; }
::-webkit-scrollbar-thumb:hover { background-color:var(--text3); }
* { scrollbar-width:thin; scrollbar-color:var(--border2) transparent; }
body{background-color:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif,"Apple Color Emoji","Segoe UI Emoji",system-ui;height:100vh;overflow:hidden;}
.topbar{background:linear-gradient(135deg,#111827,#1a1a2e);border-bottom:1px solid var(--border);padding:0 2rem;height:52px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.topbar h1{font-size:1.05rem;font-weight:700;background:linear-gradient(135deg,#60a5fa,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.topbar .status{display:flex;align-items:center;gap:.5rem;font-size:.8rem;color:#ffffff}
.topbar .dot{width:8px;height:8px;border-radius:50%;background:var(--red);animation:pulse 2s infinite}
.topbar .dot.on{background:var(--green)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}



/* Custom Checkbox */
.custom-checkbox { display: flex; align-items: center; gap: 0.5rem; cursor: pointer; user-select: none; }
.custom-checkbox input { position: absolute; opacity: 0; cursor: pointer; height: 0; width: 0; }
.custom-checkbox .checkmark { position: relative; height: 1.15rem; width: 1.15rem; background-color: var(--card2); border: 1px solid var(--border); border-radius: 4px; transition: all 0.2s; flex-shrink: 0; }
.custom-checkbox:hover input ~ .checkmark { border-color: var(--accent); }
.custom-checkbox input:checked ~ .checkmark { background-color: var(--accent); border-color: var(--accent); }
.custom-checkbox .checkmark:after { content: ""; position: absolute; display: none; }
.custom-checkbox input:checked ~ .checkmark:after { display: block; }
.custom-checkbox .checkmark:after { left: 0.35rem; top: 0.15rem; width: 0.25rem; height: 0.5rem; border: solid white; border-width: 0 2px 2px 0; transform: rotate(45deg); }

/* Number Controls */
input[type=number]::-webkit-inner-spin-button,
input[type=number]::-webkit-outer-spin-button { -webkit-appearance: none; margin: 0; }
input[type=number] { -moz-appearance: textfield; }
.num-ctrl { display:flex; position:relative; width:100%; border:1px solid var(--border); border-radius:10px; background-color:var(--card2); transition:border .2s; }
.num-ctrl:focus-within { border-color:var(--accent); }
.num-ctrl input { width:100%; border:none; padding:0.65rem 2rem 0.65rem 0.8rem; background:transparent; color:#f3f4f6; font-size:0.85rem; border-radius:10px; outline:none; }
.num-ctrl .btns { position:absolute; right:0; top:0; bottom:0; display:flex; flex-direction:column; width:1.6rem; border-left:1px solid var(--border); }
.num-ctrl .btn-s { flex:1; background:transparent; border:none; color:var(--text3); display:flex; align-items:center; justify-content:center; cursor:pointer; padding:0; transition:color 0.2s, background 0.2s; }
.num-ctrl .btn-s:hover { background:var(--border2); color:#fff; }
.num-ctrl .btn-s:first-child { border-bottom:1px solid var(--border); border-top-right-radius:9px; }
.num-ctrl .btn-s:last-child { border-bottom-right-radius:9px; }

/* Layout */
.app-container{display:flex;height:calc(100vh - 52px);}
.sidebar{width:280px;min-width:280px;border-right:1px solid var(--border);padding:1rem;overflow-y:auto;display:flex;flex-direction:column;gap:0.75rem;transition:all 0.3s cubic-bezier(0.16,1,0.3,1);opacity:1;}
.sidebar.closed{width:0;min-width:0;padding:0;border-right:0px solid var(--border);opacity:0;}
.main-content{flex:1;display:flex;flex-direction:column;overflow:hidden;position:relative;background-color:var(--bg);}
.log-sidebar{width:0;min-width:0;max-width:0;border-left:0px solid var(--border);background-color:var(--card);display:flex;flex-direction:column;overflow:hidden;transition:all 0.3s cubic-bezier(0.16,1,0.3,1);opacity:0;}
.log-sidebar.open{width:280px;min-width:280px;max-width:280px;border-left:1px solid var(--border);opacity:1;}
/* 日志面板底部 sticky 卡片区 */
.log-footer{flex-shrink:0;border-top:1px solid var(--border);padding:0.6rem;display:flex;flex-direction:column;gap:0.5rem;background:var(--card);}
.mini-progress{height:4px;background:var(--border2);border-radius:2px;overflow:hidden;}
.mini-progress-fill{height:100%;border-radius:2px;transition:width 1s linear;}
/* 抢课进度条（大按钮尺寸） */
.snatch-progress{height:42px;background:var(--card2);border-radius:8px;overflow:hidden;position:relative;border:1px solid var(--border);}
.snatch-progress-fill{height:100%;border-radius:8px;transition:width 0.3s linear;}
.snatch-progress-text{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:0.78rem;font-weight:600;color:#fff;z-index:1;}

/* Components */
.card{background-color:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:1.1rem;}
.card h3{font-size:.8rem;font-weight:600;color:#ffffff;text-transform:uppercase;letter-spacing:.05em;margin-bottom:.75rem}
.field{margin-bottom:.7rem}
.field label{display:block;font-size:.72rem;color:#f3f4f6;margin-bottom:.25rem;font-weight:500}
.field input,.field select{width:100%;background-color:var(--bg);border:1px solid var(--border2);padding:.65rem .75rem;border-radius:10px;color:var(--text);font-size:.82rem;outline:none;transition:border-color .2s}
.field input:focus,.field select:focus{border-color:var(--accent)}

.ui-control{border:1px solid var(--border);background-color:var(--bg);color:var(--text);border-radius:6px;font-size:0.75rem;}
.ui-btn-lite{border:1px solid var(--border);background-color:var(--bg);color:var(--text);cursor:pointer;border-radius:6px;font-size:0.75rem;}
.ui-select{padding:0 1.6rem 0 0.8rem;min-width:100px;}
.ui-search-input{width:100%;border:1px solid var(--border);padding:0 0.6rem 0 2rem;background-color:var(--bg);color:var(--text);border-radius:6px;font-size:0.75rem;}
.capacity-text{font-size:0.85rem;color:#f3f4f6;}
/* Account Dropdown */
.toolbar-mini-btn{padding:0.22rem 0.55rem !important;font-size:0.75rem !important;line-height:1 !important;min-height:30px !important;border-radius:8px !important;}
.toolbar-mini-btn .ico{width:1.05em;height:1.05em;display:inline-flex;align-items:center;justify-content:center;}
.panel-inline{display:none;flex-wrap:wrap;gap:0.5rem;align-items:center;background-color:var(--card);padding:0.4rem 0.6rem;border-radius:8px;border:1px solid var(--border);}
.filter-section-title{font-weight:bold;font-size:0.8rem;color:var(--text);border-bottom:1px solid var(--border);padding-bottom:0.4rem;}
.filter-section-title.with-top-gap{margin-top:0.4rem;}
.empty-state{grid-column:1/-1;text-align:center;color:#f3f4f6;padding:2rem;}
.account-dropdown{position:absolute;top:100%;left:0;right:0;margin-top:0.25rem;background:var(--card);border:1px solid var(--border);border-radius:10px;max-height:200px;overflow-y:auto;z-index:1000;box-shadow:0 4px 12px rgba(0,0,0,0.3);}
.account-dropdown-item{padding:0.65rem 0.75rem;cursor:pointer;color:var(--text);font-size:0.82rem;transition:background 0.2s;border-bottom:1px solid var(--border2);}
.account-dropdown-item:last-child{border-bottom:none;}
.account-dropdown-item:hover{background:var(--card2);color:#fff;}
.account-dropdown::-webkit-scrollbar{width:6px;}
.account-dropdown::-webkit-scrollbar-track{background:var(--bg);}
.account-dropdown::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px;}
.account-dropdown::-webkit-scrollbar-thumb:hover{background:var(--border2);}
.login-card-header{display:flex;align-items:center;justify-content:space-between;gap:0.8rem;margin-bottom:.2rem;}
.card .login-card-title{display:flex;align-items:center;gap:0.7rem;margin:0 !important;line-height:1;}
.login-card-title .ico{display:inline-flex;align-items:center;justify-content:center;line-height:1;}
.login-more-wrap{display:inline-flex;align-items:center;justify-content:center;align-self:center;width:24px;height:24px;line-height:1;color:var(--text2);cursor:pointer;user-select:none;transition:color .2s,transform .2s;flex-shrink:0;}
.login-more-wrap:hover{color:var(--text);}
.login-more-inline-arrow{display:inline-flex;align-items:center;justify-content:center;width:1em;height:1em;transition:transform .25s cubic-bezier(0.16,1,0.3,1);}
.login-more-inline-arrow svg{display:block;}
.login-more-wrap.open .login-more-inline-arrow{transform:rotate(180deg);}
.login-more-content{max-height:0;opacity:0;overflow:hidden;transition:max-height .32s cubic-bezier(0.16,1,0.3,1),opacity .24s ease,margin .24s ease;padding:0;}
.login-more-content.show{max-height:220px;opacity:1;margin:0.15rem 0 0.7rem;}
.login-more-inner{padding:0;}
.login-more-actions{display:flex;justify-content:flex-end;margin-top:.8rem;}
.login-more-num{margin-bottom:.7rem;}
.unified-select-trigger{gap:0.5rem;padding:0 0.85rem;}
.unified-select-trigger .selected-text{flex:1;min-width:0;padding-right:0.15rem;}
.unified-select-arrow{color:currentColor;flex-shrink:0;margin-left:0.15rem;transition:transform 0.25s;}


.btn{display:inline-flex;align-items:center;justify-content:center;gap:.3rem;padding:.65rem 1rem;border-radius:10px;border:none;font-weight:600;font-size:.82rem;cursor:pointer;transition:all .2s;}
.btn-primary{background:var(--accent);color:#fff}.btn-primary:hover{background:#2563eb;}
.btn-success{background:var(--green);color:#fff}.btn-success:hover{background:#16a34a}
.btn-danger{background:var(--red);color:#fff}.btn-danger:hover{background:#dc2626}
.btn-ghost{background:transparent;color:#ffffff;border:1px solid var(--border2)}.btn-ghost:hover{background:var(--card2);color:var(--text)}
.btn:disabled{opacity:.4;cursor:not-allowed;}
.btn-massive{padding:0.65rem; border-radius:10px; font-size:1rem; font-weight:bold; letter-spacing:0.05em; margin-top:0.6rem; width:100%; display:inline-flex; align-items:center; justify-content:center; gap:0.4rem;}
.w-full{width:100%}

/* Content Area */
.content-header{border-bottom:1px solid var(--border);padding:1rem 1.5rem;display:flex;gap:1.5rem;align-items:center;background-color:var(--card);}
.content-body{flex:1;overflow-y:auto;padding:1.5rem;}
.grid-list{display:grid;grid-template-columns:repeat(auto-fill, minmax(250px, 1fr));gap:1rem;}
.list-view{display:flex;flex-direction:column;gap:.6rem;}
.class-grid{display:grid;grid-template-columns:repeat(auto-fill, minmax(280px, 1fr));gap:1rem;}

.view-toggle {display:flex;background-color:var(--bg);border-radius:6px;padding:2px;border:1px solid var(--border2);}
.view-btn {background:transparent;border:none;color:#f3f4f6;padding:0.2rem 0.6rem;font-size:0.75rem;cursor:pointer;border-radius:4px;transition:all .2s;}
.view-btn:hover {color:var(--text);}
.view-btn.active {background:var(--card2);color:var(--accent2);box-shadow:0 1px 3px rgba(0,0,0,0.2);}

/* List items */
.course-item{background:var(--card2);border:1px solid var(--border);border-radius:var(--radius);padding:1rem;cursor:pointer;transition:all .15s;}
.course-item:hover{border-color:var(--accent);transform:translateY(-2px);box-shadow:0 4px 12px rgba(0,0,0,0.2);}
.course-item.selected{border-color:var(--green);background:#064e3b30;}
.grid-list .course-item.selected{grid-column: 1 / -1;}
.course-item.selected:hover{transform:none;box-shadow:none;cursor:default;}
.course-item.disabled .course-info-row{opacity:0.75;}
.course-item.disabled:hover{border-color:var(--accent);}

/* Elements defaults for Grid mode */
.course-info-row { display: flex; flex-direction: column; }
.c-name {font-size:1rem; font-weight:600; margin-bottom:0.5rem;}
.c-tags {display:flex; flex-wrap:wrap; gap:0.4rem; margin-bottom:0.5rem;}
.c-cat  {font-size:0.75rem;color:#f3f4f6;margin-bottom:0.3rem;}
.c-meta {display:flex; justify-content:space-between; align-items:center;}

/* Overrides for List mode (horizontal flow) */
.list-view .course-item {display:flex; flex-direction:column; gap:0; padding:0.75rem 1rem;}
.list-view .course-item:hover {transform:translateX(2px) translateY(0);}
.list-view .course-item.selected:hover {transform:none;}
.list-view .course-info-row {display:flex; flex-direction:row; align-items:center; justify-content:space-between; gap:1.0rem; width:100%;}
.list-view .c-name {margin-bottom:0; flex:0 0 auto; max-width:35%; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.list-view .c-tags {margin-bottom:0; flex:1; align-items:center; justify-content:flex-end;}
.list-view .c-cat  {margin-bottom:0; flex-basis:15%; flex-shrink:0; text-align:left;}
.list-view .c-meta {margin-bottom:0; flex-basis:15%; flex-shrink:0; justify-content:flex-end; gap:0.6rem;}

.tag{display:inline-block;padding:0.15rem 0.5rem;border-radius:6px;font-size:0.7rem;font-weight:500;background-color:var(--card);border:1px solid var(--border2);color:#ffffff;}
.tag-blue{background:#1e3a5f;border-color:#3b82f640;color:var(--accent2);}

.class-item{background:var(--card2);border:1px solid var(--border);border-radius:var(--radius);padding:.8rem 1rem;cursor:pointer;transition:all .15s;}
.class-item:hover{border-color:var(--accent);transform:translateY(-2px);}
.class-item.selected{border-color:var(--green);background:#064e3b30;}

.class-item-single{padding:.2rem 0.2rem;cursor:pointer;transition:all .15s;position:relative;overflow:hidden;}
.class-item-single:hover{transform:translateX(4px);}
.class-item-single.selected{color:inherit;}
.log-line .msg .latency-note{color:var(--accent2);font-size:.72rem;opacity:.95;white-space:nowrap;}

/* Target Banner */
.target-banner{background:linear-gradient(135deg,#172554,#1e1b4b);border:1px solid #3b82f640;border-radius:8px;padding:.75rem;margin-bottom:.75rem}
.target-banner .info{font-weight:600;font-size:.85rem;margin-bottom:.2rem}
.target-banner .sub{color:#ffffff;font-size:.7rem;}
.target-banner.compact{background:var(--card2);border:1px solid var(--border);border-radius:var(--radius);padding:0.8rem;text-align:left;margin-bottom:0.5rem;transition:all 0.2s;}
.target-banner-title{font-size:0.95rem;font-weight:600;color:#93c5fd;margin-bottom:0.4rem;}
.target-banner-id{font-size:0.75rem;color:#9ca3af;margin-bottom:0.3rem;}
.target-banner-meta{display:flex;justify-content:space-between;align-items:center;margin-bottom:0.4rem;}
.target-banner-meta-text{font-size:0.85rem;color:#9ca3af;display:flex;align-items:center;}
.target-banner-body{color:#f3f4f6;font-size:.85rem;line-height:1.4;display:grid;gap:0.2rem;}
.target-banner-row{display:flex;align-items:center;}

/* Log Area */
.log-scroll{flex:1;overflow-y:auto;font-family:monospace;font-size:.75rem;line-height:1.6;padding:.75rem;}
.log-line { display: flex; align-items: flex-start; gap: 0.5rem; margin-bottom: 0.25rem; font-family: monospace; }
.log-line .time { color: #f3f4f6; flex-shrink: 0; line-height: 1.5; padding-top: 0; }
.log-line.SUCCESS .msg { color: var(--green); font-weight: 600; }
.log-line.ERROR .msg { color: var(--red); }
.log-line.WARN .msg { color: var(--orange); }
.log-line .msg { display: flex; align-items: flex-start; gap: 0.35rem; line-height: 1.5; padding-top: 0; }
.log-line .msg .ico { width: 1.25em !important; height: 1.25em !important; display: inline-flex; justify-content: center; align-items: center; flex-shrink: 0; margin-top: 0.22em !important; }
.log-line .msg .ico svg { width: 100% !important; height: 100% !important; display: block !important; margin: 0 !important; }

/* engine status */
.engine-status{display:flex; align-items:center; justify-content:center; gap:0.4rem; padding:0.65rem; border-radius:10px; font-size:1rem; font-weight:bold; letter-spacing:0.05em; margin-bottom:0.6rem; width:100%; transition:all 0.4s cubic-bezier(0.25, 1, 0.5, 1); box-sizing:border-box; overflow:hidden; max-height:60px; opacity:1;}
.engine-status.idle{background:var(--card2);color:#f3f4f6; max-height:0; padding-top:0; padding-bottom:0; margin-bottom:0; opacity:0;}
.engine-status.running{background:#172554;color:var(--accent2)}
.engine-status.success{background:var(--green-bg);color:var(--green)}
.spinner{width:14px;height:14px;display:inline-block;flex-shrink:0;animation:spin .8s linear infinite !important;background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 1024 1024' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M960 576h-128c-35.392 0-64-28.608-64-64a64 64 0 0 1 64-64h128a64 64 0 0 1 64 64c0 35.392-28.608 64-64 64z m-176.512-244.992c-25.024 25.024-65.472 25.024-90.496 0s-25.024-65.472 0-90.496l90.496-90.56a64 64 0 1 1 90.496 90.56l-90.496 90.496zM512 1024a64 64 0 0 1-64-64v-128a64.021333 64.021333 0 0 1 128 0v128c0 35.392-28.608 64-64 64z m0-768a64 64 0 0 1-64-64V64a64.021333 64.021333 0 0 1 128 0v128c0 35.392-28.608 64-64 64zM240.448 874.048c-25.024 25.024-65.472 25.024-90.496 0s-25.024-65.536 0-90.496l90.496-90.56a64 64 0 1 1 90.496 90.56l-90.496 90.496z m0-543.04l-90.496-90.496a64 64 0 1 1 90.496-90.56l90.496 90.56a63.936 63.936 0 0 1 0 90.496 64.042667 64.042667 0 0 1-90.496 0zM256 512a64 64 0 0 1-64 64H64a64 64 0 1 1 0-128h128c35.328 0 64 28.672 64 64z m527.488 180.992l90.496 90.56c25.024 24.96 25.024 65.472 0 90.496s-65.472 25.024-90.496 0l-90.496-90.496a64 64 0 1 1 90.496-90.56z' fill='%2360a5fa'/%3E%3C/svg%3E");background-size:contain;background-repeat:no-repeat;background-position:center;}
@keyframes spin{to{transform:rotate(360deg)}}

.steps{display:flex;gap:0;background-color:var(--card);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;}
.step{flex:1;text-align:center;padding:.5rem;font-size:.65rem;font-weight:600;color:#f3f4f6;border-right:1px solid var(--border);background:var(--card2);}
.step:last-child{border-right:none;}
.step.active{color:var(--accent2);background-color:var(--card);}
.step.done{color:var(--green);background-color:var(--card);}

/* SVG 内联图标基础样式 */
.ico { display:inline-flex; align-items:center; justify-content:center; width:1em; height:1em; vertical-align:-0.125em; flex-shrink:0; }
.ico svg { width:100%; height:100%; fill:currentColor; }
.anim-arrow { transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1); display: inline-block; }
.anim-arrow.up { transform: rotate(-180deg); }

select:focus, input:focus, button:focus {
    outline: none !important;
    box-shadow: none !important;
}


.unified-height {
    height: 38px;
    box-sizing: border-box;
    display: inline-flex;
    align-items: center;
}
.filter-arrow-svg {
    transition: transform 0.2s;
}
.filter-arrow-svg.up {
    transform: rotate(180deg);
}


/* 自定义原生选择器小箭头 */

.custom-select:focus {
    border-color: var(--border) !important;
    outline: none !important;
    box-shadow: none !important;
    /* 展开时箭头旋转 */
    background-image: url("data:image/svg+xml;charset=UTF-8,%3Csvg viewBox='0 0 1024 1024' xmlns='http://www.w3.org/2000/svg' fill='%239ca3af' style='transform: rotate(180deg);'%3E%3Cpath d='M831.872 340.864L512 652.672 192.128 340.864a30.592 30.592 0 0 0-42.752 0 29.12 29.12 0 0 0 0 41.6l342.4 333.312a32 32 0 0 0 40.448 0l342.4-333.312a29.12 29.12 0 0 0 0-41.6 30.592 30.592 0 0 0-42.752 0z'/%3E%3C/svg%3E");
}

.custom-select {
    appearance: none;
    -webkit-appearance: none;
    background-image: url("data:image/svg+xml;charset=UTF-8,%3Csvg viewBox='0 0 1024 1024' xmlns='http://www.w3.org/2000/svg' fill='%239ca3af'%3E%3Cpath d='M831.872 340.864L512 652.672 192.128 340.864a30.592 30.592 0 0 0-42.752 0 29.12 29.12 0 0 0 0 41.6l342.4 333.312a32 32 0 0 0 40.448 0l342.4-333.312a29.12 29.12 0 0 0 0-41.6 30.592 30.592 0 0 0-42.752 0z'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 0.8rem center;
    background-size: 1.2em;
    transition: background-image 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    padding-right: 1.6rem !important;
}

/* 之前加的 unified-height */
/* 修改 filter-dropdown-menu 的动画 */
.filter-dropdown-menu {
    display: flex;
    visibility: hidden;
    opacity: 0;
    transform: translateY(-8px) scale(0.98);
    position: absolute;
    top: 48px;
    right: 0;
    background-color:var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1rem;
    z-index: 100;
    flex-direction: column;
    gap: 0.8rem;
    min-width: 200px;
    box-shadow: 0 10px 25px -5px rgba(0,0,0,0.5);
    transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
    pointer-events: none;
}
.filter-dropdown-menu.show {
    visibility: visible;
    opacity: 1;
    transform: translateY(0) scale(1);
    pointer-events: auto;
}


/* 自定义深色悬浮 Select 系统取代原生系统下拉 */
.unified-select-wrapper {
    min-width: 120px;
}
.unified-select-trigger {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0 0.8rem;
    color: var(--text);
    font-size: 0.75rem;
    display: flex;
    justify-content: space-between;
    align-items: center;
    cursor: pointer;
    transition: all 0.2s;
}
.unified-select-trigger:hover {
    border-color: var(--text3);
}
.unified-select-arrow {
    color: currentColor;
}
.unified-select-arrow.up {
    transform: rotate(180deg);
}
.unified-select-options {
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    right: 0;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    box-shadow: 0 10px 25px -5px rgba(0,0,0,0.5);
    z-index: 999;
    max-height: 250px;
    overflow-y: auto;

    visibility: hidden;
    opacity: 0;
    transform: translateY(-8px) scale(0.98);
    transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
    pointer-events: none;

    /* 滚动条美化 */
    scrollbar-width: thin;
    scrollbar-color: var(--border2) transparent;
}
.unified-select-options.show {
    visibility: visible;
    opacity: 1;
    transform: translateY(0) scale(1);
    pointer-events: auto;
}
.unified-select-options::-webkit-scrollbar {
    width: 6px;
}
.unified-select-options::-webkit-scrollbar-thumb {
    background-color: var(--border2);
    border-radius: 10px;
}
.unified-select-option {
    padding: 0.6rem 0.8rem;
    font-size: 0.75rem;
    color: var(--text);
    cursor: pointer;
    transition: background 0.1s;
}
.unified-select-option:hover {
    background: var(--border2);
}
.unified-select-option.active {
    color: var(--accent2);
    font-weight: 600;
}




</style>
</head>
<body>
<div class="topbar">
  <h1>青果选课平台-Beta</h1>
  <div style="display:flex; align-items:center; gap:1.2rem;">
    <div class="status" id="pingStatus" style="font-size:0.7rem; color:#f3f4f6;"><span class="ico"><svg style="color:var(--accent2);" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M640 213.333333h85.333333a85.333333 85.333333 0 0 1 85.333334 85.333334v348.586666a128.042667 128.042667 0 1 1-85.333334 0V298.666667h-85.333333v128l-192-170.666667L640 85.333333v128z"/><path d="M213.333333 376.746667a128.042667 128.042667 0 1 1 85.333334 0v270.506666a128.042667 128.042667 0 1 1-85.333334 0V376.746667z"/></svg></span> --ms</div>
    <div class="status"><div class="dot" id="statusDot"></div><span id="statusText">未登录</span></div>
    <button class="btn btn-ghost toolbar-mini-btn" id="btnToggleLogin" onclick="toggleSidebar()" style="display:none;"><span class="ico"><svg style="color:var(--text); vertical-align:-0.15em; width:1.2em; height:1.2em;" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M288 277.333333a32 32 0 0 0-32 32v405.333334c0 17.664 14.336 32 32 32H362.666667a32 32 0 0 0 32-32v-405.333334a32 32 0 0 0-32-32H288z"/><path d="M213.333333 106.666667a128 128 0 0 0-128 128v554.666666a128 128 0 0 0 128 128h597.333334a128 128 0 0 0 128-128v-554.666666a128 128 0 0 0-128-128H213.333333z m597.333334 85.333333a42.666667 42.666667 0 0 1 42.666666 42.666667v554.666666a42.666667 42.666667 0 0 1-42.666666 42.666667H213.333333a42.666667 42.666667 0 0 1-42.666666-42.666667v-554.666666a42.666667 42.666667 0 0 1 42.666666-42.666667h597.333334z"/></svg></span> 控制面板</button>
    <button class="btn btn-ghost toolbar-mini-btn" id="btnLogoutHead" onclick="logout()" style="display:none;"><span class="ico"><svg style="color:var(--red);" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M853.33 362.67v42.67a21.33 21.33 0 0 1-21.33 21.33H810.67v99.84a170.67 170.67 0 0 1-49.92 120.75l-83.2 85.33A128 128 0 0 1 597.33 768v149.33a21.33 21.33 0 0 1-21.33 21.33h-128a21.33 21.33 0 0 1-21.33-21.33V768a128 128 0 0 1-80.21-36.69l-83.2-85.33A170.67 170.67 0 0 1 213.33 526.51V426.67h-21.33a21.33 21.33 0 0 1-21.33-21.33v-42.67a21.33 21.33 0 0 1 21.33-21.33H298.67V106.67a21.33 21.33 0 0 1 21.33-21.33h42.67a21.33 21.33 0 0 1 21.33 21.33V341.33h256V106.67a21.33 21.33 0 0 1 21.33-21.33h42.67a21.33 21.33 0 0 1 21.33 21.33V341.33h106.67a21.33 21.33 0 0 1 21.33 21.33z"/></svg></span> 挂断会话</button>
    <button class="btn btn-ghost toolbar-mini-btn" onclick="window.open('/jw/wsxk/stu_xszx.aspx', '_blank')"><span class="ico"><svg style="color:var(--accent);" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M698.03 597.33C701.44 569.17 704 541.01 704 512 704 482.99 701.44 454.83 698.03 426.67L842.24 426.67C849.07 453.97 853.33 482.56 853.33 512 853.33 541.44 849.07 570.03 842.24 597.33M622.51 834.56C648.11 787.2 667.73 736 681.39 682.67L807.25 682.67C766.29 753.07 701.01 807.68 622.51 834.56M611.84 597.33 412.16 597.33C407.89 569.17 405.33 541.01 405.33 512 405.33 482.99 407.89 454.4 412.16 426.67L611.84 426.67C615.68 454.4 618.67 482.99 618.67 512 618.67 541.01 615.68 569.17 611.84 597.33M512 851.63C476.59 800.43 448 743.68 430.51 682.67L593.49 682.67C576 743.68 547.41 800.43 512 851.63M341.33 341.33 216.75 341.33C257.28 270.51 322.99 215.89 401.07 189.44 375.47 236.8 356.27 288 341.33 341.33M216.75 682.67 341.33 682.67C356.27 736 375.47 787.2 401.07 834.56 322.99 807.68 257.28 753.07 216.75 682.67M181.76 597.33C174.93 570.03 170.67 541.44 170.67 512 170.67 482.56 174.93 453.97 181.76 426.67L325.97 426.67C322.56 454.83 320 482.99 320 512 320 541.01 322.56 569.17 325.97 597.33M512 171.95C547.41 223.15 576 280.32 593.49 341.33L430.51 341.33C448 280.32 476.59 223.15 512 171.95M807.25 341.33 681.39 341.33C667.73 288 648.11 236.8 622.51 189.44 701.01 216.32 766.29 270.51 807.25 341.33M512 85.33C276.05 85.33 85.33 277.33 85.33 512 85.33 747.52 276.48 938.67 512 938.67 747.52 938.67 938.67 747.52 938.67 512 938.67 276.48 747.52 85.33 512 85.33Z"/></svg></span> 教务选课网页</button>
    <button class="btn btn-ghost toolbar-mini-btn" id="btnToggleLog" onclick="toggleLog()"><span class="ico"><svg style="color:#a78bfa;" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M853.33 981.33H170.67c-23.47 0-42.67-19.2-42.67-42.67V85.33c0-23.47 19.2-42.67 42.67-42.67h682.67c23.47 0 42.67 19.2 42.67 42.67v853.33c0 23.47-19.2 42.67-42.67 42.67zm-618.67-85.33h554.67c12.8 0 21.33-8.53 21.33-21.33V149.33c0-12.8-8.53-21.33-21.33-21.33H234.67c-12.8 0-21.33 8.53-21.33 21.33v725.33c0 12.8 8.53 21.33 21.33 21.33z M654.93 334.93H369.07c-23.47 0-42.67-19.2-42.67-42.67s19.2-42.67 42.67-42.67h283.73c23.47 0 42.67 19.2 42.67 42.67 2.13 23.47-17.07 42.67-40.53 42.67zM654.93 524.8H369.07c-23.47 0-42.67-19.2-42.67-42.67s19.2-42.67 42.67-42.67h283.73c23.47 0 42.67 19.2 42.67 42.67 2.13 23.47-17.07 42.67-40.53 42.67zM654.93 710.4H369.07c-23.47 0-42.67-19.2-42.67-42.67s19.2-42.67 42.67-42.67h283.73c23.47 0 42.67 19.2 42.67 42.67 2.13 23.47-17.07 42.67-40.53 42.67z"/></svg></span> 展开日志</button>
  </div>
</div>

<div class="app-container">
  <!-- 左栏：<span class="ico"><svg style="color:var(--text); vertical-align:-0.15em; width:1.2em; height:1.2em;" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M288 277.333333a32 32 0 0 0-32 32v405.333334c0 17.664 14.336 32 32 32H362.666667a32 32 0 0 0 32-32v-405.333334a32 32 0 0 0-32-32H288z"/><path d="M213.333333 106.666667a128 128 0 0 0-128 128v554.666666a128 128 0 0 0 128 128h597.333334a128 128 0 0 0 128-128v-554.666666a128 128 0 0 0-128-128H213.333333z m597.333334 85.333333a42.666667 42.666667 0 0 1 42.666666 42.666667v554.666666a42.666667 42.666667 0 0 1-42.666666 42.666667H213.333333a42.666667 42.666667 0 0 1-42.666666-42.666667v-554.666666a42.666667 42.666667 0 0 1 42.666666-42.666667h597.333334z"/></svg></span> 控制面板 -->
  <div class="sidebar" id="sidebarPanel">

    <!-- 登录 -->
    <div class="card" id="cardLogin">
      <div class="login-card-header">
        <h3 class="login-card-title"><span class="ico" style="width:2.5em; height:2.5em;"><svg style="color:var(--accent2); width:100%; height:100%;" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M512 85.333c-235.64 0-426.666 191.026-426.666 426.667 0 235.64 191.026 426.666 426.666 426.666 235.64 0 426.667-191.026 426.667-426.666 0-235.64-191.026-426.667-426.667-426.667zm0 170.667c70.693 0 128 57.307 128 128s-57.307 128-128 128-128-57.307-128-128 57.307-128 128-128zm0 512c-106.027 0-197.888-59.563-245.973-147.2 4.437-81.579 164.864-108.8 245.973-108.8 81.024 0 241.451 27.221 245.973 108.8-48.043 87.637-139.904 147.2-245.973 147.2z"/></svg></span> 登录教务系统</h3>
        <div class="login-more-wrap" id="loginMoreToggle" onclick="toggleLoginMorePanel()" title="更多设置">
          <span class="login-more-inline-arrow"><svg class="unified-select-arrow" viewBox="0 0 1024 1024" fill="currentColor" width="1.1em" height="1.1em"><path d="M831.872 340.864L512 652.672 192.128 340.864a30.592 30.592 0 0 0-42.752 0 29.12 29.12 0 0 0 0 41.6l342.4 333.312a32 32 0 0 0 40.448 0l342.4-333.312a29.12 29.12 0 0 0 0-41.6 30.592 30.592 0 0 0-42.752 0z"/></svg></span>
        </div>
      </div>
      <div class="login-more-content" id="loginMorePanel">
        <div class="login-more-inner">
          <div class="field login-more-num">
            <label>定时登录（分钟后）</label>
            <div class="num-ctrl">
              <input id="inpDelayedLoginMinutes" type="number" min="0" step="0.5" placeholder="例如 5 或 0.5">
              <div class="btns">
                <button class="btn-s" type="button" onclick="document.getElementById('inpDelayedLoginMinutes').stepUp()"><svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor"><path d="M7 14l5-5 5 5z"/></svg></button>
                <button class="btn-s" type="button" onclick="document.getElementById('inpDelayedLoginMinutes').stepDown()"><svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor"><path d="M7 10l5 5 5-5z"/></svg></button>
              </div>
            </div>
          </div>
          <label class="field custom-checkbox" style="margin-bottom:0.2rem;">
            <input type="checkbox" id="chkLoginRetryUntilSuccess" checked>
            <span class="checkmark"></span>
            <span style="font-size:.78rem;color:#ffffff;">持续请求直到登录成功</span>
          </label>
        </div>
      </div>
      <div class="field">
        <label>学号</label>
        <div style="position:relative;">
          <input id="inpUser" placeholder="请输入学号" autocomplete="off">
          <div id="accountDropdown" class="account-dropdown" style="display:none;"></div>
        </div>
      </div>
      <div class="field"><label>密码</label><input type="password" id="inpPass" placeholder="请输入密码"></div>
      <button class="btn btn-primary w-full" id="btnLogin" onclick="doLogin()">登录</button>
    </div>

    <!-- 引擎控制 -->
    <div class="card" id="cardEngine" style="display:none">
      <h3> 选课控制</h3>
      <div id="targetBanner"></div>
      <div class="field">
        <label>请求等待时间（秒）</label>
        <div class="num-ctrl">
          <input type="number" id="inpInterval" value="4" step="0.5" min="0">
          <div class="btns">
             <button class="btn-s" onclick="document.getElementById('inpInterval').stepUp()"><svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor"><path d="M7 14l5-5 5 5z"/></svg></button>
             <button class="btn-s" onclick="document.getElementById('inpInterval').stepDown()"><svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor"><path d="M7 10l5 5 5-5z"/></svg></button>
          </div>
        </div>
      </div>
      <label class="field custom-checkbox" style="margin-bottom:0.8rem;">
        <input type="checkbox" id="chkVerify" checked>
        <span class="checkmark"></span>
        <span style="font-size:.78rem;color:#ffffff;">选课后二次验证是否成功</span>
      </label>
      <label class="field custom-checkbox" style="margin-bottom:0.8rem; margin-top:-0.25rem;">
        <input type="checkbox" id="chkBizLatency">
        <span class="checkmark"></span>
        <span style="font-size:.78rem;color:#ffffff;">在日志中显示业务请求延迟</span>
      </label>
      <div id="engineStatus" class="engine-status idle" style="font-size:1rem; position:relative;">
          <div id="engineProgressFill" style="position:absolute; left:0; top:0; bottom:0; width:0%; background:rgba(255,255,255,0.1); transition:width 0.1s linear; z-index:0;"></div>
          <span id="engineStatusText" style="position:relative; z-index:1; display:flex; align-items:center; justify-content:center; gap:0.3rem; height:100%;">⏸ 待命</span>
      </div>
      <button class="btn btn-success btn-massive" id="btnStart" onclick="startSnatch()" style="margin-top:.4rem;">▶ 启动选课</button>
      <button class="btn btn-danger btn-massive" id="btnStop" onclick="stopSnatch()" style="margin-top:.4rem;display:none"><span class="ico"><svg t="1773332868844" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="5658" fill="currentColor"><path d="M768 832h-128c-35.392 0-64-28.608-64-64V256c0-35.392 28.608-64 64-64h128c35.392 0 64 28.608 64 64v512c0 35.392-28.608 64-64 64z m-384 0h-128c-35.392 0-64-28.608-64-64V256c0-35.392 28.608-64 64-64h128c35.392 0 64 28.608 64 64v512c0 35.392-28.608 64-64 64z" p-id="5659"></path></svg></span> 停止</button>
      <!-- 进度条融入上级容器 -->
    </div>
  </div>

  <!-- 中间主要区域：课程浏览 -->
  <div class="main-content">
    <div class="content-header" id="filterBar" style="display:none; flex-wrap: wrap; justify-content: space-between; align-items:center; gap: 1rem;">
       <div style="display:flex; align-items:center; gap:1.5rem;">
         <div style="font-weight:600; white-space:nowrap;">选择范围</div>
         <div id="filterSelects" style="display:flex; flex-wrap:wrap; align-items:center; gap:0.8rem;"></div>
         <button class="btn btn-primary" id="btnFetch" onclick="fetchCourses()" style="padding:0.4rem 1rem; white-space:nowrap;"><span class="ico"><svg style="color:currentColor;" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M640 213.333333h85.333333a85.333333 85.333333 0 0 1 85.333334 85.333334v348.586666a128.042667 128.042667 0 1 1-85.333334 0V298.666667h-85.333333v128l-192-170.666667L640 85.333333v128z"/><path d="M213.333333 376.746667a128.042667 128.042667 0 1 1 85.333334 0v270.506666a128.042667 128.042667 0 1 1-85.333334 0V376.746667z"/></svg></span> 拉取课程</button>
       </div>

       <!-- 高级筛选与搜索区域 -->
       <div id="advancedFilters" class="panel-inline">
           <!-- 搜索框 -->
           <div style="position:relative; display:flex; align-items:center; flex:1; min-width:140px;">
             <div style="position:absolute; left:0.6rem; color:#f3f4f6; pointer-events:none; display:flex;">
               <span class="ico"><svg style="color:var(--accent);" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M940.57 852.69L726.1 637.97c44.78-60.86 69.26-134.08 69.26-211.01 0-95.73-37.35-185.78-104.89-253.31-67.78-67.54-157.83-104.89-253.56-104.89s-185.78 37.35-253.31 104.89c-139.52 139.52-139.52 366.86 0 506.63 67.54 67.54 157.58 104.89 253.31 104.89 77.18 0 150.16-24.49 211.26-69.26l214.48 214.72c21.52 21.52 56.4 21.52 77.68 0 21.77-21.52 21.77-56.4 0.25-77.92zM252.87 611.26c-101.43-101.67-101.43-266.92 0-368.34 49.23-49.23 114.54-76.19 184.3-76.19 69.51 0 134.82 26.97 184.05 76.19 49.23 49.23 76.44 114.54 76.44 184.3 0 69.76-27.21 135.07-76.44 184.3-49.23 49.23-114.54 76.19-184.05 76.19-69.76-0.25-135.07-27.21-184.3-76.44z"/></svg></span>
             </div>
             <input type="text" class="unified-height ui-control ui-search-input" id="inpCourseSearch" placeholder="搜索课程名称/代码..." oninput="applyCourseFilters()">
           </div>
           <!-- 筛选菜单触发器 -->
           <div style="position:relative;">
               <button id="btnToggleFilterMenu" onclick="toggleFilterMenu()" class="unified-height ui-btn-lite" style="padding:0 0.8rem; gap:0.4rem;" title="展开高级筛选">
                   <svg style="color:currentColor;" viewBox="0 0 1024 1024" fill="currentColor" width="1.2em" height="1.2em" vertical-align="-0.15em"><path d="M675.693 121.905c42.569 0 77.068 35.133 77.068 78.482 0 15.116-4.29 29.867-12.336 42.569l-186.783 294.473v364.642l-269.214-182.76v-181.882L97.67 242.956c-23.089-36.401-12.825-84.992 22.918-108.495A76.069 76.069 0 0 1 162.402 121.905h513.292z m160.231 601.624v78.458H604.72v-78.458h231.205z m102.742-130.78v78.458H604.721v-78.458h333.945z"></path></svg>
                   <span>筛选面板</span>
                   <svg id="filterMenuArrow" class="anim-arrow" style="width:1.2em; height:1.2em; vertical-align:-0.15em;" viewBox="0 0 1024 1024" fill="currentColor" width="1em" height="1em"><path d="M831.872 340.864L512 652.672 192.128 340.864a30.592 30.592 0 0 0-42.752 0 29.12 29.12 0 0 0 0 41.6l342.4 333.312a32 32 0 0 0 40.448 0l342.4-333.312a29.12 29.12 0 0 0 0-41.6 30.592 30.592 0 0 0-42.752 0z"/></svg>
               </button>
               <!-- 下拉面板内容 -->
               <div id="filterMenuBody" class="filter-dropdown-menu">
                   <div class="filter-section-title">排序与视图</div>
                   <!-- 排序 -->
                   <button id="btnSortOrder" onclick="toggleSortOrder()" data-asc="false" class="unified-height ui-btn-lite" style="padding:0 0.8rem; justify-content:space-between;" title="切换升序/降序">
                       <span id="textSortOrder">降序</span>
                       <svg class="anim-arrow" style="width:1.2em; height:1.2em; vertical-align:-0.15em;" viewBox="0 0 1024 1024" fill="currentColor"><path d="M831.872 340.864L512 652.672 192.128 340.864a30.592 30.592 0 0 0-42.752 0 29.12 29.12 0 0 0 0 41.6l342.4 333.312a32 32 0 0 0 40.448 0l342.4-333.312a29.12 29.12 0 0 0 0-41.6 30.592 30.592 0 0 0-42.752 0z"/></svg>
                   </button>
                   <select id="selSortType" class="unified-height custom-select ui-control ui-select" onchange="applyCourseFilters()">
                     <option value="default">默认排序</option>
                     <option value="credit">按学分</option>
                     <option value="hours">按总学时</option>
                     <option value="capacity">按剩余名额 (已知)</option>
                   </select>

                   <div class="filter-section-title with-top-gap">高级筛选</div>
                   <select id="selFilterCredit" class="unified-height custom-select ui-control ui-select" onchange="applyCourseFilters()">
                     <option value="">所有学分</option>
                   </select>
                   <select id="selFilterHours" class="unified-height custom-select ui-control ui-select" onchange="applyCourseFilters()">
                     <option value="">所有学时</option>
                   </select>
                   <select id="selFilterCategory" class="unified-height custom-select ui-control ui-select" onchange="applyCourseFilters()">
                     <option value="">所有类别</option>
                   </select>
                   <!-- 新增按时间（星期）筛选 -->
                   <select id="selFilterSchedule" class="unified-height custom-select ui-control ui-select" onchange="applyCourseFilters()">
                     <option value="">所有课程时间</option>
                     <option value="一">星期一</option>
                     <option value="二">星期二</option>
                     <option value="三">星期三</option>
                     <option value="四">星期四</option>
                     <option value="五">星期五</option>
                     <option value="六">星期六</option>
                     <option value="日">星期日</option>
                   </select>
               </div>
           </div>
       </div>
    </div>

    <div class="content-body">
       <!-- 空状态 -->
       <div id="emptyState" style="text-align:center; margin-top:15vh; color:#f3f4f6; display:flex; flex-direction:column; align-items:center;">
         <div style="font-size:3.5rem; margin-bottom:1rem;"><svg width="1.2em" height="1.2em" style="vertical-align:-0.15em; fill:currentColor;" t="1772773786831" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="12079" data-darkreader-inline-fill="currentColor"  ><path d="M292.571429 358.144a18.285714 18.285714 0 0 0 31.232 12.909714l65.536-65.536a18.285714 18.285714 0 0 1 25.892571 0l65.536 65.536A18.285714 18.285714 0 0 0 512 358.144V73.142857h329.142857c40.228571 0 73.142857 32.914286 73.142857 73.142857v731.428572c0 40.228571-32.914286 73.142857-73.142857 73.142857H182.857143c-40.228571 0-73.142857-32.914286-73.142857-73.142857V146.285714c0-40.228571 32.914286-73.142857 73.142857-73.142857h109.714286z" fill="currentColor" p-id="12080" style="--darkreader-inline-fill: var(--darkreader-background-9094a1, #52595c);" data-darkreader-inline-fill="currentColor"></path></svg></div>
         <h2 style="color:#ffffff">欢迎使用选课平台</h2>
         <p style="margin-top:0.5rem;">请先在左侧登录教务系统，然后拉取课程</p>
       </div>

       <!-- 课程与班级区 -->
       <!-- 课程与班级区 -->
       <div id="titleCourses" style="display:none; margin-bottom:1rem; flex-direction:column; gap:0.8rem;">
         <div style="display:flex; align-items:center; justify-content:space-between;">
           <div style="display:flex;align-items:center;gap:1.2rem;">
             <h3 style="margin:0;"><svg width="1.2em" height="1.2em" style="vertical-align:-0.15em; fill:currentColor;" t="1772773786831" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="12079" data-darkreader-inline-fill="currentColor"  ><path d="M292.571429 358.144a18.285714 18.285714 0 0 0 31.232 12.909714l65.536-65.536a18.285714 18.285714 0 0 1 25.892571 0l65.536 65.536A18.285714 18.285714 0 0 0 512 358.144V73.142857h329.142857c40.228571 0 73.142857 32.914286 73.142857 73.142857v731.428572c0 40.228571-32.914286 73.142857-73.142857 73.142857H182.857143c-40.228571 0-73.142857-32.914286-73.142857-73.142857V146.285714c0-40.228571 32.914286-73.142857 73.142857-73.142857h109.714286z" fill="currentColor" p-id="12080" style="--darkreader-inline-fill: var(--darkreader-background-9094a1, #52595c);" data-darkreader-inline-fill="currentColor"></path></svg> 课程列表 <span style="font-size:0.75rem;color:#f3f4f6;font-weight:normal;">(点击选择班级)</span></h3>
             <button class="btn btn-ghost" id="btnRefreshCourses" onclick="refreshCourses()" style="padding:0.2rem 0.6rem; font-size:0.75rem; border:1px solid var(--border);"><span class="ico"><svg style="color:#93c5fd;" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M936.43 603.42q0 2.85-0.58 4-36.58 153.15-153.15 248.29t-273.15 95.14q-83.42 0-161.44-31.42t-139.14-89.73l-73.73 73.73q-10.85 10.85-25.73 10.85t-25.73-10.85-10.85-25.73l0-256q0-14.85 10.85-25.73t25.73-10.85l256 0q14.85 0 25.73 10.85t10.85 25.73-10.85 25.73l-78.27 78.27q40.58 37.73 92 58.27t106.85 20.58q76.58 0 142.85-37.15t106.27-102.27q6.27-9.73 30.27-66.85 4.58-13.15 17.15-13.15l109.73 0q7.42 0 12.86 5.44t5.44 12.86zM950.74 146.27l0 256q0 14.85-10.85 25.73t-25.73 10.85l-256 0q-14.85 0-25.73-10.85t-10.85-25.73 10.85-25.73l78.85-78.85q-84.58-78.27-199.42-78.27-76.58 0-142.85 37.15t-106.27 102.27q-6.27 9.73-30.27 66.85-4.58 13.15-17.15 13.15l-113.73 0q-7.42 0-12.86-5.44t-5.44-12.86l0-4q37.15-153.15 154.27-248.29t274.27-95.14q83.42 0 162.27 31.71t140 89.44l74.27-73.73q10.85-10.85 25.73-10.85t25.73 10.85 10.85 25.73z"/></svg></span> 刷新列表</button>
           </div>
           <div class="view-toggle">
             <button class="view-btn active" id="btnViewList" onclick="setViewMode('list')" title="条形视图">☰ 条形</button>
             <button class="view-btn" id="btnViewGrid" onclick="setViewMode('grid')" title="网格视图">☷ 网格</button>
           </div>
         </div>
       </div>

       <div id="courseList" class="list-view"></div>

    </div>
  </div>

  <!-- 右侧日志边栏 -->
  <div class="log-sidebar" id="logPanel">
    <div style="padding:1rem; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center;">
      <h3 style="font-size:.85rem; margin:0; font-weight:600;"><span class="ico"><svg style="color:var(--green);" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M853.33 810.67V298.67H170.67v512h682.67m0-682.67a85.33 85.33 0 0 1 85.33 85.33v597.33a85.33 85.33 0 0 1-85.33 85.33H170.67a85.33 85.33 0 0 1-85.33-85.33V213.33a85.33 85.33 0 0 1 85.33-85.33h682.67m-298.67 597.33v-85.33h213.33v85.33h-213.33m-145.92-170.67L237.65 384H358.4l140.8 140.8c16.64 16.64 16.64 43.95 0 60.59L359.25 725.33H238.51l170.24-170.67z"/></svg></span> 控制台日志</h3>
      <button class="btn-ghost" style="border:none; cursor:pointer; padding:0.2rem 0.5rem; border-radius:4px" onclick="toggleLog()">✖</button>
    </div>
    <div class="log-scroll" id="logScroll"></div>
    <div class="log-footer" id="logFooter">
      <div style="font-size:0.7rem; color:var(--text2);">
        <div style="display:flex; justify-content:space-between; margin-bottom:3px;">
          <span>Session 有效期</span>
          <span id="sessionCountdown" style="color:var(--accent2); font-weight:600;">--:--</span>
        </div>
        <div class="mini-progress">
          <div class="mini-progress-fill" id="sessionBar" style="width:0%; background:var(--green);"></div>
        </div>
      </div>
      <div style="font-size:0.7rem; color:var(--text2); display:flex; justify-content:space-between;">
        <span>最近20秒请求</span>
        <span id="reqRateValue" style="color:var(--accent2); font-weight:600;">0 次/20秒</span>
      </div>
    </div>
  </div>
</div>

<script>
// ========================================================
// 前端延迟与防刷限制时间集中管理 (单位：毫秒)
// ========================================================
const CONFIG_DELAY_COURSE_REFRESH = {{ DELAY_COURSE_REFRESH_MS }};  // 刷新课程列表防抖冷却时间
const CONFIG_DELAY_CLASS_FETCH = {{ DELAY_CLASS_FETCH_MS }};     // 拉取班级详情防抖冷却时间
const CONFIG_POLL_LOGS = {{ POLL_LOGS_MS }};             // 日志轮询间隔
const CONFIG_POLL_STATE = {{ POLL_STATE_MS }};             // 状态轮询间隔
const CONFIG_POLL_PING = {{ POLL_PING_MS }};            // 延迟检测轮询间隔
// ========================================================

const WAIT_ICON = `<span class="ico"><svg viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg"><path d="M856 64c22.091 0 40 17.909 40 40s-17.909 40-40 40h-26.273c-14.244 141.436-94.29 297.85-202.86 366.539l-0.71 0.44v3.652l0.705 0.442c107.891 68.869 187.804 224.251 202.65 364.928L856 880c22.091 0 40 17.909 40 40s-17.909 40-40 40H168c-22.091 0-40-17.909-40-40s17.909-40 40-40l26.459 0.001c14.704-141.014 94.054-296.803 201.896-365.421l3.285-2.055v-0.423l-3.282-2.056C288.183 441.104 208.484 285.1 194.269 144H168c-22.091 0-40-17.909-40-40s17.909-40 40-40h688zM477.567 695.579l-0.559 0.564L371.046 804.98a16 16 0 0 0-4.536 11.117c-0.025 8.836 7.119 16.02 15.955 16.044l18.764 0.043c36.185 0.065 70.62 0.018 103.304-0.14l19.75-0.101c35.982-0.165 74.913-0.191 116.792-0.08a16 16 0 0 0 11.263-4.594c6.212-6.11 6.379-16.046 0.44-22.362l-0.254-0.264L545.62 695.966c-0.243-0.246-0.488-0.49-0.735-0.731-18.805-18.308-48.777-18.092-67.318 0.344zM442.76 361.015c-8.837 0.015-15.988 7.19-15.974 16.027a16 16 0 0 0 4.535 11.134l46.407 47.672c18.49 18.996 48.88 19.405 67.876 0.914a48 48 0 0 0 0.74-0.737l46.77-47.552c6.196-6.3 6.112-16.43-0.188-22.627a16 16 0 0 0-11.245-4.593l-14.526 0.014h-4.98a9356.27 9356.27 0 0 1-44.885-0.108l-11.698-0.06c-19.937-0.092-40.881-0.12-62.832-0.084z" fill="currentColor"></path></svg></span>`;
// 常用 SVG 图标常量（避免重复内联）
const ICO_PING = `<span class="ico"><svg style="color:var(--accent2);" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M511.98 0a512 512 0 1 1-366.06 154.04a30.08 30.08 0 0 1 43.07 42.11A451.75 451.75 0 1 0 555.5 62.33L542.06 61.12v149.62a30.08 30.08 0 0 1-24.64 29.63L511.98 240.95a30.08 30.08 0 0 1-29.63-24.7l-0.51-5.44V0h30.14zM212.09 210.55L543.34 477.42a53.12 53.12 0 1 1-75.9 73.34L212.09 210.55z"/></svg></span>`;
const ICO_PEOPLE = `<svg width="1.2em" height="1.2em" style="vertical-align:-0.15em; fill:currentColor;" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg"><path d="M682.666667 469.333333c70.613333 0 127.573333-57.386667 127.573333-128s-56.96-128-127.573333-128c-70.613333 0-128 57.386667-128 128s57.386667 128 128 128z m-341.333334 0c70.613333 0 127.573333-57.386667 127.573334-128s-56.96-128-127.573334-128c-70.613333 0-128 57.386667-128 128s57.386667 128 128 128z m0 85.333334c-99.626667 0-298.666667 49.92-298.666666 149.333333v106.666667h597.333333v-106.666667c0-99.413333-199.04-149.333333-298.666667-149.333333z m341.333334 0c-12.373333 0-26.24 0.853333-41.173334 2.346666C690.986667 592.64 725.333333 640.64 725.333333 704v106.666667h256v-106.666667c0-99.413333-199.04-149.333333-298.666666-149.333333z"/></svg>`;
const ICO_REFRESH = `<span class="ico"><svg style="color:#93c5fd;" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M936.43 603.42q0 2.85-0.58 4-36.58 153.15-153.15 248.29t-273.15 95.14q-83.42 0-161.44-31.42t-139.14-89.73l-73.73 73.73q-10.85 10.85-25.73 10.85t-25.73-10.85-10.85-25.73l0-256q0-14.85 10.85-25.73t25.73-10.85l256 0q14.85 0 25.73 10.85t10.85 25.73-10.85 25.73l-78.27 78.27q40.58 37.73 92 58.27t106.85 20.58q76.58 0 142.85-37.15t106.27-102.27q6.27-9.73 30.27-66.85 4.58-13.15 17.15-13.15l109.73 0q7.42 0 12.86 5.44t5.44 12.86zM950.74 146.27l0 256q0 14.85-10.85 25.73t-25.73 10.85l-256 0q-14.85 0-25.73-10.85t-10.85-25.73 10.85-25.73l78.85-78.85q-84.58-78.27-199.42-78.27-76.58 0-142.85 37.15t-106.27 102.27q-6.27 9.73-30.27 66.85-4.58 13.15-17.15 13.15l-113.73 0q-7.42 0-12.86-5.44t-5.44-12.86l0-4q37.15-153.15 154.27-248.29t274.27-95.14q83.42 0 162.27 31.71t140 89.44l74.27-73.73q10.85-10.85 25.73-10.85t25.73 10.85 10.85 25.73z"/></svg></span>`;
const ICO_FETCH = `<span class="ico"><svg style="color:currentColor;" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M640 213.333333h85.333333a85.333333 85.333333 0 0 1 85.333334 85.333334v348.586666a128.042667 128.042667 0 1 1-85.333334 0V298.666667h-85.333333v128l-192-170.666667L640 85.333333v128z"/><path d="M213.333333 376.746667a128.042667 128.042667 0 1 1 85.333334 0v270.506666a128.042667 128.042667 0 1 1-85.333334 0V376.746667z"/></svg></span>`;
function setButtonContent(btn, html) {
  if (btn) btn.innerHTML = html;
}
function iconLabel(iconHtml, label) {
  return `${iconHtml} ${label}`;
}
let courses = [];
let classes = [];
let selectedCourse = null;
let selectedClass = null;
let lastLogId = 0;
let uiLoggedIn = false;
let filterMeta = [];  // 从后端获取的筛选维度元数据
let currentViewMode = localStorage.getItem('courseViewMode') || 'list';

window.addEventListener('DOMContentLoaded', () => { initState(); startPolling(); setViewMode(currentViewMode); loadPresetAccounts(); });
document.addEventListener('click', (e) => {
  const toggle = document.getElementById('loginMoreToggle');
  const panel = document.getElementById('loginMorePanel');
  if (toggle && panel && !toggle.contains(e.target) && !panel.contains(e.target)) {
    panel.classList.remove('show');
    toggle.classList.remove('open');
  }
});


function setViewMode(mode) {
  currentViewMode = mode;
  localStorage.setItem('courseViewMode', mode);
  document.getElementById('btnViewList').className = mode === 'list' ? 'view-btn active' : 'view-btn';
  document.getElementById('btnViewGrid').className = mode === 'grid' ? 'view-btn active' : 'view-btn';
  const listEl = document.getElementById('courseList');
  if(listEl) listEl.className = mode === 'list' ? 'list-view' : 'grid-list';
}

function toggleLog() {
   const lp = document.getElementById('logPanel');
   const btn = document.getElementById('btnToggleLog');
   if (lp.classList.contains('open')) {
      lp.classList.remove('open');
      btn.innerHTML = `<span class="ico"><svg style="color:#a78bfa;" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M853.33 981.33H170.67c-23.47 0-42.67-19.2-42.67-42.67V85.33c0-23.47 19.2-42.67 42.67-42.67h682.67c23.47 0 42.67 19.2 42.67 42.67v853.33c0 23.47-19.2 42.67-42.67 42.67zm-618.67-85.33h554.67c12.8 0 21.33-8.53 21.33-21.33V149.33c0-12.8-8.53-21.33-21.33-21.33H234.67c-12.8 0-21.33 8.53-21.33 21.33v725.33c0 12.8 8.53 21.33 21.33 21.33z M654.93 334.93H369.07c-23.47 0-42.67-19.2-42.67-42.67s19.2-42.67 42.67-42.67h283.73c23.47 0 42.67 19.2 42.67 42.67 2.13 23.47-17.07 42.67-40.53 42.67zM654.93 524.8H369.07c-23.47 0-42.67-19.2-42.67-42.67s19.2-42.67 42.67-42.67h283.73c23.47 0 42.67 19.2 42.67 42.67 2.13 23.47-17.07 42.67-40.53 42.67zM654.93 710.4H369.07c-23.47 0-42.67-19.2-42.67-42.67s19.2-42.67 42.67-42.67h283.73c23.47 0 42.67 19.2 42.67 42.67 2.13 23.47-17.07 42.67-40.53 42.67z"/></svg></span> 展开日志`;
   } else {
      lp.classList.add('open');
      btn.innerHTML = `<span class="ico"><svg style="color:#a78bfa;" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M853.33 981.33H170.67c-23.47 0-42.67-19.2-42.67-42.67V85.33c0-23.47 19.2-42.67 42.67-42.67h682.67c23.47 0 42.67 19.2 42.67 42.67v853.33c0 23.47-19.2 42.67-42.67 42.67zm-618.67-85.33h554.67c12.8 0 21.33-8.53 21.33-21.33V149.33c0-12.8-8.53-21.33-21.33-21.33H234.67c-12.8 0-21.33 8.53-21.33 21.33v725.33c0 12.8 8.53 21.33 21.33 21.33z M654.93 334.93H369.07c-23.47 0-42.67-19.2-42.67-42.67s19.2-42.67 42.67-42.67h283.73c23.47 0 42.67 19.2 42.67 42.67 2.13 23.47-17.07 42.67-40.53 42.67zM654.93 524.8H369.07c-23.47 0-42.67-19.2-42.67-42.67s19.2-42.67 42.67-42.67h283.73c23.47 0 42.67 19.2 42.67 42.67 2.13 23.47-17.07 42.67-40.53 42.67zM654.93 710.4H369.07c-23.47 0-42.67-19.2-42.67-42.67s19.2-42.67 42.67-42.67h283.73c23.47 0 42.67 19.2 42.67 42.67 2.13 23.47-17.07 42.67-40.53 42.67z"/></svg></span> 收起日志`;
      const scroll = document.getElementById('logScroll');
      scroll.scrollTop = scroll.scrollHeight;
   }
}

// ---- 初始化 ----
let delayedLoginTimer = null;
let delayedLoginDeadline = 0;
let delayedLoginLogEl = null;
let activeRateLimitCountdownTimer = null;
let activeRateLimitCountdownLogEl = null;
let activeRateLimitUntil = 0;

function renderOrUpdateLogEntry(entry) {
  if (!entry) return null;
  const scroll = document.getElementById('logScroll');
  if (!scroll) return null;
  const atBot = scroll.scrollTop + scroll.clientHeight >= scroll.scrollHeight - 30;
  let div = scroll.querySelector(`[data-log-id="${entry.id}"]`);
  if (!div) {
    div = document.createElement('div');
    div.setAttribute('data-log-id', String(entry.id));
    div.innerHTML = `<span class="time"></span><span class="msg"></span>`;
    scroll.appendChild(div);
  }
  div.className = 'log-line ' + entry.level;
  div.querySelector('.time').textContent = entry.time;
  div.querySelector('.msg').innerHTML = entry.msg;
  if (atBot) scroll.scrollTop = scroll.scrollHeight;
  return div;
}
const LOG_ICONS = {{ LOG_ICONS_JSON | safe }};

function formatClientLogText(msg) {
  if (typeof msg !== 'string') return msg;
  for (const [key, svg] of Object.entries(LOG_ICONS)) {
    if (msg.includes(key)) {
      msg = msg.split(key).join(`<span class="ico">${svg}</span>`);
    }
  }
  return msg;
}

function pushClientLog(msg, level='INFO') {
  const scroll = document.getElementById('logScroll');
  if (!scroll) return null;
  const atBot = scroll.scrollTop + scroll.clientHeight >= scroll.scrollHeight - 30;
  const div = document.createElement('div');
  const timeSpan = document.createElement('span');
  const msgSpan = document.createElement('span');
  div.className = 'log-line ' + level;
  timeSpan.className = 'time';
  msgSpan.className = 'msg';
  timeSpan.textContent = new Date().toLocaleTimeString('zh-CN', {hour12:false});
  msgSpan.innerHTML = formatClientLogText(msg);
  div.appendChild(timeSpan);
  div.appendChild(msgSpan);
  scroll.appendChild(div);
  if (atBot) scroll.scrollTop = scroll.scrollHeight;
  return div;
}
function updateClientLog(lineEl, msg, level='INFO') {
  if (!lineEl) return pushClientLog(msg, level);
  lineEl.className = 'log-line ' + level;
  const timeSpan = lineEl.querySelector('.time');
  const msgSpan = lineEl.querySelector('.msg');
  if (timeSpan) timeSpan.textContent = new Date().toLocaleTimeString('zh-CN', {hour12:false});
  if (msgSpan) msgSpan.innerHTML = formatClientLogText(msg);
  return lineEl;
}
function stopRateLimitCountdown(markDone = false) {
  if (activeRateLimitCountdownTimer) {
    clearInterval(activeRateLimitCountdownTimer);
    activeRateLimitCountdownTimer = null;
  }
  activeRateLimitUntil = 0;
  if (markDone && activeRateLimitCountdownLogEl) {
    activeRateLimitCountdownLogEl = updateClientLog(activeRateLimitCountdownLogEl, '::success:: 风控等待结束，可重新尝试', 'SUCCESS');
  }
}
function syncRateLimitCountdown(rateLimitUntil) {
  const until = Number(rateLimitUntil || 0);
  if (!until) {
    stopRateLimitCountdown(false);
    return;
  }
  if (activeRateLimitUntil === until && activeRateLimitCountdownTimer) return;
  stopRateLimitCountdown(false);
  activeRateLimitUntil = until;
  const render = () => {
    const remain = Math.max(0, Math.ceil(activeRateLimitUntil - Date.now() / 1000));
    if (remain <= 0) {
      stopRateLimitCountdown(true);
      courses.forEach(it => { if (it && it._rateLimited) it._rateLimited = false; });
      return;
    }
    const msg = `::warning:: 风控等待倒计时：${Math.floor(remain / 60)}分${String(remain % 60).padStart(2,'0')}秒`;
    activeRateLimitCountdownLogEl = updateClientLog(activeRateLimitCountdownLogEl, msg, 'WARN');
  };
  render();
  activeRateLimitCountdownTimer = setInterval(render, 1000);
}
function toggleLoginMorePanel() {
  const panel = document.getElementById('loginMorePanel');
  const toggle = document.getElementById('loginMoreToggle');
  panel.classList.toggle('show');
  toggle.classList.toggle('open', panel.classList.contains('show'));
}
function persistLoginMoreSettings(silent = true) {
  const mins = parseFloat(document.getElementById('inpDelayedLoginMinutes').value || '0');
  localStorage.setItem('login_retry_until_success', document.getElementById('chkLoginRetryUntilSuccess').checked ? '1' : '0');
  localStorage.setItem('login_delay_minutes', String(isNaN(mins) ? 0 : mins));
  if (!silent) pushClientLog('::success:: 登录附加设置已自动保存', 'SUCCESS');
}
function bindLoginMoreAutoSave() {
  const minsEl = document.getElementById('inpDelayedLoginMinutes');
  const retryEl = document.getElementById('chkLoginRetryUntilSuccess');
  if (minsEl && !minsEl.dataset.autosaveBound) {
    minsEl.addEventListener('change', () => persistLoginMoreSettings(false));
    minsEl.addEventListener('input', () => persistLoginMoreSettings(true));
    minsEl.dataset.autosaveBound = '1';
  }
  if (retryEl && !retryEl.dataset.autosaveBound) {
    retryEl.addEventListener('change', () => persistLoginMoreSettings(false));
    retryEl.dataset.autosaveBound = '1';
  }
}
function loadLoginMoreSettings() {
  document.getElementById('chkLoginRetryUntilSuccess').checked = localStorage.getItem('login_retry_until_success') !== '0';
  document.getElementById('inpDelayedLoginMinutes').value = localStorage.getItem('login_delay_minutes') || '';
  bindLoginMoreAutoSave();
}
async function performLoginRequest(u, p) {
  const res = await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})});
  return await res.json();
}
async function initState() {
  try {
    const res = await fetch('/api/state');
    const s = await res.json();
    if (s.logged_in) switchToLoggedInUI();
    const verifyEl = document.getElementById('chkVerify');
    const bizEl = document.getElementById('chkBizLatency');
    if (verifyEl) verifyEl.checked = !!s.verify_after;
    if (bizEl) bizEl.checked = !!s.measure_business_latency;
    loadLoginMoreSettings();
  } catch(e){}
}
function switchToLoggedInUI() {
  if (uiLoggedIn) return;
  uiLoggedIn = true;
  document.getElementById('sidebarPanel').classList.add('closed');
  document.getElementById('btnToggleLogin').style.display = 'inline-flex';
  document.getElementById('btnLogoutHead').style.display = 'inline-flex';
  document.getElementById('emptyState').style.display = 'none';
  document.getElementById('filterBar').style.display = 'flex';
  loadFilters();
}

function toggleSidebar() {
  const sb = document.getElementById('sidebarPanel');
  sb.classList.toggle('closed');
}

// ---- 动态筛选栏 ----
async function loadFilters() {
  if (filterMeta.length > 0) return; // 已加载过
  try {
    const res = await fetch('/api/filters',{method:'POST'});
    const d = await res.json();
    if (!d.ok || !d.filters) return;
    filterMeta = d.filters;
    const container = document.getElementById('filterSelects');
    container.innerHTML = '';
    for (const f of filterMeta) {
      const wrap = document.createElement('div');
      wrap.style.cssText = 'display:flex;align-items:center;gap:0.4rem;';
      const lbl = document.createElement('label');
      lbl.style.cssText = 'margin:0;font-size:0.8rem;color:#ffffff;white-space:nowrap;';
      lbl.textContent = f.label;
      const sel = document.createElement('select');
      sel.id = 'filter_' + f.name;
      sel.dataset.filterName = f.name;
      sel.className = 'unified-height custom-select ui-control ui-select';

      for (const opt of f.options) {
        const o = document.createElement('option');
        o.value = opt.value;
        o.textContent = opt.text;
        sel.appendChild(o);
      }
      wrap.appendChild(lbl);
      wrap.appendChild(sel);
      container.appendChild(wrap);
    }

setTimeout(initCustomSelects, 50);

  } catch(e){ console.error('加载筛选选项失败', e); }
}

// 收集动态筛选参数
function getFilterParams() {
  const params = {};
  for (const f of filterMeta) {
    const el = document.getElementById('filter_' + f.name);
    if (el) params[f.name] = el.value;
  }
  return params;
}

// ---- 延迟检测（纯网络延迟，反映网络质量） ----
async function pollPing() {
  if (isPollingPing) return;
  isPollingPing = true;
  const el = document.getElementById('pingStatus');
  try {
    const res = await fetch('/api/ping');
    const d = await res.json();
    if (d.ok) {
      const ms = d.ms;
      if (ms < 0) {
        el.innerHTML = `${ICO_PING} <span style="color:#f3f4f6;font-size:0.65rem;">测量中...</span>`;
        return;
      }
      const color = ms < 50 ? 'var(--green)' : ms < 150 ? 'var(--orange)' : 'var(--red)';
      el.innerHTML = `${ICO_PING} <span style="color:${color};font-weight:600">延迟 ${ms}ms</span>`;
    } else {
      el.innerHTML = `${ICO_PING} <span style="color:var(--red)">超时</span>`;
    }
  } catch(e) {
    el.innerHTML = `${ICO_PING} <span style="color:var(--red)">离线</span>`;
  } finally {
    isPollingPing = false;
  }
}

// ---- 预设账号管理 ----
let presetAccounts = [];

async function loadPresetAccounts() {
  try {
    const res = await fetch('/api/preset_accounts');
    const d = await res.json();
    presetAccounts = d.accounts || [];

    const inpUser = document.getElementById('inpUser');
    const dropdown = document.getElementById('accountDropdown');

    // 点击输入框显示下拉
    inpUser.addEventListener('focus', () => {
      if (presetAccounts.length > 0) {
        dropdown.innerHTML = presetAccounts.map(acc =>
          `<div class="account-dropdown-item" data-username="${acc}">${acc}</div>`
        ).join('');
        dropdown.style.display = 'block';
      }
    });

    // 点击下拉项选择账号
    dropdown.addEventListener('click', async (e) => {
      const item = e.target.closest('.account-dropdown-item');
      if (item) {
        const username = item.dataset.username;
        inpUser.value = username;
        dropdown.style.display = 'none';

        // 自动填充密码
        try {
          const res = await fetch('/api/get_password', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({username})
          });
          const d = await res.json();
          if (d.ok) {
            document.getElementById('inpPass').value = d.password;
          }
        } catch(e) {}
      }
    });

    // 点击外部关闭下拉
    document.addEventListener('click', (e) => {
      if (!inpUser.contains(e.target) && !dropdown.contains(e.target)) {
        dropdown.style.display = 'none';
      }
    });
  } catch(e) {}
}

// ---- 登录 ----
async function doLogin(forceImmediate = false) {
  const btn = document.getElementById('btnLogin');
  const u = document.getElementById('inpUser').value.trim();
  const p = document.getElementById('inpPass').value.trim();
  const retryUntilSuccess = document.getElementById('chkLoginRetryUntilSuccess').checked;
  const delayMinutes = parseFloat(document.getElementById('inpDelayedLoginMinutes').value || '0');
  if (!u||!p) return alert('请填写学号和密码');
  if (!forceImmediate && delayMinutes > 0) {
    if (delayedLoginTimer) {
      clearInterval(delayedLoginTimer);
      delayedLoginLogEl = updateClientLog(delayedLoginLogEl, '::warning:: 旧的定时登录任务已被新的定时任务覆盖', 'WARN');
    }
    delayedLoginDeadline = Date.now() + delayMinutes * 60 * 1000;
    delayedLoginLogEl = pushClientLog(`::time:: 定时登录倒计时剩余 ${(delayMinutes * 60).toFixed(1)} 秒`, 'INFO');

    delayedLoginTimer = setInterval(async () => {
      const remainMs = delayedLoginDeadline - Date.now();
      if (remainMs <= 0) {
        clearInterval(delayedLoginTimer);
        delayedLoginTimer = null;
        delayedLoginLogEl = updateClientLog(delayedLoginLogEl, '::success:: 定时登录倒计时结束，开始自动登录', 'SUCCESS');
        document.getElementById('inpDelayedLoginMinutes').value = '';
        localStorage.setItem('login_delay_minutes', '0');
        await doLogin(true);
        return;
      }
      const remainSec = remainMs / 1000;
      delayedLoginLogEl = updateClientLog(delayedLoginLogEl, `::time:: 定时登录倒计时剩余 ${remainSec.toFixed(1)} 秒`, 'INFO');
    }, 1000);
    return;
  }
  btn.disabled = true; btn.innerHTML = WAIT_ICON + ' 登录中...';
  if (!document.getElementById('logPanel').classList.contains('open')) toggleLog();
  try {
    while (true) {
      try {
        const d = await performLoginRequest(u, p);
        if (d.ok) {
          switchToLoggedInUI();
          // 用户要求不自动拉取课程: setTimeout(() => fetchCourses(), 500);
          break;
        }
        const msg = String(d.msg || '未知错误');
        const shouldRetry = retryUntilSuccess && /超时|timeout|504|502|503|网关|阻塞|连接|connection/i.test(msg);
        if (!shouldRetry) return alert('登录失败: ' + msg);
        pushClientLog(`::warning:: 登录失败(${msg})，2秒后自动重试...`, 'WARN');
      } catch(e) {
        const msg = String(e);
        if (!retryUntilSuccess) return alert('请求失败: ' + msg);
        pushClientLog(`::warning:: 登录请求异常(${msg})，2秒后自动重试...`, 'WARN');
      }
      await new Promise(r => setTimeout(r, 2000));
    }
  } finally { btn.disabled = false; setButtonContent(btn, '登录'); }
}

// ---- 拉取课程 ----
let lastRefreshTime = 0;
async function refreshCourses() {
  const now = Date.now();
  if (now - lastRefreshTime < CONFIG_DELAY_COURSE_REFRESH) {
    pushClientLog(`::warning:: 请等待 ${CONFIG_DELAY_COURSE_REFRESH/1000} 秒后再刷新课程列表`, "WARN");
    return;
  }
  lastRefreshTime = now;
  const btn = document.getElementById('btnRefreshCourses');
  if (btn) { btn.disabled = true; setButtonContent(btn, iconLabel(WAIT_ICON, '...')); }
  await fetchCourses();
  if (btn) { btn.disabled = false; setButtonContent(btn, iconLabel(ICO_REFRESH, '刷新列表')); }
}

async function fetchCourses() {
  const btn = document.getElementById('btnFetch');
  btn.disabled = true; setButtonContent(btn, iconLabel(WAIT_ICON, '拉取中...'));
  const params = getFilterParams();

  selectedCourse = null; selectedClass = null;
  document.getElementById('cardEngine').style.display = 'none';
  document.getElementById('emptyState').style.display = 'none';

  try {
    const res = await fetch('/api/courses',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(params)});
    const d = await res.json();
    if(d.status==='error') { alert('拉取失败: '+d.msg); return; }

    courses = d.courses || [];
    let credits = new Set(), hours = new Set(), cats = new Set();
    courses.forEach((c, i) => {
       c._originalIndex = i;
       c._classes = null;
       if (c.credit) credits.add(c.credit);
       if (c.hours)  hours.add(c.hours);
       if (c.category) cats.add(c.category);
    });

    // 初始化下拉框选项
    const selC = document.getElementById('selFilterCredit');
    const selH = document.getElementById('selFilterHours');
    const selCat = document.getElementById('selFilterCategory');
    selC.innerHTML = '<option value="">所有学分</option>' + [...credits].sort().map(x => `<option value="${x}">${x}</option>`).join('');
    selH.innerHTML = '<option value="">所有学时</option>' + [...hours].sort((a,b)=>Number(a)-Number(b)).map(x => `<option value="${x}">${x}</option>`).join('');
    selCat.innerHTML = '<option value="">所有类别</option>' + [...cats].sort().map(x => `<option value="${x}">${x}</option>`).join('');

    document.getElementById('advancedFilters').style.display = 'flex';
    document.getElementById('titleCourses').style.display = 'flex';
    renderCourses();

setTimeout(initCustomSelects, 50);

  } catch(e) { alert('拉取失败: '+e); }
  finally { btn.disabled = false; setButtonContent(btn, iconLabel(ICO_FETCH, '拉取课程')); }
}

function renderClassesHTML(c) {
  if (c._loadingClasses) return `<div style="padding:1rem;text-align:center;color:var(--accent2);font-size:0.85rem;">${WAIT_ICON} 正在拉取班级...</div>`;
  if (c._rateLimited) return `<div style="padding:1.2rem;text-align:center;color:var(--orange);font-size:0.85rem;">${esc(c._classFetchError || '触发风控，请稍后重试')}</div>`;
  if (c._classFetchError) return `<div style="padding:1rem;text-align:center;color:var(--red);border-radius:6px;background-color:var(--card);font-size:0.85rem;"><span class="ico"><svg style="color:var(--orange);" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M934.4 770.13L605.87 181.33C586.67 147.2 550.4 128 512 128s-74.67 21.33-93.87 53.33L89.6 770.13c-19.2 34.13-19.2 76.8 0 110.93S145.07 938.67 183.47 938.67h657.07c40.53 0 74.67-21.33 93.87-57.6 19.2-34.13 19.2-76.8 0-110.93zM480 362.67c0-17.07 14.93-32 32-32s29.87 12.8 32 29.87V640c0 17.07-14.93 32-32 32s-29.87-12.8-32-29.87V362.67zM512 832c-23.47 0-42.67-19.2-42.67-42.67s19.2-42.67 42.67-42.67 42.67 19.2 42.67 42.67-19.2 42.67-42.67 42.67z"/></svg></span> ${esc(c._classFetchError)}</div>`;
  const clist = c._classes || [];
  if (clist.length === 0) return '<div style="padding:1rem;text-align:center;color:#f3f4f6;font-size:0.9rem;">无可用班级</div>';

  let dList = clist.slice();
  const q = (document.getElementById('inpCourseSearch').value || '').toLowerCase();
  if (q) {
      dList = dList.filter(cls => {
          const s = (cls.class_name||'')+' '+(cls.location||'')+' '+(cls.teacher||'')+' '+(cls.class_id||'');
          return s.toLowerCase().includes(q);
      });
  }

  const sortType = document.getElementById('selSortType').value;
  const fS = (document.getElementById('selFilterSchedule')||{}).value;
  if (fS) {
      dList = dList.filter(cls => cls.schedule && (cls.schedule.includes(fS) || cls.schedule.includes('星期' + fS)));
  }
  if (sortType === 'capacity') {
      dList.sort((a,b)=>{
          const getRem = (x) => {
              if(!x.capacity) return -1;
              const m = x.capacity.match(/(\d+)\/(\d+)/);
              return m ? parseInt(m[2])-parseInt(m[1]) : -1;
          };
          const va = getRem(a), vb = getRem(b);
          if (va === vb) return 0;
          return sortAsc ? va - vb : vb - va;
      });
  } else {
      if (!sortAsc) dList.reverse();
  }

  if (dList.length === 0) return '<div style="padding:1rem;text-align:center;color:#f3f4f6;font-size:0.9rem;">没有符合筛选条件的班级</div>';

  const isSingle = dList.length === 1;
  const items = dList.map((clsItem) => {
    const sel = selectedClass && selectedClass.class_id === clsItem.class_id ? ' selected' : '';
    const title = clsItem.class_name ? esc(clsItem.class_name) : (clsItem.location ? esc(clsItem.location) : esc(clsItem.class_id));
    const subTitle = (clsItem.class_name || clsItem.location) ? `<div style="font-size:0.75rem; color:#9ca3af; margin-bottom:0.3rem;">ID: ${esc(clsItem.class_id)}</div>` : '';

    let barHtml = '';
    if (clsItem.capacity) {
        const m = clsItem.capacity.match(/(\d+)\/(\d+)/);
        if (m) {
            let cur = parseInt(m[1]), tot = parseInt(m[2]);
            if (tot > 0) {
                let pct = (cur / tot) * 100;
                let color = 'var(--accent)'; // 默认蓝色
                if (pct >= 100) color = '#500000'; // 深红色，更深更暗
                else if (pct >= 95) color = '#ff0000'; // 红色，最纯最亮的正红色
                else if (pct >= 85) color = '#f97316'; // 橙色
                else if (pct >= 70) color = '#eab308'; // 黄色

                barHtml = `<div style="position:absolute; bottom:0; left:0; width:100%; height:4px; background-color:var(--bg);">
                  <div class="class-progress-fill" style="width:${Math.min(pct, 100)}%; height:100%; background:${color}; transition:width 0.3s ease;"></div>
                </div>`;
            }
        }
    }

    const containerCls = isSingle ? `class-item-single${sel}` : `class-item${sel}`;
    return `<div class="${containerCls}" data-class-id="${esc(clsItem.class_id)}" style="position:relative; overflow:hidden; padding-bottom:1.1rem;" onclick="selectClassForCourse(event, '${esc(c.code)}', '${esc(clsItem.class_id)}')">
      <div style="font-size:0.95rem; font-weight:600; color:#93c5fd; margin-bottom:0.4rem;">${title}</div>
      ${subTitle}
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.4rem;">
         <span style="font-size:0.85rem; color:#9ca3af; display:flex; align-items:center;">${clsItem.teacher ? '<svg width="1.2em" height="1.2em" style="vertical-align:-0.15em; fill:currentColor;" t="1772773474330" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="8987" data-darkreader-inline-fill="currentColor"  ><path d="M128 640a42.666667 42.666667 0 0 1 0-85.333333h768a42.666667 42.666667 0 0 1 0 85.333333h-42.666667v213.333333a85.333333 85.333333 0 0 1-85.333333 85.333334H256a85.333333 85.333333 0 0 1-85.333333-85.333334v-213.333333H128z m724.266667-276.266667a42.666667 42.666667 0 0 1 0 60.330667L764.373333 512H316.416a213.376 213.376 0 0 1 367.829333-40.533333l107.690667-107.690667a42.666667 42.666667 0 0 1 60.330667 0zM512 85.333333a128 128 0 1 1 0 256 128 128 0 0 1 0-256z" fill="currentColor" p-id="8988" style="--darkreader-inline-fill: var(--darkreader-background-000000, #000000);" data-darkreader-inline-fill="currentColor"></path></svg> '+esc(clsItem.teacher) : '<svg width="1.2em" height="1.2em" style="vertical-align:-0.15em; fill:currentColor;" t="1772773474330" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="8987" data-darkreader-inline-fill="currentColor"  ><path d="M128 640a42.666667 42.666667 0 0 1 0-85.333333h768a42.666667 42.666667 0 0 1 0 85.333333h-42.666667v213.333333a85.333333 85.333333 0 0 1-85.333333 85.333334H256a85.333333 85.333333 0 0 1-85.333333-85.333334v-213.333333H128z m724.266667-276.266667a42.666667 42.666667 0 0 1 0 60.330667L764.373333 512H316.416a213.376 213.376 0 0 1 367.829333-40.533333l107.690667-107.690667a42.666667 42.666667 0 0 1 60.330667 0zM512 85.333333a128 128 0 1 1 0 256 128 128 0 0 1 0-256z" fill="currentColor" p-id="8988" style="--darkreader-inline-fill: var(--darkreader-background-000000, #000000);" data-darkreader-inline-fill="currentColor"></path></svg> 待定'}</span>
         <span class="capacity-text js-capacity-text" style="display:flex; align-items:center;">${clsItem.capacity ? ICO_PEOPLE+' '+esc(clsItem.capacity) : ''}</span>
      </div>
      <div style="color:#f3f4f6;font-size:.85rem; line-height:1.4; display:grid; gap:0.2rem;">
         <div>${clsItem.schedule ? '<svg width="1.2em" height="1.2em" style="vertical-align:-0.15em; fill:currentColor;" t="1772773595295" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="9985" data-darkreader-inline-fill="currentColor"  ><path d="M810.666667 128h-42.666667V42.666667h-85.333333v85.333333H341.333333V42.666667h-85.333333v85.333333h-42.666667c-47.146667 0-84.906667 38.186667-84.906666 85.333333L128 810.666667c0 47.146667 38.186667 85.333333 85.333333 85.333333h597.333334c47.146667 0 85.333333-38.186667 85.333333-85.333333V213.333333c0-47.146667-38.186667-85.333333-85.333333-85.333333z m0 682.666667H213.333333V341.333333h597.333334v469.333334zM298.666667 426.666667h213.333333v213.333333H298.666667z" fill="currentColor" p-id="9986" style="--darkreader-inline-fill: var(--darkreader-background-000000, #000000);" data-darkreader-inline-fill="currentColor"></path></svg> '+esc(clsItem.schedule) : '<svg width="1.2em" height="1.2em" style="vertical-align:-0.15em; fill:currentColor;" t="1772773595295" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="9985" data-darkreader-inline-fill="currentColor"  ><path d="M810.666667 128h-42.666667V42.666667h-85.333333v85.333333H341.333333V42.666667h-85.333333v85.333333h-42.666667c-47.146667 0-84.906667 38.186667-84.906666 85.333333L128 810.666667c0 47.146667 38.186667 85.333333 85.333333 85.333333h597.333334c47.146667 0 85.333333-38.186667 85.333333-85.333333V213.333333c0-47.146667-38.186667-85.333333-85.333333-85.333333z m0 682.666667H213.333333V341.333333h597.333334v469.333334zM298.666667 426.666667h213.333333v213.333333H298.666667z" fill="currentColor" p-id="9986" style="--darkreader-inline-fill: var(--darkreader-background-000000, #000000);" data-darkreader-inline-fill="currentColor"></path></svg> 无时间信息'}</div>
         ${clsItem.location ? `<div><span class="ico"><svg style="color:var(--red);" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M512 85.33c-164.95 0-298.67 133.72-298.67 298.67 0 224 298.67 554.67 298.67 554.67s298.67-330.67 298.67-554.67c0-164.95-133.72-298.67-298.67-298.67zm0 405.33a106.67 106.67 0 1 1 0-213.33 106.67 106.67 0 0 1 0 213.33z"/></svg></span> ${esc(clsItem.location)}</div>` : ''}
      </div>
      ${barHtml}
    </div>`;
  }).join('');

  const wrapperCls = isSingle ? '' : 'class-grid';
  return `<div class="${wrapperCls}" style="margin-top:0.8rem; padding-top:0.8rem; border-top:1px dashed var(--border);">${items}</div>`;
}

let sortAsc = false;
function toggleSortOrder() {
  sortAsc = !sortAsc;
  const btn = document.getElementById('btnSortOrder');
  const svg = btn.querySelector('.anim-arrow');
  if (svg) svg.classList.toggle('up', sortAsc);
  const txt = document.getElementById('textSortOrder');
  if (txt) txt.textContent = sortAsc ? '升序' : '降序';
  applyCourseFilters();与
}

let filteredCourses = [];
function toggleFilterMenu() {
    const b = document.getElementById('filterMenuBody');
    const a = document.getElementById('filterMenuArrow');
    if(b.classList.contains('show')) {
        b.classList.remove('show');
        a.classList.remove('up');
    } else {
        b.classList.add('show');
        a.classList.add('up');
    }
}
// 点击外部隐藏
document.addEventListener('click', (e) => {
    const btn = document.getElementById('btnToggleFilterMenu');
    const b = document.getElementById('filterMenuBody');
    if (btn && b && !btn.contains(e.target) && !b.contains(e.target)) {
        b.classList.remove('show');
        document.getElementById('filterMenuArrow')?.classList.remove('up');
    }
});


// 将系统原生的 <select> 强行转化成我们所设计的炫酷网页级别组件
function initCustomSelects() {
    document.querySelectorAll('.custom-select:not(.styled)').forEach(selectEl => {
        selectEl.classList.add('styled');
        selectEl.style.display = 'none'; // 隐藏系统原生框

        // 创建包裹
        const wrapper = document.createElement('div');
        wrapper.className = 'unified-select-wrapper';
        wrapper.style.position = 'relative';
        wrapper.style.flex = '1';

        // 创建触发按钮
        const trigger = document.createElement('div');
        trigger.className = 'unified-select-trigger unified-height';
        trigger.innerHTML = `<span class="selected-text">${selectEl.options[selectEl.selectedIndex].text}</span>
            <svg class="unified-select-arrow" viewBox="0 0 1024 1024" fill="currentColor" width="1.2em" height="1.2em" style="transition:transform 0.25s;"><path d="M831.872 340.864L512 652.672 192.128 340.864a30.592 30.592 0 0 0-42.752 0 29.12 29.12 0 0 0 0 41.6l342.4 333.312a32 32 0 0 0 40.448 0l342.4-333.312a29.12 29.12 0 0 0 0-41.6 30.592 30.592 0 0 0-42.752 0z"/></svg>`;

        // 创建面板
        const optionsPanel = document.createElement('div');
        optionsPanel.className = 'unified-select-options';

        // 渲染选项
        const renderOptions = () => {
             optionsPanel.innerHTML = '';
             Array.from(selectEl.options).forEach((opt, index) => {
                 const optDiv = document.createElement('div');
                 optDiv.className = 'unified-select-option' + (index === selectEl.selectedIndex ? ' active' : '');
                 optDiv.textContent = opt.text;
                 optDiv.onclick = (e) => {
                     e.stopPropagation();
                     selectEl.selectedIndex = index;
                     // 触发原生 onChange 事件！
                     const event = new Event('change', { bubbles: true });
                     selectEl.dispatchEvent(event);

                     trigger.querySelector('.selected-text').textContent = opt.text;
                     closeAllSelects();
                     renderOptions(); // 重新渲染刷新高亮
                 };
                 optionsPanel.appendChild(optDiv);
             });
        };
        renderOptions();

        // 点击展开/收起下拉
        trigger.onclick = (e) => {
            e.stopPropagation();
            const isOpen = optionsPanel.classList.contains('show');
            closeAllSelects();
            if(!isOpen) {
                optionsPanel.classList.add('show');
                trigger.querySelector('.unified-select-arrow').classList.add('up');
                trigger.style.borderColor = 'var(--text2)';
            }
        };

        wrapper.appendChild(trigger);
        wrapper.appendChild(optionsPanel);
        selectEl.parentNode.insertBefore(wrapper, selectEl.nextSibling);

        // 如果动态增加参数，监控 select 的重新注入
        const observer = new MutationObserver(() => {
            if(selectEl.options.length > optionsPanel.children.length || selectEl.selectedIndex !== -1) {
                if(selectEl.options[selectEl.selectedIndex]) {
                    trigger.querySelector('.selected-text').textContent = selectEl.options[selectEl.selectedIndex].text;
                }
                renderOptions();
            }
        });
        observer.observe(selectEl, { childList: true, subtree: true, attributes: true, attributeFilter:['value']});
    });
}


function closeAllSelects() {
    document.querySelectorAll('.unified-select-options').forEach(p => p.classList.remove('show'));
    document.querySelectorAll('.unified-select-arrow').forEach(a => a.classList.remove('up'));
    document.querySelectorAll('.unified-select-trigger').forEach(t => t.style.borderColor = '');
}

document.addEventListener('click', closeAllSelects);


function applyCourseFilters() {
  const q = (document.getElementById('inpCourseSearch').value || '').toLowerCase();
  const fC = document.getElementById('selFilterCredit').value;
  const fH = document.getElementById('selFilterHours').value;
  const fCat = document.getElementById('selFilterCategory').value;
  const sortType = document.getElementById('selSortType').value;
  const fS = (document.getElementById('selFilterSchedule')||{}).value;

  filteredCourses = courses.filter(c => {
    let matchCourse = true;
    if (q) {
        matchCourse = c.name.toLowerCase().includes(q) || c.code.toLowerCase().includes(q);
        if (!matchCourse && c._classes) {
            matchCourse = c._classes.some(cls => {
                const s = (cls.class_name||'')+' '+(cls.location||'')+' '+(cls.teacher||'')+' '+(cls.class_id||'');
                return s.toLowerCase().includes(q);
            });
        }
    }
    if (!matchCourse) return false;
    if (fC && c.credit !== fC) return false;
    if (fH && c.hours !== fH) return false;
    if (fCat && c.category !== fCat) return false;
    if (fS && c._classes) {
        let hasTime = c._classes.some(cls => cls.schedule && cls.schedule.includes("星" + "期" + fS) || (cls.schedule && cls.schedule.includes(fS)));
        if (!hasTime) return false;
    }
    return true;
  });

  // Sort
  if (sortType !== 'default' && sortType !== 'capacity') {
      filteredCourses.sort((a, b) => {
          let va = 0, vb = 0;
          if (sortType === 'credit') { va = Number(a.credit||0); vb = Number(b.credit||0); }
          else if (sortType === 'hours') { va = Number(a.hours||0); vb = Number(b.hours||0); }

          if (va === vb) return a._originalIndex - b._originalIndex;
          return sortAsc ? va - vb : vb - va;
      });
  } else {
      if (sortType === 'capacity') {
          // 按余额排序只作用于底层班级卡片，外层课程保持原始顺序不乱动
          filteredCourses.sort((a, b) => a._originalIndex - b._originalIndex);
      } else {
          // 默认排序依然受升降序控制
          filteredCourses.sort((a, b) => sortAsc ? a._originalIndex - b._originalIndex : b._originalIndex - a._originalIndex);
      }
  }

  renderCoursesUI();
}

function renderCourses() {
  applyCourseFilters();
}

function renderCoursesUI() {
  const el = document.getElementById('courseList');
  if (courses.length === 0) {
    el.innerHTML = '<div class="empty-state">没有拉取到课程，请尝试更换范围重新拉取</div>';
    return;
  }
  if (filteredCourses.length === 0) {
    el.innerHTML = '<div class="empty-state">无匹配的课程选项</div>';
    return;
  }

  el.innerHTML = filteredCourses.map((c) => {
    const status = c.disabled ? '<span class="ico"><svg style="color:var(--orange);" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M704 192h160v736H160V192h160.064v64H704zM311.616 537.28l-45.312 45.248L447.36 763.52l316.8-316.8-45.312-45.184L447.36 673.024zM384 192V96h256v96z"/></svg></span> 已选' : '<span class="ico"><svg style="color:var(--green);" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M512 967.0656c-251.392 0-455.0656-203.776-455.0656-455.0656S260.608 56.9344 512 56.9344s455.0656 203.776 455.0656 455.0656S763.392 967.0656 512 967.0656z"/><path d="M289.8944 515.3792l140.9024 131.3792c10.3424 9.6256 26.8288 10.0352 36.9664 0.7168L791.04 346.112c6.7584-6.3488 7.2704-15.872 0.8192-21.9136l-7.3728-6.9632c-5.9392-5.5296-16.5888-5.9392-23.3472-0.8192L459.8784 544.5632c-5.632 4.3008-15.9744 4.7104-22.016 1.024l-113.9712-71.0656c-7.168-4.4032-17.2032-2.8672-22.6304 3.4816L288.1536 493.568c-5.632 6.656-4.608 15.872 1.7408 21.8112z"/></svg></span> 可选';
    const cls = c.disabled ? 'course-item disabled' : 'course-item';
    const sel = selectedCourse && selectedCourse.code === c.code;
    const selClass = sel ? ' selected' : '';

    let html = `<div class="${cls}${selClass}" onclick="selectCourseByCode('${c.code}')">
      <div class="course-info-row">
        <div class="c-name">${esc(c.name)}</div>
        <div class="c-tags">
          <span class="tag" style="background-color:var(--bg); border:1px solid var(--border); color:#ffffff;">ID: ${c.code}</span>
          ${c.credit ? `<span class="tag" style="background-color:var(--bg); border:1px solid var(--border); color:#ffffff;"><span class="ico"><svg style="color:#fbbf24; vertical-align:-0.15em; width:1.1em; height:1.1em;" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M548 101l104.1 221.5c5.9 12.6 17.9 21.2 31.6 23l243 30.5c34.1 4.3 47.7 46.3 22.7 69.7L770.8 613.1c-10.1 9.5-14.7 23.5-12 37.1l46 240.3c6.4 33.7-29.3 59.6-59.3 43.1l-214.6-118c-12.2-6.7-26.9-6.7-39.1 0l-214.6 118c-30.1 16.5-65.8-9.4-59.3-43.1l46-240.3c2.6-13.6-1.9-27.7-12.1-37.1L73.2 445.7c-25.1-23.5-11.4-65.4 22.7-69.7l243-30.5c13.8-1.7 25.8-10.4 31.6-23L474.7 101c14.5-31 58.7-31 73.3 0z"/></svg></span> ${c.credit}学分</span>` : ''}
          ${c.hours ? `<span class="tag" style="background-color:var(--bg); border:1px solid var(--border); color:#ffffff;"><span class="ico"><svg style="color:var(--orange); vertical-align:-0.15em; width:1.1em; height:1.1em;" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M810.67 314.45l48.21-48.21a21.33 21.33 0 0 0 0-29.87l-30.29-30.29a21.33 21.33 0 0 0-30.29 0L750.93 256A384 384 0 0 0 597.33 180.48V128a42.67 42.67 0 0 0-42.67-42.67h-85.33a42.67 42.67 0 0 0-42.67 42.67v52.48A384 384 0 0 0 128 554.67a388.69 388.69 0 0 0 372.91 384A384 384 0 0 0 810.67 314.45zM512 853.33a298.67 298.67 0 1 1 298.67-298.67 298.67 298.67 0 0 1-298.67 298.67zm16.21-512h-32.43a21.33 21.33 0 0 0-21.33 21.33v213.33a21.33 21.33 0 0 0 21.33 21.33h32.43a21.33 21.33 0 0 0 21.33-21.33v-213.33a21.33 21.33 0 0 0-21.33-21.33z"/></svg></span> ${c.hours}学时</span>` : ''}
          ${c.exam_type ? `<span class="tag" style="background-color:var(--bg); border:1px solid var(--border); color:#ffffff;">${c.exam_type==='考试' ? '<span class="ico"><svg style="color:#a78bfa; vertical-align:-0.15em; width:1.1em; height:1.1em;" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M853.33 981.33H170.67c-23.47 0-42.67-19.2-42.67-42.67V85.33c0-23.47 19.2-42.67 42.67-42.67h682.67c23.47 0 42.67 19.2 42.67 42.67v853.33c0 23.47-19.2 42.67-42.67 42.67zm-618.67-85.33h554.67c12.8 0 21.33-8.53 21.33-21.33V149.33c0-12.8-8.53-21.33-21.33-21.33H234.67c-12.8 0-21.33 8.53-21.33 21.33v725.33c0 12.8 8.53 21.33 21.33 21.33z"/></svg></span>' : '<span class="ico"><svg style="color:var(--orange); vertical-align:-0.15em; width:1.1em; height:1.1em;" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M704 192h160v736H160V192h160.064v64H704zM311.616 537.28l-45.312 45.248L447.36 763.52l316.8-316.8-45.312-45.184L447.36 673.024zM384 192V96h256v96z"/></svg></span>'} ${esc(c.exam_type)}</span>` : ''}
        </div>
        <div class="c-cat">${c.category ? `<span class="ico"><svg style="color:var(--orange);" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M170.67 469.33h256a42.67 42.67 0 0 0 42.67-42.67V170.67a42.67 42.67 0 0 0-42.67-42.67H170.67a42.67 42.67 0 0 0-42.67 42.67v256a42.67 42.67 0 0 0 42.67 42.67zm426.67 0h256a42.67 42.67 0 0 0 42.67-42.67V170.67a42.67 42.67 0 0 0-42.67-42.67h-256a42.67 42.67 0 0 0-42.67 42.67v256a42.67 42.67 0 0 0 42.67 42.67zM170.67 896h256a42.67 42.67 0 0 0 42.67-42.67v-256a42.67 42.67 0 0 0-42.67-42.67H170.67a42.67 42.67 0 0 0-42.67 42.67v256a42.67 42.67 0 0 0 42.67 42.67zm554.67 0c94.12 0 170.67-76.54 170.67-170.67s-76.54-170.67-170.67-170.67-170.67 76.54-170.67 170.67 76.54 170.67 170.67 170.67z"/></svg></span> ${esc(c.category)}` : ''}</div>
        <div class="c-meta" style="flex-wrap: nowrap; gap: 0.5rem; justify-content: space-between; align-items:center;">
          ${c.existing_class ? `<span style="font-size:0.75rem;color:#f3f4f6;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="班级: ${c.existing_class}">班级: ${c.existing_class}</span>` : '<span></span>'}
          <span style="font-size:0.78rem; font-weight:600; color:${c.disabled ? 'var(--orange)' : 'var(--green)'}; white-space:nowrap; flex-shrink:0;">${status}</span>
        </div>
      </div>`;

    if (sel) {
      html += renderClassesHTML(c);
    }

    html += `</div>`;
    return html;
  }).join('');
}

let isFetchingClasses = false;
let lastFetchClassTime = 0;

async function selectCourseByCode(code) {
  const c = courses.find(x => x.code === code);
  if(!c) return;

  if (selectedCourse && selectedCourse.code === c.code) {
    // 已经展开的再次点击，就是折叠
    selectedCourse = null;
    selectedClass = null;
    renderCourses();

setTimeout(initCustomSelects, 50);

    document.getElementById('cardEngine').style.display = 'none';
    return;
  }

  if (c._rateLimited && c._classFetchError) {
     renderCourses();
     setTimeout(initCustomSelects, 50);
     return;
  }

  const now = Date.now();
  // 间隔一定时间才能重新拉取任意班级，防风控
  if (isFetchingClasses || (now - lastFetchClassTime < CONFIG_DELAY_CLASS_FETCH)) {
     if (!c._classes) {
        pushClientLog(`::warning:: 等待班级拉取冷却中... (${CONFIG_DELAY_CLASS_FETCH/1000}秒防风控保护)`, "WARN");
        return;
     }
  }

  // 展开该课程的面版，自动折叠其它面版
  selectedCourse = c;
  selectedClass = null;
  document.getElementById('cardEngine').style.display = 'none';

  if (c._classes) {
     // 用之前的缓存
     renderCourses();

setTimeout(initCustomSelects, 50);

     return;
  }

  // 必须发请求了
  isFetchingClasses = true;
  lastFetchClassTime = now;
  c._loadingClasses = true;
  c._classFetchError = null;
  c._rateLimited = false;
  renderCourses();

setTimeout(initCustomSelects, 50);
 // 展示 loading

  try {
    const filters = getFilterParams();
    const reqBody = {
      value: c.look_value || c.value,
      index: c.index,
      skbjval: c.existing_class || '',
      xq: filters['sel_xq'] || ''
    };
    const res = await fetch('/api/classes',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(reqBody)});
    const d = await res.json();

    if (!d.ok) {
       c._classFetchError = d.msg;
       if (d.rate_limited) {
          c._rateLimited = true;
          c._loadingClasses = false;
          renderCourses();
       }
    } else {
       let clist = d.classes || [];
       if (clist.length === 0 && c.disabled && c.existing_class) {
           clist.push({
               class_id: c.existing_class, class_name: '', location: '', teacher: '', schedule: '已选班级 (教务系统保护未返回排课)', capacity: ''
           });
       }
       c._classes = clist;
       if (clist.length === 1 && (!selectedClass || selectedClass.class_id !== clist[0].class_id)) {
           setTimeout(() => selectClassForCourse(null, c.code, clist[0].class_id), 80);
       }
    }
  } catch(e) {
    c._classFetchError = String(e);
  } finally {
    c._loadingClasses = false;
    isFetchingClasses = false;
    // 让最新的状态生效
    if(selectedCourse && selectedCourse.code === c.code) renderCourses();

setTimeout(initCustomSelects, 50);

  }
}

function renderTargetBanner(course, clsItem) {
  const timeInfo = clsItem.schedule ? `<strong style="font-weight:900; color:#ffffff;">${esc(clsItem.schedule)}</strong>` : '<strong style="font-weight:900; color:#ffffff;">无时间信息</strong>';
  return `
    <div class="target-banner compact">
      <div class="target-banner-title">${esc(course.name)}</div>
      <div class="target-banner-id">ID: ${esc(clsItem.class_id)}</div>
      <div class="target-banner-meta">
         <span class="target-banner-meta-text"><svg width="1.2em" height="1.2em" style="vertical-align:-0.15em; fill:currentColor; margin-right:4px;" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg"><path d="M128 640a42.666667 42.666667 0 0 1 0-85.333333h768a42.666667 42.666667 0 0 1 0 85.333333h-42.666667v213.333333a85.333333 85.333333 0 0 1-85.333333 85.333334H256a85.333333 85.333333 0 0 1-85.333333-85.333334v-213.333333H128z m724.266667-276.266667a42.666667 42.666667 0 0 1 0 60.330667L764.373333 512H316.416a213.376 213.376 0 0 1 367.829333-40.533333l107.690667-107.690667a42.666667 42.666667 0 0 1 60.330667 0zM512 85.333333a128 128 0 1 1 0 256 128 128 0 0 1 0-256z"/></svg> ${clsItem.teacher ? esc(clsItem.teacher) : '待定'}</span>
         <span class="capacity-text js-capacity-text" style="display:flex; align-items:center;">${clsItem.capacity ? ICO_PEOPLE + ' ' + esc(clsItem.capacity) : ''}</span>
      </div>
      <div class="target-banner-body">
         <div class="target-banner-row"><svg width="1.2em" height="1.2em" style="vertical-align:-0.15em; fill:currentColor; margin-right:4px;" viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg"><path d="M810.666667 128h-42.666667V42.666667h-85.333333v85.333333H341.333333V42.666667h-85.333333v85.333333h-42.666667c-47.146667 0-84.906667 38.186667-84.906666 85.333333L128 810.666667c0 47.146667 38.186667 85.333333 85.333333 85.333333h597.333334c47.146667 0 85.333333-38.186667 85.333333-85.333333V213.333333c0-47.146667-38.186667-85.333333-85.333333-85.333333z m0 682.666667H213.333333V341.333333h597.333334v469.333334zM298.666667 426.666667h213.333333v213.333333H298.666667z"/></svg> ${timeInfo}</div>
      </div>
    </div>`;
}

function selectClassForCourse(event, code, classId) {
  if (event) event.stopPropagation();
  const c = courses.find(x => x.code === code);
  if (!c || !c._classes) return;
  selectedClass = c._classes.find(x => x.class_id === classId);
  renderCourses();
  setTimeout(initCustomSelects, 50);
  const sb = document.getElementById('sidebarPanel');
  sb.classList.remove('closed');
  document.getElementById('cardEngine').style.display = 'block';
  const clsItem = selectedClass;
  document.getElementById('targetBanner').innerHTML = renderTargetBanner(selectedCourse, clsItem);
}

// ---- 引擎 ----
async function startSnatch() {
  if (!selectedCourse || !selectedClass) return alert('请先选择课程和班级');
  const params = getFilterParams();
  const interval = document.getElementById('inpInterval').value;
  const verify = document.getElementById('chkVerify').checked;
  const measureBizLatency = document.getElementById('chkBizLatency').checked;

  await fetch('/api/target',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
    target: {
      course_code: selectedCourse.code, course_name: selectedCourse.name,
      class_id: selectedClass.class_id, class_name: `${selectedClass.class_id} ${selectedClass.teacher||''}`.trim(),
      radio_value: selectedClass.radio_value || '',
      class_page_value: selectedClass.course_value || selectedCourse.look_value || selectedCourse.value,
      full_course_value: selectedCourse.value,
      class_skbjval: selectedClass.skbjval || selectedCourse.existing_class || '',
      xq: selectedClass.xq || params['sel_xq'] || '',
    },
    ...params, interval: parseFloat(interval), verify_after: verify, measure_business_latency: measureBizLatency
  })});;

  // 1. 本地马上响应 UI：隐藏 start 按钮，显示加载中的状态框
  const eng = document.getElementById('engineStatus');
  const btnS = document.getElementById('btnStart');
  eng.className = 'engine-status running';
  document.getElementById('engineStatusText').innerHTML = '<span>正在初始化...</span>';
  btnS.style.display = 'none';

  const res = await fetch('/api/snatch/start',{method:'POST'});
  const d = await res.json();
  if (!d.ok) alert(d.msg);
  else if (!document.getElementById('logPanel').classList.contains('open')) toggleLog();
}
async function stopSnatch() {
  const btnT = document.getElementById('btnStop');
  const eng = document.getElementById('engineStatus');
  stopAnim();
  eng.classList.add('idle');
  eng.classList.remove('running');
  btnT.innerHTML='<div class="spinner" style="width:12px;height:12px;border-width:2px;margin-right:4px;"></div><span>正在停止...</span>';
  btnT.classList.add('disabled');
  await fetch('/api/snatch/stop',{method:'POST'});
}

// ---- 轮询 ----
let lastSessionRemaining = -1;
let sessionCountdownInterval = null;

let isPollingLogs = false;
let isPollingState = false;
let isPollingPing = false;

function startPolling() {
  setInterval(pollLogs, CONFIG_POLL_LOGS);
  setInterval(pollState, CONFIG_POLL_STATE);
  setInterval(pollPing, CONFIG_POLL_PING);
  pollPing();
  sessionCountdownInterval = setInterval(tickSessionCountdown, 1000);
}

function renderSessionCountdown(seconds) {
  const cdEl = document.getElementById('sessionCountdown');
  const barEl = document.getElementById('sessionBar');
  if (!cdEl || !barEl) return;
  if (seconds <= 0) {
    cdEl.textContent = '--:--';
    barEl.style.width = '0%';
    cdEl.style.color = 'var(--text3)';
    return;
  }
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  cdEl.textContent = String(mins).padStart(2,'0') + ':' + String(secs).padStart(2,'0');
  const pct = Math.min(100, seconds / 1200 * 100);
  barEl.style.width = pct + '%';
  if (seconds > 300) { barEl.style.background = 'var(--green)'; cdEl.style.color = 'var(--green)'; }
  else if (seconds > 120) { barEl.style.background = 'var(--orange)'; cdEl.style.color = 'var(--orange)'; }
  else { barEl.style.background = 'var(--red)'; cdEl.style.color = 'var(--red)'; }
}
function tickSessionCountdown() {
  if (lastSessionRemaining <= 0) {
    renderSessionCountdown(0);
    return;
  }
  lastSessionRemaining = Math.max(0, lastSessionRemaining - 1);
  renderSessionCountdown(lastSessionRemaining);
}
function syncSessionCountdownFromServer(expireTime) {
  const nextRemaining = expireTime > 0 ? Math.max(0, expireTime - Date.now() / 1000) : -1;
  lastSessionRemaining = nextRemaining;
  renderSessionCountdown(nextRemaining);
}
async function pollLogs() {
  if (isPollingLogs) return;
  isPollingLogs = true;
  try {
    const res = await fetch('/api/logs?since='+lastLogId);
    const logs = await res.json();
    if (!logs.length) return;
    for (const l of logs) {
      lastLogId = l.id;
      renderOrUpdateLogEntry(l);
    }
  } catch(e){} finally {
    isPollingLogs = false;
  }
}
async function pollState() {
  if (isPollingState) return;
  isPollingState = true;
  try {
    const res = await fetch('/api/state');
    const s = await res.json();
    document.getElementById('statusDot').className = s.logged_in ? 'dot on' : 'dot';
    document.getElementById('statusText').textContent = s.logged_in ? '已登录: '+s.username : '未登录';
    // 正向同步：后端已登录但前端还没切过去
    if (s.logged_in && !uiLoggedIn) switchToLoggedInUI();
    // 反向同步：后端已掉线但前端还停在已登录视图 → 切回登录界面
    if (!s.logged_in && uiLoggedIn) {
      uiLoggedIn = false;
      document.getElementById('sidebarPanel').classList.remove('closed');
      document.getElementById('btnToggleLogin').style.display = 'none';
      document.getElementById('btnLogoutHead').style.display = 'none';
      document.getElementById('filterBar').style.display = 'none';
      document.getElementById('emptyState').style.display = 'flex';
      document.getElementById('titleCourses').style.display = 'none';
      document.getElementById('courseList').innerHTML = '';
      document.getElementById('titleClasses').style.display = 'none';
      document.getElementById('classList').innerHTML = '';
      document.getElementById('cardEngine').style.display = 'none';
      selectedCourse = null; selectedClass = null; courses = []; classes = [];
    }

    // === 功能一：Session 倒计时进度条（服务端同步） ===
    syncSessionCountdownFromServer(Number(s.session_expire_time || 0));

    // === 功能二：风控倒计时（结构化状态同步） ===
    if (s.rate_limit_active && s.rate_limit_until) syncRateLimitCountdown(Number(s.rate_limit_until));
    else stopRateLimitCountdown(false);

    // === 功能三：请求频率（只展示最近20秒请求次数） ===
    const rrEl = document.getElementById('reqRateValue');
    if (rrEl) {
      const count20 = Number(s.req_count_20s || 0);
      rrEl.textContent = `${Math.round(count20)} 次/20秒`;
    }

    // === 功能四：实时更新班级人数（抢课过程中无感更新） ===
    if (s.target_capacity_live && selectedClass && s.target && s.target.class_id === selectedClass.class_id) {
      const newCapacity = s.target_capacity_live;
      const oldCapacity = selectedClass.capacity || '';

      if (newCapacity !== oldCapacity) {
        // 人数发生变化，更新前端显示
        selectedClass.capacity = newCapacity;

        // 更新班级列表中的显示
        const classItem = document.querySelector(`.class-item[data-class-id="${CSS.escape(selectedClass.class_id)}"]`);
        if (classItem) {
          const capacitySpan = classItem.querySelector('.js-capacity-text');
          if (capacitySpan) {
            capacitySpan.innerHTML = ICO_PEOPLE + ' ' + newCapacity;
          }

          const progressBar = classItem.querySelector('.class-progress-fill');
          if (progressBar && newCapacity) {
            const match = newCapacity.match(/(\d+)\/(\d+)/);
            if (match) {
              const cur = parseInt(match[1]);
              const tot = parseInt(match[2]);
              if (tot > 0) {
                const pct = (cur / tot) * 100;
                let color = 'var(--accent)';
                if (pct >= 100) color = '#500000';
                else if (pct >= 95) color = '#ff0000';
                else if (pct >= 85) color = '#f97316';
                else if (pct >= 70) color = '#eab308';
                progressBar.style.background = color;
                progressBar.style.width = pct + '%';
              }
            }
          }
        }

        // 更新引擎控制面板中的目标横幅
        const targetBanner = document.getElementById('targetBanner');
        if (targetBanner) {
          const capacitySpan = targetBanner.querySelector('.js-capacity-text');
          if (capacitySpan && newCapacity) {
            capacitySpan.innerHTML = ICO_PEOPLE + ' ' + newCapacity;
          }
        }
      }
    }

    // === 引擎控制 + 功能二：融入式进度条 ===
    const eng = document.getElementById('engineStatus');
    const bgFill = document.getElementById('engineProgressFill');
    const stText = document.getElementById('engineStatusText');
    const btnS = document.getElementById('btnStart');
    const btnT = document.getElementById('btnStop');

    if (s.snatch_success) {
      eng.className='engine-status success'; stText.innerHTML='选课完毕！';
      bgFill.style.width = '100%'; bgFill.style.background = 'rgba(255,255,255,0.2)';
      btnS.style.display='none'; btnT.style.display='none';
      stopAnim();
    } else if (s.snatch_running) {
      eng.className='engine-status running';
      btnS.style.display='none'; btnT.style.display='block';
      btnT.innerHTML='<span class="ico"><svg t="1773332868844" class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" p-id="5658" fill="currentColor"><path d="M768 832h-128c-35.392 0-64-28.608-64-64V256c0-35.392 28.608-64 64-64h128c35.392 0 64 28.608 64 64v512c0 35.392-28.608 64-64 64z m-384 0h-128c-35.392 0-64-28.608-64-64V256c0-35.392 28.608-64 64-64h128c35.392 0 64 28.608 64 64v512c0 35.392-28.608 64-64 64z" p-id="5659"></path></svg></span> 停止';

      // 启动丝滑动画
      startAnim(s.snatch_phase, s.snatch_interval, s.snatch_phase_start, s.server_time);
    } else {
      eng.className='engine-status idle'; stText.innerHTML='⏸ 待命';
      bgFill.style.width = '0%';
      btnS.style.display='block'; btnT.style.display='none';
      stopAnim();
    }
  } catch(e){} finally {
    isPollingState = false;
  }
}

// === 丝滑进度条动画系统 ===
let animFrameId = null;
let currentAnimState = null;

function startAnim(phase, interval, phaseStart, serverTimeAtFetch) {
  const eng = document.getElementById('engineStatus');
  const bgFill = document.getElementById('engineProgressFill');
  const stText = document.getElementById('engineStatusText');

  if (phase === 'requesting') {
    stopAnim();
    eng.className = 'engine-status running';
    bgFill.style.background = 'rgba(255,255,255,0.4)';
    bgFill.style.width = '100%';
    stText.innerHTML = '<div class="spinner"></div><span>正在向服务器提交...</span>';
    return;
  }
  if (phase === 'request_done') {
    stopAnim();
    eng.className = 'engine-status running';
    bgFill.style.background = 'rgba(96,165,250,0.32)';
    bgFill.style.width = '100%';
    return;
  }
  if (phase === 'verifying') {
    stopAnim();
    eng.className = 'engine-status running';
    bgFill.style.background = 'rgba(255,200,0,0.3)';
    bgFill.style.width = '100%';
    stText.innerHTML = '<div class="spinner"></div><span>验证结果...</span>';
    return;
  }

  eng.className = 'engine-status running';
  bgFill.style.background = 'rgba(255,255,255,0.15)';
  const localTimeNow = performance.now() / 1000;
  currentAnimState = {
    interval: interval || 4.0,
    startTime: phaseStart,
    serverTimeOffset: serverTimeAtFetch - localTimeNow
  };

  if (!animFrameId) {
    const loop = () => {
      if (!currentAnimState) return;
      const nowS = (performance.now() / 1000) + currentAnimState.serverTimeOffset;
      const elapsed = nowS - currentAnimState.startTime;
      let pct = (elapsed / currentAnimState.interval) * 100;
      let remaining = currentAnimState.interval - elapsed;

      if (remaining <= 0) {
        remaining = 0;
        pct = 100;
        stText.innerHTML = `<div class="spinner"></div><span>即将开始下一轮请求...</span>`;
      } else if (currentAnimState.interval >= 60) {
        // 风控等待：显示分:秒格式
        const m = Math.floor(remaining / 60);
        const s = Math.floor(remaining % 60);
        stText.innerHTML = `<div class="spinner"></div><span>风控等待 ${m}:${String(s).padStart(2,'0')}</span>`;
        bgFill.style.background = 'rgba(255,100,100,0.25)';
      } else {
        stText.innerHTML = `<span>监控间隔 ${remaining.toFixed(1)}s </span>`;
      }

      bgFill.style.width = `${Math.max(0, Math.min(100, pct))}%`;
      animFrameId = requestAnimationFrame(loop);
    };
    animFrameId = requestAnimationFrame(loop);
  }
}

function stopAnim() {
  if (animFrameId) { cancelAnimationFrame(animFrameId); animFrameId = null; }
  currentAnimState = null;
}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}

async function logout() {
  try {
    const res = await fetch('/api/logout', {method:'POST'});
    const d = await res.json();
    if (d.ok) alert('已成功挂断会话');
  } catch(e) { alert('挂断失败: ' + e); }
}
window.addEventListener("load", initCustomSelects);
</script>
</body>
</html>"""


@app.route('/')
def index():
    import json
    return render_template_string(
        PANEL_HTML,
        DELAY_COURSE_REFRESH_MS=DELAY_COURSE_REFRESH_MS,
        DELAY_CLASS_FETCH_MS=DELAY_CLASS_FETCH_MS,
        POLL_LOGS_MS=POLL_LOGS_MS,
        POLL_STATE_MS=POLL_STATE_MS,
        POLL_PING_MS=POLL_PING_MS,
        LOG_ICONS_JSON=json.dumps(LOG_ICON_MAP)
    )


if __name__ == '__main__':
    app_host = os.environ.get("QK_BIND_HOST", "127.0.0.1")
    app_port = int(os.environ.get("QK_BIND_PORT", "5000"))
    display_host = "127.0.0.1" if app_host == "0.0.0.0" else app_host
    panel_url = f"http://{display_host}:{app_port}"
    print("=" * 55)
    print("  青果选课平台-Beta")
    print("=" * 55)
    print(f"  控制面板: {panel_url}")
    print("=" * 55)
    import threading, webbrowser
    threading.Timer(1.5, lambda: webbrowser.open(panel_url)).start()
    app.run(host=app_host, port=app_port, debug=False)
