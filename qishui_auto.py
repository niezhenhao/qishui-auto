import argparse
import os
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

import cv2
import numpy as np
from rapidocr_onnxruntime import RapidOCR

# ================== 配置区 ==================
# 目标文字（按优先级：先点靠前的）
TARGETS = ["领取奖励", "继续观看", "领取成功"]
# 置信度下限
OCR_SCORE = 0.5
# 允许的轻微识别误差（去空格后做包含/近似）
FUZZY = True
POLL_INTERVAL = 1.2
CLICK_COOLDOWN = 2.0
# 未命中时是否随机上滑（默认关，避免误滑干扰排查）
ENABLE_RANDOM_SWIPE = False
SWIPE_PROB = 0.12
# 各目标允许点击的区域（相对屏幕宽高 0~1）
# 「领取奖励」「继续观看」必须在屏幕中央；「领取成功」常见右上角
TARGET_ROI = {
    "领取奖励": {"x": (0.25, 0.75), "y": (0.35, 0.70)},  # 中央
    "继续观看": {"x": (0.25, 0.75), "y": (0.35, 0.70)},  # 中央
    "领取成功": {"x": (0.0, 1.0), "y": (0.01, 0.97)},    # 几乎全屏（含右上）
}
CENTER_TARGETS = {"领取奖励", "继续观看"}
# 截图过大时缩小再 OCR，加快速度（坐标会按比例还原）
OCR_MAX_WIDTH = 720
# 每次点击保存标注截图，方便排查误点
SAVE_CLICK_DEBUG = True
# ===========================================

# ================== ADB 无线调试配置区 ==================
# 手动指定 adb 路径的三种方式：--adb 参数 / 环境变量 ADB_PATH / 下方已知目录自动探测
ADB_PATH_ENV = os.environ.get("ADB_PATH", "").strip()
KNOWN_ADB_PATHS = [
    r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe",
    r"%USERPROFILE%\AppData\Local\Android\Sdk\platform-tools\adb.exe",
    r"%USERPROFILE%\platform-tools\adb.exe",
    r"%ProgramFiles%\Android\platform-tools\adb.exe",
    r"%ProgramFiles(x86)%\Android\platform-tools\adb.exe",
    r"C:\platform-tools\adb.exe",
    r"D:\platform-tools\adb.exe",
    r"C:\Android\platform-tools\adb.exe",
    r"D:\Android\platform-tools\adb.exe",
    r"D:\JB\platform-tools\adb.exe",
]
PAIR_TIMEOUT = 60          # adb pair 最长等待秒数
CONNECT_TIMEOUT = 30       # adb connect 最长等待秒数
DEVICE_READY_TIMEOUT = 30  # 等待设备变为 device 状态的最长秒数
PAIRING_STYPE = "_adb-tls-pairing._tcp"
CONNECT_STYPE = "_adb-tls-connect._tcp"
ADB_BIN = "adb"
LAST_ADB_ERR = ""
CONNECTED_ADDR = None
INPUT_CLOSED = False
# =======================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEBUG_DIR = os.path.join(SCRIPT_DIR, "debug_clicks")
ocr_engine = None
click_seq = 0

# ================== 电脑端扫码配对（主推方式）配置 ==================
PAIR_WAIT_TIMEOUT = 180     # 生成二维码后等待手机广播配对服务的最长秒数
PAIR_POLL_INTERVAL = 2.0    # 轮询 adb mdns services 的间隔
CONNECT_WAIT_TIMEOUT = 40   # 配对成功后等待连接服务出现的最长秒数
QR_IMAGE_PATH = os.path.join(SCRIPT_DIR, "pair_qr.png")  # 二维码图片输出位置
# ==================================================================


# ================== ADB 基础能力 ==================
def resolve_adb(explicit=None, verbose=True):
    """三级解析 adb 路径：命令行参数 > 环境变量 > PATH > 已知安装目录。"""
    global ADB_BIN
    candidates = []
    if explicit:
        candidates.append(explicit)
    if ADB_PATH_ENV:
        candidates.append(ADB_PATH_ENV)
    found = shutil.which("adb")
    if found:
        candidates.append(found)
    for p in KNOWN_ADB_PATHS:
        candidates.append(os.path.expandvars(p))

    tried = []
    for c in candidates:
        if not c:
            continue
        c = str(c).strip().strip('"')
        path = c
        if os.path.sep not in c and "/" not in c:
            hit = shutil.which(c)
            if hit:
                path = hit
        if not os.path.isfile(path):
            tried.append(path)
            continue
        try:
            r = subprocess.run(
                [path, "version"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=20,
            )
        except Exception as e:
            tried.append(f"{path} ({e})")
            continue
        out = ((r.stdout or "") + (r.stderr or ""))
        if r.returncode == 0 and "Android Debug Bridge" in out:
            ADB_BIN = path
            if verbose:
                first = (r.stdout or "").strip().splitlines()
                print(f"✅ adb: {path}")
                if first:
                    print(f"   {first[0].strip()}")
            return ADB_BIN
        tried.append(path)

    ADB_BIN = "adb"
    if verbose:
        print("⚠️ 未找到可用的 adb 可执行文件，将直接尝试 PATH 中的 adb")
        print(f"   已尝试: {'; '.join(tried[:6]) if tried else '(无)'}")
        print("   解决办法：安装 Android Platform-Tools，或设置环境变量 ADB_PATH 指向 adb.exe")
    return ADB_BIN


GLOBAL_ADB_CMDS = {
    "devices", "mdns", "pair", "connect", "disconnect",
    "start-server", "kill-server", "version", "keygen", "help",
}


def adb_command(cmd, timeout=None):
    """执行 adb 子命令；失败时把 stderr 记录下来，方便定位连接问题。"""
    global LAST_ADB_ERR
    cmd = [str(c) for c in cmd]
    # 多设备（USB + 无线并存）时，把设备命令定向到已连接的无线设备
    if CONNECTED_ADDR and cmd and "-s" not in cmd and cmd[0] not in GLOBAL_ADB_CMDS:
        cmd = ["-s", str(CONNECTED_ADDR)] + cmd
    args = [ADB_BIN] + cmd
    try:
        r = subprocess.run(
            args, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except FileNotFoundError:
        LAST_ADB_ERR = f"找不到 adb 可执行文件: {ADB_BIN}"
        return subprocess.CompletedProcess(args=args, returncode=-1, stdout="", stderr=LAST_ADB_ERR)
    except subprocess.TimeoutExpired:
        LAST_ADB_ERR = f"命令超时（{timeout}s）: adb {' '.join(str(c) for c in cmd)}"
        return subprocess.CompletedProcess(args=args, returncode=-1, stdout="", stderr=LAST_ADB_ERR)
    except Exception as e:
        LAST_ADB_ERR = f"调用 adb 失败: {e}"
        return subprocess.CompletedProcess(args=args, returncode=-1, stdout="", stderr=LAST_ADB_ERR)

    if r.returncode != 0:
        LAST_ADB_ERR = ((r.stderr or "") + (r.stdout or "")).strip()
    return r


def adb_output(r):
    return ((r.stdout or "") + (r.stderr or "")).strip()


def adb_devices():
    """解析 adb devices -l，返回 [{serial, state, extra}]"""
    r = adb_command(["devices", "-l"], timeout=25)
    devices = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = re.split(r"\s+", line)
        if len(parts) < 2:
            continue
        devices.append({"serial": parts[0], "state": parts[1], "extra": " ".join(parts[2:])})
    return devices


def online_devices():
    return [d for d in adb_devices() if d["state"] == "device"]


def wait_for_device(timeout=DEVICE_READY_TIMEOUT, quiet=False):
    """轮询直到出现 state==device 的设备；online/unauthorized 会给出提示。"""
    deadline = time.time() + max(1, timeout)
    last_states = ""
    while time.time() < deadline:
        devs = adb_devices()
        ready = [d for d in devs if d["state"] == "device"]
        if ready:
            return ready[0]
        states = "、".join(f"{d['serial']}({d['state']})" for d in devs) or "无设备"
        if not quiet and states != last_states:
            print(f"   · 当前设备列表: {states}")
            last_states = states
        time.sleep(1.5)
    return None


def adb_mdns_services():
    """解析 adb mdns services，返回 [{name, type, host, port, addr}]"""
    r = adb_command(["mdns", "services"], timeout=25)
    out = adb_output(r)
    if not out:
        return []
    low = out.lower()
    if "unknown command" in low or "unrecognized" in low:
        print("⚠️ 当前 adb 版本不支持 mdns 命令，请升级 platform-tools（1.0.41 / adb 31+）")
        return []
    if "mdns daemon" in low and ("not" in low or "unavailable" in low or "error" in low):
        print("⚠️ adb 的 mDNS 发现服务不可用，可尝试: adb kill-server 后重新运行")
        print(f"   adb 输出: {out.splitlines()[0] if out else ''}")

    items = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("list of discovered"):
            continue
        if line.startswith("*") or line.upper().startswith("ERROR"):
            continue
        parts = re.split(r"\s+", line)
        if len(parts) < 3:
            continue
        name, stype, addr = parts[0], parts[1], parts[-1]
        if "._tcp" not in stype:
            continue
        host, sep, port = addr.rpartition(":")
        if not sep or not host or not port.isdigit():
            continue
        items.append({"name": name, "type": stype, "host": host, "port": port, "addr": f"{host}:{port}"})
    return items


def find_mdns(stype):
    return [s for s in adb_mdns_services() if stype in s["type"]]


def ask(prompt, default=None):
    global INPUT_CLOSED
    try:
        s = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        INPUT_CLOSED = True
        return default if default is not None else ""
    s = s.strip()
    if not s and default is not None:
        return default
    return s


def choose_item(items, title):
    """多个候选时让用户选，返回选中条目；直接回车取第一个。"""
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    print(f"发现多个{title}候选：")
    for i, s in enumerate(items, 1):
        print(f"  [{i}] {s['name']}  ->  {s['addr']}")
    raw = ask("请选择序号（回车默认 1）> ", "1")
    if raw.isdigit() and 1 <= int(raw) <= len(items):
        return items[int(raw) - 1]
    return items[0]


def choose_from(items, title):
    """多个候选时让用户选；直接回车取第一个。"""
    if not items:
        return None
    if len(items) == 1:
        return items[0]["addr"]
    item = choose_item(items, title)
    return item["addr"] if item else None


IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


def guess_phone_ip():
    """从当前 mDNS 记录里推断手机 IP（用于只输入端口时补全地址）。"""
    for stype in (PAIRING_STYPE, CONNECT_STYPE):
        try:
            for s in find_mdns(stype):
                host = s.get("host", "")
                if IPV4_RE.match(host):
                    return host
        except Exception:
            continue
    return None


def normalize_addr(addr, what="地址"):
    """把用户输入规范成 IP:端口；只有端口时自动补手机 IP，避免出现 `adb pair 41197`。"""
    if addr is None:
        return None
    s = str(addr).strip().strip('"').strip("'").strip()
    s = s.replace("：", ":")                         # 中文冒号
    s = re.sub(r"(?i)^adb\s+pair\s+", "", s)         # 误粘整条命令
    s = re.sub(r"\s*:\s*", ":", s)                   # 「IP : 端口」中间的空格
    parts = s.split()
    if len(parts) > 1:                                # 误粘「地址 + 配对码」时只保留地址部分
        if ":" in parts[0]:
            s = parts[0]
        elif parts[0].isdigit() and parts[1].isdigit() and len(parts[1]) == 6:
            s = parts[0]                              # 端口 + 6 位配对码
        elif parts[1].isdigit():
            s = parts[0] + ":" + parts[1]             # 「IP 端口」中间带空格
        else:
            s = parts[0]
    s = s.replace(" ", "")
    if not s:
        return None

    if s.isdigit():                                   # 只给了端口
        port = s
        host = guess_phone_ip()
        if not host:
            print(f"⚠️ {what}只检测到端口 {port}，缺少手机 IP；请重新输入完整地址（形如 192.168.1.5:{port}）")
            return None
        print(f"· {what}只包含端口 {port}，已自动补全为 {host}:{port}")
        return f"{host}:{port}"

    host, sep, port = s.rpartition(":")
    if not sep or not host:
        print(f"⚠️ {what}缺少端口：{s}；请输入 IP:端口（以手机『无线调试』页面显示为准）")
        return None
    if not port.isdigit():
        print(f"⚠️ {what}端口不是数字：{s}")
        return None
    return f"{host}:{port}"


def pick_pairing_addr(service_name=None, tries=3):
    """自动发现配对端口；优先匹配二维码里的服务名。"""
    for i in range(tries):
        items = find_mdns(PAIRING_STYPE)
        if service_name:
            exact = [s for s in items if s["name"] == service_name]
            if exact:
                return choose_from(exact, "配对服务")
        if items:
            return choose_from(items, "配对服务")
        if i < tries - 1:
            print("   · 暂未发现配对服务，2 秒后重试 ...")
            time.sleep(2)
    return None


def pick_connect_addr(service_name=None, tries=5):
    """自动发现连接端口；配对后设备可能短暂消失，故多次重试。"""
    for i in range(tries):
        items = find_mdns(CONNECT_STYPE)
        if service_name:
            exact = [s for s in items if s["name"] == service_name]
            if exact:
                return choose_from(exact, "连接服务")
            prefix = service_name.split("-")[0]
            weak = [s for s in items if s["name"].startswith(prefix)]
            if weak:
                return choose_from(weak, "连接服务")
        if items:
            return choose_from(items, "连接服务")
        if i < tries - 1:
            print("   · 暂未发现连接服务，2 秒后重试 ...")
            time.sleep(2)
    return None


def adb_pair(addr, code):
    print(f"⏳ 正在配对: adb pair {addr}（配对码 {code}）")
    r = adb_command(["pair", addr, str(code).strip()], timeout=PAIR_TIMEOUT)
    out = adb_output(r)
    low = out.lower()
    ok = ("successfully paired" in low) or ("already paired" in low)
    if ok:
        print("✅ 配对成功")
    else:
        print("❌ 配对失败")
        if "failed" in low or "unable" in low or "error" in low:
            print(f"   adb 返回: {out}")
            print("   常见原因：配对码已过期（手机上重新点『使用配对码配对设备』）、配对端口填错、手机与电脑不在同一 Wi-Fi")
        elif out:
            print(f"   adb 返回: {out}")
        else:
            print("   adb 无任何返回（可能超时）：请确认手机停留在『使用配对码配对设备』弹窗页面")
    return ok, out


def adb_connect(addr):
    print(f"⏳ 正在连接: adb connect {addr}")
    r = adb_command(["connect", addr], timeout=CONNECT_TIMEOUT)
    out = adb_output(r)
    low = out.lower()
    ok = ("connected to" in low) or ("already connected" in low)
    if ok:
        print(f"✅ {out}")
    else:
        print(f"❌ 连接失败: {out or '(无返回)'}")
        print("   提示：确认手机『无线调试』开关处于开启状态，端口以手机页面显示为准（重启会变化）")
    return ok, out


def collect_mdns(stype):
    """按服务类型筛选 mDNS 记录。"""
    return [s for s in adb_mdns_services() if stype in s["type"]]


def wait_mdns_service(stype, service_name=None, timeout=PAIR_WAIT_TIMEOUT,
                      baseline=None, hint="服务"):
    """轮询等待 mDNS 服务出现：优先精确匹配服务名，其次识别新增服务；超时返回 None。"""
    baseline = set(baseline or [])
    deadline = time.time() + max(5, timeout)
    next_tip = 0.0
    while True:
        if INPUT_CLOSED:
            return None
        items = collect_mdns(stype)
        if service_name:
            exact = [s for s in items if s["name"] == service_name]
            if exact:
                return choose_item(exact, hint)
        fresh = [s for s in items if s["addr"] not in baseline]
        if fresh:
            return choose_item(fresh, hint)

        now = time.time()
        if now >= deadline:
            return None
        if now >= next_tip:
            left = int(deadline - now)
            print(f"   · 正在等待{hint} ... 剩余 {left}s")
            next_tip = now + 15
        time.sleep(PAIR_POLL_INTERVAL)


def make_service_name():
    """生成随机 mDNS 服务名，格式与手机端一致：adb-XXXXXX-YYYYYY。"""
    alphabet = "0123456789ABCDEF"
    def seg():
        return "".join(random.choice(alphabet) for _ in range(6))
    return f"adb-{seg()}-{seg()}"


def make_pair_code():
    """生成 6 位随机配对码。"""
    return f"{random.randint(0, 999999):06d}"


def build_qr_payload(service_name, code):
    return f"WIFI:T:ADB;S:{service_name};P:{code};;"


def render_pair_qr(payload, path=None):
    """用 qrcode 库渲染配对二维码 PNG，返回图片路径。"""
    try:
        import qrcode
    except ImportError:
        raise RuntimeError('缺少 qrcode 库，请先执行: pip install "qrcode[pil]"')
    path = path or QR_IMAGE_PATH
    qr = qrcode.QRCode(version=None, box_size=12, border=4)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    img.save(path)
    return path


def open_image(path):
    """用系统默认看图器打开图片（Windows 走 os.startfile）。"""
    try:
        if os.name == "nt":
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception as e:
        print(f"⚠️ 无法自动打开图片（{e}），请手动双击打开：{path}")
        return False


def show_pair_qr(service_name, code, auto_open=True):
    """生成并显示配对二维码，返回图片路径（失败返回 None）。"""
    payload = build_qr_payload(service_name, code)
    print()
    print("─" * 60)
    print("  手机扫码配对：让手机扫电脑屏幕上的二维码")
    print("─" * 60)
    print(f"  服务名  : {service_name}")
    print(f"  配对码  : {code}")
    print(f"  二维码内容: {payload}")
    png = None
    try:
        png = render_pair_qr(payload)
        print(f"  二维码图片: {png}")
    except Exception as e:
        print(f"⚠️ 二维码生成失败：{e}")
        print(f"   可手动在手机『无线调试』里输入配对码 {code} 完成配对，选菜单里的『配对码配对』")

    if png:
        print()
        print("手机操作：设置 → 开发者选项 → 无线调试 → 『使用二维码配对设备』→ 扫描电脑屏幕上的二维码")
        if auto_open:
            if open_image(png):
                print(f"   已用默认看图器打开二维码：{png}")
        else:
            print(f"   请手动打开该图片后扫码：{png}")
    print("─" * 60)
    return png


def pair_by_pc_qr(auto_open=True, wait_timeout=PAIR_WAIT_TIMEOUT):
    """电脑端生成二维码 → 手机扫码 → 自动发现配对端口并完成配对、连接（主推方式）。"""
    global CONNECTED_ADDR
    service_name = make_service_name()
    code = make_pair_code()

    print()
    print("=" * 60)
    print("  扫码配对（电脑生成二维码 · 手机扫码）")
    print("=" * 60)
    baseline = {s["addr"] for s in collect_mdns(PAIRING_STYPE)}
    show_pair_qr(service_name, code, auto_open=auto_open)

    while True:
        if INPUT_CLOSED:
            print("⚠️ 未检测到可交互的输入环境，无法继续等待扫码")
            return False
        print(f"⏳ 等待手机扫码（最长 {wait_timeout}s）...")
        item = wait_mdns_service(
            PAIRING_STYPE, service_name, timeout=wait_timeout,
            baseline=baseline, hint="手机的配对服务",
        )
        if item:
            break

        print()
        print(f"⚠️ {wait_timeout}s 内没发现手机的配对服务广播")
        print("   排查：1) 手机是否已扫到二维码并点了『配对』")
        print("         2) 手机『无线调试』是否开启、与电脑是否同一 Wi-Fi")
        print("         3) 可在另一个窗口执行 adb mdns services 看是否出现配对服务")
        print("         4) Windows 防火墙若拦截 mDNS（5353/UDP），需放行 adb")
        choice = ask("[1] 继续等待  [2] 重新生成二维码  [3] 放弃扫码配对 > ", "1")
        if INPUT_CLOSED or choice == "3":
            print("已放弃扫码配对")
            return False
        if choice == "2":
            service_name = make_service_name()
            code = make_pair_code()
            baseline = {s["addr"] for s in collect_mdns(PAIRING_STYPE)}
            show_pair_qr(service_name, code, auto_open=auto_open)
        continue

    addr = normalize_addr(item["addr"], "配对地址")
    print(f"✅ 已发现手机的配对服务：{item['name']} -> {item['addr']}")
    if not addr:
        print("❌ 配对地址无效，请改用菜单里的『配对码配对』并手动输入手机上的 IP:端口")
        return False

    conn_baseline = {s["addr"] for s in collect_mdns(CONNECT_STYPE)}
    ok, _ = adb_pair(addr, code)
    if not ok:
        again = ask("配对失败，是否重新生成二维码再试一次？[1] 重试  [2] 放弃 > ", "1")
        if INPUT_CLOSED or again != "1":
            return False
        return pair_by_pc_qr(auto_open=auto_open, wait_timeout=wait_timeout)

    print("⏳ 配对成功，等待设备就绪 ...")
    time.sleep(2)
    if wait_for_device(8, quiet=True):
        devs = online_devices()
        if devs:
            CONNECTED_ADDR = devs[0]["serial"]
            print(f"✅ 设备已就绪：{CONNECTED_ADDR}")
        return True
    return connect_after_pair(service_name, baseline=conn_baseline)


def try_reconnect():
    """主循环截图连续失败时尝试重连无线设备。"""
    if not CONNECTED_ADDR or ":" not in str(CONNECTED_ADDR):
        return False
    print(f"🔄 尝试重新连接 {CONNECTED_ADDR} ...")
    ok, _ = adb_connect(CONNECTED_ADDR)
    if wait_for_device(12, quiet=True):
        print("✅ 重连成功，继续运行")
        return True
    print("❌ 重连失败，请检查手机无线调试是否开启")
    return False


def parse_qr_payload(text):
    """解析二维码文本，形如 WIFI:T:ADB;S:adb-xxxx;P:123456;;"""
    if not text:
        return None
    raw = str(text).strip()
    # 去掉 WIFI: 前缀，便于统一按 key:value 解析（如 WIFI:T:ADB;S:...;P:...;;）
    body = re.sub(r"(?i)^WIFI:", "", raw)
    fields = {}
    for k, v in re.findall(r"(?:^|;)([A-Za-z]):((?:[^;\\]|\\.)*)", body):
        key = k.upper()
        if key not in fields:
            fields[key] = v.replace("\\;", ";").replace("\\\\", "\\")
    code = fields.get("P", "").strip()
    if not code:
        m = re.search(r"\b(\d{6})\b", str(text))
        if m:
            code = m.group(1)
    return {
        "service": fields.get("S", "").strip(),
        "code": code,
        "type": fields.get("T", "").strip().upper(),
        "raw": str(text).strip(),
    }


def decode_qr_image(path):
    """用 OpenCV 解码二维码图片（兼容中文路径与过小图片）。"""
    path = str(path).strip().strip('"')
    if not os.path.isfile(path):
        return None, f"文件不存在: {path}"
    try:
        buf = np.fromfile(path, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception as e:
        return None, f"读取图片失败: {e}"
    if img is None:
        return None, "图片解析失败，请确认是有效的 PNG/JPG 图片"

    detector = cv2.QRCodeDetector()
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    pad = max(24, int(min(h, w) * 0.08))
    candidates = [img]
    if max(h, w) < 600:
        big = cv2.resize(img, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
        candidates.append(big)
        candidates.append(cv2.cvtColor(big, cv2.COLOR_BGR2GRAY))
    candidates.append(gray)
    # 裁得过紧（缺静区）时补白边，提高识别率
    candidates.append(cv2.copyMakeBorder(img, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=(255, 255, 255)))
    candidates.append(cv2.copyMakeBorder(gray, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255))

    for cand in candidates:
        try:
            data = detector.detectAndDecode(cand)[0]
        except Exception:
            data = None
        if data:
            return str(data).strip(), ""

    try:
        ok, texts, _, _ = detector.detectAndDecodeMulti(img)
        if ok and texts:
            for t in texts:
                if t:
                    return str(t).strip(), ""
    except Exception:
        pass
    return None, "未在图片中检测到二维码（建议裁切到二维码本体，或改用粘贴二维码文本）"


# ================== 连接建立流程 ==================
def pair_by_code(code, addr=None, service_name=None):
    global CONNECTED_ADDR
    code = str(code).strip()
    if not re.fullmatch(r"\d{6}", code):
        print(f"⚠️ 配对码通常是 6 位数字，当前输入为: {code}（仍会尝试配对）")

    if not addr:
        print("⏳ 正在通过 mDNS 自动发现配对端口 ...")
        addr = pick_pairing_addr(service_name)
    addr = normalize_addr(addr, "配对地址") if addr else None
    if not addr:
        print("⚠️ 未能自动发现配对服务（手机是否已点开『使用配对码配对设备』？）")
        addr = normalize_addr(
            ask("请手动输入手机上显示的配对地址（IP:端口，如 192.168.1.5:37000）> "),
            "配对地址",
        )
    if not addr:
        print("❌ 未提供有效的配对地址（必须是 IP:端口，只有端口无法配对），配对终止")
        return False

    conn_baseline = {s["addr"] for s in collect_mdns(CONNECT_STYPE)}
    ok, _ = adb_pair(addr, code)
    if not ok:
        return False

    print("⏳ 等待设备就绪 ...")
    time.sleep(2)
    if wait_for_device(8, quiet=True):
        devs = online_devices()
        if devs:
            CONNECTED_ADDR = devs[0]["serial"]
            print(f"✅ 设备已就绪: {CONNECTED_ADDR}")
        return True
    return connect_after_pair(service_name, baseline=conn_baseline)


def connect_after_pair(service_name=None, manual=None, baseline=None):
    global CONNECTED_ADDR
    addr = normalize_addr(manual, "连接地址") if manual else None
    if not addr:
        print("⏳ 正在发现连接端口（_adb-tls-connect._tcp）...")
        item = wait_mdns_service(
            CONNECT_STYPE, service_name, timeout=CONNECT_WAIT_TIMEOUT,
            baseline=baseline, hint="连接服务",
        )
        addr = item["addr"] if item else pick_connect_addr(service_name)
    if not addr:
        print("⚠️ 未能自动发现连接端口")
        addr = normalize_addr(
            ask("请手动输入连接地址（手机『无线调试』页面显示的 IP:端口）> "),
            "连接地址",
        )
    if not addr:
        print("❌ 未提供有效的连接地址（必须是 IP:端口），无法建立连接")
        return False

    ok, _ = adb_connect(addr)
    if not ok:
        # 无线调试端口会随开关变化，mDNS 记录可能已过期，重新解析后重试一次
        print("· 首次连接失败，重新解析 mDNS 端口后重试一次 ...")
        for s in find_mdns(CONNECT_STYPE):
            cand = s.get("addr")
            if cand and cand != addr:
                print(f"· 尝试新发现的连接端口: {cand}")
                ok, _ = adb_connect(cand)
                if ok:
                    addr = cand
                    break
    if not ok:
        print("❌ 无法连接设备：请确认手机『无线调试』仍处于开启状态（关闭后端口会变化），必要时重新配对")
        return False
    print("⏳ 等待设备就绪 ...")
    dev = wait_for_device(DEVICE_READY_TIMEOUT)
    if dev:
        CONNECTED_ADDR = addr
        print(f"✅ 无线设备已就绪: {addr}")
        return True
    print("❌ 连接已发起但设备未就绪（可能被拒绝授权，请查看手机弹窗）")
    return False


def pair_by_qr(args):
    payload_text = args.qr_text
    if args.qr_image:
        print(f"⏳ 正在解码二维码图片: {args.qr_image}")
        text, err = decode_qr_image(args.qr_image)
        if not text:
            print(f"❌ 二维码解码失败: {err}")
            return False
        print(f"✅ 二维码内容: {text}")
        payload_text = text
    if not payload_text:
        print("❌ 未提供二维码文本")
        return False

    info = parse_qr_payload(payload_text)
    if not info or not info["code"]:
        print(f"❌ 无法从二维码文本解析出配对码，原文: {payload_text}")
        print("   正确格式示例: WIFI:T:ADB;S:adb-XXXXXX-YYYYYY;P:123456;;")
        return False
    if info["type"] and info["type"] != "ADB":
        print(f"⚠️ 二维码类型不是 ADB（T={info['type']}），仍按无线调试配对尝试")

    name = info["service"]
    manual = args.pair_addr
    clean_name = None
    if name:
        if ":" in name:
            manual = manual or name
        else:
            clean_name = name
    print(f"   服务名: {name or '(未提供)'}    配对码: {info['code']}")
    return pair_by_code(info["code"], manual, service_name=clean_name)


def interactive_pair_menu(args):
    """交互菜单：满足不熟悉命令行的使用者。"""
    while True:
        if INPUT_CLOSED:
            print("⚠️ 未检测到可交互的输入环境，已退出配对流程")
            return False
        print()
        print("检测到当前没有可用的无线调试设备，请选择配对方式：")
        print("  [1] 扫码配对（推荐：电脑显示二维码，手机『使用二维码配对设备』扫一扫）")
        print("  [2] 配对码配对（手机『无线调试 → 使用配对码配对设备』显示 6 位码）")
        print("  [3] 二维码配对（粘贴手机二维码识别出的文本）")
        print("  [4] 二维码配对（输入二维码图片路径，由 OpenCV 解码）")
        print("  [5] 直接连接已开启无线调试的设备（手动输入 IP:端口）")
        print("  [0] 退出")
        choice = ask("请输入序号 [1]: ", "1")

        if choice == "0":
            print("已取消连接")
            return False

        if choice == "1":
            wait = getattr(args, "pair_wait", PAIR_WAIT_TIMEOUT) or PAIR_WAIT_TIMEOUT
            auto_open = not getattr(args, "no_open", False)
            if pair_by_pc_qr(auto_open=auto_open, wait_timeout=wait):
                return True
            continue

        if choice == "2":
            print()
            print("请在手机上操作：设置 → 开发者选项 → 无线调试 → 使用配对码配对设备")
            print("手机会显示『配对码』与『IP 地址和端口』")
            mode = ask("配对地址获取方式 [1] 自动发现（推荐）  [2] 手动输入 > ", "1")
            addr = None
            if mode == "2":
                addr = ask("请输入手机显示的 IP:端口 > ")
            code = ask("请输入手机显示的 6 位配对码 > ")
            if not code:
                print("⚠️ 未输入配对码，返回菜单")
                continue
            if pair_by_code(code, addr):
                return True
            continue

        if choice == "3":
            print()
            print("请粘贴手机『无线调试 → 使用二维码配对设备』页面上二维码识别出的文本")
            print("格式示例: WIFI:T:ADB;S:adb-XXXXXX-YYYYYY;P:123456;;")
            text = ask("二维码文本 > ")
            if not text:
                print("⚠️ 未输入内容，返回菜单")
                continue
            info = parse_qr_payload(text)
            if not info or not info["code"]:
                print("❌ 解析失败：未找到配对码字段 P:")
                continue
            name = info["service"]
            manual = None
            clean_name = None
            if name:
                if ":" in name:
                    manual = name
                else:
                    clean_name = name
            print(f"   服务名: {name or '(未提供)'}    配对码: {info['code']}")
            if pair_by_code(info["code"], manual, service_name=clean_name):
                return True
            continue

        if choice == "4":
            print()
            path = ask("请输入二维码图片的完整路径（可拖拽文件到窗口）> ").strip().strip('"')
            if not path:
                print("⚠️ 未输入路径，返回菜单")
                continue
            text, err = decode_qr_image(path)
            if not text:
                print(f"❌ 二维码解码失败: {err}")
                continue
            print(f"✅ 二维码内容: {text}")
            info = parse_qr_payload(text)
            if not info or not info["code"]:
                print("❌ 解析失败：未找到配对码字段 P:")
                continue
            name = info["service"]
            manual = name if (name and ":" in name) else None
            clean_name = name if (name and ":" not in name) else None
            print(f"   服务名: {name or '(未提供)'}    配对码: {info['code']}")
            if pair_by_code(info["code"], manual, service_name=clean_name):
                return True
            continue

        if choice == "5":
            print()
            print("请确认手机：设置 → 开发者选项 → 无线调试 已开启")
            addr = ask("请输入手机『无线调试』页面显示的 IP:端口 > ")
            if not addr:
                print("⚠️ 未输入地址，返回菜单")
                continue
            if connect_after_pair(None, manual=addr):
                return True
            continue

        print("⚠️ 输入无效，请重新选择")


def print_connection_help():
    print("  手机端准备（每台手机只需一次）：")
    print("    1. 设置 → 关于手机 → 连点『版本号』7 次，开启开发者模式")
    print("    2. 设置 → 开发者选项 → 打开『无线调试』")
    print("    3. 手机与电脑连接同一个 Wi-Fi（同一网段）")
    print("    4. 小米/vivo 等机型还需开启『USB 调试（安全设置）』以允许模拟点击")
    print("  配对方式（推荐第 1 种）：")
    print("    1) 扫码：电脑弹出二维码 → 手机『无线调试 → 使用二维码配对设备』扫一扫")
    print("    2) 配对码：手机『使用配对码配对设备』→ 把 6 位码和 IP:端口 填进电脑")


def prepare_connection(args):
    """建立 ADB 无线连接：优先复用已连接设备，其次走配对流程。"""
    global CONNECTED_ADDR
    resolve_adb(args.adb, verbose=False)

    print("⏳ 启动 adb server ...")
    adb_command(["start-server"], timeout=60)

    if args.skip_pair:
        print("· 已指定 --skip-pair，跳过配对直接使用当前设备")
        dev = wait_for_device(10)
        if dev:
            CONNECTED_ADDR = dev["serial"]
            print(f"✅ 使用现有设备: {dev['serial']}")
            return True
        print("❌ 当前没有处于 device 状态的设备")
        return False

    if args.connect_addr:
        ok = connect_after_pair(None, manual=args.connect_addr)
        if not ok:
            return False
        return True

    if args.qr_make:
        return pair_by_pc_qr(auto_open=not args.no_open, wait_timeout=args.pair_wait)

    if args.qr_text or args.qr_image:
        return pair_by_qr(args)

    if args.pair_code:
        return pair_by_code(args.pair_code, args.pair_addr)

    devs = online_devices()
    if devs:
        if not args.repair:
            for d in devs:
                if ":" in d["serial"]:
                    CONNECTED_ADDR = d["serial"]
                    break
            print(f"✅ 已检测到在线设备: {devs[0]['serial']}")
            print("   （如需重新配对，请加 --repair 参数或选择配对菜单）")
            return True
        print("· 已指定 --repair，忽略现有设备，重新配对")

    print()
    print_connection_help()

    unauthorized = [d for d in adb_devices() if d["state"] == "unauthorized"]
    if unauthorized:
        print()
        print(f"⚠️ 检测到设备 {unauthorized[0]['serial']} 处于未授权状态，请先在手机上点『允许』并在配对弹窗确认")

    return interactive_pair_menu(args)


def run_diag():
    """诊断模式：打印 adb / 设备 / mDNS 信息，便于排查连接问题。"""
    print("=" * 60)
    print("  ADB 无线调试诊断")
    print("=" * 60)
    print(f"adb 路径: {ADB_BIN}")
    r = adb_command(["version"], timeout=25)
    ver = adb_output(r)
    print(ver if ver else "(adb version 无输出)")

    print()
    print("[设备列表] adb devices -l")
    devs = adb_devices()
    if not devs:
        print("  （无设备）")
    for d in devs:
        print(f"  {d['serial']}\t{d['state']}\t{d['extra']}")

    print()
    print("[mDNS 服务] adb mdns services")
    items = adb_mdns_services()
    if not items:
        print("  （无）")
        print("  说明：手机需开启『无线调试』并向电脑广播服务；若手机在配对页，应能看到 "
              f"{PAIRING_STYPE}")
    for s in items:
        tag = ""
        if PAIRING_STYPE in s["type"]:
            tag = "  <- 配对端口"
        elif CONNECT_STYPE in s["type"]:
            tag = "  <- 连接端口"
        print(f"  {s['name']}\t{s['type']}\t{s['addr']}{tag}")

    print()
    if LAST_ADB_ERR:
        print(f"最近一次 adb 错误: {LAST_ADB_ERR}")
    print("=" * 60)


# ================== OCR / 点击逻辑（保持原逻辑） ==================
def get_ocr():
    global ocr_engine
    if ocr_engine is None:
        print("⏳ 加载 OCR 模型...")
        ocr_engine = RapidOCR()
        print("✅ OCR 就绪")
    return ocr_engine


def screenshot():
    global LAST_ADB_ERR
    cmd = [ADB_BIN]
    if CONNECTED_ADDR:
        cmd += ["-s", str(CONNECTED_ADDR)]
    cmd += ["exec-out", "screencap", "-p"]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        LAST_ADB_ERR = "截图超时（设备可能已断开）"
        return None
    except FileNotFoundError:
        LAST_ADB_ERR = f"找不到 adb: {ADB_BIN}"
        return None
    if result.returncode != 0 or not result.stdout:
        err = result.stderr.decode("utf-8", "replace").strip() if result.stderr else ""
        LAST_ADB_ERR = err or f"returncode={result.returncode}, 截图输出为空"
        return None
    img = cv2.imdecode(np.frombuffer(result.stdout, np.uint8), cv2.IMREAD_COLOR)
    return img


def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.strip().replace(" ", "").replace("\u3000", "")
    # 去掉 OCR 常见尾巴符号（如「领取成功×」）
    s = re.sub(r"[×xX✕✖×·•\|丨\[\]【】()（）\d:：]+$", "", s)
    s = re.sub(r"^[×xX✕✖·•\|丨]+", "", s)
    # 常见 OCR 混淆
    s = s.replace("领収", "领取").replace("领职", "领取").replace("领敢", "领取")
    s = s.replace("奨", "奖").replace("奬", "奖")
    s = s.replace("勵", "励")
    return s


def text_match(recognized: str, target: str) -> bool:
    a = normalize_text(recognized)
    b = normalize_text(target)
    if not a:
        return False
    # 排除标题/倒计时等提示文字
    if any(k in a for k in ["秒", "解锁", "后可领取", "第"]):
        return False
    if a == b or b in a:
        return True
    if not FUZZY:
        return False
    hit = sum(1 for ch in b if ch in a)
    return hit >= len(b)


def box_center(box):
    """box: 4 点 [[x,y], ...] -> (cx, cy)"""
    pts = np.array(box, dtype=np.float32)
    cx = float(pts[:, 0].mean())
    cy = float(pts[:, 1].mean())
    return cx, cy


def in_roi(cx, cy, sw, sh, target: str):
    roi = TARGET_ROI.get(target, {"x": (0.0, 1.0), "y": (0.0, 1.0)})
    x0, x1 = roi["x"]
    y0, y1 = roi["y"]
    return x0 * sw <= cx <= x1 * sw and y0 * sh <= cy <= y1 * sh


def pos_pct(cx, cy, sw, sh):
    return cx / sw * 100.0, cy / sh * 100.0


def save_click_debug(screen, x, y, target, text, score, seq, ts):
    """保存带点击标记的截图，方便对照误点"""
    if not SAVE_CLICK_DEBUG:
        return None
    os.makedirs(DEBUG_DIR, exist_ok=True)
    vis = screen.copy()
    # 大红十字 + 圆，醒目
    cv2.drawMarker(vis, (x, y), (0, 0, 255), markerType=cv2.MARKER_CROSS, markerSize=60, thickness=3)
    cv2.circle(vis, (x, y), 28, (0, 0, 255), 3)
    cv2.circle(vis, (x, y), 6, (0, 255, 255), -1)
    # 画该目标 ROI 框
    sh, sw = screen.shape[:2]
    roi = TARGET_ROI.get(target, {"x": (0.0, 1.0), "y": (0.0, 1.0)})
    x0, y0 = int(roi["x"][0] * sw), int(roi["y"][0] * sh)
    x1, y1 = int(roi["x"][1] * sw), int(roi["y"][1] * sh)
    cv2.rectangle(vis, (x0, y0), (x1, y1), (0, 255, 0), 2)
    label = f"#{seq} {target} | OCR:{text} | {score:.3f} | ({x},{y})"
    cv2.rectangle(vis, (0, 0), (min(sw, 20 + 14 * len(label)), 48), (0, 0, 0), -1)
    cv2.putText(vis, label, (10, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
    safe_target = re.sub(r"[^\w\u4e00-\u9fff]+", "_", target)
    path = os.path.join(DEBUG_DIR, f"{ts}_{seq:04d}_{safe_target}_{x}_{y}.png")
    cv2.imwrite(path, vis)
    return path


def log_click(screen, target, text, score, x, y, candidates):
    """点击时打醒目日志 + 存调试图"""
    global click_seq
    click_seq += 1
    sh, sw = screen.shape[:2]
    px, py = pos_pct(x, y, sw, sh)
    roi = TARGET_ROI.get(target, {"x": (0.0, 1.0), "y": (0.0, 1.0)})
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    now = datetime.now().strftime("%H:%M:%S")

    print()
    print("=" * 60)
    print(f"  >>> 点击 #{click_seq}  [{now}]  <<<")
    print("=" * 60)
    print(f"  目标关键字 : 「{target}」")
    print(f"  OCR 原文   : 「{text}」")
    print(f"  归一化文本 : 「{normalize_text(text)}」")
    print(f"  置信度     : {score:.3f}  (阈值>={OCR_SCORE})")
    print(f"  点击坐标   : ({x}, {y})   屏幕={sw}x{sh}")
    print(f"  相对位置   : 水平 {px:.1f}%  垂直 {py:.1f}%  (左上=0%, 右下=100%)")
    print(f"  允许 ROI   : x={roi['x']}  y={roi['y']}")
    print(f"  候选数量   : {len(candidates)}")
    for i, (sc, cx, cy, t) in enumerate(candidates[:8], 1):
        mark = " <-- 实际点击" if i == 1 else ""
        cpx, cpy = pos_pct(cx, cy, sw, sh)
        print(
            f"    [{i}] 「{t}」 score={sc:.3f} "
            f"@({int(cx)},{int(cy)}) ~({cpx:.1f}%,{cpy:.1f}%){mark}"
        )
    path = save_click_debug(screen, x, y, target, text, score, click_seq, ts)
    if path:
        print(f"  调试截图   : {path}")
    print("=" * 60)
    print()


def run_ocr(screen):
    """返回 [(text, score, cx, cy), ...]，坐标为原图像素"""
    h, w = screen.shape[:2]
    scale = 1.0
    img = screen
    if w > OCR_MAX_WIDTH:
        scale = OCR_MAX_WIDTH / float(w)
        img = cv2.resize(screen, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

    result, _ = get_ocr()(img)
    items = []
    if not result:
        return items

    inv = 1.0 / scale
    for row in result:
        # row: [box, text, score]
        if not row or len(row) < 3:
            continue
        box, text, score = row[0], row[1], float(row[2])
        if score < OCR_SCORE:
            continue
        cx, cy = box_center(box)
        cx *= inv
        cy *= inv
        items.append((str(text), score, cx, cy))
    return items


def find_and_click(screen):
    """识别目标文字并点击。命中返回目标字符串，未命中返回 None"""
    sh, sw = screen.shape[:2]
    items = run_ocr(screen)

    if not items:
        print("· OCR 无结果")
        return None

    # 调试：打印本帧识别到的中文相关片段
    preview = [f"{t}({s:.2f})@({int(x)},{int(y)})" for t, s, x, y in items if re.search(r"[\u4e00-\u9fff]", t)]
    if preview:
        print("· 识别:", " | ".join(preview[:12]))

    for target in TARGETS:
        candidates = []
        for text, score, cx, cy in items:
            if not text_match(text, target):
                continue
            if not in_roi(cx, cy, sw, sh, target):
                px, py = pos_pct(cx, cy, sw, sh)
                print(
                    f"· 跳过非目标区域「{target}」: 「{text}」 "
                    f"@({int(cx)},{int(cy)}) ~({px:.1f}%,{py:.1f}%)"
                )
                continue
            candidates.append((score, cx, cy, text))

        if not candidates:
            continue

        if target in CENTER_TARGETS:
            # 中央目标：置信度优先，其次更靠屏幕正中
            mid_x, mid_y = sw * 0.5, sh * 0.5
            candidates.sort(
                key=lambda c: (-c[0], (c[1] - mid_x) ** 2 + (c[2] - mid_y) ** 2)
            )
        else:
            # 「领取成功」等：置信度优先；同分偏右上
            candidates.sort(key=lambda c: (-c[0], c[2], -c[1]))

        score, cx, cy, text = candidates[0]
        x, y = int(round(cx)), int(round(cy))
        log_click(screen, target, text, score, x, y, candidates)
        adb_command(["shell", "input", "tap", str(x), str(y)], timeout=30)
        return target

    print("· 未找到:", " / ".join(TARGETS))
    return None


# 点完这些目标后不进入冷却，立刻下一轮识别
NO_COOLDOWN_TARGETS = {"领取成功"}


def run_main_loop():
    print("🚀 汽水音乐自动看广告脚本启动（OCR 文字识别 · 无线调试）")
    print(f"   目标: {TARGETS}  置信度>={OCR_SCORE}  轮询={POLL_INTERVAL}s")
    print(f"   点击调试图目录: {DEBUG_DIR}")
    print(f"   无冷却目标: {sorted(NO_COOLDOWN_TARGETS)}")
    print(f"   当前设备: {CONNECTED_ADDR or '(默认设备)'}")

    miss_count = 0
    screen_fail = 0
    while True:
        try:
            screen = screenshot()
            if screen is None:
                screen_fail += 1
                print(f"⚠️ 截图失败（连续 {screen_fail} 次），检查 adb 连接")
                if LAST_ADB_ERR:
                    print(f"   adb 错误: {LAST_ADB_ERR}")
                if screen_fail == 1:
                    print("   排查：adb devices 是否有 device 状态的设备；无线调试是否仍开启；手机是否锁屏")
                if screen_fail >= 3:
                    try_reconnect()
                    screen_fail = 0
                time.sleep(3)
                continue
            screen_fail = 0

            sh_h = screen.shape[0]
            is_live = any(text in ["关注", "更多直播"] and cy < sh_h * 0.25 for text, score, cx, cy in run_ocr(screen))
            if is_live:
                print("⚡ 检测到顶部[关注/更多直播]，秒退直播间！")
                adb_command(["shell", "input", "keyevent", "4"], timeout=30)
                time.sleep(1.5)
                continue
            clicked = find_and_click(screen)
            if clicked:
                miss_count = 0
            if clicked:
                if clicked in NO_COOLDOWN_TARGETS:
                    print(f"⚡ 「{clicked}」已点，跳过等待，立即继续识别")
                    continue
                time.sleep(CLICK_COOLDOWN)
                continue

            if ENABLE_RANDOM_SWIPE and random.random() < SWIPE_PROB:
                h, w = screen.shape[:2]
                x = w // 2
                y1 = int(h * 0.55)
                y2 = int(h * 0.40)
                print(f"↕ 随机上滑 ({x},{y1}) -> ({x},{y2})")
                adb_command(["shell", "input", "swipe", str(x), str(y1), str(x), str(y2), "350"], timeout=30)

            time.sleep(POLL_INTERVAL)
            miss_count += 1
            if miss_count >= 5:
                print("⚠️ 连续未匹配，疑似进入直播间，执行返回退出")
                adb_command(["shell", "input", "keyevent", "4"], timeout=30)
                miss_count = 0
                time.sleep(2)

        except KeyboardInterrupt:
            print("\n已停止")
            break
        except Exception as e:
            print(f"⚠️ 错误: {e}")
            time.sleep(5)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="汽水音乐自动看广告脚本（ADB 无线调试 · 支持配对码 / 二维码配对）"
    )
    p.add_argument("--adb", help="adb 可执行文件路径（默认自动查找）")
    p.add_argument("--qr-make", action="store_true",
                   help="电脑端生成配对二维码，手机扫码配对（推荐方式）")
    p.add_argument("--pair-wait", type=int, default=PAIR_WAIT_TIMEOUT,
                   help=f"扫码后等待手机广播配对服务的最长秒数（默认 {PAIR_WAIT_TIMEOUT}）")
    p.add_argument("--no-open", action="store_true", help="生成二维码后不自动打开图片")
    p.add_argument("--pair-code", help="6 位无线调试配对码（配对端口自动发现）")
    p.add_argument("--pair-addr", help="配对地址，形如 192.168.1.5:37000（必须含 IP 和端口）")
    p.add_argument("--qr-text", help="二维码文本，形如 WIFI:T:ADB;S:adb-xxx;P:123456;;")
    p.add_argument("--qr-image", help="二维码图片路径，由 OpenCV 自动解码")
    p.add_argument("--connect-addr", help="直接连接 IP:端口，跳过配对")
    p.add_argument("--skip-pair", action="store_true", help="不配对，直接使用当前已连接设备")
    p.add_argument("--repair", action="store_true", help="即使已有在线设备也重新配对")
    p.add_argument("--diag", action="store_true", help="打印 adb / 设备 / mDNS 诊断信息后退出")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    resolve_adb(args.adb)

    if args.diag:
        run_diag()
        return 0

    if not prepare_connection(args):
        print()
        print("❌ 未能建立 ADB 连接，脚本退出。可用 python qishui_auto.py --diag 查看连接诊断。")
        return 1

    get_ocr()
    run_main_loop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已停止")
        sys.exit(0)
