import os
from pathlib import Path

# 文件路径
TOKENS_FILE = Path("./kimik3_tokens.json")

# 上游地址
KIMIK3_WEB = "https://www.kimik3.net"
KIMIK3_API = "https://www.kimik3.net/api"

# 号池严格固定为 3 个账号
ACTIVE_POOL_SIZE = 3

# 默认 HTTP 请求头 (防拦截)
COMMON_HEADERS: dict[str, str] = {
    "accept": "*/*",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
    "origin": "https://www.kimik3.net",
    "referer": "https://www.kimik3.net/settings",
    "priority": "u=1, i",
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Microsoft Edge";v="150"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0"
}

HTTP_PROXY = os.getenv("HTTP_PROXY", "")
HTTPS_PROXY = os.getenv("HTTPS_PROXY", "")

if HTTP_PROXY: os.environ["http_proxy"] = HTTP_PROXY
if HTTPS_PROXY: os.environ["https_proxy"] = HTTPS_PROXY