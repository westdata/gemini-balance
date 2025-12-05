"""
Gemini 文件上传器 - 支持 session_id
绕过 SDK 限制，直接使用 HTTP 请求上传文件
"""
import httpx
import uuid
import time
import sys
import socket
from pathlib import Path
from typing import Optional, Union
import io
from urllib.parse import urlparse

# 设置 Windows 控制台编码为 UTF-8，避免中文乱码
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except:
        pass


def diagnose_connection(url: str) -> dict:
    """
    诊断到服务器的连接
    
    Returns:
        dict: 诊断结果
    """
    result = {
        "url": url,
        "dns_resolved": False,
        "ip_address": None,
        "tcp_connectable": False,
        "error": None
    }
    
    try:
        # 解析 URL
        parsed = urlparse(url)
        hostname = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        
        print(f"🔍 诊断连接: {hostname}:{port}")
        
        # 1. DNS 解析测试
        print(f"  [1/3] DNS 解析...")
        try:
            ip = socket.gethostbyname(hostname)
            result["dns_resolved"] = True
            result["ip_address"] = ip
            print(f"  ✓ DNS 解析成功: {hostname} -> {ip}")
        except socket.gaierror as e:
            result["error"] = f"DNS 解析失败: {e}"
            print(f"  ✗ DNS 解析失败: {e}")
            return result
        
        # 2. TCP 连接测试
        print(f"  [2/3] TCP 连接测试 ({ip}:{port})...")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        try:
            start_time = time.time()
            sock.connect((ip, port))
            connect_time = time.time() - start_time
            result["tcp_connectable"] = True
            print(f"  ✓ TCP 连接成功 (耗时: {connect_time:.2f}秒)")
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            result["error"] = f"TCP 连接失败: {e}"
            print(f"  ✗ TCP 连接失败: {e}")
        finally:
            sock.close()
        
        # 3. HTTP 请求测试
        if result["tcp_connectable"]:
            print(f"  [3/3] HTTP 请求测试...")
            try:
                with httpx.Client(timeout=10.0, trust_env=False) as client:
                    start_time = time.time()
                    response = client.get(f"{parsed.scheme}://{hostname}:{port}/")
                    request_time = time.time() - start_time
                    print(f"  ✓ HTTP 请求成功 (状态码: {response.status_code}, 耗时: {request_time:.2f}秒)")
            except Exception as e:
                print(f"  ⚠ HTTP 请求失败: {e}")
        
    except Exception as e:
        result["error"] = f"诊断过程出错: {e}"
        print(f"  ✗ 诊断出错: {e}")
    
    return result


class GeminiFileUploader:
    """Gemini 文件上传器，支持 session_id 关联多个文件到同一个 API key"""
    
    def __init__(self, base_url: str, auth_token: str, use_proxy: bool = False):
        """
        初始化上传器
        
        Args:
            base_url: Gemini Balance 服务器地址
            auth_token: 认证令牌
            use_proxy: 是否使用系统代理（默认 False，直连）
        """
        self.base_url = base_url.rstrip('/')
        self.auth_token = auth_token
        
        # 配置超时：连接超时10秒，读取超时60秒
        timeout_config = httpx.Timeout(
            connect=10.0,  # 连接超时
            read=60.0,     # 读取超时
            write=60.0,    # 写入超时
            pool=10.0      # 连接池超时
        )
        
        # HTTP 连接不需要 SSL 验证
        ssl_verify = not base_url.startswith("http://")
        
        # 如果不使用代理，禁用环境变量中的代理设置
        if not use_proxy:
            self.client = httpx.Client(
                timeout=timeout_config,
                trust_env=False,  # 不信任环境变量（HTTP_PROXY等），强制直连
                verify=ssl_verify
            )
        else:
            self.client = httpx.Client(
                timeout=timeout_config,
                trust_env=True,  # 信任环境变量，使用系统代理
                verify=ssl_verify
            )
    
    def wait_for_file_active(
        self,
        file_name: str,
        timeout: int = 120,
        check_interval: float = 2.0
    ) -> str:
        """
        等待文件处理完成（状态变为 ACTIVE）
        
        Args:
            file_name: 文件名（格式：files/xxx）
            timeout: 超时时间（秒）
            check_interval: 检查间隔（秒）
            
        Returns:
            str: 文件状态（ACTIVE 或其他）
        """
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                # 获取文件信息
                response = self.client.get(
                    f"{self.base_url}/v1beta/{file_name}",
                    params={"key": self.auth_token}
                )
                
                if response.status_code == 200:
                    file_info = response.json()
                    state = file_info.get("state", "UNKNOWN")
                    
                    if state == "ACTIVE":
                        return state
                    elif state == "FAILED":
                        raise Exception(f"File processing failed: {file_name}")
                    
                    # 仍在处理中，继续等待
                    time.sleep(check_interval)
                else:
                    raise Exception(f"Failed to get file status: {response.status_code} - {response.text}")
                    
            except Exception as e:
                print(f"  检查文件状态时出错: {e}")
                time.sleep(check_interval)
        
        raise TimeoutError(f"File did not become active within {timeout} seconds")
    
    def upload_file(
        self, 
        file_path: Union[str, Path, io.BytesIO],
        mime_type: str = "application/pdf",
        display_name: Optional[str] = None,
        session_id: Optional[str] = None,
        wait_for_active: bool = True,
        timeout: int = 120
    ) -> dict:
        """
        上传文件到 Gemini
        
        Args:
            file_path: 文件路径或 BytesIO 对象
            mime_type: MIME 类型
            display_name: 显示名称
            session_id: 会话 ID（可选），用于将多个文件关联到同一个 API key
            wait_for_active: 是否等待文件处理完成（默认 True）
            timeout: 等待超时时间（秒，默认 120）
            
        Returns:
            dict: 文件信息，包含 name, uri, state 等字段
        """
        # 读取文件数据
        if isinstance(file_path, io.BytesIO):
            file_data = file_path.getvalue()
            if not display_name:
                display_name = "uploaded_file"
        elif isinstance(file_path, (str, Path)):
            file_path = Path(file_path)
            file_data = file_path.read_bytes()
            if not display_name:
                display_name = file_path.name
        else:
            raise ValueError("file_path must be a path string, Path object, or BytesIO")
        
        file_size = len(file_data)
        
        # 1. 初始化上传
        init_headers = {
            "x-goog-upload-protocol": "resumable",
            "x-goog-upload-command": "start",
            "x-goog-upload-header-content-length": str(file_size),
            "x-goog-upload-header-content-type": mime_type,
            "content-type": "application/json",
        }
        
        # 构建 URL，包含 session_id（如果提供）
        params = {"key": self.auth_token}
        if session_id:
            params["session_id"] = session_id
        
        init_response = self.client.post(
            f"{self.base_url}/upload/v1beta/files",
            headers=init_headers,
            params=params
        )
        
        if init_response.status_code != 200:
            raise Exception(f"Upload initialization failed: {init_response.status_code} - {init_response.text}")
        
        # 获取上传 URL
        upload_url = init_response.headers.get("x-goog-upload-url")
        if not upload_url:
            raise Exception("No upload URL in response headers")
        
        # 如果 base_url 是 HTTP，确保 upload_url 也使用 HTTP
        if self.base_url.startswith("http://") and upload_url.startswith("https://"):
            upload_url = upload_url.replace("https://", "http://", 1)
            print(f"  [调试] 将上传 URL 转换为 HTTP: {upload_url[:80]}...")
        
        # 2. 上传文件数据
        upload_headers = {
            "X-Goog-Upload-Command": "upload, finalize",
            "X-Goog-Upload-Offset": "0",
            "Content-Length": str(file_size),
        }
        
        upload_response = self.client.post(
            upload_url,
            headers=upload_headers,
            content=file_data
        )
        
        if upload_response.status_code != 200:
            raise Exception(f"File upload failed: {upload_response.status_code} - {upload_response.text}")
        
        # 解析响应
        result = upload_response.json()
        file_info = result.get("file", {})
        
        if not file_info.get("name"):
            raise Exception(f"No file name in response: {result}")
        
        # 如果需要等待文件处理完成
        if wait_for_active:
            file_name = file_info["name"]
            print(f"  等待文件处理完成...")
            try:
                state = self.wait_for_file_active(file_name, timeout=timeout)
                file_info["state"] = state
                print(f"  文件已就绪（状态：{state}）")
            except TimeoutError as e:
                print(f"  警告：{e}")
                file_info["state"] = "PROCESSING"
            except Exception as e:
                print(f"  警告：无法确认文件状态 - {e}")
                file_info["state"] = "UNKNOWN"
        
        return file_info
    
    def close(self):
        """关闭 HTTP 客户端"""
        self.client.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def upload_files_with_session(
    base_url: str,
    auth_token: str,
    file_paths: list,
    mime_type: str = "application/pdf",
    session_id: Optional[str] = None,
    wait_for_active: bool = True,
    timeout: int = 120,
    use_proxy: bool = False
) -> list:
    """
    便捷函数：上传多个文件，使用相同的 session_id
    
    Args:
        base_url: Gemini Balance 服务器地址
        auth_token: 认证令牌
        file_paths: 文件路径列表
        mime_type: MIME 类型
        session_id: 会话 ID（如果不提供，会自动生成）
        wait_for_active: 是否等待文件处理完成（默认 True）
        timeout: 等待超时时间（秒，默认 120）
        use_proxy: 是否使用系统代理（默认 False，建议国内直连海外服务器时设为 False）
        
    Returns:
        list: 文件信息列表
    """
    if session_id is None:
        session_id = str(uuid.uuid4())
    
    print(f"Session ID: {session_id}")
    
    with GeminiFileUploader(base_url, auth_token, use_proxy=use_proxy) as uploader:
        uploaded_files = []
        for i, file_path in enumerate(file_paths, 1):
            print(f"上传文件 {i}/{len(file_paths)}: {file_path}")
            file_info = uploader.upload_file(
                file_path=file_path,
                mime_type=mime_type,
                session_id=session_id,
                wait_for_active=wait_for_active,
                timeout=timeout
            )
            uploaded_files.append(file_info)
            print(f"  成功: {file_info['name']} (状态: {file_info.get('state', 'UNKNOWN')})")
        
    return uploaded_files


# 使用示例
if __name__ == "__main__":
    from pathlib import Path
    
    # 配置
    # BASE_URL = "https://iobjdlhzuzno.jp-members-1.clawcloudrun.com"
    BASE_URL = "https://yguqxvradkbs.jp-members-1.clawcloudrun.com"
    # BASE_URL = "http://localhost:8000"
    AUTH_TOKEN = "solution"
    
    print("=" * 80)
    print("开始连接诊断")
    print("=" * 80)
    
    # 先诊断连接
    diag_result = diagnose_connection(BASE_URL)
    
    if not diag_result["tcp_connectable"]:
        print("\n❌ 连接诊断失败！")
        print(f"错误: {diag_result['error']}")
        print("\n可能的解决方案:")
        print("1. 检查您的网络连接")
        print("2. 确认服务器地址是否正确")
        print("3. 检查防火墙设置")
        print("4. 如果在国内，可能需要使用 VPN")
        if diag_result["ip_address"]:
            print(f"5. 尝试直接使用 IP 地址: http://{diag_result['ip_address']}")
        exit(1)
    
    print("\n✓ 连接诊断通过，开始上传文件...\n")
    print("=" * 80)
    
    # 测试文件
    test_files = [
        Path(r"E:\WorkDir\2025\四川执法\环评信息提取\indicate_EIA\test\1.pdf"),
        Path(r"E:\WorkDir\2025\四川执法\环评信息提取\indicate_EIA\test\2.pdf"),
    ]
    
    # 上传文件（使用相同的 session_id）
    try:
        uploaded_files = upload_files_with_session(
            base_url=BASE_URL,
            auth_token=AUTH_TOKEN,
            file_paths=test_files
        )
        
        print(f"\n上传完成！共 {len(uploaded_files)} 个文件")
        print("\n现在可以在对话中使用这些文件了：")
        
        # 使用 SDK 进行对话
        from google import genai
        from google.genai.types import HttpOptions, Part
        
        # 本地 HTTP 服务需要禁用 SSL 验证
        if BASE_URL.startswith("http://"):
            # HTTP 连接：禁用 SSL 验证
            http_opts = HttpOptions(
                base_url=BASE_URL,
                api_version="v1beta",
                client_args={"verify": False}
            )
        else:
            http_opts = HttpOptions(base_url=BASE_URL)
        
        client = genai.Client(
            api_key=AUTH_TOKEN,
            http_options=http_opts
        )
        
        # 构建文件引用 - 使用 Part.from_uri() 创建正确的引用
        file_parts = []
        for file_info in uploaded_files:
            # 获取文件的 URI（格式：https://generativelanguage.googleapis.com/v1beta/files/xxx）
            file_uri = file_info.get('uri')
            if file_uri:
                file_parts.append(Part.from_uri(file_uri=file_uri, mime_type="application/pdf"))
            else:
                print(f"  警告：文件 {file_info['name']} 没有 URI")
        
        if not file_parts:
            print("错误：没有有效的文件引用")
        else:
            # 进行对话
            print("\n测试对话（使用两个文件）...")
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=file_parts + ["请简单总结这两个文档的主要内容（各用一句话）。"]
            )
            
            print(f"\n对话成功！")
            print(f"回答：\n{response.text}")
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()

