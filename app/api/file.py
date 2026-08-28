"""文件上传接口模块"""

from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
# APIRouter：路由注册器，用来把接口函数挂载到主应用上，相当于给接口“登记门牌号”。
# File：依赖注入工具，告诉FastAPI这个参数是从请求的“文件”部分获取的。
# HTTPException：HTTP异常类，当请求出错时，用它抛出标准的HTTP错误状态码（如400、500）。
# UploadFile：上传文件对象，FastAPI提供的类型，封装了上传文件的内容、文件名等信息。
from fastapi.responses import JSONResponse
# 用于返回JSON格式的HTTP响应，这样前端（如网页）能方便解析结果
from app.services.vector_index_service import vector_index_service
from loguru import logger

router = APIRouter() # 创建一个路由实例，后续用@router.post装饰器来注册接口。

# 文件上传后存储的路径
UPLOAD_DIR = Path("./uploads")
# 支持的文件类型
ALLOWED_EXTENSIONS = ["txt", "md"]
# 单个文件支持最大大小
MAX_FILE_SIZE = 10 * 1024 * 1024  #单个文件最大大小，10MB，超过会报错。


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):# 这是一个异步函数（用async def定义），它处理POST请求，路径为/upload。
    # file: UploadFile = File(...)：参数file的类型是UploadFile，且必须从请求的“文件”字段获取（...表示必填）
    """
    上传文件并自动创建向量索引

    Args:
        file: 上传的文件

    Returns:
        JSONResponse: 上传结果
    """
    try:
        # 1. 验证文件
        if not file.filename:
            raise HTTPException(status_code=400, detail="文件名不能为空")# 首先判断文件名是否为空，如果为空则抛出HTTPException，状态码400（客户端错误），并给出错误信息。

        # 2. 规范化文件名（去除空格，处理 Windows 上传的文件）
        safe_filename = _sanitize_filename(file.filename) #调用辅助函数_sanitize_filename，清理文件名，比如把空格替换成下划线，移除\ / : * ? " < > |等非法字符

        # 3. 验证文件扩展名
        file_extension = _get_file_extension(safe_filename)# _get_file_extension获取文件后缀（如“txt”），然后检查是否在允许列表中
        if file_extension not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件格式，仅支持: {', '.join(ALLOWED_EXTENSIONS)}",
            )

        # 4. 创建上传目录
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)# 确保uploads目录存在，如果不存在则创建它

        # 5. 保存文件
        file_path = UPLOAD_DIR / safe_filename # 用/运算符拼接目录和文件名，得到完整的保存路径（Path对象）

        # 如果文件已存在，先删除旧文件（实现覆盖更新）
        if file_path.exists():
            logger.info(f"文件已存在，将覆盖: {file_path}")
            file_path.unlink()
            # 检查该文件是否已经存在，若存在则记录日志并删除（unlink()），实现覆盖上传

        # 读取并保存文件内容 异步读取上传文件的全部内容到内存（await表示等待I/O操作完成
        content = await file.read()

        # 验证文件大小
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"文件大小超过限制（最大 {MAX_FILE_SIZE} 字节）")

        file_path.write_bytes(content)# 将字节内容写入磁盘文件

        logger.info(f"文件上传成功: {file_path}")

        # 5. 自动创建向量索引
        try:
            logger.info(f"开始为上传文件创建向量索引: {file_path}")
            vector_index_service.index_single_file(str(file_path))
            logger.info(f"向量索引创建成功: {file_path}")
        except Exception as e:
            logger.error(f"向量索引创建失败: {file_path}, 错误: {e}")
            # 注意：即使索引失败，文件上传仍然成功，只是记录错误日志

        # 6. 返回响应 前端可根据此信息判断上传成功
        return JSONResponse(
            status_code=200,
            content={
                "code": 200,
                "message": "success",
                "data": {
                    "filename": safe_filename,
                    "file_path": str(file_path),
                    "size": len(content),
                },
            },
        )

    except HTTPException:# 捕获到HTTPException时，直接重新抛出（让FastAPI处理），不做转换
        raise
    except Exception as e:
        logger.error(f"文件上传失败: {e}")
        raise HTTPException(status_code=500, detail=f"文件上传失败: {e}")


@router.post("/index_directory")
async def index_directory(directory_path: str = None):#定义另一个POST接口，路径为/index_directory，接收一个可选参数directory_path（字符串），默认为None。这个接口用于批量索引整个目录下的文件，而不是单个上传。
    """
    索引指定目录下的所有文件

    Args:
        directory_path: 目录路径（可选，默认使用 uploads 目录）

    Returns:
        JSONResponse: 索引结果
    """
    try:
        logger.info(f"开始索引目录: {directory_path or 'uploads'}")

        # 执行索引
        result = vector_index_service.index_directory(directory_path)# 该方法会遍历目录中的所有支持文件，逐一生成索引，并返回一个结果对象（包含成功/失败统计等

        return JSONResponse(
            status_code=200,
            content={
                "code": 200,
                "message": "success" if result.success else "partial_success",
                "data": result.to_dict(),
            },
        )
    #返回JSON响应，状态码200，消息根据结果是否完全成功而定，数据部分调用result.to_dict()转换成字典

    except Exception as e:
        logger.error(f"索引目录失败: {e}")
        raise HTTPException(status_code=500, detail=f"索引目录失败: {e}")


def _get_file_extension(filename: str) -> str:
    """
    获取文件扩展名

    Args:
        filename: 文件名

    Returns:
        str: 扩展名（小写，不含点）
    """
    parts = filename.rsplit(".", 1)
    if len(parts) == 2:
        return parts[1].lower()
    return ""


def _sanitize_filename(filename: str) -> str:
    """
    规范化文件名，去除空格和特殊字符

    Args:
        filename: 原始文件名

    Returns:
        str: 规范化后的文件名
    """
    # 去除空格
    sanitized = filename.replace(" ", "_")
    # 去除其他可能导致问题的字符
    for char in ['\\', '/', ':', '*', '?', '"', '<', '>', '|']:
        sanitized = sanitized.replace(char, "_")
    return sanitized
