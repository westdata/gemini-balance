"""
文件管理服务
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple
from httpx import AsyncClient
import asyncio

from app.config.config import settings
from app.database import services as db_services
from app.database.models import FileState
from app.domain.file_models import FileMetadata, ListFilesResponse
from fastapi import HTTPException
from app.log.logger import get_files_logger
from app.utils.helpers import redact_key_for_logging
from app.service.client.api_client import GeminiApiClient
from app.service.key.key_manager import get_key_manager_instance

logger = get_files_logger()

# 全局上傳會話存儲（內存緩存，用於單 Worker 環境的快速查找）
_upload_sessions: Dict[str, Dict[str, Any]] = {}
_upload_sessions_lock = asyncio.Lock()

# 注意：為了支持多 Worker 環境，會話信息也會存儲到數據庫中
# 查找順序：1. 內存緩存 → 2. 數據庫

class FilesService:
    """文件管理服务类"""
    
    def __init__(self):
        self.api_client = GeminiApiClient(base_url=settings.BASE_URL)
        self.key_manager = None
    
    async def _get_key_manager(self):
        """获取 KeyManager 实例"""
        if not self.key_manager:
            self.key_manager = await get_key_manager_instance(
                settings.API_KEYS, 
                settings.VERTEX_API_KEYS
            )
        return self.key_manager
    
    async def initialize_upload(
        self, 
        headers: Dict[str, str], 
        body: Optional[bytes],
        user_token: str,
        request_host: str = None  # 添加請求主機參數
    ) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """
        初始化文件上传
        
        Args:
            headers: 请求头
            body: 请求体
            user_token: 用户令牌
            
        Returns:
            Tuple[Dict[str, Any], Dict[str, str]]: (响应体, 响应头)
        """
        try:
            # 從請求頭讀取 session_id（由路由層從查詢參數轉換而來）
            # session_id 僅用於 gemini-balance 內部控制 API key，不會傳遞給 Google API
            session_id = headers.get("x-session-id") or headers.get("X-Session-Id")
            
            # 從請求體中解析文件信息（僅提取 displayName，不處理 session_id）
            display_name = ""
            if body:
                try:
                    request_data = json.loads(body)
                    # 確保 request_data 是字典類型
                    if isinstance(request_data, dict):
                        display_name = request_data.get("displayName", "")
                        logger.debug(f"Parsed request body: displayName={display_name}")
                    else:
                        logger.warning(f"Request body is not a dictionary: {type(request_data)}")
                except json.JSONDecodeError as e:
                    logger.debug(f"Failed to parse request body as JSON: {e}")
                except Exception as e:
                    logger.debug(f"Failed to parse request body: {e}")
            
            # 驗證 session_id
            if session_id:
                if not isinstance(session_id, str) or not session_id.strip():
                    logger.warning(f"Invalid session_id: {session_id}, ignoring")
                    session_id = None
                else:
                    session_id = session_id.strip()
                    logger.debug(f"Using session_id from header: {session_id}")
            
            # 使用 session_id 控制 API key
            # 如果提供了有效的 session_id，嘗試復用該會話的 API key
            if session_id:
                async with _upload_sessions_lock:
                    # 查找該 session_id 對應的 API key
                    api_key = None
                    for upload_id, session_info in _upload_sessions.items():
                        if session_info.get("session_id") == session_id:
                            api_key = session_info["api_key"]
                            logger.info(f"Reusing API key for session {session_id}")
                            break
                    
                    if not api_key:
                        # 新會話，獲取新的 key
                        key_manager = await self._get_key_manager()
                        api_key = await key_manager.get_next_key()
                        logger.info(f"New session {session_id}, using new API key")
            else:
                # 沒有 session_id，保持原有流程：獲取新的 key
                key_manager = await self._get_key_manager()
                api_key = await key_manager.get_next_key()
                logger.debug("No session_id provided, using default key rotation")
            
            if not api_key:
                raise HTTPException(status_code=503, detail="No available API keys")
            
            # 轉發請求體（不需要修改，因為 session_id 不在請求體中）
            forward_body = body
            
            # 转发请求到真实的 Gemini API
            async with AsyncClient() as client:
                # 准备请求头
                forward_headers = {
                    "X-Goog-Upload-Protocol": headers.get("x-goog-upload-protocol", "resumable"),
                    "X-Goog-Upload-Command": headers.get("x-goog-upload-command", "start"),
                    "Content-Type": headers.get("content-type", "application/json"),
                }
                
                # 添加其他必要的头
                if "x-goog-upload-header-content-length" in headers:
                    forward_headers["X-Goog-Upload-Header-Content-Length"] = headers["x-goog-upload-header-content-length"]
                if "x-goog-upload-header-content-type" in headers:
                    forward_headers["X-Goog-Upload-Header-Content-Type"] = headers["x-goog-upload-header-content-type"]
                
                # 发送请求
                response = await client.post(
                    "https://generativelanguage.googleapis.com/upload/v1beta/files",
                    headers=forward_headers,
                    content=forward_body,  # 使用清理後的請求體
                    params={"key": api_key}
                )
                
                if response.status_code != 200:
                    logger.error(f"Upload initialization failed: {response.status_code} - {response.text}")
                    raise HTTPException(status_code=response.status_code, detail="Upload initialization failed")
                
                # 获取上传 URL
                upload_url = response.headers.get("x-goog-upload-url")
                if not upload_url:
                    raise HTTPException(status_code=500, detail="No upload URL in response")
                
                logger.info(f"Original upload URL from Google: {upload_url}")
                    
                
                # 儲存上傳資訊到 headers 中，供後續使用
                # 不在這裡創建數據庫記錄，等到上傳完成後再創建
                logger.info(f"Upload initialized with API key: {redact_key_for_logging(api_key)}")
                
                # 解析响应 - 初始化响应可能是空的
                response_data = {}
                
                # 從 upload URL 中提取 upload_id
                import urllib.parse
                parsed_url = urllib.parse.urlparse(upload_url)
                query_params = urllib.parse.parse_qs(parsed_url.query)
                upload_id = query_params.get('upload_id', [None])[0]
                
                if upload_id:
                    mime_type = headers.get("x-goog-upload-header-content-type", "application/octet-stream")
                    size_bytes = int(headers.get("x-goog-upload-header-content-length", "0"))
                    
                    # 1. 儲存到內存緩存（用於單 Worker 環境的快速查找）
                    async with _upload_sessions_lock:
                        _upload_sessions[upload_id] = {
                            "api_key": api_key,
                            "user_token": user_token,
                            "session_id": session_id,
                            "display_name": display_name,
                            "mime_type": mime_type,
                            "size_bytes": size_bytes,
                            "created_at": datetime.now(timezone.utc),
                            "upload_url": upload_url
                        }
                        logger.debug(f"Stored upload session in memory: upload_id={upload_id}")
                    
                    # 2. 同時儲存到數據庫（用於多 Worker 環境）
                    try:
                        await db_services.create_upload_session(
                            upload_id=upload_id,
                            api_key=api_key,
                            user_token=user_token,
                            session_id=session_id,
                            display_name=display_name,
                            mime_type=mime_type,
                            size_bytes=size_bytes,
                            upload_url=upload_url
                        )
                        logger.info(f"Stored upload session in DB: upload_id={upload_id}, session_id={session_id}, api_key={redact_key_for_logging(api_key)}")
                    except Exception as e:
                        # 數據庫寫入失敗不應阻止上傳流程，但需要記錄警告
                        logger.warning(f"Failed to store upload session in DB (will use memory only): {e}")
                else:
                    logger.warning(f"No upload_id found in upload URL: {upload_url}")
                
                # 定期清理過期的會話（超過1小時）
                asyncio.create_task(self._cleanup_expired_sessions())
                
                # 替換 Google 的 URL 為我們的代理 URL
                proxy_upload_url = upload_url
                if request_host:
                    # 確保使用HTTPS協議
                    if not request_host.startswith('https://'):
                        if request_host.startswith('http://'):
                            request_host = request_host.replace('http://', 'https://', 1)
                        else:
                            request_host = f"https://{request_host}"
                    
                    # 原始: https://generativelanguage.googleapis.com/upload/v1beta/files?key=AIzaSyDc...&upload_id=xxx&upload_protocol=resumable
                    # 替換為: https://request-host/upload/v1beta/files?key=sk-123456&upload_id=xxx&upload_protocol=resumable
                    
                    # 先替換域名
                    proxy_upload_url = upload_url.replace(
                        "https://generativelanguage.googleapis.com",
                        request_host.rstrip('/')
                    )
                    
                    # 再替換 key 參數
                    import re
                    # 匹配 key=xxx 參數
                    key_pattern = r'(\?|&)key=([^&]+)'
                    match = re.search(key_pattern, proxy_upload_url)
                    if match:
                        # 替換為我們的 token
                        proxy_upload_url = proxy_upload_url.replace(
                            f"{match.group(1)}key={match.group(2)}",
                            f"{match.group(1)}key={user_token}"
                        )
                    
                    logger.info(f"Replaced upload URL: {upload_url} -> {proxy_upload_url}")
                
                return response_data, {
                    "X-Goog-Upload-URL": proxy_upload_url,
                    "X-Goog-Upload-Status": "active"
                }
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to initialize upload: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")
    
    async def _cleanup_expired_sessions(self):
        """清理過期的上傳會話"""
        try:
            async with _upload_sessions_lock:
                now = datetime.now(timezone.utc)
                expired_keys = []
                for key, session in _upload_sessions.items():
                    if now - session["created_at"] > timedelta(hours=1):
                        expired_keys.append(key)
                
                for key in expired_keys:
                    del _upload_sessions[key]
                    
                if expired_keys:
                    logger.info(f"Cleaned up {len(expired_keys)} expired upload sessions")
        except Exception as e:
            logger.error(f"Error cleaning up upload sessions: {str(e)}")
    
    async def get_upload_session(self, key: str) -> Optional[Dict[str, Any]]:
        """
        獲取上傳會話信息（支持 upload_id 或完整 URL）
        
        查找順序：1. 內存緩存 → 2. 數據庫
        這樣可以支持多 Worker 環境
        """
        # 提取 upload_id
        upload_id = key
        if key.startswith("http"):
            import urllib.parse
            parsed_url = urllib.parse.urlparse(key)
            query_params = urllib.parse.parse_qs(parsed_url.query)
            upload_id = query_params.get('upload_id', [key])[0]
        
        # 1. 先嘗試從內存緩存查找
        async with _upload_sessions_lock:
            session = _upload_sessions.get(upload_id)
            if session:
                logger.debug(f"Found session in memory: upload_id={upload_id}")
                return session
        
        # 2. 內存中沒有，嘗試從數據庫查找（支持多 Worker 環境）
        try:
            db_session = await db_services.get_upload_session_by_id(upload_id)
            if db_session:
                logger.info(f"Found session in DB (multi-worker fallback): upload_id={upload_id}")
                # 將數據庫記錄轉換為內存格式
                return {
                    "api_key": db_session["api_key"],
                    "user_token": db_session["user_token"],
                    "session_id": db_session.get("session_id"),
                    "display_name": db_session.get("display_name"),
                    "mime_type": db_session.get("mime_type"),
                    "size_bytes": db_session.get("size_bytes"),
                    "created_at": db_session.get("created_at"),
                    "upload_url": db_session.get("upload_url")
                }
        except Exception as e:
            logger.warning(f"Failed to query session from DB: {e}")
        
        logger.debug(f"No session found for key: {redact_key_for_logging(key)}")
        return None
    
    async def get_file(self, file_name: str, user_token: str) -> FileMetadata:
        """
        获取文件信息
        
        Args:
            file_name: 文件名称 (格式: files/{file_id})
            user_token: 用户令牌
            
        Returns:
            FileMetadata: 文件元数据
        """
        try:
            # 查询文件记录
            file_record = await db_services.get_file_record_by_name(file_name)
            
            if not file_record:
                raise HTTPException(status_code=404, detail="File not found")
            
            # 检查是否过期
            expiration_time = datetime.fromisoformat(str(file_record["expiration_time"]))
            # 如果是 naive datetime，假设为 UTC
            if expiration_time.tzinfo is None:
                expiration_time = expiration_time.replace(tzinfo=timezone.utc)
            if expiration_time <= datetime.now(timezone.utc):
                raise HTTPException(status_code=404, detail="File has expired")
            
            # 使用原始 API key 获取文件信息
            api_key = file_record["api_key"]
            
            async with AsyncClient() as client:
                response = await client.get(
                    f"{settings.BASE_URL}/{file_name}",
                    params={"key": api_key}
                )
                
                if response.status_code != 200:
                    logger.error(f"Failed to get file: {response.status_code} - {response.text}")
                    raise HTTPException(status_code=response.status_code, detail="Failed to get file")
                
                file_data = response.json()
                
                # 檢查並更新文件狀態
                google_state = file_data.get("state", "PROCESSING")
                if google_state != file_record.get("state", "").value if file_record.get("state") else None:
                    logger.info(f"File state changed from {file_record.get('state')} to {google_state}")
                    # 更新數據庫中的狀態
                    if google_state == "ACTIVE":
                        await db_services.update_file_record_state(
                            file_name=file_name,
                            state=FileState.ACTIVE,
                            update_time=datetime.now(timezone.utc)
                        )
                    elif google_state == "FAILED":
                        await db_services.update_file_record_state(
                            file_name=file_name,
                            state=FileState.FAILED,
                            update_time=datetime.now(timezone.utc)
                        )
                
                # 构建响应
                return FileMetadata(
                    name=file_data["name"],
                    displayName=file_data.get("displayName"),
                    mimeType=file_data["mimeType"],
                    sizeBytes=str(file_data["sizeBytes"]),
                    createTime=file_data["createTime"],
                    updateTime=file_data["updateTime"],
                    expirationTime=file_data["expirationTime"],
                    sha256Hash=file_data.get("sha256Hash"),
                    uri=file_data["uri"],
                    state=google_state
                )
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get file {file_name}: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")
    
    async def list_files(
        self, 
        page_size: int = 10,
        page_token: Optional[str] = None,
        user_token: Optional[str] = None
    ) -> ListFilesResponse:
        """
        列出文件
        
        Args:
            page_size: 每页大小
            page_token: 分页标记
            user_token: 用户令牌（可选，如果提供则只返回该用户的文件）
            
        Returns:
            ListFilesResponse: 文件列表响应
        """
        try:
            logger.debug(f"list_files called with page_size={page_size}, page_token={page_token}")
            
            # 从数据库获取文件列表
            files, next_page_token = await db_services.list_file_records(
                user_token=user_token,
                page_size=page_size,
                page_token=page_token
            )
            
            logger.debug(f"Database returned {len(files)} files, next_page_token={next_page_token}")
            
            # 转换为响应格式
            file_list = []
            for file_record in files:
                file_list.append(FileMetadata(
                    name=file_record["name"],
                    displayName=file_record.get("display_name"),
                    mimeType=file_record["mime_type"],
                    sizeBytes=str(file_record["size_bytes"]),
                    createTime=file_record["create_time"].isoformat() + "Z",
                    updateTime=file_record["update_time"].isoformat() + "Z",
                    expirationTime=file_record["expiration_time"].isoformat() + "Z",
                    sha256Hash=file_record.get("sha256_hash"),
                    uri=file_record["uri"],
                    state=file_record["state"].value if file_record.get("state") else "ACTIVE"
                ))
            
            response = ListFilesResponse(
                files=file_list,
                nextPageToken=next_page_token
            )
            
            logger.debug(f"Returning response with {len(response.files)} files, nextPageToken={response.nextPageToken}")
            
            return response
            
        except Exception as e:
            logger.error(f"Failed to list files: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")
    
    async def delete_file(self, file_name: str, user_token: str) -> bool:
        """
        删除文件
        
        Args:
            file_name: 文件名称
            user_token: 用户令牌
            
        Returns:
            bool: 是否删除成功
        """
        try:
            # 查询文件记录
            file_record = await db_services.get_file_record_by_name(file_name)
            
            if not file_record:
                raise HTTPException(status_code=404, detail="File not found")
            
            # 使用原始 API key 删除文件
            api_key = file_record["api_key"]
            
            async with AsyncClient() as client:
                response = await client.delete(
                    f"{settings.BASE_URL}/{file_name}",
                    params={"key": api_key}
                )
                
                if response.status_code not in [200, 204]:
                    logger.error(f"Failed to delete file: {response.status_code} - {response.text}")
                    # 如果 API 删除失败，但文件已过期，仍然删除数据库记录
                    expiration_time = datetime.fromisoformat(str(file_record["expiration_time"]))
                    if expiration_time.tzinfo is None:
                        expiration_time = expiration_time.replace(tzinfo=timezone.utc)
                    if expiration_time <= datetime.now(timezone.utc):
                        await db_services.delete_file_record(file_name)
                        return True
                    raise HTTPException(status_code=response.status_code, detail="Failed to delete file")
            
            # 删除数据库记录
            await db_services.delete_file_record(file_name)
            return True
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to delete file {file_name}: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")
    
    async def check_file_state(self, file_name: str, api_key: str) -> str:
        """
        檢查並更新文件狀態
        
        Args:
            file_name: 文件名稱
            api_key: API密鑰
            
        Returns:
            str: 當前狀態
        """
        try:
            async with AsyncClient() as client:
                response = await client.get(
                    f"{settings.BASE_URL}/{file_name}",
                    params={"key": api_key}
                )
                
                if response.status_code != 200:
                    logger.error(f"Failed to check file state: {response.status_code}")
                    return "UNKNOWN"
                
                file_data = response.json()
                google_state = file_data.get("state", "PROCESSING")
                
                # 更新數據庫狀態
                if google_state == "ACTIVE":
                    await db_services.update_file_record_state(
                        file_name=file_name,
                        state=FileState.ACTIVE,
                        update_time=datetime.now(timezone.utc)
                    )
                elif google_state == "FAILED":
                    await db_services.update_file_record_state(
                        file_name=file_name,
                        state=FileState.FAILED,
                        update_time=datetime.now(timezone.utc)
                    )
                
                return google_state
                
        except Exception as e:
            logger.error(f"Failed to check file state: {str(e)}")
            return "UNKNOWN"
    
    async def cleanup_expired_files(self) -> int:
        """
        清理过期文件
        
        Returns:
            int: 清理的文件数量
        """
        try:
            # 获取过期文件
            expired_files = await db_services.delete_expired_file_records()
            
            if not expired_files:
                return 0
            
            # 尝试从 Gemini API 删除文件
            for file_record in expired_files:
                try:
                    api_key = file_record["api_key"]
                    file_name = file_record["name"]
                    
                    async with AsyncClient() as client:
                        await client.delete(
                            f"{settings.BASE_URL}/{file_name}",
                            params={"key": api_key}
                        )
                except Exception as e:
                    # 记录错误但继续处理其他文件
                    logger.error(f"Failed to delete file {file_record['name']} from API: {str(e)}")
            
            return len(expired_files)
            
        except Exception as e:
            logger.error(f"Failed to cleanup expired files: {str(e)}")
            return 0


# 单例实例
_files_service_instance: Optional[FilesService] = None


async def get_files_service() -> FilesService:
    """获取文件服务单例实例"""
    global _files_service_instance
    if _files_service_instance is None:
        _files_service_instance = FilesService()
    return _files_service_instance