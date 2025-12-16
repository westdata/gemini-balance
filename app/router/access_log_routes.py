"""
访问日志和IP黑名单路由模块
"""

from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

from app.core.security import verify_auth_token
from app.database import services as db_services
from app.log.logger import get_log_routes_logger
from app.middleware.access_log_middleware import invalidate_blacklist_cache

router = APIRouter(prefix="/api/access", tags=["access"])

logger = get_log_routes_logger()


# ==================== 访问日志相关 ====================

class AccessLogItem(BaseModel):
    id: int
    request_time: Optional[datetime] = None
    client_ip: Optional[str] = None
    token_used: Optional[str] = None
    model_name: Optional[str] = None
    request_preview: Optional[str] = None
    status_code: Optional[int] = None
    request_path: Optional[str] = None
    request_method: Optional[str] = None


class AccessLogListResponse(BaseModel):
    logs: List[AccessLogItem]
    total: int


@router.get("/logs", response_model=AccessLogListResponse)
async def get_access_logs_api(
    request: Request,
    limit: int = Query(20, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    ip_search: Optional[str] = Query(None, description="IP地址搜索"),
    model_search: Optional[str] = Query(None, description="模型名称搜索"),
    status_code_search: Optional[int] = Query(None, description="状态码搜索"),
    start_date: Optional[datetime] = Query(None, description="开始时间"),
    end_date: Optional[datetime] = Query(None, description="结束时间"),
    sort_by: str = Query("id", description="排序字段"),
    sort_order: str = Query("desc", description="排序顺序"),
):
    """获取访问日志列表"""
    auth_token = request.cookies.get("auth_token")
    if not auth_token or not verify_auth_token(auth_token):
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    try:
        logs = await db_services.get_access_logs(
            limit=limit,
            offset=offset,
            ip_search=ip_search,
            model_search=model_search,
            status_code_search=status_code_search,
            start_date=start_date,
            end_date=end_date,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        total = await db_services.get_access_logs_count(
            ip_search=ip_search,
            model_search=model_search,
            status_code_search=status_code_search,
            start_date=start_date,
            end_date=end_date,
        )
        
        return AccessLogListResponse(
            logs=[AccessLogItem(**log) for log in logs],
            total=total
        )
    except Exception as e:
        logger.exception(f"Failed to get access logs: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/logs/all", status_code=status.HTTP_204_NO_CONTENT)
async def delete_all_access_logs_api(request: Request):
    """删除所有访问日志"""
    auth_token = request.cookies.get("auth_token")
    if not auth_token or not verify_auth_token(auth_token):
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    try:
        await db_services.delete_all_access_logs()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except Exception as e:
        logger.exception(f"Failed to delete all access logs: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== IP黑名单相关 ====================

class IpBlacklistItem(BaseModel):
    id: int
    ip_address: str
    reason: Optional[str] = None
    created_at: Optional[datetime] = None


class IpBlacklistListResponse(BaseModel):
    entries: List[IpBlacklistItem]
    total: int


class AddIpRequest(BaseModel):
    ip_address: str
    reason: Optional[str] = None


class BulkAddIpRequest(BaseModel):
    ip_addresses: List[str]
    reason: Optional[str] = None


@router.get("/blacklist", response_model=IpBlacklistListResponse)
async def get_blacklist_api(
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """获取IP黑名单列表"""
    auth_token = request.cookies.get("auth_token")
    if not auth_token or not verify_auth_token(auth_token):
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    try:
        entries = await db_services.get_blacklist_entries(limit=limit, offset=offset)
        total = await db_services.get_blacklist_count()
        
        return IpBlacklistListResponse(
            entries=[IpBlacklistItem(**entry) for entry in entries],
            total=total
        )
    except Exception as e:
        logger.exception(f"Failed to get blacklist: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/blacklist", status_code=status.HTTP_201_CREATED)
async def add_ip_to_blacklist_api(request: Request, data: AddIpRequest):
    """添加IP到黑名单"""
    auth_token = request.cookies.get("auth_token")
    if not auth_token or not verify_auth_token(auth_token):
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    try:
        success = await db_services.add_ip_to_blacklist(
            ip_address=data.ip_address,
            reason=data.reason
        )
        if success:
            invalidate_blacklist_cache()
            return {"success": True, "message": f"IP {data.ip_address} added to blacklist"}
        else:
            raise HTTPException(status_code=400, detail="IP may already be in blacklist")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to add IP to blacklist: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/blacklist/bulk", status_code=status.HTTP_201_CREATED)
async def bulk_add_ips_to_blacklist_api(request: Request, data: BulkAddIpRequest):
    """批量添加IP到黑名单"""
    auth_token = request.cookies.get("auth_token")
    if not auth_token or not verify_auth_token(auth_token):
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    try:
        added_count = 0
        for ip in data.ip_addresses:
            ip = ip.strip()
            if ip:
                success = await db_services.add_ip_to_blacklist(
                    ip_address=ip,
                    reason=data.reason
                )
                if success:
                    added_count += 1
        
        invalidate_blacklist_cache()
        return {"success": True, "added_count": added_count, "total_requested": len(data.ip_addresses)}
    except Exception as e:
        logger.exception(f"Failed to bulk add IPs to blacklist: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/blacklist/{ip_address}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_ip_from_blacklist_api(request: Request, ip_address: str):
    """从黑名单移除IP"""
    auth_token = request.cookies.get("auth_token")
    if not auth_token or not verify_auth_token(auth_token):
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    try:
        await db_services.remove_ip_from_blacklist(ip_address)
        invalidate_blacklist_cache()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except Exception as e:
        logger.exception(f"Failed to remove IP from blacklist: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
