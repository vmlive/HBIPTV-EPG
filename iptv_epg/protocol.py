import datetime
import logging
import os
import re
import socket
import time

import requests
from Crypto.Cipher import DES
from Crypto.Util.Padding import pad
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)
REAUTH_FREQUENCY = 60


class InterfaceAdapter(HTTPAdapter):
    def __init__(self, interface, fwmark):
        self.socket_options = list(HTTPConnection.default_socket_options)
        if interface is not None:
            self.socket_options.append(
                (socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b'\0')
            )
        if fwmark is not None:
            self.socket_options.append((socket.SOL_SOCKET, socket.SO_MARK, fwmark))
        super().__init__(max_retries=Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "POST"],
        ))

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, socket_options=self.socket_options, **kwargs)


def create_session(interface, auth_config, *, fwmark=None):
    if interface is not None:
        socket.if_nametoindex(interface)
    session = requests.Session()
    session.trust_env = False
    session.auth_config = auth_config
    session.user_token = None
    session.stbid_short = None
    session.host = None
    for scheme in ('http://', 'https://'):
        session.mount(scheme, InterfaceAdapter(interface, fwmark))
    return session


def des_encrypt(text, key=None):
    """DES ECB 加密，返回十六进制字符串"""
    if key is None:
        raise ValueError("Key cannot be None for DES encryption")
    if isinstance(key, str):
        key = key.encode('utf-8')
    if len(key) != 8:
        key = key.ljust(8, b'\0')[:8]
    cryptor = DES.new(key, DES.MODE_ECB)
    if isinstance(text, str):
        text = text.encode('utf-8')
    padded_text = pad(text, 8)
    ciphertext = cryptor.encrypt(padded_text)
    return ciphertext.hex()


def need_reauth(index, freq=REAUTH_FREQUENCY):
    return (index - 1) % freq == 0


def get_group_title(channel_name):
    """根据频道名称返回分组名称"""
    name = channel_name.lower()
    if ('cctv' in name or 'cgtn' in name or '央视' in channel_name):
        return '央视频道'
    if '卫视' in channel_name:
        return '卫视频道'
    if ('cetv' in name or '教育' in channel_name):
        return '教育频道'
    if '少儿' in channel_name or '卡通' in channel_name:
        return '少儿频道'
    if '电影' in channel_name or '剧场' in channel_name or '影院' in channel_name:
        return '影视频道'
    if '购' in channel_name or '精选' in channel_name or '特产' in channel_name:
        return '购物频道'
    if '直播' in channel_name:
        return '直播频道'
    if '河北' in channel_name and '卫视' not in channel_name:
        return '河北省台'
    if '石家庄' in channel_name:
        return '本地频道'
    if '电视' in channel_name:
        return '县区频道'
    return '其他频道'


def extract_channel_from_response(text, *, unicom=False):
    """
    从 getchannellist JSP 的响应中提取频道信息
    匹配整个 Channel 配置块，提取所有字段
    返回字典列表，每个字典包含：
        channel_id, channel_name, userchannel_id, igmp_url, timeshift_url_full,
        fcc_ip, fcc_port, fec_port (可选)
    """
    channels = []
    pattern = r"Authentication\.CTCSetConfig\('Channel','(.*?)'\);"
    flags = re.DOTALL
    if unicom:
        pattern = pattern.replace('CTCSetConfig', 'CUSetConfig')
        flags |= re.IGNORECASE
    matches = re.findall(pattern, text, flags)

    for match in matches:
        try:
            channel_id = re.search(r'ChannelID="(\d+)"', match)
            channel_name = re.search(r'ChannelName="(.*?)"', match)
            userchannel_id = re.search(r'UserChannelID="(\d+)"', match)
            channel_url = re.search(r'ChannelURL="igmp://([^"]+)"', match)
            channel_sdp = re.search(r'ChannelSDP="igmp://([^"]+)"', match)
            timeshift_url = re.search(r'TimeShiftURL="([^"]+)"', match)

            fcc_ip = re.search(r'ChannelFCCIP="(.*?)"', match)
            fcc_port = re.search(r'ChannelFCCPort="(.*?)"', match)
            fec_port = re.search(r'ChannelFECPort="(.*?)"', match)

            if not (channel_id and channel_name and userchannel_id):
                continue

            channel_id = channel_id.group(1)
            channel_name = channel_name.group(1)
            userchannel_id = userchannel_id.group(1)

            igmp_url = None
            if channel_url:
                igmp_url = channel_url.group(1)
            elif channel_sdp:
                igmp_url = channel_sdp.group(1)

            timeshift_url_full = timeshift_url.group(1) if timeshift_url else None

            if igmp_url:
                group = get_group_title(channel_name)
                channel_info = {
                    'channel_id': channel_id,
                    'channel_name': channel_name,
                    'userchannel_id': userchannel_id,
                    'igmp_url': igmp_url,
                    'timeshift_url_full': timeshift_url_full,
                    'group_title': group,
                }
                if fcc_ip and fcc_ip.group(1) and fcc_port and fcc_port.group(1):
                    channel_info['fcc_ip'] = fcc_ip.group(1)
                    channel_info['fcc_port'] = fcc_port.group(1)
                    if fec_port and fec_port.group(1) and fec_port.group(1) != '0':
                        channel_info['fec_port'] = fec_port.group(1)

                channels.append(channel_info)

        except Exception:
            logger.debug("解析单个频道失败")
            continue

    return channels


def query_logo_url(session, userchannel_id, today_zero_ms):
    """查询单个频道的 logo URL（使用 channelNO 作为参数）"""
    url = f"http://{session.host}/VSP/V3/QueryPlaybillContext"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest"
    }
    data = {
        "queryChannel": {
            "count": "1",
            "offset": "0",
            "channelNOs": [userchannel_id]
        },
        "queryPlaybillContext": {
            "date": today_zero_ms,
            "type": 1,
            "preNumber": 1,
            "nextNumber": 1
        }
    }
    try:
        resp = session.post(url, headers=headers, json=data, timeout=10)
        resp.raise_for_status()
        match = re.search(r'"channelPics":\["(https?://[^"]+)"', resp.text)
        if match:
            return match.group(1)
        else:
            logger.debug(f"频道 {userchannel_id} 响应中未找到 logo URL")
            return None
    except Exception:
        logger.error(f"查询频道 {userchannel_id} logo 失败")
        return None


def download_logos(session, channel_list, logo_dir, authenticate):
    """下载所有频道的 Logo"""
    if not os.path.exists(logo_dir):
        os.makedirs(logo_dir)
        logger.info(f"创建 logo 目录: {logo_dir}")

    today = datetime.datetime.today().replace(hour=0, minute=0, second=0, microsecond=0)
    today_zero_ms = int(today.timestamp() * 1000)

    for idx, ch in enumerate(channel_list, start=1):
        uc_id = ch['userchannel_id']
        if need_reauth(idx):
            logger.info("重新鉴权...")
            if not authenticate(session):
                logger.error("重新鉴权失败，终止 logo 下载")
                raise RuntimeError("下载 Logo 时重新鉴权失败")

        logo_url = query_logo_url(session, uc_id, today_zero_ms)
        if logo_url:
            logo_path = os.path.join(logo_dir, f"{uc_id}.png")
            try:
                img_resp = session.get(logo_url, timeout=10)
                if img_resp.status_code == 200:
                    with open(logo_path, 'wb') as f:
                        f.write(img_resp.content)
                    logger.info(f"下载 {uc_id}.png 成功")
                else:
                    logger.warning(f"下载 {uc_id}.png 失败，HTTP {img_resp.status_code}")
            except Exception:
                logger.error(f"下载 {uc_id}.png 异常")
        else:
            logger.debug(f"频道 {uc_id} 无 logo")


def format_day_playdata(playlist_text):
    """解析一天节目单响应，返回节目列表 [(start, name, end), ...]"""
    day_programs = []
    try:
        pattern = r'startTime":"(\d+)".*?name":"(.*?)".*?endTime":"(\d+)"'
        matches = re.findall(pattern, playlist_text, re.DOTALL)
        for start, name, end in matches:
            try:
                start_sec = int(start) // 1000
                end_sec = int(end) // 1000
                start_str = time.strftime("%Y%m%d%H%M%S", time.localtime(start_sec)) + " +0800"
                end_str = time.strftime("%Y%m%d%H%M%S", time.localtime(end_sec)) + " +0800"
                name = name.replace('<', '《').replace('>', '》')
                day_programs.append((start_str, name, end_str))
            except Exception:
                logger.debug("解析单个节目条目异常")
    except Exception:
        logger.error("format_day_playdata 解析失败")
    return day_programs


def get_all_epg(session, channel_list, authenticate, *, reauth_on_503):
    """获取所有频道 8 天的节目单数据"""
    today = datetime.datetime.today().replace(hour=0, minute=0, second=0, microsecond=0)
    today_zero_ms = int(today.timestamp() * 1000)
    limit_time = today_zero_ms - 518400000  # 6天前零点

    playlist_data = []

    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest"
    }

    total = len(channel_list)
    for idx, ch in enumerate(channel_list, start=1):
        if need_reauth(idx):
            logger.info("重新鉴权...")
            if not authenticate(session):
                logger.error("重新鉴权失败，终止 EPG 获取")
                return []

        channel_id = ch['channel_id']
        userchannel_id = ch['userchannel_id']
        channel_name = ch['channel_name']

        days_programs = []
        start_time = limit_time
        for day in range(8):
            data = {
                "queryChannel": {
                    "channelIDs": [channel_id]
                },
                "queryPlaybill": {
                    "type": "0",
                    "startTime": start_time,
                    "endTime": start_time + 86400000,
                    "count": "100",
                    "offset": "0",
                    "isFillProgram": "0",
                    "mustIncluded": "0"
                },
                "needChannel": "0"
            }

            max_retries = 3
            retry_delay = 1
            success = False
            for attempt in range(max_retries):
                try:
                    resp = session.post(f"http://{session.host}/VSP/V3/QueryPlaybillList",
                                        headers=headers, json=data, timeout=15)
                    if resp.status_code == 200:
                        day_progs = format_day_playdata(resp.text)
                        days_programs.append(day_progs)
                        success = True
                        break
                    elif resp.status_code == 503:
                        logger.warning(f"频道 {channel_name} 第{day+1}天 503 错误，"
                                       f"尝试重试 ({attempt+1}/{max_retries})")
                        if attempt < max_retries - 1:
                            time.sleep(retry_delay)
                            retry_delay *= 2
                            if reauth_on_503 and attempt == 1:
                                authenticate(session)
                        else:
                            logger.error(f"频道 {channel_name} 第{day+1}天 503 失败超过最大次数")
                    else:
                        resp.raise_for_status()
                except requests.exceptions.RequestException:
                    logger.error(f"获取频道 {channel_name} 第{day+1}天节目单异常")
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        retry_delay *= 2
                    else:
                        logger.error(f"频道 {channel_name} 第{day+1}天最终失败")

            if not success:
                days_programs.append([])

            start_time += 86400000
            time.sleep(0.1)

        playlist_data.append((channel_id, userchannel_id, channel_name, days_programs))
        logger.info(f"已处理 {idx}/{total} 频道: {channel_name}")

    return playlist_data
