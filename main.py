#!/usr/bin/env python3    # -*- coding: utf-8 -*

"""
main.py 南科大TIS喵课助手

@CreateDate 2021-1-9
@UpdateDate 2024-9-9
"""

import time
import os
import threading
from getpass import getpass
from json import loads, dumps
from html.parser import HTMLParser
from http.cookies import SimpleCookie
from urllib.parse import urljoin, urlparse

import requests
from colorama import init

import sys
import warnings
from urllib3.exceptions import InsecureRequestWarning


def warn(message, category, filename, lineno, _file=None, line=None):
    if category is not InsecureRequestWarning:
        sys.stderr.write(warnings.formatwarning(message, category, filename, lineno, line))

CLASS_CACHE_PATH = "class.txt"
COURSE_INFO_PATH = "course.txt"
USER_INFO_PATH = "user.txt"
warnings.showwarning = warn
SUCCESS = "[\x1b[0;32m+\x1b[0m] "
STAR = "[\x1b[0;32m*\x1b[0m] "
ERROR = "[\x1b[0;31mx\x1b[0m] "
INFO = "[\x1b[0;36m!\x1b[0m] "
FAIL = "[\x1b[0;33m-\x1b[0m] "
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
head = {
    "user-agent": UA,
    "x-requested-with": "XMLHttpRequest"
}

COURSE_TYPE = {'bxxk': "通识必修选课", 'xxxk': "通识选修选课", "kzyxk": '培养方案内课程',
               "zynknjxk": '非培养方案内课程', "cxxk": '重修选课', "jhnxk": '计划内选课新生'}

# 2024 年秋季起，选课请求的最小间隔约为 1500ms。
# 留出网络波动余量，程序强制所有选课线程共享至少 1600ms 的全局间隔。
# 如需调大，可设置环境变量 TIS_REQUEST_INTERVAL_MS；低于 1600 的值不会生效。
MIN_REQUEST_INTERVAL_MS = max(
    1600,
    int(os.environ.get("TIS_REQUEST_INTERVAL_MS", "1600"))
)
MIN_REQUEST_INTERVAL = MIN_REQUEST_INTERVAL_MS / 1000.0
HTTP_TIMEOUT = (5, 15)  # 连接超时、响应超时

_request_lock = threading.Lock()
_last_request_started_at = 0.0
_backoff_until = 0.0
_rate_limit_count = 0

course_list = []  # 需要喵的课程队列
# 由于Tis的新限制，逻辑改为同时只选一门课


def rate_limited_submit(data):
    """串行发送选课请求，并保证全局请求间隔不小于配置值。

    锁会覆盖等待和网络请求，因此即使以后重新引入多线程，也不会出现
    多个请求同时发出、各线程分别 sleep 却仍触发服务端限流的情况。
    """
    global _last_request_started_at, _backoff_until, _rate_limit_count

    with _request_lock:
        now = time.monotonic()
        earliest_start = max(
            _last_request_started_at + MIN_REQUEST_INTERVAL,
            _backoff_until
        )
        wait_seconds = earliest_start - now
        if wait_seconds > 0:
            time.sleep(wait_seconds)

        _last_request_started_at = time.monotonic()
        try:
            response = requests.post(
                'https://tis.sustech.edu.cn/Xsxk/addGouwuche',
                data=data,
                headers=head,
                verify=False,
                timeout=HTTP_TIMEOUT
            )
        except requests.RequestException:
            # 网络异常后至少暂停 5 秒，避免故障期间持续请求。
            _backoff_until = time.monotonic() + 5
            raise

        response_text = response.text
        is_rate_limited = (
            response.status_code in {403, 429}
            or "频繁" in response_text
            or "过快" in response_text
        )

        if is_rate_limited:
            _rate_limit_count += 1
            retry_after = response.headers.get("Retry-After", "")
            if retry_after.isdigit():
                backoff_seconds = max(int(retry_after), 5)
            else:
                backoff_seconds = min(5 * (2 ** (_rate_limit_count - 1)), 60)
            _backoff_until = time.monotonic() + backoff_seconds
            print(
                ERROR + f"检测到访问频率限制，将暂停 {backoff_seconds} 秒",
                flush=True
            )
        elif response.status_code >= 500:
            _backoff_until = time.monotonic() + 5
            print(ERROR + "TIS 服务暂时异常，将至少暂停 5 秒", flush=True)
        else:
            _rate_limit_count = 0

        return response


def response_message(response):
    """尽量从 TIS 响应中提取可读消息，避免非 JSON 响应导致程序崩溃。"""
    try:
        payload = response.json()
        if isinstance(payload, dict) and payload.get("message"):
            return str(payload["message"])
    except ValueError:
        pass
    return response.text.strip()[:200] or f"HTTP {response.status_code}"

def load_course():
    """ 用于加载本地要喵的课程
    如果存在文件就读文件里的，不存在就手动录入
    有些(我忘了是哪些了)情况会在文件头会有几个不可见字符，但是会被python读进来，所以第一行建议忽略留空"""
    courses = []
    if os.path.exists(CLASS_CACHE_PATH) and os.path.isfile(CLASS_CACHE_PATH):
        print(INFO + "读取规划课表...")
        with open(CLASS_CACHE_PATH, "r", encoding="utf8") as f:
            courses = f.readlines()
        print(SUCCESS + "规划课表读取完毕")
    else:
        print(FAIL + "没有找到规划课表，请手动输入课程信息，输入-1结束录入")
        s = "===本文件是待喵课程的列表，一行输入一个课程名字==请勿删除本行==="
        while s != "-1":
            courses.append(s)
            s = input()
        s = input(INFO + "是否保存录入的信息（y/N）？")
        if s in "yY":
            with open(CLASS_CACHE_PATH, "w", encoding="utf8") as f:
                f.writelines('\n'.join(courses))
    return courses


class _LoginFormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.execution = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name") == "execution":
            self.execution = attrs.get("value") or ""


def cas_login(sid, pwd):
    """收集 TIS 的 SESSION 或旧版 JSESSIONID；返回 route 和会话值。"""
    login_url = "https://cas.sustech.edu.cn/cas/login?service=https%3A%2F%2Ftis.sustech.edu.cn%2Fcas"
    tis_url = "https://tis.sustech.edu.cn/Xsxk/queryXkdqXnxq"
    # 避免将之前的 TIS Cookie 手动发送给 CAS。
    headers = {k: v for k, v in head.items() if k.lower() != "cookie"}
    phase = "获取CAS登录页"
    trace = []
    session = None
    completed = False

    def record_response(response, **kwargs):
        # 仅记录固定页面路径，省略查询参数及可能含会话标识的未知路径。
        parsed = urlparse(response.url)
        path = parsed.path
        if path not in {"/", "/cas", "/cas/login", "/login", "/login.jsp",
                        "/index", "/index.html", "/home"}:
            path = "/<其他路径已省略>"
        trace.append(f"{phase}: HTTP {response.status_code} {parsed.hostname}{path}")

    print(INFO + "测试CAS链接...")
    try:
        with requests.Session() as session:
            session.headers.update(headers)
            session.hooks['response'].append(record_response)
            req = session.get(login_url, verify=False, timeout=HTTP_TIMEOUT)
            req.raise_for_status()
            parser = _LoginFormParser()
            parser.feed(req.text)
            if not parser.execution:
                print(ERROR + "CAS登录页面缺少 execution 字段，请检查服务状态或登录页面是否变化")
                return "", ""
            print(SUCCESS + "成功连接到CAS...")
            print(INFO + "登录中...")
            phase = "提交CAS登录表单"
            req = session.post(
                login_url,
                data={'username': sid, 'password': pwd,
                      'execution': parser.execution, '_eventId': 'submit',
                      'geolocation': ''},
                allow_redirects=False, verify=False, timeout=HTTP_TIMEOUT
            )
            req.raise_for_status()
            location = req.headers.get("Location")
            if not req.is_redirect or not location:
                print(ERROR + "CAS未返回登录跳转，请检查账号密码或是否需要额外验证")
                return "", ""
            phase = "跟随登录跳转至TIS"
            req = session.get(urljoin(req.url, location),
                              verify=False, timeout=HTTP_TIMEOUT)
            req.raise_for_status()
            if urlparse(req.url).hostname != "tis.sustech.edu.cn":
                print(ERROR + "登录跳转未到达TIS，可能需要额外认证或TIS未接受登录票据")
                return "", ""
            # 按目标域名、路径与 Secure 属性选择 Cookie，避免误取 CAS 的同名值。
            prepared = session.prepare_request(requests.Request("POST", tis_url))
            cookie_header = prepared.headers.get("Cookie", "")
            cookies = SimpleCookie()
            cookies.load(cookie_header)
            session_cookie = next(
                (cookies[name] for name in ("SESSION", "JSESSIONID")
                 if name in cookies and cookies[name].value), None
            )
            if session_cookie is None:
                print(ERROR + f"TIS未返回可用的会话Cookie（SESSION/JSESSIONID，HTTP {req.status_code}，"
                      f"跳转 {len(req.history)} 次），需要核对实际认证方式")
                return "", ""
            route = cookies.get("route")
            head['cookie'] = cookie_header
            completed = True
            print(SUCCESS + "已获取TIS会话，登录完成")
            return route.value if route else "", session_cookie.value
    except requests.RequestException as ex:
        # 异常文本可能含带 ticket 的 URL，不能直接打印。
        status = ex.response.status_code if ex.response is not None else "无响应"
        print(ERROR + f"{phase}失败（{type(ex).__name__}，HTTP {status}）")
        return "", ""
    finally:
        if not completed:
            for entry in trace:
                print(INFO + "[登录诊断] " + entry)
            if session is not None:
                names = sorted({f"{cookie.name} @ {cookie.domain}"
                                for cookie in session.cookies})
                print(INFO + "[登录诊断] 已保存Cookie名称及域名: "
                      + (", ".join(names) or "无"))
                prepared = session.prepare_request(requests.Request("POST", tis_url))
                applicable = SimpleCookie()
                applicable.load(prepared.headers.get("Cookie", ""))
                print(INFO + "[登录诊断] 可发送给TIS接口的Cookie名称: "
                      + (", ".join(sorted(applicable)) or "无"))


def getinfo(semester_data):
    """ 用于向tis请求当前学期的课程ID，得到的ID将用于选课的请求
    输入当前学期的日期信息，返回的json包括了课程名和内部的ID """
    try:
        os.remove(COURSE_INFO_PATH)
        print(INFO + f"已删除旧课程缓存 {COURSE_INFO_PATH}，本次重新下载")
    except FileNotFoundError:
        pass
    print(INFO + "从服务器下载课程信息，请稍等...")
    _course_info = {}
    for c_type in COURSE_TYPE.keys():
        data = {
            "p_xn": semester_data['p_xn'],  # 当前学年
            "p_xq": semester_data['p_xq'],  # 当前学期
            "p_xnxq": semester_data['p_xnxq'],  # 当前学年学期
            "p_pylx": 1,
            "mxpylx": 1,
            "p_xkfsdm": c_type,
            "pageNum": 1,
            "pageSize": 1000  # 每学期总共开课在1000左右，所以单分类可以包括学期的全部课程
        }
        print("[\x1b[0;36m*\x1b[0m] " + f"获取 {COURSE_TYPE[c_type]} 列表...")
        count = 0
        while True:
            for attempt in range(3):
                time.sleep(max(MIN_REQUEST_INTERVAL, 5 if attempt else 0))
                try:
                    req = requests.post(
                        'https://tis.sustech.edu.cn/Xsxk/queryKxrw', data=data,
                        headers=head, verify=False, timeout=HTTP_TIMEOUT)
                    req.raise_for_status()
                    payload = req.json()
                    page = payload.get('kxrwList') if isinstance(payload, dict) else None
                    if not isinstance(page, dict) or not isinstance(page.get('list'), list):
                        raise ValueError('响应缺少 kxrwList.list，可能是登录失效、限流或接口返回错误')
                    rows = page['list']
                    if any(not isinstance(row, dict) or not row.get('rwmc')
                           or not row.get('id') for row in rows):
                        raise ValueError('课程记录缺少名称或ID')
                    pages = int(page.get('pages') or 0)
                    total = int(page.get('total') or 0)
                    break
                except (requests.RequestException, ValueError, TypeError) as ex:
                    status = req.status_code if isinstance(ex, ValueError) else '请求失败'
                    print(ERROR + f"{COURSE_TYPE[c_type]} 第{data['pageNum']}页读取失败"
                          f"（{type(ex).__name__}，{status}），尝试 {attempt + 1}/3")
                    if attempt == 2:
                        raise RuntimeError(f"{COURSE_TYPE[c_type]} 下载失败，未保存不完整课程缓存") from None
            for row in rows:
                _course_info[row['rwmc']] = (row['id'], c_type)
            count += len(rows)
            more = (data['pageNum'] < pages or count < total
                    or (not pages and not total and len(rows) == data['pageSize']))
            if not more:
                break
            if not rows:
                raise RuntimeError(f"{COURSE_TYPE[c_type]} 分页数据不完整，未保存课程缓存")
            data['pageNum'] += 1
        print(INFO + f"{COURSE_TYPE[c_type]}：{count} 门")
    print(SUCCESS + f"课程信息读取完毕，共读取{str(len(_course_info))}门课程信息")
    with open(COURSE_INFO_PATH, "w", encoding="utf8") as f:
        f.write(str(semester_data['p_xnxq']) + "\n")
        f.write(dumps(_course_info, ensure_ascii=False))
    return _course_info


def submit(semester_data, loop=3):
    """ 用于向tis发送喵课的请求
    这里假设主要耗时在网络IO上，本地处理时间几乎可以忽略
    （什么，购物车是怎么回事？那首先排除教务系统是个魔改的电商项目）"""
    for _ in range(loop):
        if not course_list:
            print(SUCCESS + "⌯'ㅅ'⌯所有课程已喵完，再见😾")
            exec("os._exit(0)")  # lint hack
        c_id, c_type, c_name = course_list[0]
        data = {
            "p_pylx": 1,
            "p_xktjz": "rwtjzyx",  # 提交至，可选任务，rwtjzgwc提交至购物车，rwtjzyx提交至已选 gwctjzyx购物车提交至已选
            "p_xn": semester_data['p_xn'],
            "p_xq": semester_data['p_xq'],
            "p_xnxq": semester_data['p_xnxq'],
            "p_xkfsdm": c_type,  # 选课方式
            "p_id": c_id,  # 课程id
            "p_sfxsgwckb": 1,  # 固定
        }
        try:
            req = rate_limited_submit(data)
        except requests.RequestException as ex:
            print(ERROR + f"选课请求失败：{ex}", flush=True)
            continue
        res = response_message(req)
        is_success = "成功" in req.text
        should_skip = any(x in req.text for x in ["冲突", "已选", "已满"])
        if is_success:
            print("[\x1b[0;34m{}\x1b[0m]".format("=" * 50), flush=True)
            print("[\x1b[0;34m█\x1b[0m]\t\t\t" + res, flush=True)
            print("[\x1b[0;34m{}\x1b[0m]".format("=" * 50), flush=True)
        else:
            print("[\x1b[0;30m-\x1b[0m]\t\t\t" + res, flush=True)
        if should_skip:
            print(f"[\x1b[0;31m!\x1b[0m] ({c_name})因为({res})跳过", flush=True)
        if is_success or should_skip:
            course_list.pop(0)
        
        
def submit_sequential(semester_data):
    """ 按照输入课程顺序向tis发送喵课请求 """
    if not course_list:
        print(SUCCESS + "⌯'ㅅ'⌯所有课程已喵完，再见😾")
        exec("os._exit(0)")  # lint hack
    course_list_copy = course_list.copy()
    for course in course_list_copy:
        c_id, c_type, c_name = course
        if course in course_list:
            data = {
                "p_pylx": 1,
                "p_xktjz": "rwtjzyx",  # 提交至，可选任务，rwtjzgwc提交至购物车，rwtjzyx提交至已选 gwctjzyx购物车提交至已选
                "p_xn": semester_data['p_xn'],
                "p_xq": semester_data['p_xq'],
                "p_xnxq": semester_data['p_xnxq'],
                "p_xkfsdm": c_type,  # 选课方式
                "p_id": c_id,  # 课程id
                "p_sfxsgwckb": 1,  # 固定
            }
            try:
                req = rate_limited_submit(data)
            except requests.RequestException as ex:
                print(ERROR + f"选课请求失败：{ex}", flush=True)
                continue
            res = response_message(req)
            is_success = "成功" in req.text
            should_skip = any(x in req.text for x in ["冲突", "已选", "已满"])
            if is_success:
                print("[\x1b[0;34m{}\x1b[0m]".format("=" * 50), flush=True)
                print("[\x1b[0;34m█\x1b[0m]\t\t\t" + res, flush=True)
                print("[\x1b[0;34m{}\x1b[0m]".format("=" * 50), flush=True)
            else:
                print("[\x1b[0;30m-\x1b[0m]\t\t\t" + res, flush=True)
            if should_skip:
                print(f"[\x1b[0;31m!\x1b[0m] ({c_name})因为({res})跳过", flush=True)
            if is_success or should_skip:
                course_list.remove(course)


def exit():
    """ 退出函数 """
    print(INFO + "退出喵课助手，再见😾")
    exec("os._exit(0)")  # lint hack


if __name__ == '__main__':
    init(autoreset=True)  # 某窗口系统的优质终端并不直接支持如下转义彩色字符，所以需要一些库来帮忙
    course_name_list = load_course()  # 读取本地待喵的课程
    # 下面是CAS登录
    route, session_id = "", ""
    has_saved_user_info = False
    if os.path.exists(USER_INFO_PATH): # 如果有保存的用户信息，尝试从文件自动登录
        try:
            with open(USER_INFO_PATH, "r", encoding="utf8") as f:
                lines = f.read().splitlines()
                if len(lines) >= 2:
                    has_saved_user_info = True
                    user_name, pass_word = lines[0], lines[1]
                    route, session_id = cas_login(user_name, pass_word)
        except Exception as e:
            print(FAIL + f"自动登录出现异常: {e}")
        if session_id == "":
            print(FAIL + "自动登录失败，需要手动登录")

    while session_id == "":
        user_name = input("请输入您的学号：")  # getpass在PyCharm里不能正常工作，请改为input或写死
        pass_word = getpass("请输入CAS密码（密码不显示，输入完按回车即可）：")
        route, session_id = cas_login(user_name, pass_word)
        if session_id == "":
            print(FAIL + "请重试...")
        elif not has_saved_user_info: # 仅首次手动登录时询问保存，已有信息不重复询问
            s = input(INFO + "是否保存用户信息（y/N）？")
            if s.lower() in {"y", "yes"}:
                with open(USER_INFO_PATH, "w", encoding="utf8") as f:
                    f.write(f"{user_name}\n{pass_word}")
                has_saved_user_info = True
    # 下面先获取当前的学期
    print(INFO + "从服务器获取当前喵课时间...")
    semester_info = loads(
        requests.post('https://tis.sustech.edu.cn/Xsxk/queryXkdqXnxq',
                      data={"mxpylx": 1}, headers=head, verify=False).text)  # 这里要加mxpylx才能获取到选课所在最新学期
    print(SUCCESS + f"当前学期是{semester_info['p_xn']}学年第{semester_info['p_xq']}学期，为"
                    f"{['', '秋季', '春季', '小'][int(semester_info['p_xq'])]}学期")
    # 然后获取本学期全部课程信息
    print(INFO + "读取课程信息...")
    try:
        course_info = getinfo(semester_info)
    except (RuntimeError, OSError) as ex:
        print(ERROR + str(ex))
        sys.exit(1)
    # 分析要喵课程的ID
    for name in course_name_list:
        name = name.lstrip('\ufeff').strip()
        if not name:
            continue
        if name in course_info.keys():
            course_id, course_type = course_info[name]
            course_list.append([course_id, course_type, name])
        else:
            print(FAIL + f"本次服务器课程列表中未找到：{name}；请核对上方分类数量和网页可选课程")
    print("[\x1b[0;34m{}\x1b[0m]".format("=" * 25))
    for course in course_list:
        print(f"{COURSE_TYPE[course[1]]} : {course[2]}\t\tID为: {course[0]}")
    print("[\x1b[0;34m{}\x1b[0m]".format("=" * 25))
    print(SUCCESS + "成功读入以上信息\n")
    # 喵课主逻辑

    if not course_list:
        print("没有读取到要喵的课程，请检查课程名称是否正确")
        exit()
    
    mode = input("请输入喵课模式：[1] -- 优先按照输入课程顺序喵课，2 -- 所有课程循环喵课，0 -- 退出\n") or "1"
    
    while True:

        if mode == "1":
            print(INFO + "当前模式: 优先按照输入课程顺序喵课")
            while course_list:
                if input(
                    STAR + f"按一下回车依次喵三次（全局间隔至少 {MIN_REQUEST_INTERVAL_MS}ms），"
                    "任意字符跳过当前课程\n"
                ):
                    course_list.pop(0)
                    if not course_list:
                        print(SUCCESS + "⌯'ㅅ'⌯所有课程已喵完，再见😾")
                        exec("os._exit(0)")
                try:
                    submit(semester_info, 3)
                except Exception as e:
                    print(f"[{e}] 请求异常")
        
        if mode == "2":
            print(INFO + "当前模式: 所有课程循环喵课")
            while course_list:
                if input(
                    STAR + f"按一下回车依次对所有课程喵一次（全局间隔至少 {MIN_REQUEST_INTERVAL_MS}ms），"
                    "任意字符退出\n"
                ):
                    exit()
                try:
                    submit_sequential(semester_info)
                except Exception as e:
                    print(f"[{e}] 请求异常")
        
        if mode == "0":
            exit()
