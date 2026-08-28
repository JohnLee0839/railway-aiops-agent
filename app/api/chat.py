"""对话接口

提供基于 RAG Agent 的普通对话和流式对话接口
"""

import json
from fastapi import APIRouter, HTTPException
# APIRouter：相当于一个“路由注册器”，用来把不同的URL路径（如/chat）绑定到对应的处理函数。
# HTTPException：用来主动抛出HTTP错误（如500内部错误），前端会收到对应的状态码。
from sse_starlette.sse import EventSourceResponse
from app.models.request import ChatRequest, ClearRequest
from app.models.response import SessionInfoResponse, ApiResponse
from app.agent.mcp_client import format_exception_chain
from app.services.rag_agent_service import rag_agent_service
from loguru import logger

router = APIRouter()


@router.post("/chat")
async def chat(request: ChatRequest):
    """快速对话接口
    {
        "code": 200,
        "message": "success",
        "data": {
            "success": true,
            "answer": "回答内容",
            "errorMessage": null
        }
    }

    Args:
        request: 对话请求

    Returns:
        统一格式的对话响应
    """
    try:
        logger.info(f"[会话 {request.id}] 收到快速对话请求: {request.question}")
        answer = await rag_agent_service.query(
            request.question,
            session_id=request.id
        )

        logger.info(f"[会话 {request.id}] 快速对话完成")

        return {
            "code": 200,
            "message": "success",
            "data": {
                "success": True,
                "answer": answer,
                "errorMessage": None
            }
        }

    except Exception as e:
        logger.error(f"对话接口错误: {e}")
        return {
            "code": 500,
            "message": "error",
            "data": {
                "success": False,
                "answer": None,
                "errorMessage": str(e)
            }
        }


@router.post("/chat_stream")
async def chat_stream(request: ChatRequest):
    """流式对话接口（基于 RAG Agent，SSE）

    返回 SSE 格式，data 字段为 JSON：

    工具调用事件:
    event: message
    data: {"type":"tool_call","data":{"tool":"工具名","status":"start|end","input":{...}}}

    内容流式事件:
    event: message
    data: {"type":"content","data":"内容块"}

    完成事件:
    event: message
    data: {"type":"done","data":{"answer":"完整答案","tool_calls":[...]}}

    Args:
        request: 对话请求

    Returns:
        SSE 事件流
    """
    logger.info(f"[会话 {request.id}] 收到流式对话请求: {request.question}")

    async def event_generator():# 定义一个异步生成器;异步生成器则是async def + yield，可以在产生值的间隙等待异步操作
        try:
            async for chunk in rag_agent_service.query_stream(request.question, session_id=request.id):
                # 调用 rag_agent_service.query_stream()，它本身也是一个异步生成器，会不断产生一个个数据块（chunk）。这个调用会触发 RAG Agent 的真正执行流程：检索文档、调用大模型、逐步生成回答。
                chunk_type = chunk.get("type", "unknown")
                chunk_data = chunk.get("data", None)

                # 处理调试类型消息（新增）
                if chunk_type == "debug":
                    # 调试信息，可以选择发送或忽略
                    yield {
                        "event": "message",
                        "data": json.dumps({
                            "type": "debug",
                            "node": chunk.get("node", "unknown"),
                            "message_type": chunk.get("message_type", "unknown")
                        }, ensure_ascii=False)
                    }
                elif chunk_type == "tool_call":# 当 RAG Agent 决定调用外部工具（比如搜索互联网、计算器等）时，会发出此事件
                    # 发送工具调用事件（可选，前端可以显示工具调用状态）
                    yield {
                        "event": "message",
                        "data": json.dumps({
                            "type": "tool_call",
                            "data": chunk_data# 包含工具名、输入参数、状态（开始/结束）等信息
                        }, ensure_ascii=False)
                    }
                elif chunk_type == "search_results":
                    # 当 Agent 从向量数据库检索到相关文档片段时，会把这些“参考材料”推送出来。
                    # 虽然前端可能不直接展示原始文档，但可以用于调试或显示“参考了哪些资料”。
                    yield {
                        "event": "message",
                        "data": json.dumps({
                            "type": "search_results",
                            "data": chunk_data
                        }, ensure_ascii=False)
                    }
                elif chunk_type == "content":
                    # chunk_data 就是大模型生成的一个文本片段,每收到一个，前端就把这个片段追加到对话框里
                    # - 关键：data 字段被构造成一个 JSON 对象，里面再嵌套一个 data 字段来存放实际文本
                    yield {
                        "event": "message",
                        "data": json.dumps({
                            "type": "content",
                            "data": chunk_data
                        }, ensure_ascii=False)
                    }
                elif chunk_type == "complete":
                    # 发送完成信号
                    yield {
                        "event": "message",
                        "data": json.dumps({
                            "type": "done",
                            "data": chunk_data
                        }, ensure_ascii=False)
                    }
                elif chunk_type == "error":
                    # 发送错误信息
                    yield {
                        "event": "message",
                        "data": json.dumps({
                            "type": "error",
                            "data": str(chunk_data)
                        }, ensure_ascii=False)
                    }

            logger.info(f"[会话 {request.id}] 流式对话完成")

        except Exception as e:
            logger.error(f"流式对话接口错误: {format_exception_chain(e)}")
            yield {
                "event": "message",
                "data": json.dumps({
                    "type": "error",
                    "data": str(e)
                }, ensure_ascii=False)
            }
        # 如果整个生成过程中出现意外异常，捕获后记录错误，并且仍然通过 SSE 发送一个 error 事件给前端，让前端知道出了问题

    return EventSourceResponse(event_generator())


@router.post("/chat/clear", response_model=ApiResponse)
async def clear_session(request: ClearRequest):
    """清空会话历史

    Args:
        request: 清空请求

    Returns:
        操作结果
    """
    try:
        success = rag_agent_service.clear_session(request.session_id)
        logger.info(f"清空会话: {request.session_id}, 结果: {success}")

        return ApiResponse(
            status="success" if success else "error",
            message="会话已清空" if success else "清空会话失败",
            data=None
        )

    except Exception as e:
        logger.error(f"清空会话错误: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chat/session/{session_id}", response_model=SessionInfoResponse)
async def get_session_info(session_id: str) -> SessionInfoResponse:
    """查询会话历史

    Args:
        session_id: 会话 ID

    Returns:
        会话信息
    """
    try:
        history = rag_agent_service.get_session_history(session_id)

        return SessionInfoResponse(
            session_id=session_id,
            message_count=len(history),
            history=history
        )

    except Exception as e:
        logger.error(f"获取会话信息错误: {e}")
        raise HTTPException(status_code=500, detail=str(e))
