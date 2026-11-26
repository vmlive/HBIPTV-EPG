import logging
import re
import time
from urllib.parse import urlparse

from .protocol import des_encrypt, extract_channel_from_response

logger = logging.getLogger(__name__)

AUTH_FIELDS = (
    'UserID',
    'mac',
    'STBID',
    'ip',
    'STBType',
    'STBVersion',
    'templateName',
    'areaId',
    'userGroupId',
    'productPackageId',
    'UserField',
    'SoftwareVersion',
    'IsSmartStb',
    'XMPPCapability',
    'ChipID',
    'conntype',
    'tempKey',
    'UserAgent',
    'password',
    'IPTV_AUTH_URL',
)


def get_token(session):
    """
    第一步：获取动态 host 和 EncryptToken（电信版）
    返回 (host, EncryptToken)
    """
    # 电信的 AuthenticationURL 没有 Mode 参数
    url = f"{session.auth_config['IPTV_AUTH_URL']}?UserID={session.auth_config['UserID']}&Action=Login"
    headers = {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'User-Agent': session.auth_config['UserAgent'],
        'X-Requested-With': 'com.android.smart.terminal.iptv',
    }
    try:
        resp = session.get(url, headers=headers, timeout=10, allow_redirects=True)
        resp.raise_for_status()
        # 从重定向后的 URL 中提取 host
        host = urlparse(resp.url).netloc

        # 接着请求 authLoginHWCTC.jsp 获取 EncryptToken
        auth_url = f"http://{host}/EPG/jsp/authLoginHWCTC.jsp"
        headers.update({
            'Referer': f"http://{host}/EPG/jsp/AuthenticationURL?UserID={session.auth_config['UserID']}&Action=Login",
            'Content-Type': 'application/x-www-form-urlencoded',
        })
        data = {'UserID': session.auth_config['UserID'], 'VIP': ''}
        resp2 = session.post(auth_url, headers=headers, data=data, timeout=10)
        resp2.raise_for_status()
        resp2.encoding = 'utf-8'
        txt = resp2.text

        # 电信的 EncryptToken 出现在 var EncryptToken = "..." 中
        r_enc = re.search(r'EncryptToken\s*=\s*"([A-F0-9]+)"', txt)
        if not r_enc:
            logger.error("无法从响应中提取 EncryptToken")
            return None, None
        EncryptToken = r_enc.group(1)

        # 同时提取 userToken（可能相同）
        r_user = re.search(r'userToken\.value\s*=\s*"([A-F0-9]+)"', txt)
        if r_user:
            userToken = r_user.group(1)
            return host, userToken  # 使用 userToken
        else:
            return host, EncryptToken
    except Exception:
        logger.error("获取 token 失败")
        return None, None


def authenticate(session):
    """
    第二步：使用 DES 加密生成 Authenticator，完成最终鉴权
    保存 user_token, stbid_short 到 session 对象
    """
    host, token = get_token(session)
    if not host or not token:
        return False

    # 构造加密原文（与移动相同格式，但使用电信的密码）
    rand = str(int(time.time() * 1000))[-8:]  # 取当前毫秒时间戳后8位
    session_ref = f"{rand}${token}${session.auth_config['UserID']}${session.auth_config['STBID']}${session.auth_config['ip']}${session.auth_config['mac']}$$CTC"
    Authenticator = des_encrypt(session_ref, session.auth_config['password'])

    url = f"http://{host}/EPG/jsp/ValidAuthenticationHWCTC.jsp"
    headers = {
        'User-Agent': session.auth_config['UserAgent'],
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': f"http://{host}/EPG/jsp/authLoginHWCTC.jsp",
    }
    # 构造请求数据，包含所有电信要求的字段
    data = {
        'UserID': session.auth_config['UserID'],
        'Lang': '1',
        'SupportHD': '1',
        'NetUserID': '',
        'Authenticator': Authenticator,
        'STBType': session.auth_config['STBType'],
        'STBVersion': session.auth_config['STBVersion'],
        'conntype': session.auth_config['conntype'],
        'STBID': session.auth_config['STBID'],
        'templateName': session.auth_config['templateName'],
        'areaId': session.auth_config['areaId'],
        'userToken': token,
        'userGroupId': session.auth_config['userGroupId'],
        'productPackageId': session.auth_config['productPackageId'],
        'mac': session.auth_config['mac'],
        'UserField': session.auth_config['UserField'],
        'SoftwareVersion': session.auth_config['SoftwareVersion'],
        'IsSmartStb': session.auth_config['IsSmartStb'],
        'desktopId': '',
        'stbmaker': '',
        'XMPPCapability': session.auth_config['XMPPCapability'],
        'ChipID': session.auth_config['ChipID'],
        'VIP': '',
    }
    try:
        resp = session.post(url, headers=headers, data=data, timeout=10)
        resp.raise_for_status()
        resp.encoding = 'utf-8'
        response_text = resp.text

        # 从响应中提取新的 UserToken 和 stbid_short
        re_token = re.search(r"Authentication\.CTCSetConfig\('UserToken','(.*?)'\)", response_text)
        re_stbid = re.search(r"document\.authform\.stbid\.value = \"(.*?)\"", response_text)

        # 如果未找到，尝试其他可能的格式
        if not re_token:
            re_token = re.search(r"Authentication\.CTCSetConfig\(\"UserToken\",\"(.*?)\"\)", response_text)
        if not re_stbid:
            re_stbid = re.search(r"stbid\" value=\"(.*?)\"", response_text)

        if re_token and re_stbid:
            session.user_token = re_token.group(1)
            session.stbid_short = re_stbid.group(1)
            session.host = host
            return True
        else:
            logger.error("鉴权响应中未找到 UserToken 或 stbid")
            return False
    except Exception:
        logger.error("鉴权请求异常")
        return False


def get_channel_list(session):
    """获取频道列表，返回排序后的频道。"""
    url = f"http://{session.host}/EPG/jsp/getchannellistHWCTC.jsp"
    data = {
        'conntype': session.auth_config['conntype'],
        'UserToken': session.user_token,
        'tempKey': session.auth_config['tempKey'],
        'stbid': session.stbid_short,
        'SupportHD': '1',
        'UserID': session.auth_config['UserID'],
        'Lang': '1'
    }
    try:
        resp = session.post(url, data=data, timeout=10)
        resp.raise_for_status()
        resp.encoding = 'utf-8'
        response_text = resp.text

        channels = extract_channel_from_response(response_text)
        channels.sort(key=lambda x: int(x['userchannel_id']) if x['userchannel_id'].isdigit() else 9999)
        for idx, ch in enumerate(channels, 1):
            ch['index'] = idx
        logger.info(f"成功获取 {len(channels)} 个频道")
        return channels
    except Exception:
        logger.error("获取频道列表失败")
        return []
