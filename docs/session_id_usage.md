# Session ID 使用指南

## 问题背景

当使用 Google Gemini Python SDK 上传多个文件时，如果希望这些文件使用相同的 API key（例如用于后续的对话中引用这些文件），需要通过 `session_id` 来关联它们。

但是，SDK 的 `UploadFileConfig` 不允许直接传递 `session_id` 参数，会报错：

```
1 validation error for UploadFileConfig
session_id
  Extra inputs are not permitted [type=extra_forbidden, ...]
```

## 解决方案

通过 `http_options` 中的 `headers` 来传递 `session_id`。

### 客户端代码示例

```python
import uuid
import io
from pathlib import Path
from google import genai
from google.genai.types import HttpOptions

# 初始化客户端
client = genai.Client(
    api_key="your-auth-token",  # 您的认证令牌
    http_options=HttpOptions(
        base_url="https://your-gemini-balance-server.com"  # 您的服务器地址
    )
)

# 生成会话 ID（用于关联同一批次的文件）
session_id = str(uuid.uuid4())
print(f"Session ID: {session_id}")

# 准备文件
pdf_path_1 = Path("document1.pdf")
pdf_path_2 = Path("document2.pdf")

doc_data_1 = io.BytesIO(pdf_path_1.read_bytes())
doc_data_2 = io.BytesIO(pdf_path_2.read_bytes())

# 上传第一个文件 - 通过 headers 传递 session_id
sample_pdf_1 = client.files.upload(
    file=doc_data_1,
    config=dict(
        mime_type='application/pdf',
        display_name='Document 1',
        http_options={
            'headers': {
                'X-Session-Id': session_id  # 通过自定义 HTTP 头传递
            }
        }
    )
)

print(f"Uploaded file 1: {sample_pdf_1.name}")

# 上传第二个文件 - 使用相同的 session_id
sample_pdf_2 = client.files.upload(
    file=doc_data_2,
    config=dict(
        mime_type='application/pdf',
        display_name='Document 2',
        http_options={
            'headers': {
                'X-Session-Id': session_id  # 相同的 session_id
            }
        }
    )
)

print(f"Uploaded file 2: {sample_pdf_2.name}")

# 现在这两个文件已经关联到相同的 API key
# 可以在同一个对话中使用它们
response = client.models.generate_content(
    model='gemini-2.0-flash-exp',
    contents=[
        f"请分析这两个文档：{sample_pdf_1.uri} 和 {sample_pdf_2.uri}",
    ]
)

print(response.text)
```

## 工作原理

### 1. 客户端流程

- 客户端通过 `http_options.headers` 传递 `X-Session-Id` 头
- Gemini SDK 会将这个自定义头添加到 HTTP 请求中
- 请求发送到 gemini-balance 服务器

### 2. 服务端处理

服务端代码位置：
- **路由层**：`app/router/files_routes.py:34` - 接收请求头中的 `X-Session-Id`
- **服务层**：`app/service/files/files_service.py:62-112` - 使用 session_id 控制 API key 复用

处理逻辑：

```python
# 1. 从请求头获取 session_id
session_id = headers.get("x-session-id")

# 2. 如果提供了 session_id，尝试复用该会话的 API key
if session_id:
    # 查找该 session_id 对应的 API key
    for upload_id, session_info in _upload_sessions.items():
        if session_info.get("session_id") == session_id:
            api_key = session_info["api_key"]
            # 复用找到的 API key
            break
    
    if not api_key:
        # 新会话，获取新的 key
        api_key = await key_manager.get_next_key()
else:
    # 没有 session_id，使用默认的 key 轮转
    api_key = await key_manager.get_next_key()

# 3. 转发请求到 Google API 时，不包含 X-Session-Id 头
forward_headers = {
    "X-Goog-Upload-Protocol": headers.get("x-goog-upload-protocol", "resumable"),
    "X-Goog-Upload-Command": headers.get("x-goog-upload-command", "start"),
    "Content-Type": headers.get("content-type", "application/json"),
    # 注意：不包含 X-Session-Id，这是我们内部使用的头
}
```

### 3. 关键点

✅ **使用 headers（推荐）**：
- 语义清晰：session_id 是会话级别的元数据
- 处理简单：路由层直接通过 `Header()` 依赖注入获取
- 不影响请求体结构
- 转发给 Google API 时自动过滤掉

❌ **不推荐使用 extra_body**：
- 需要解析和重构请求体
- 可能与 Google API 的预期结构冲突
- 增加代码复杂度

## 注意事项

1. **Session ID 的生命周期**：
   - Session 信息会在服务端内存中保存 1 小时
   - 超过 1 小时后会被自动清理
   - 建议在同一批次的文件上传完成后立即使用

2. **Session ID 格式**：
   - 推荐使用 UUID 格式：`str(uuid.uuid4())`
   - 必须是非空字符串
   - 大小写不敏感（服务端会标准化）

3. **错误处理**：
   - 如果 session_id 格式无效，服务端会忽略它
   - 如果找不到对应的 session，会自动分配新的 API key

## 其他传递方式

除了 headers，服务端也支持通过查询参数传递（但不推荐用于 SDK）：

```python
# 直接 HTTP 请求示例（不使用 SDK）
import httpx

response = httpx.post(
    "https://your-server.com/upload/v1beta/files?session_id=xxx",
    headers={
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        # ...
    }
)
```

但使用 SDK 时，**强烈推荐使用 headers 方式**。

## 验证

上传后，可以通过日志确认 session_id 是否生效：

```
INFO - Received session_id: b9552e1d-4a8e-454c-8d98-af7780bafb21 (from header)
INFO - Reusing API key for session b9552e1d-4a8e-454c-8d98-af7780bafb21
```

如果看到 "Reusing API key"，说明 session_id 已经成功关联文件了。

