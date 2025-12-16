"""
外部访问日志中间件，记录外部请求来源信息并检查IP黑名单
"""
import json
from datetime import datetime
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.log.logger import get_middleware_logger

logger = get_middleware_logger()

# 需要记录访问日志的路径前缀
LOGGED_PATH_PREFIXES = [
    "/v1/",
    "/gemini/",
    "/v1beta/",
    "/openai/",
    "/hf/",
    "/vertex-express/",
]

# 内存缓存IP黑名单，避免每次请求都查询数据库
_ip_blacklist_cache: set = set()
_cache_last_updated: Optional[datetime] = None
CACHE_TTL_SECONDS = 60  # 缓存有效期60秒


def get_client_ip(request: Request) -> str:
    """
    获取客户端真实IP地址
    优先从 X-Forwarded-For 或 X-Real-IP 头获取
    """
    # 尝试从 X-Forwarded-For 获取
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # X-Forwarded-For 可能包含多个IP，取第一个
        return forwarded_for.split(",")[0].strip()
    
    # 尝试从 X-Real-IP 获取
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    
    # 从连接信息获取
    if request.client:
        return request.client.host
    
    return "unknown"


def extract_token_from_request(request: Request) -> Optional[str]:
    """
    从请求中提取使用的令牌
    """
    # 从 Authorization header 获取
    auth_header = request.headers.get("Authorization")
    if auth_header:
        if auth_header.startswith("Bearer "):
            return auth_header[7:][:20] + "..."  # 只显示前20字符
        return auth_header[:20] + "..."
    
    # 从查询参数获取
    api_key = request.query_params.get("key")
    if api_key:
        return api_key[:20] + "..."
    
    return None


def should_log_request(path: str) -> bool:
    """
    判断是否需要记录该请求
    """
    for prefix in LOGGED_PATH_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


async def refresh_blacklist_cache():
    """
    刷新IP黑名单缓存
    """
    global _ip_blacklist_cache, _cache_last_updated
    
    try:
        from app.database.services import get_all_blacklisted_ips
        ips = await get_all_blacklisted_ips()
        _ip_blacklist_cache = set(ips)
        _cache_last_updated = datetime.now()
        logger.debug(f"IP blacklist cache refreshed, {len(_ip_blacklist_cache)} IPs in blacklist")
    except Exception as e:
        logger.error(f"Failed to refresh IP blacklist cache: {e}")


async def is_ip_blacklisted(ip: str) -> bool:
    """
    检查IP是否在黑名单中
    """
    global _cache_last_updated
    
    # 检查缓存是否需要刷新
    if _cache_last_updated is None or \
       (datetime.now() - _cache_last_updated).total_seconds() > CACHE_TTL_SECONDS:
        await refresh_blacklist_cache()
    
    return ip in _ip_blacklist_cache


def invalidate_blacklist_cache():
    """
    使黑名单缓存失效，下次请求时会重新加载
    """
    global _cache_last_updated
    _cache_last_updated = None


class AccessLogMiddleware(BaseHTTPMiddleware):
    """
    外部访问日志中间件
    """
    
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        
        # 只处理需要记录的路径
        if not should_log_request(path):
            return await call_next(request)
        
        # 获取客户端IP
        client_ip = get_client_ip(request)
        
        # 检查IP黑名单
        if await is_ip_blacklisted(client_ip):
            logger.warning(f"Blocked request from blacklisted IP: {client_ip}")
            return JSONResponse(
                status_code=403,
                content={"error": "Access denied", "message": "Your IP has been blocked"}
            )
        
        # 提取请求信息
        token_used = extract_token_from_request(request)
        request_method = request.method
        
        # 尝试获取请求体预览和模型名称
        request_preview = None
        model_name = None
        
        try:
            if request_method == "POST":
                body = await request.body()
                if body:
                    body_str = body.decode("utf-8", errors="ignore")
                    # 截取前200字符
                    request_preview = body_str[:200] if len(body_str) > 200 else body_str
                    
                    # 尝试解析JSON获取模型名称
                    try:
                        body_json = json.loads(body_str)
                        model_name = body_json.get("model")
                    except json.JSONDecodeError:
                        pass
                
                # 重置请求体以便后续处理
                async def receive():
                    return {"type": "http.request", "body": body, "more_body": False}
                request._receive = receive
        except Exception as e:
            logger.debug(f"Failed to read request body: {e}")
        
        # 如果没有从body获取到模型名称，尝试从路径获取
        if not model_name:
            import re
            match = re.search(r"/models/([^/:]+)", path)
            if match:
                model_name = match.group(1)
        
        # 执行请求
        request_time = datetime.now()
        response = await call_next(request)
        status_code = response.status_code
        
        # 异步记录访问日志
        try:
            from app.database.services import add_access_log
            await add_access_log(
                client_ip=client_ip,
                token_used=token_used,
                model_name=model_name,
                request_preview=request_preview,
                status_code=status_code,
                request_path=path,
                request_method=request_method,
                request_time=request_time,
            )
        except Exception as e:
            logger.error(f"Failed to log access: {e}")
        
        return response
