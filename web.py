"""
Main Web Integration - Integrates all routers and modules
集合router并开启主服务
"""
import os
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import get_server_host, get_server_port
from log import log

# Import managers and utilities
from src.credential_manager import credential_manager

# Import all routers
from src.router.antigravity.openai import router as antigravity_openai_router
from src.router.antigravity.gemini import router as antigravity_gemini_router
from src.router.antigravity.anthropic import router as antigravity_anthropic_router
from src.router.antigravity.model_list import router as antigravity_model_list_router
from src.router.geminicli.openai import router as geminicli_openai_router
from src.router.geminicli.gemini import router as geminicli_gemini_router
from src.router.geminicli.anthropic import router as geminicli_anthropic_router
from src.router.geminicli.model_list import router as geminicli_model_list_router
from src.task_manager import shutdown_all_tasks
from src.panel import router as panel_router

# 全局凭证管理器
global_credential_manager = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global global_credential_manager

    log.info("启动 GCLI2API 主服务")

    # 初始化配置缓存（优先执行）
    try:
        import config
        await config.init_config()
        log.info("配置缓存初始化成功")
    except Exception as e:
        log.error(f"配置缓存初始化失败: {e}")

    # 初始化全局凭证管理器（通过单例工厂）
    try:
        # credential_manager 会在第一次调用时自动初始化
        # 这里预先触发初始化以便在启动时检测错误
        await credential_manager._get_or_create()
        log.info("凭证管理器初始化成功")
    except Exception as e:
        log.error(f"凭证管理器初始化失败: {e}")
        global_credential_manager = None

    # OAuth回调服务器将在需要时按需启动

    yield

    # 清理资源
    log.info("开始关闭 GCLI2API 主服务")

    # 首先关闭所有异步任务
    try:
        await shutdown_all_tasks(timeout=10.0)
        log.info("所有异步任务已关闭")
    except Exception as e:
        log.error(f"关闭异步任务时出错: {e}")

    # 然后关闭凭证管理器
    if global_credential_manager:
        try:
            await global_credential_manager.close()
            log.info("凭证管理器已关闭")
        except Exception as e:
            log.error(f"关闭凭证管理器时出错: {e}")

    log.info("GCLI2API 主服务已停止")


# 创建FastAPI应用
app = FastAPI(
    title="GCLI2API",
    description="Gemini API proxy with OpenAI compatibility",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载路由器
# OpenAI兼容路由 - 处理OpenAI格式请求
app.include_router(geminicli_openai_router, prefix="", tags=["Geminicli OpenAI API"])

# Gemini原生路由 - 处理Gemini格式请求
app.include_router(geminicli_gemini_router, prefix="", tags=["Geminicli Gemini API"])

# Geminicli模型列表路由 - 处理Gemini格式的模型列表请求
app.include_router(geminicli_model_list_router, prefix="", tags=["Geminicli Model List"])

# Antigravity路由 - 处理OpenAI格式请求并转换为Antigravity API
app.include_router(antigravity_openai_router, prefix="", tags=["Antigravity OpenAI API"])

# Antigravity路由 - 处理Gemini格式请求并转换为Antigravity API
app.include_router(antigravity_gemini_router, prefix="", tags=["Antigravity Gemini API"])

# Antigravity模型列表路由 - 处理Gemini格式的模型列表请求
app.include_router(antigravity_model_list_router, prefix="", tags=["Antigravity Model List"])

# Antigravity Anthropic Messages 路由 - Anthropic Messages 格式兼容
app.include_router(antigravity_anthropic_router, prefix="", tags=["Antigravity Anthropic Messages"])

# Geminicli Anthropic Messages 路由 - Anthropic Messages 格式兼容 (Geminicli)
app.include_router(geminicli_anthropic_router, prefix="", tags=["Geminicli Anthropic Messages"])

# Panel路由 - 包含认证、凭证管理和控制面板功能
app.include_router(panel_router, prefix="", tags=["Panel Interface"])

# 静态文件路由 - 服务docs目录下的文件
app.mount("/docs", StaticFiles(directory="docs"), name="docs")

# 静态文件路由 - 服务front目录下的文件（HTML、JS、CSS等）
app.mount("/front", StaticFiles(directory="front"), name="front")


# 保活接口（仅响应 HEAD）
@app.head("/keepalive")
async def keepalive() -> Response:
    return Response(status_code=200)

async def main():
    """异步主启动函数"""
    from hypercorn.asyncio import serve
    from hypercorn.config import Config

    # 日志系统现在直接使用环境变量，无需初始化
    # 从环境变量或配置获取端口和主机
    port = await get_server_port()
    host = await get_server_host()

    log.info("=" * 60)
    log.info("启动 GCLI2API")
    log.info("=" * 60)
    log.info(f"控制面板: http://127.0.0.1:{port}")
    log.info("=" * 60)
    log.info("API端点:")
    log.info(f"   Geminicli (OpenAI格式): http://127.0.0.1:{port}/v1")
    log.info(f"   Geminicli (Claude格式): http://127.0.0.1:{port}/v1")
    log.info(f"   Geminicli (Gemini格式): http://127.0.0.1:{port}")
    
    log.info(f"   Antigravity (OpenAI格式): http://127.0.0.1:{port}/antigravity/v1")
    log.info(f"   Antigravity (Claude格式): http://127.0.0.1:{port}/antigravity/v1")
    log.info(f"   Antigravity (Gemini格式): http://127.0.0.1:{port}/antigravity")

    # 配置hypercorn
    config = Config()
    config.bind = [f"{host}:{port}"]
    config.accesslog = "-"
    config.errorlog = "-"
    config.loglevel = "INFO"

    # 设置连接超时
    config.keep_alive_timeout = 600  # 10分钟
    config.read_timeout = 600  # 10分钟读取超时

    await serve(app, config)


# 把這一段貼在 web.py 的最下面
if __name__ == "__main__":
    import os
    import asyncio
    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    
    # 直接從環境變量讀取端口，不依賴緩慢的 config.py
    # 如果 Docker 說 8080，這裡就立刻拿到 8080
    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("HOST", "0.0.0.0")
    
    # 打印日誌讓我們知道它活著
    print("=" * 60)
    print(f"🚀 GCLI2API 正在啟動，強制監聽: http://{host}:{port}")
    print("=" * 60)
    
    # 配置 Hypercorn 服務器
    config = Config()
    config.bind = [f"{host}:{port}"]
    config.accesslog = "-"
    config.errorlog = "-"
    config.loglevel = "INFO"
    config.keep_alive_timeout = 120
    
    
    asyncio.run(serve(app, config))
