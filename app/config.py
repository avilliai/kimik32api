import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# 运行端口
PORT = int(os.getenv("PORT", "8077"))

# 文件路径
TOKENS_FILE = BASE_DIR / "kimik3_tokens.json"

# Outlook 账号文件配置：优先从环境变量读取，其次自动寻找根目录 outlook_live_*.txt
OUTLOOK_FILE_ENV = os.getenv("OUTLOOK_FILE", "").strip()
if OUTLOOK_FILE_ENV:
    OUTLOOK_FILE = Path(OUTLOOK_FILE_ENV) if Path(OUTLOOK_FILE_ENV).is_absolute() else (BASE_DIR / OUTLOOK_FILE_ENV)
else:
    candidates = sorted(list(BASE_DIR.glob("outlook_live_*.txt")), reverse=True)
    OUTLOOK_FILE = candidates[0] if candidates else (BASE_DIR / "outlook_live_20261018_222014.txt")

OUTLOOK_POINTER_FILE = BASE_DIR / "outlook_pointer.json"

# 上游地址
KIMIK3_WEB = "https://www.kimik3.net"
KIMIK3_API = "https://www.kimik3.net/api"

# 代理配置
PROXY = os.getenv("PROXY", os.getenv("HTTP_PROXY", ""))
if PROXY:
    os.environ["http_proxy"] = PROXY
    os.environ["https_proxy"] = PROXY

# 邮箱认证与注册配置
# REGISTER_EMAIL_TYPE: auto (优先outlook，失败降级域名邮箱), outlook (只用本地outlook), domain (只用cloudflare worker域名邮箱)
REGISTER_EMAIL_TYPE = os.getenv("REGISTER_EMAIL_TYPE", "auto").lower()

# 方式一：Outlook 文件账号池配置
OUTLOOK_TOKEN_URL = os.getenv("OUTLOOK_TOKEN_URL", "https://login.microsoftonline.com/common/oauth2/v2.0/token")
OUTLOOK_IMAP_HOSTS = os.getenv("OUTLOOK_IMAP_HOSTS", "outlook.office365.com;imap-mail.outlook.com")
OUTLOOK_IMAP_PORT = int(os.getenv("OUTLOOK_IMAP_PORT", "993"))

# 方式二：Cloudflare Worker 域名临时邮箱配置
WORKER_DOMAIN = os.getenv("WORKER_DOMAIN", "")
EMAIL_DOMAIN = os.getenv("EMAIL_DOMAIN", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# Turnstile 验证配置（如需验证码代答；kimik3当前注册未强制要求）
YESCAPTCHA_KEY = os.getenv("YESCAPTCHA_KEY", "")
LOCAL_TURNSTILE_URL = os.getenv("LOCAL_TURNSTILE_URL", "http://127.0.0.1:5072")

# 激活号池大小
ACTIVE_POOL_SIZE = int(os.getenv("ACTIVE_POOL_SIZE", "3"))

# 模型配置
SUPPORTED_MODELS = [
    "deepseek-v4-flash",
    "glm-5.3-flash",
    "kimi-k3",
    "gpt-3.5-turbo",
    "gpt-4",
    "deepseek-chat",
    "deepseek-v4",
]

DEFAULT_MODEL = "deepseek-v4-flash"

MODEL_MAPPING = {
    "deepseek-v4-flash": "deepseek-v4-flash",
    "deepseek-chat": "deepseek-v4-flash",
    "deepseek-v4": "deepseek-v4-flash",
    "deepseek": "deepseek-v4-flash",
    "glm-5.3-flash": "glm-5.3-flash",
    "glm-5.3": "glm-5.3-flash",
    "glm-4-flash": "glm-5.3-flash",
    "glm": "glm-5.3-flash",
    "kimi-k3": "kimi-k3",
    "kimi": "kimi-k3",
    "gpt-3.5-turbo": "deepseek-v4-flash",
    "gpt-4": "deepseek-v4-flash",
    "gpt-4o": "deepseek-v4-flash",
}

# 默认 HTTP 请求头
COMMON_HEADERS: dict[str, str] = {
    "accept": "*/*",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
    "origin": "https://www.kimik3.net",
    "referer": "https://www.kimik3.net/",
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
