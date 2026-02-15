"""
Gemini Router - Handles native Gemini format API requests
处理原生Gemini格式请求的路由模块
"""
import re
import sys
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# 标准库
import asyncio
import json

# 第三方库
from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse, StreamingResponse

# 本地模块 - 配置和日志
from config import get_anti_truncation_max_attempts
from log import log

# 本地模块 - 工具和认证
from src.utils import (
    get_base_model_from_feature_model,
    is_anti_truncation_model,
    authenticate_gemini_flexible,
    is_fake_streaming_model
)

# 本地模块 - 转换器（假流式需要）
from src.converter.fake_stream import (
    parse_response_for_fake_stream,
    build_gemini_fake_stream_chunks,
    create_gemini_heartbeat_chunk,
)

# 本地模块 - 基础路由工具
from src.router.hi_check import is_health_check_request, create_health_check_response

# 本地模块 - 数据模型
from src.models import GeminiRequest, model_to_dict

# 本地模块 - 任务管理
from src.task_manager import create_managed_task


# ==================== 路由器初始化 ====================

router = APIRouter()


# ==================== API 路由 ====================

@router.post("/v1beta/models/{model:path}:generateContent")
@router.post("/v1/models/{model:path}:generateContent")
async def generate_content(
    gemini_request: "GeminiRequest",
    model: str = Path(..., description="Model name"),
    api_key: str = Depends(authenticate_gemini_flexible),
):
    """
    处理Gemini格式的内容生成请求（非流式）

    Args:
        gemini_request: Gemini格式的请求体
        model: 模型名称
        api_key: API 密钥
    """
    log.debug(f"[GEMINICLI] Non-streaming request for model: {model}")

    # 转换为字典
    normalized_dict = model_to_dict(gemini_request)

    # 健康检查
    if is_health_check_request(normalized_dict, format="gemini"):
        response = create_health_check_response(format="gemini")
        return JSONResponse(content=response)

    # 处理模型名称和功能检测
    use_anti_truncation = is_anti_truncation_model(model)
    real_model = get_base_model_from_feature_model(model)

    # 对于抗截断模型的非流式请求，给出警告
    if use_anti_truncation:
        log.warning("抗截断功能仅在流式传输时有效，非流式请求将忽略此设置")

    # 更新模型名为真实模型名
    normalized_dict["model"] = real_model

    # 规范化 Gemini 请求 (使用 geminicli 模式)
    from src.converter.gemini_fix import normalize_gemini_request
    normalized_dict = await normalize_gemini_request(normalized_dict, mode="geminicli")

    # 准备API请求格式 - 提取model并将其他字段放入request中
    api_request = {
        "model": normalized_dict.pop("model"),
        "request": normalized_dict
    }

    # 调用 API 层的非流式请求
    from src.api.geminicli import non_stream_request
    response = await non_stream_request(body=api_request)

    # 解包装响应：GeminiCli API 返回的格式有额外的 response 包装层
    # 需要提取 response.response 并返回标准 Gemini 格式
    try:
        if response.status_code == 200:
            response_data = json.loads(response.body if hasattr(response, 'body') else response.content)
            # 如果有 response 包装，解包装它
            if "response" in response_data:
                unwrapped_data = response_data["response"]
                return JSONResponse(content=unwrapped_data)
        # 错误响应或没有 response 字段，直接返回
        return response
    except Exception as e:
        log.warning(f"Failed to unwrap response: {e}, returning original response")
        return response

@router.post("/v1beta/models/{model:path}:streamGenerateContent")
@router.post("/v1/models/{model:path}:streamGenerateContent")
async def stream_generate_content(
    gemini_request: GeminiRequest,
    model: str = Path(..., description="Model name"),
    api_key: str = Depends(authenticate_gemini_flexible),
):
    """
    【修改版】強制使用假流式 (Fake Streaming) 以便進行思考鏈過濾
    """
    log.debug(f"[GEMINICLI] Streaming request for model: {model}")

    # 转换为字典
    normalized_dict = model_to_dict(gemini_request)
    
    # 强制开启假流式
    use_fake_streaming = True 
    
    # 获取真实模型名
    real_model = get_base_model_from_feature_model(model)
    normalized_dict["model"] = real_model

    # ========== 定義過濾函數 ==========
    def clean_thinking_text(text):
        if not text: return ""
        # 1. 去除 <think> 标签
        text = re.sub(r'<think>[\s\S]*?</think>', '', text)
        # 2. 去除 llm 的碎碎念 
        text = re.sub(r"(?i)^(okay|alright),?\s+here'?s\s+what\s+i'?m\s+thinking.*?\n", "", text)
        # 3. 去除 Analysis/Reasoning 开头 
        text = re.sub(r"(?i)^(analysis|reasoning|thought process):[\s\S]*?\n\n", "", text)
        return text

    # ========== 修改後的假流式生成器 ==========
    async def fake_stream_generator():
        from src.converter.gemini_fix import normalize_gemini_request
        from src.converter.fake_stream import parse_response_for_fake_stream, build_gemini_fake_stream_chunks, create_gemini_heartbeat_chunk
        
        normalized_req = await normalize_gemini_request(normalized_dict.copy(), mode="geminicli")

        api_request = {
            "model": normalized_req.pop("model"),
            "request": normalized_req
        }

        # 發送心跳
        heartbeat = create_gemini_heartbeat_chunk()
        yield f"data: {json.dumps(heartbeat)}\n\n".encode()

        # 等待完整回復
        from src.api.geminicli import non_stream_request
        response = await non_stream_request(body=api_request)

        # 處理響應
        response_body = response.body.decode() if hasattr(response, "body") else str(response)
        
        try:
            response_data = json.loads(response_body)
            
            # 如果有错误，直接返回
            if "error" in response_data:
                yield f"data: {json.dumps(response_data)}\n\n".encode()
                yield "data: [DONE]\n\n".encode()
                return

            # 解析内容
            content, reasoning_content, finish_reason, images = parse_response_for_fake_stream(response_data)

            # 在這裡清洗 content
            if content:
                log.debug(f"[FILTER] Original content length: {len(content)}")
                content = clean_thinking_text(content)
                log.debug(f"[FILTER] Cleaned content length: {len(content)}")

            # 構建偽裝的流式塊
            chunks = build_gemini_fake_stream_chunks(content, reasoning_content, finish_reason, images)
            
            for chunk in chunks:
                yield f"data: {json.dumps(chunk)}\n\n".encode()

        except Exception as e:
            log.error(f"Fake stream filtering failed: {e}")
            # 如果失敗了，原樣返回
            yield f"data: {response_body}\n\n".encode()

        yield "data: [DONE]\n\n".encode()

    # 直接返回假流式
    return StreamingResponse(fake_stream_generator(), media_type="text/event-stream")



@router.post("/v1beta/models/{model:path}:countTokens")
@router.post("/v1/models/{model:path}:countTokens")
async def count_tokens(
    request: Request = None,
    api_key: str = Depends(authenticate_gemini_flexible),
):
    """
    模拟Gemini格式的token计数
    
    使用简单的启发式方法：大约4字符=1token
    """

    try:
        request_data = await request.json()
    except Exception as e:
        log.error(f"Failed to parse JSON request: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")

    # 简单的token计数模拟 - 基于文本长度估算
    total_tokens = 0

    # 如果有contents字段
    if "contents" in request_data:
        for content in request_data["contents"]:
            if "parts" in content:
                for part in content["parts"]:
                    if "text" in part:
                        # 简单估算：大约4字符=1token
                        text_length = len(part["text"])
                        total_tokens += max(1, text_length // 4)

    # 如果有generateContentRequest字段
    elif "generateContentRequest" in request_data:
        gen_request = request_data["generateContentRequest"]
        if "contents" in gen_request:
            for content in gen_request["contents"]:
                if "parts" in content:
                    for part in content["parts"]:
                        if "text" in part:
                            text_length = len(part["text"])
                            total_tokens += max(1, text_length // 4)

    # 返回Gemini格式的响应
    return JSONResponse(content={"totalTokens": total_tokens})

# ==================== 测试代码 ====================

if __name__ == "__main__":
    """
    测试代码：演示Gemini路由的流式和非流式响应
    运行方式: python src/router/geminicli/gemini.py
    """

    from fastapi.testclient import TestClient
    from fastapi import FastAPI

    print("=" * 80)
    print("Gemini Router 测试")
    print("=" * 80)

    # 创建测试应用
    app = FastAPI()
    app.include_router(router)

    # 测试客户端
    client = TestClient(app)

    # 测试请求体 (Gemini格式)
    test_request_body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": "Hello, tell me a joke in one sentence."}]
            }
        ]
    }

    # 测试API密钥（模拟）
    test_api_key = "pwd"

    def test_non_stream_request():
        """测试非流式请求"""
        print("\n" + "=" * 80)
        print("【测试2】非流式请求 (POST /v1/models/gemini-2.5-flash:generateContent)")
        print("=" * 80)
        print(f"请求体: {json.dumps(test_request_body, indent=2, ensure_ascii=False)}\n")

        response = client.post(
            "/v1/models/gemini-2.5-flash:generateContent",
            json=test_request_body,
            params={"key": test_api_key}
        )

        print("非流式响应数据:")
        print("-" * 80)
        print(f"状态码: {response.status_code}")
        print(f"Content-Type: {response.headers.get('content-type', 'N/A')}")

        try:
            content = response.text
            print(f"\n响应内容 (原始):\n{content}\n")

            # 尝试解析JSON
            try:
                json_data = response.json()
                print(f"响应内容 (格式化JSON):")
                print(json.dumps(json_data, indent=2, ensure_ascii=False))
            except json.JSONDecodeError:
                print("(非JSON格式)")
        except Exception as e:
            print(f"内容解析失败: {e}")

    def test_stream_request():
        """测试流式请求"""
        print("\n" + "=" * 80)
        print("【测试3】流式请求 (POST /v1/models/gemini-2.5-flash:streamGenerateContent)")
        print("=" * 80)
        print(f"请求体: {json.dumps(test_request_body, indent=2, ensure_ascii=False)}\n")

        print("流式响应数据 (每个chunk):")
        print("-" * 80)

        with client.stream(
            "POST",
            "/v1/models/gemini-2.5-flash:streamGenerateContent",
            json=test_request_body,
            params={"key": test_api_key}
        ) as response:
            print(f"状态码: {response.status_code}")
            print(f"Content-Type: {response.headers.get('content-type', 'N/A')}\n")

            chunk_count = 0
            for chunk in response.iter_bytes():
                if chunk:
                    chunk_count += 1
                    print(f"\nChunk #{chunk_count}:")
                    print(f"  类型: {type(chunk).__name__}")
                    print(f"  长度: {len(chunk)}")

                    # 解码chunk
                    try:
                        chunk_str = chunk.decode('utf-8')
                        print(f"  内容预览: {repr(chunk_str[:200] if len(chunk_str) > 200 else chunk_str)}")

                        # 如果是SSE格式，尝试解析每一行
                        if chunk_str.startswith("data: "):
                            # 按行分割，处理每个SSE事件
                            for line in chunk_str.strip().split('\n'):
                                line = line.strip()
                                if not line:
                                    continue

                                if line == "data: [DONE]":
                                    print(f"  => 流结束标记")
                                elif line.startswith("data: "):
                                    try:
                                        json_str = line[6:]  # 去掉 "data: " 前缀
                                        json_data = json.loads(json_str)
                                        print(f"  解析后的JSON: {json.dumps(json_data, indent=4, ensure_ascii=False)}")
                                    except Exception as e:
                                        print(f"  SSE解析失败: {e}")
                    except Exception as e:
                        print(f"  解码失败: {e}")

            print(f"\n总共收到 {chunk_count} 个chunk")

    def test_fake_stream_request():
        """测试假流式请求"""
        print("\n" + "=" * 80)
        print("【测试4】假流式请求 (POST /v1/models/假流式/gemini-2.5-flash:streamGenerateContent)")
        print("=" * 80)
        print(f"请求体: {json.dumps(test_request_body, indent=2, ensure_ascii=False)}\n")

        print("假流式响应数据 (每个chunk):")
        print("-" * 80)

        with client.stream(
            "POST",
            "/v1/models/假流式/gemini-2.5-flash:streamGenerateContent",
            json=test_request_body,
            params={"key": test_api_key}
        ) as response:
            print(f"状态码: {response.status_code}")
            print(f"Content-Type: {response.headers.get('content-type', 'N/A')}\n")

            chunk_count = 0
            for chunk in response.iter_bytes():
                if chunk:
                    chunk_count += 1
                    chunk_str = chunk.decode('utf-8')

                    print(f"\nChunk #{chunk_count}:")
                    print(f"  长度: {len(chunk_str)} 字节")

                    # 解析chunk中的所有SSE事件
                    events = []
                    for line in chunk_str.split('\n'):
                        line = line.strip()
                        if line.startswith("data: "):
                            events.append(line)

                    print(f"  包含 {len(events)} 个SSE事件")

                    # 显示每个事件
                    for event_idx, event_line in enumerate(events, 1):
                        if event_line == "data: [DONE]":
                            print(f"  事件 #{event_idx}: [DONE]")
                        else:
                            try:
                                json_str = event_line[6:]  # 去掉 "data: " 前缀
                                json_data = json.loads(json_str)
                                # 提取text内容
                                text = json_data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                                finish_reason = json_data.get("candidates", [{}])[0].get("finishReason")
                                print(f"  事件 #{event_idx}: text={repr(text[:50])}{'...' if len(text) > 50 else ''}, finishReason={finish_reason}")
                            except Exception as e:
                                print(f"  事件 #{event_idx}: 解析失败 - {e}")

            print(f"\n总共收到 {chunk_count} 个HTTP chunk")

    def test_anti_truncation_stream_request():
        """测试流式抗截断请求"""
        print("\n" + "=" * 80)
        print("【测试5】流式抗截断请求 (POST /v1/models/流式抗截断/gemini-2.5-flash:streamGenerateContent)")
        print("=" * 80)
        print(f"请求体: {json.dumps(test_request_body, indent=2, ensure_ascii=False)}\n")

        print("流式抗截断响应数据 (每个chunk):")
        print("-" * 80)

        with client.stream(
            "POST",
            "/v1/models/流式抗截断/gemini-2.5-flash:streamGenerateContent",
            json=test_request_body,
            params={"key": test_api_key}
        ) as response:
            print(f"状态码: {response.status_code}")
            print(f"Content-Type: {response.headers.get('content-type', 'N/A')}\n")

            chunk_count = 0
            for chunk in response.iter_bytes():
                if chunk:
                    chunk_count += 1
                    print(f"\nChunk #{chunk_count}:")
                    print(f"  类型: {type(chunk).__name__}")
                    print(f"  长度: {len(chunk)}")

                    # 解码chunk
                    try:
                        chunk_str = chunk.decode('utf-8')
                        print(f"  内容预览: {repr(chunk_str[:200] if len(chunk_str) > 200 else chunk_str)}")

                        # 如果是SSE格式，尝试解析每一行
                        if chunk_str.startswith("data: "):
                            # 按行分割，处理每个SSE事件
                            for line in chunk_str.strip().split('\n'):
                                line = line.strip()
                                if not line:
                                    continue

                                if line == "data: [DONE]":
                                    print(f"  => 流结束标记")
                                elif line.startswith("data: "):
                                    try:
                                        json_str = line[6:]  # 去掉 "data: " 前缀
                                        json_data = json.loads(json_str)
                                        print(f"  解析后的JSON: {json.dumps(json_data, indent=4, ensure_ascii=False)}")
                                    except Exception as e:
                                        print(f"  SSE解析失败: {e}")
                    except Exception as e:
                        print(f"  解码失败: {e}")

            print(f"\n总共收到 {chunk_count} 个chunk")

    # 运行测试
    try:
        # 测试非流式请求
        test_non_stream_request()

        # 测试流式请求
        test_stream_request()

        # 测试假流式请求
        test_fake_stream_request()

        # 测试流式抗截断请求
        test_anti_truncation_stream_request()

        print("\n" + "=" * 80)
        print("测试完成")
        print("=" * 80)

    except Exception as e:
        print(f"\n❌ 测试过程中出现异常: {e}")
        import traceback
        traceback.print_exc()
