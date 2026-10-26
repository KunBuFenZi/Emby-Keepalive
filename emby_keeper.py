#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Emby 智能模拟播放保活脚本 (真流匀速防检测版)
- 速率精准匹配：根据视频真实码率动态计算播放时长，流量与时长 100% 对应
- 匀速平滑拉流：采用令牌桶/节拍流控算法，像真实播放器一样按播放秒数慢慢缓冲拉取，绝不瞬间突发
- 真实流量浮动：默认每次消耗 150MB ~ 250MB（可配置），且具有随机浮动
- 定时上报心跳：边平滑拉取视频流边每 10 秒上报播放进度 (TimeUpdate)
- 执行时间点浮动：计划任务唤醒后随机延迟（0~30分钟 Jitter）
- 周内随机日期调度：每日定时唤醒，周内随机某天打卡保活，周末自动保底
- 代理支持：原生支持 HTTP/HTTPS 代理，配套支持 SOCKS5 代理
"""

import os
import sys
import time
import json
import uuid
import random
import hashlib
import argparse
import datetime
import urllib.request
import urllib.error

# ============================ 默认用户配置 ============================
EMBY_HOST = os.getenv("EMBY_HOST", "https://lite.cn2gias.uk")
EMBY_USER = os.getenv("EMBY_USER", "")
EMBY_PASS = os.getenv("EMBY_PASS", "")

# 流量浮动范围（MB）：每次在此区间随机生成本次目标流量
TRAFFIC_MIN_MB = int(os.getenv("TRAFFIC_MIN_MB", "150"))
TRAFFIC_MAX_MB = int(os.getenv("TRAFFIC_MAX_MB", "250"))

# 模拟播放时的等效视频码率范围（Mbps，兆比特/秒）
# 真实高清视频（1080p / 4K）码率通常在 16 Mbps ~ 28 Mbps 之间（即 2.0 MB/s ~ 3.5 MB/s）
# 播放时长将根据：【目标流量 / 选定码率】自动精确计算，确保物理上完全吻合！
BITRATE_MIN_MBPS = float(os.getenv("BITRATE_MIN_MBPS", "16.0"))
BITRATE_MAX_MBPS = float(os.getenv("BITRATE_MAX_MBPS", "26.0"))

# 代理配置：支持 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080
PROXY_URL = os.getenv("PROXY_URL", "")

# 随机执行时间点延迟上限（秒）：计划任务触发后先随机等待 0 ~ N 秒再执行
RANDOM_DELAY_MAX = int(os.getenv("RANDOM_DELAY_MAX", "0"))

# 周内随机日期模式：每天定时唤醒脚本，周内随机挑选一天打卡
WEEKLY_RANDOM_MODE = os.getenv("WEEKLY_RANDOM_MODE", "false").lower() in ("true", "1", "yes")

# 客户端预设：默认 Hills（适用于仅允许指定播放器 UA 的服务器）
CLIENT_PRESET = os.getenv("CLIENT_PRESET", "hills").lower()
# 播放完毕后是否清理客户端界面的“继续观看/播放进度”历史（避免主页残留未看过的影片）
CLEAN_HISTORY = os.getenv("CLEAN_HISTORY", "true").lower() in ("true", "1", "yes")

STATE_FILE = os.getenv("STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".emby_keeper_state.json"))
# ======================================================================

# 基于用户名生成固定的 DeviceId，模拟同一台日常使用的真实客户端
FIXED_DEVICE_ID = "keeper_" + hashlib.md5(EMBY_USER.encode("utf-8")).hexdigest()[:12]

# 客户端标识预设表
CLIENT_PROFILES = {
    "emby-web": {
        "client": "Emby Web",
        "device": "Chrome on Windows",
        "version": "4.8.10.0",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    },
    "hills": {
        "client": "Hills",
        "device": "iPhone 15 Pro",
        "version": "1.3.1",
        "ua": "Hills/1.3.1 (iOS; iPhone15,2; Scale/3.00)"
    },
    "emby-theater": {
        "client": "Emby Theater",
        "device": "Windows PC",
        "version": "3.0.20",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) EmbyTheater/3.0.20 Chrome/120.0.0.0 Electron/28.1.0 Safari/537.36"
    }
}

active_profile = CLIENT_PROFILES.get(CLIENT_PRESET, CLIENT_PROFILES["hills"])
CLIENT_NAME = active_profile["client"]
DEVICE_NAME = active_profile["device"]
CLIENT_VERSION = active_profile["version"]
UA = active_profile["ua"]

_url_opener = None


def get_my_ip_info():
    """获取当前出口 IP 及地理位置，确保符合服务区要求"""
    try:
        req = urllib.request.Request("https://ipapi.co/json/", headers={"User-Agent": "curl/7.68.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
            ip = data.get("ip", "未知")
            country = data.get("country_name", "")
            code = data.get("country_code", "")
            city = data.get("city", "")
            print(f"[*] 当前网络出口: {ip} ({country} / {code} - {city})")
            return code
    except Exception:
        try:
            req = urllib.request.Request("https://api.ipify.org?format=json", headers={"User-Agent": "curl/7.68.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                print(f"[*] 当前网络出口 IP: {data.get('ip')}")
        except Exception:
            pass
    return None


def setup_proxy(proxy_url):
    """配置 HTTP/HTTPS 或 SOCKS5 代理"""
    global _url_opener
    proxy_url = proxy_url.strip() if proxy_url else ""
    
    if not proxy_url:
        proxy_url = os.getenv("ALL_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or ""
    
    if not proxy_url:
        _url_opener = urllib.request.build_opener()
        return

    print(f"[*] 正在配置网络代理: {proxy_url}")
    if proxy_url.lower().startswith("socks5://") or proxy_url.lower().startswith("socks5h://"):
        try:
            import socks
            import socket
            raw = proxy_url.split("://", 1)[1]
            auth, host_port = raw.split("@") if "@" in raw else (None, raw)
            host, port = host_port.split(":")
            username, password = auth.split(":") if auth else (None, None)
            
            socks.set_default_proxy(
                socks.SOCKS5,
                host,
                int(port),
                username=username,
                password=password
            )
            socket.socket = socks.socksocket
            _url_opener = urllib.request.build_opener()
            print("[+] SOCKS5 代理接管成功！")
        except ImportError:
            print("[!] 未安装 PySocks 库，无法直接使用原生 SOCKS5。")
            print("    建议执行: pip install PySocks")
            print("    或者直接使用客户端的 HTTP 端口（如 http://127.0.0.1:7890）")
            sys.exit(1)
        except Exception as e:
            print(f"[x] 配置 SOCKS5 代理失败: {e}")
            sys.exit(1)
    else:
        proxy_handler = urllib.request.ProxyHandler({
            "http": proxy_url,
            "https": proxy_url
        })
        _url_opener = urllib.request.build_opener(proxy_handler)
        print("[+] HTTP/HTTPS 代理配置成功！")


def make_request(url, method="GET", headers=None, data=None, stream=False, timeout=30):
    """发送 HTTP 请求"""
    global _url_opener
    if _url_opener is None:
        _url_opener = urllib.request.build_opener()

    if headers is None:
        headers = {}
    headers.setdefault("User-Agent", UA)
    headers.setdefault("Accept", "application/json, */*")
    
    encoded_data = None
    if data is not None:
        headers.setdefault("Content-Type", "application/json")
        encoded_data = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(url, data=encoded_data, headers=headers, method=method)
    try:
        resp = _url_opener.open(req, timeout=timeout)
        if stream:
            return resp.status, resp
        raw = resp.read()
        resp.close()
        if raw:
            try:
                return resp.status, json.loads(raw.decode("utf-8"))
            except Exception:
                return resp.status, raw.decode("utf-8", errors="ignore")
        return resp.status, {}
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore")
        return e.code, err_msg
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络连接异常: {e.reason}")


def get_auth_header(token=None):
    auth = (
        f'MediaBrowser Client="{CLIENT_NAME}", '
        f'Device="{DEVICE_NAME}", '
        f'DeviceId="{FIXED_DEVICE_ID}", '
        f'Version="{CLIENT_VERSION}"'
    )
    if token:
        auth += f', Token="{token}"'
    return auth


def check_weekly_schedule():
    """周内随机日期调度判定"""
    now = datetime.datetime.now()
    current_year_week = f"{now.year}-W{now.isocalendar()[1]}"
    
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = {}
            
    last_completed_week = state.get("last_completed_week")
    if last_completed_week == current_year_week:
        last_time = state.get("last_completed_time", "未知")
        print(f"[*] 本周 ({current_year_week}) 已于 {last_time} 成功执行过保活，今日跳过。")
        return False

    weekday = now.weekday()  # 0=周一, 6=周日
    if weekday >= 5:
        print(f"[*] 周末保底机制触发（星期{weekday+1}，本周尚未运行），立即执行！")
        return True
    
    hit = random.random() < 0.35
    if hit:
        print(f"[*] 周内随机调度命中（星期{weekday+1}），今日开始执行保活！")
        return True
    else:
        print(f"[*] 周内随机调度未命中（星期{weekday+1}），将在后续日期继续尝试。")
        return False


def mark_weekly_completed():
    now = datetime.datetime.now()
    current_year_week = f"{now.year}-W{now.isocalendar()[1]}"
    state = {
        "last_completed_week": current_year_week,
        "last_completed_time": now.strftime("%Y-%m-%d %H:%M:%S")
    }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[!] 写入状态文件失败: {e}")


def login(host, username, password):
    if not username or not password:
        raise RuntimeError("请配置 EMBY_USER 和 EMBY_PASS 环境变量（Actions 中配置同名 Secrets）")
    url = f"{host.rstrip('/')}/Users/AuthenticateByName"
    headers = {"X-Emby-Authorization": get_auth_header()}
    # Emby 使用 Pw；部分兼容服务/认证网关使用 Password，单次请求同时提交。
    # 不自动重试登录，避免触发服务端限流或账号锁定；不裁剪密码中的空白。
    payload = {"Username": username, "Pw": password, "Password": password}
    print(f"[*] 正在登录 Emby: {host} (用户: {username}) ...")
    status, res = make_request(url, method="POST", headers=headers, data=payload)
    if status == 401:
        raise RuntimeError(
            "登录失败 (HTTP 401)：服务器拒绝了凭据。请核对 Actions Secrets 的 "
            "EMBY_HOST/EMBY_USER/EMBY_PASS（Secret 优先于本地配置），"
            "检查是否误加引号、换行，以及服务器是否限制客户端或网络出口。"
        )
    if status == 429:
        raise RuntimeError("登录失败 (HTTP 429)：认证请求被限流，请暂停重试，等待服务器解除限制。")
    if status != 200 or not isinstance(res, dict):
        raise RuntimeError(f"登录失败 (HTTP {status})")

    token = res.get("AccessToken")
    user_id = res.get("User", {}).get("Id")
    if not token or not user_id:
        raise RuntimeError("登录响应不完整：缺少 AccessToken 或用户 ID")
    print(f"[+] 登录成功！用户: {username} (ID: {user_id})")
    return token, user_id


def report_capabilities(host, token):
    url = f"{host.rstrip('/')}/Sessions/Capabilities/Full"
    headers = {"X-Emby-Authorization": get_auth_header(token)}
    caps = {
        "PlayableMediaTypes": ["Audio", "Video"],
        "SupportedCommands": ["Play", "Playstate"],
        "SupportsMediaControl": True
    }
    make_request(url, method="POST", headers=headers, data=caps)


def fetch_random_media(host, user_id, token):
    url = (
        f"{host.rstrip('/')}/Users/{user_id}/Items"
        f"?IncludeItemTypes=Movie,Episode"
        f"&Recursive=true"
        f"&Limit=30"
        f"&SortBy=Random"
        f"&Fields=MediaSources"
    )
    headers = {"X-Emby-Authorization": get_auth_header(token)}
    print("[*] 正在检索媒体库随机影视...")
    status, res = make_request(url, method="GET", headers=headers)
    if status != 200 or not isinstance(res, dict):
        raise RuntimeError(f"检索媒体列表失败 (HTTP {status})")
    
    items = res.get("Items", [])
    if not items:
        raise RuntimeError("未在媒体库中找到可播放的影视资源")
    
    item = random.choice(items)
    item_id = item.get("Id")
    name = item.get("Name", "未知影片")
    print(f"[+] 选中目标影片: 《{name}》 (ItemId: {item_id})")
    return item_id, name


def get_playback_info(host, user_id, token, item_id):
    url = f"{host.rstrip('/')}/Items/{item_id}/PlaybackInfo?UserId={user_id}"
    headers = {"X-Emby-Authorization": get_auth_header(token)}
    payload = {
        "UserId": user_id,
        "StartTimeTicks": 0,
        "IsPlayback": True,
        "AutoOpenLiveStream": True
    }
    status, pb = make_request(url, method="POST", headers=headers, data=payload)
    if status != 200 or not isinstance(pb, dict):
        raise RuntimeError(f"获取 PlaybackInfo 失败 (HTTP {status})")
    
    play_session_id = pb.get("PlaySessionId")
    media_sources = pb.get("MediaSources", [])
    if not media_sources:
        raise RuntimeError("未解析到媒体源信息")
    
    ms = media_sources[0]
    media_source_id = ms.get("Id")
    direct_url = ms.get("DirectStreamUrl")
    return play_session_id, media_source_id, direct_url


def simulate_paced_playback(host, token, item_id, item_name, media_source_id, play_session_id, direct_url, target_mb, bitrate_mbps):
    """
    真流平滑匀速拉取引擎：
    - 严格根据 bitrate_mbps 限速，像真实播放器一样均匀消耗流量
    - 播放时长与消耗流量物理级严格对应
    - 绝无瞬时打满带宽的突发流量
    - 边拉流边每 10 秒向 Emby 上报一次播放心跳 (TimeUpdate)
    """
    headers = {"X-Emby-Authorization": get_auth_header(token)}
    
    # 1. 计算理论速率与理论播放时长
    target_bytes = int(target_mb * 1024 * 1024)
    # 速率：字节/秒 = (Mbps * 1,000,000) / 8
    target_bytes_per_sec = (bitrate_mbps * 1_000_000) / 8.0
    # 理论播放时长 (秒)
    expected_duration_sec = int(target_bytes / target_bytes_per_sec)
    
    print(f"\n[+] 播放参数计算严密对应:")
    print(f"    - 目标总消耗流量: {target_mb:.2f} MB")
    print(f"    - 模拟视频实际码率: {bitrate_mbps:.2f} Mbps (约 {target_bytes_per_sec / 1024 / 1024:.2f} MB/s)")
    print(f"    - 对应真实播放时长: {expected_duration_sec} 秒 (~{expected_duration_sec / 60:.1f} 分钟)")
    print(f"    - 拉流策略: 匀速平滑限速拉取 (Paced Streaming)\n")

    # 2. 上报播放开始
    start_url = f"{host.rstrip('/')}/Sessions/Playing"
    start_payload = {
        "ItemId": item_id,
        "MediaSourceId": media_source_id,
        "PlaySessionId": play_session_id,
        "PlayMethod": "DirectPlay",
        "PositionTicks": 0,
        "CanSeek": True,
        "IsPaused": False
    }
    print("[*] 上报播放开始 (Start Playing)...")
    make_request(start_url, method="POST", headers=headers, data=start_payload)

    # 3. 建立视频流连接
    stream_resp = None
    if direct_url:
        full_stream_url = f"{host.rstrip('/')}{direct_url}"
        st_headers = {
            "User-Agent": UA,
            "X-Emby-Authorization": get_auth_header(token)
        }
        st_code, resp_obj = make_request(full_stream_url, method="GET", headers=st_headers, stream=True, timeout=15)
        if st_code == 200:
            stream_resp = resp_obj
            print("[*] 视频流握手成功，开始边播放边匀速缓冲...")
        else:
            print(f"[!] 媒体流握手状态 HTTP {st_code}，将转为纯心跳定时推进")

    # 4. 匀速拉流与心跳推进主循环
    total_downloaded = 0
    start_time = time.time()
    last_report_time = start_time
    report_interval = 10
    chunk_size = 64 * 1024  # 64KB 小分块拉取，保证流量平滑

    try:
        while True:
            now = time.time()
            elapsed_sec = int(now - start_time)
            
            # 读取数据块
            if stream_resp and total_downloaded < target_bytes:
                chunk = stream_resp.read(chunk_size)
                if chunk:
                    total_downloaded += len(chunk)
                else:
                    stream_resp = None  # 流读取完毕

            # 心跳检查：每 10 秒上报一次
            if now - last_report_time >= report_interval:
                current_ticks = elapsed_sec * 10_000_000
                prog_payload = {
                    "ItemId": item_id,
                    "MediaSourceId": media_source_id,
                    "PlaySessionId": play_session_id,
                    "PlayMethod": "DirectPlay",
                    "PositionTicks": current_ticks,
                    "EventName": "TimeUpdate",
                    "IsPaused": False
                }
                make_request(f"{host.rstrip('/')}/Sessions/Playing/Progress", method="POST", headers=headers, data=prog_payload)
                dl_mb = total_downloaded / (1024 * 1024)
                print(f"    - 播放进行中: {elapsed_sec}/{expected_duration_sec}s | 已匀速拉取: {dl_mb:.1f}/{target_mb:.1f} MB (实时码率: {bitrate_mbps:.1f} Mbps)")
                last_report_time = now

            # 达到播放时长且拉流达标
            if elapsed_sec >= expected_duration_sec and (total_downloaded >= target_bytes or stream_resp is None):
                break

            # 令牌桶流控：严格按照时间节拍限速，防止突发过快
            if stream_resp and total_downloaded < target_bytes:
                # 这一刻理论上允许拉取的字节上限
                allowed_bytes = (time.time() - start_time) * target_bytes_per_sec
                if total_downloaded > allowed_bytes:
                    # 超前了，微睡眠等待时间推进
                    sleep_time = min(0.3, (total_downloaded - allowed_bytes) / target_bytes_per_sec)
                    if sleep_time > 0.005:
                        time.sleep(sleep_time)
            else:
                # 若无流，则按秒正常流逝
                time.sleep(1)

    finally:
        if stream_resp:
            try:
                stream_resp.close()
            except:
                pass

    # 5. 上报播放结束
    final_elapsed = int(time.time() - start_time)
    stop_url = f"{host.rstrip('/')}/Sessions/Playing/Stopped"
    stop_payload = {
        "ItemId": item_id,
        "MediaSourceId": media_source_id,
        "PlaySessionId": play_session_id,
        "PositionTicks": final_elapsed * 10_000_000
    }
    print("[*] 上报播放结束 (Stop Playing)...")
    make_request(stop_url, method="POST", headers=headers, data=stop_payload)
    
    final_mb = total_downloaded / (1024 * 1024)
    print("\n" + "=" * 60)
    print(f"[✔] 模拟播放完成！")
    print(f"    - 播放影视: 《{item_name}》")
    print(f"    - 实际播放时长: {final_elapsed} 秒 ({final_elapsed / 60:.1f} 分钟)")
    print(f"    - 实际消耗流量: {final_mb:.2f} MB")
    if final_elapsed > 0:
        actual_mbps = (final_mb * 8 * 1024 * 1024) / (final_elapsed * 1_000_000)
        print(f"    - 实际平均码率: {actual_mbps:.2f} Mbps (与高清视频完全吻合)")
    print("=" * 60 + "\n")


def cleanup_user_playback_history(host, user_id, token, item_id, item_name):
    """
    清理前端界面的播放历史/继续观看记录：
    1. 调用 DELETE /Users/{UserId}/PlayedItems/{ItemId} 标记为未播放并清空进度
    2. 重置 UserData 的 PlaybackPositionTicks 为 0，确保彻底从“继续观看”移出
    注意：服务器后台 Playback Reporting（插件日志）中的播放日志早已落盘，不受此接口影响！
    """
    headers = {"X-Emby-Authorization": get_auth_header(token)}
    print(f"[*] 正在清理前端播放历史，避免《{item_name}》出现在继续观看列表中...")
    
    # 方式 1: DELETE /Users/{UserId}/PlayedItems/{ItemId}
    del_url = f"{host.rstrip('/')}/Users/{user_id}/PlayedItems/{item_id}"
    code, _ = make_request(del_url, method="DELETE", headers=headers)
    
    # 方式 2: 重置 UserData PlaybackPositionTicks 为 0
    ud_url = f"{host.rstrip('/')}/Users/{user_id}/Items/{item_id}/UserData"
    ud_payload = {
        "PlaybackPositionTicks": 0,
        "Played": False
    }
    code_ud, _ = make_request(ud_url, method="POST", headers=headers, data=ud_payload)
    
    if code in (200, 204) or code_ud in (200, 204):
        print(f"[✔] 前端历史清理完成！《{item_name}》已从继续观看列表中彻底抹除。")
    else:
        print(f"[!] 清理响应状态: DELETE={code}, POST UserData={code_ud}")


def main():
    parser = argparse.ArgumentParser(description="Emby 智能模拟播放保活脚本")
    parser.add_argument("--force", action="store_true", help="强制立即执行，跳过周内调度与随机延迟")
    parser.add_argument("--test", action="store_true", help="快速测试模式（跑 5MB 流量、10秒时长）")
    parser.add_argument("--proxy", type=str, default=PROXY_URL, help="代理地址 (http://... 或 socks5://...)")
    parser.add_argument("--traffic", type=str, default=None, help="自定义流量区间 MB，如 150-250")
    parser.add_argument("--client", type=str, default=CLIENT_PRESET, choices=["emby-web", "hills", "emby-theater"], help="模拟的客户端标识预设")
    args = parser.parse_args()

    global CLIENT_NAME, DEVICE_NAME, CLIENT_VERSION, UA
    if args.client in CLIENT_PROFILES:
        prof = CLIENT_PROFILES[args.client]
        CLIENT_NAME = prof["client"]
        DEVICE_NAME = prof["device"]
        CLIENT_VERSION = prof["version"]
        UA = prof["ua"]

    print("=" * 60)
    print("    Emby 匀速拉流与智能防检测保活系统")
    print("=" * 60)
    print(f"[*] 模拟客户端标识: {CLIENT_NAME} ({DEVICE_NAME}) v{CLIENT_VERSION}")

    # 1. 代理初始化
    setup_proxy(args.proxy)

    # 2. 检查当前出口 IP 归属地
    get_my_ip_info()

    # 3. 周内调度判定
    if WEEKLY_RANDOM_MODE and not args.force and not args.test:
        if not check_weekly_schedule():
            return

    # 3. 时间点随机浮动 (Jitter 延迟)
    if RANDOM_DELAY_MAX > 0 and not args.force and not args.test:
        delay_sec = random.randint(10, RANDOM_DELAY_MAX)
        m, s = divmod(delay_sec, 60)
        print(f"[*] 触发时间随机浮动: 将随机等待 {m} 分 {s} 秒后再启动...")
        time.sleep(delay_sec)

    # 4. 计算本次的随机目标与码率
    if args.test:
        target_mb = 5.0
        bitrate_mbps = 4.0  # 4Mbps => 约 10 秒消耗 5MB
    else:
        t_min = TRAFFIC_MIN_MB
        t_max = TRAFFIC_MAX_MB
        if args.traffic and "-" in args.traffic:
            p1, p2 = args.traffic.split("-")
            t_min, t_max = float(p1), float(p2)
        target_mb = round(random.uniform(t_min, t_max), 2)
        bitrate_mbps = round(random.uniform(BITRATE_MIN_MBPS, BITRATE_MAX_MBPS), 2)

    try:
        token, user_id = login(EMBY_HOST, EMBY_USER, EMBY_PASS)
        report_capabilities(EMBY_HOST, token)
        item_id, item_name = fetch_random_media(EMBY_HOST, user_id, token)
        play_session_id, media_source_id, direct_url = get_playback_info(EMBY_HOST, user_id, token, item_id)
        
        simulate_paced_playback(
            EMBY_HOST, token, item_id, item_name,
            media_source_id, play_session_id, direct_url,
            target_mb, bitrate_mbps
        )
        
        # 播放完成后清理前端播放历史（保持主页无痕）
        if CLEAN_HISTORY:
            cleanup_user_playback_history(EMBY_HOST, user_id, token, item_id, item_name)

        # 打卡记录
        mark_weekly_completed()
        print("[✔] 任务全部圆满完成！已更新周状态记录。")

    except Exception as e:
        print(f"\n[x] 运行中断: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
