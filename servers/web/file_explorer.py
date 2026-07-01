import os
import errno
import shutil
import chardet
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime

import urllib.parse
from werkzeug.security import safe_join
from flask import Blueprint, Response, make_response, send_file, jsonify, current_app, request
from utils.utils_lib import LoggerManager, Utils

# 初始化蓝图
file_explorer = Blueprint('file_explorer', __name__, url_prefix='/')
logger: LoggerManager = LoggerManager(no_file_handler=True)

# 配置常量
TEXT_FILE_EXTENSIONS = {
    '.txt', '.log', '.ini', '.cfg', '.conf', '.csv', '.md', '.xml', '.json',
    '.yaml', '.yml', '.html', '.htm', '.css', '.js', '.py', '.java', '.c',
    '.cpp', '.h', '.sql', '.bat', '.sh', '.rtf', '.inf', '.reg', '.nfo',
    '.ps1', '.vbs', '.lua', '.r', '.jl', '.scala', '.kt', '.groovy', '.dart',
    '.fs', '.clj', '.hs', '.erl', '.ex', '.cr', '.nim', '.zig'
}
ILLEGAL_FILENAME_CHARS = r'<>:"/\\|?*'

NEWS_DIR_BASENAME = '新闻'
ALLOWED_IPS = []
ALLOWED_WEEKDAYS = []
UPLOAD_FILE_MIN_FREE_SPACE = 0
MAX_UPLOAD_SIZE = 10 * 1024 * 1024 * 1024

# 分片上传配置
CHUNK_SIZE = 1024 * 1024 * 1024  # 和前端分片大小保持一致
CHUNK_TMP_DIR = ".chunk_tmp"  # 分片临时存储目录


def get_base_directory() -> str:
    """从配置中获取基础目录, 增加空值校验"""
    base_dir = current_app.config.get('BASE_DIRECTORY', '')
    if not base_dir:
        logger.error("BASE_DIRECTORY 配置未设置")
        raise ValueError("BASE_DIRECTORY 配置不能为空")
    # 确保基础目录存在
    os.makedirs(base_dir, exist_ok=True)
    return os.path.abspath(base_dir)


def sanitize_filename(filename: str) -> str:
    """清理文件名, 移除或替换不安全的字符"""
    if not isinstance(filename, str):
        return ''
    # 移除Windows非法字符
    for char in ILLEGAL_FILENAME_CHARS:
        filename = filename.replace(char, '')
    # 移除首尾空白字符
    filename = filename.strip()
    # 防止空文件名
    return filename if filename else 'unnamed'


def check_access_permission(client_ip: Optional[str], path: str = '') -> Optional[Tuple[Dict[str, Any], int]]:
    """统一权限校验函数, 返回None表示有权限, 否则返回错误响应数据"""
    # 新闻目录始终允许访问
    if NEWS_DIR_BASENAME in path:
        return None

    # IP白名单校验
    if client_ip not in ALLOWED_IPS:
        logger.warning(f'IP {client_ip} 访问被禁止：不在白名单')
        return {
            'success': False,
            'message': '禁止访问：IP不在白名单'
        }, 403

    # 星期限制校验
    weekday_num = datetime.now().isoweekday()
    if weekday_num not in ALLOWED_WEEKDAYS:
        logger.warning(f'IP {client_ip} 访问被禁止：网站当前未开放')
        return {
            'success': False,
            'message': f'本网站仅在星期{ALLOWED_WEEKDAYS}开放。'
        }, 400

    return None


def get_directory_items(full_path: str) -> List[Dict[str, Any]]:
    """获取目录项列表, 封装为独立函数"""
    items = []
    try:
        for item in os.listdir(full_path):
            item_path = os.path.join(full_path, item)
            # 跳过隐藏文件/目录（可选）
            if item.startswith('.'):
                continue

            item_info = {
                'name': item,
                'type': 'dir' if os.path.isdir(item_path) else 'file',
                'size': os.path.getsize(item_path) if os.path.isfile(item_path) else None,
                'modified': datetime.fromtimestamp(os.path.getmtime(item_path)).strftime('%Y-%m-%d %H:%M:%S')
            }
            items.append(item_info)

        # 排序：文件夹在前, 文件在后, 按名称小写排序
        # items.sort(key=lambda x: (x['type'] != 'dir', x['name'].lower()))
        return items
    except PermissionError:
        logger.error(f'无权限访问目录：{full_path}')
        raise
    except Exception as e:
        logger.error(f'读取目录 {full_path} 失败：{str(e)}')
        raise


def get_chunk_temp_root() -> str:
    """获取分片临时根目录"""
    base = get_base_directory()
    tmp_root = os.path.join(base, CHUNK_TMP_DIR)
    os.makedirs(tmp_root, exist_ok=True)
    return tmp_root


def get_chunk_dir(file_id: str, dirpath: str, filename: str) -> str:
    """获取单个文件分片存放目录"""
    tmp_root = get_chunk_temp_root()
    # 路径哈希隔离, 避免冲突
    safe_dir = dirpath.replace("/", "_")
    chunk_folder = os.path.join(tmp_root, f"{safe_dir}_{filename}_{file_id}")
    os.makedirs(chunk_folder, exist_ok=True)
    return chunk_folder


def clean_chunk_folder(chunk_dir: str):
    """清空分片临时目录"""
    if not os.path.exists(chunk_dir):
        return
    for f in os.listdir(chunk_dir):
        os.remove(os.path.join(chunk_dir, f))
    os.rmdir(chunk_dir)


def is_text_file(filepath: str) -> bool:
    """判断是否为文本文件"""
    ext = os.path.splitext(filepath)[1].lower()
    return ext in TEXT_FILE_EXTENSIONS


def convert_and_send_text_file(filepath: str) -> Response:
    """检测编码并转换为UTF-8后发送。若检测为UTF-8系列编码, 直接返回原始数据, 不重复转码"""
    try:
        with open(filepath, 'rb') as f:
            raw_data = f.read()

        # 检测编码
        detected = chardet.detect(raw_data)
        encoding = detected.get('encoding', 'utf-8')
        confidence = detected.get('confidence', 0.0)

        # 低置信度 / 非UTF8, 使用ANSI兜底转UTF8
        if confidence < 0.7 or encoding is None:
            encoding = 'ansi'
            logger.warning(f'文件 {filepath} 编码检测置信度低({confidence}), 使用ANSI兜底')

        # UTF-8 系列直接返回原文件, 无需转码
        if encoding and encoding.lower() in ("utf-8", "utf-8-sig") and confidence >= 0.7:
            utf8_content = raw_data
        else:
            # 解码并重新编码为UTF-8
            text_content = raw_data.decode(encoding, errors='replace')
            utf8_content = text_content.encode('utf-8')

        response = make_response(utf8_content)
        response.headers['Content-Type'] = 'text/plain; charset=utf-8'
        filename = os.path.basename(filepath)
        response.headers['Content-Disposition'] = f'inline; filename="{filename}"'
        return response
    except Exception as e:
        logger.error(f'转换文本文件 {filepath} 失败：{str(e)}')
        return make_response(f'文件处理失败：{str(e)}', 500)


@file_explorer.route('/')
def index():
    """主页路由"""
    client_ip = request.remote_addr
    permission_err = check_access_permission(client_ip)
    if permission_err:
        return jsonify(permission_err[0]), permission_err[1]

    logger.info(f'IP {client_ip} 尝试访问文件浏览器')

    html_file = os.path.join(
        Utils.get_bundle_dir(), 'web', 'file_explorer.html'
    )
    if os.path.exists(html_file):
        return send_file(html_file)
    else:
        logger.warning("文件浏览器页面未找到")
        return "文件浏览器页面未找到, 请联系管理员", 404


@file_explorer.route('/news')
def news_page():
    """新闻页路由"""
    client_ip = request.remote_addr
    if client_ip not in ALLOWED_IPS:
        logger.warning(f'IP {client_ip} 对新闻播放页的访问被禁止：不在白名单')
        return "禁止访问：IP不在白名单", 403

    logger.info(f'IP {client_ip} 尝试访问新闻播放页')

    html_file = os.path.join(
        Utils.get_bundle_dir(), 'web', 'file_explorer.html'
    )
    if os.path.exists(html_file):
        return send_file(html_file)
    else:
        logger.warning("文件浏览器页面未找到")
        return "文件浏览器页面未找到, 请联系管理员", 404


@file_explorer.route('/api/list')
@file_explorer.route('/api/list/<path:dirpath>')
def list_directory(dirpath=''):
    """获取目录内容（JSON格式）"""
    client_ip = request.remote_addr
    try:
        permission_err = check_access_permission(client_ip, dirpath)
        if permission_err:
            return jsonify(permission_err[0]), permission_err[1]

        logger.info(
            f'IP {client_ip} 尝试获取 {"根" if not dirpath else dirpath} 目录内容')

        # 获取完整路径并校验
        # dirpath = sanitize_filename(dirpath)
        full_path = safe_join(get_base_directory(), dirpath)
        if not full_path:
            return jsonify({'success': False, 'message': '路径为空'}), 400
        elif not os.path.exists(full_path):
            return jsonify({'success': False, 'message': '路径不存在'}), 404
        elif not os.path.isdir(full_path):
            return jsonify({'success': False, 'message': '路径非文件夹'}), 400

        # 尝试获取目录内容
        try:
            items = get_directory_items(full_path)
            return jsonify({'success': True, 'items': items})
        except PermissionError:
            return jsonify({'success': False, 'message': '无权限访问该目录'}), 403

    except Exception as e:
        logger.warning(
            f'IP {client_ip} 获取 {"根" if not dirpath else dirpath} 目录内容失败：{e}')
        return jsonify({'success': False, 'message': f'获取目录内容失败: {e}'}), 500


@file_explorer.route('/api/mkdir', methods=['POST'])
@file_explorer.route('/api/mkdir/<path:dirpath>', methods=['POST'])
def mkdir(dirpath=''):
    """处理新建文件夹请求"""
    client_ip = request.remote_addr
    try:
        permission_err = check_access_permission(client_ip)
        if permission_err:
            return jsonify(permission_err[0]), permission_err[1]

        # 获取要新建的文件夹名称
        data = request.get_json() or {}
        folder_name = data.get('folder_name', '').strip()
        if not folder_name:
            return jsonify({'success': False, 'message': '文件夹名称不能为空'}), 400

        logger.info(
            f'IP {client_ip} 尝试在 {"根" if not dirpath else dirpath} 下新建文件夹: {folder_name}')

        # 获取完整路径并校验
        folder_name = sanitize_filename(folder_name)
        full_path = safe_join(get_base_directory(), dirpath, folder_name)
        if not full_path:
            return jsonify({'success': False, 'message': '非法路径, 请检查文件名中是否有特殊字符'}), 400
        elif os.path.exists(full_path):
            return jsonify({'success': False, 'message': '文件夹已存在'}), 400

        # 尝试新建文件夹
        try:
            os.makedirs(full_path, exist_ok=False)
            logger.info(f'IP {client_ip} 成功新建文件夹：{full_path}')
            return jsonify({'success': True, 'message': '创建成功'}), 200
        except PermissionError:
            logger.warning(f'IP {client_ip} 新建文件夹 {full_path} 失败：权限不足')
            return jsonify({'success': False, 'message': '权限不足, 无法创建文件夹'}), 403

    except Exception as e:
        logger.warning(
            f'IP {client_ip} 在 {"根" if not dirpath else dirpath} 下新建文件夹失败：{e}')
        return jsonify({'success': False, 'message': f'创建文件夹失败: {e}'}), 500


@file_explorer.route('/api/file')
@file_explorer.route('/api/file/<path:filepath>')
def serve_file(filepath=''):
    """提供文件服务：浏览器支持预览的文件会内联显示, 否则下载"""
    client_ip = request.remote_addr
    try:
        permission_err = check_access_permission(client_ip, filepath)
        if permission_err:
            return jsonify(permission_err[0]), permission_err[1]

        logger.info(f'IP {client_ip} 尝试获取文件: {filepath}')

        # 获取完整路径并校验
        full_path = safe_join(get_base_directory(), filepath)
        if not full_path:
            return jsonify({'success': False, 'message': '文件路径为空'}), 400
        elif os.path.isdir(full_path):
            return jsonify({'success': False, 'message': '不支持文件夹'}), 400
        elif not os.path.exists(full_path):
            return jsonify({'success': False, 'message': '文件不存在'}), 400

        # 获取文件名、文件大小
        filename = os.path.basename(full_path)
        file_size = os.path.getsize(full_path)
        # 对文件名进行URL编码（兼容RFC 5987）
        encoded_filename = urllib.parse.quote(filename, encoding='utf-8')
        # 文件大小阈值
        FILE_SIZE_THRESHOLD = 2 * 1024 * 1024 * 1024

        # 文本格式, 转码+浏览器内预览
        if is_text_file(full_path):
            response = convert_and_send_text_file(full_path)
            # 替换原有Content-Disposition, 使用编码后的文件名
            response.headers['Content-Disposition'] = f'inline; filename="{encoded_filename}"; filename*=UTF-8\'\'{encoded_filename}'
            return response
        # 判断文件大小：小于阈值直接使用send_file
        elif file_size < FILE_SIZE_THRESHOLD:
            resp = send_file(full_path)
            return resp
        # 大文件流式下载
        else:
            def stream_large_file():
                chunk_size = 1024 * 1024  # 1MB分片
                with open(full_path, "rb") as f:
                    while chunk := f.read(chunk_size):
                        yield chunk

            # 响应头使用编码后的文件名, 兼容latin-1编码
            headers = {
                "Content-Length": str(file_size),
                "Content-Disposition": f'attachment; filename="{encoded_filename}"; filename*=UTF-8\'\'{encoded_filename}'
            }
            return Response(stream_large_file(), headers=headers)

    except Exception as e:
        logger.error(f'IP 提供文件 {filepath} 失败：{e}')
        return jsonify({'success': False, 'message': f'提供文件失败: {e}'}), 500


@file_explorer.route('/api/upload', methods=['POST'])
@file_explorer.route('/api/upload/<path:dirpath>', methods=['POST'])
def upload(dirpath=''):
    """处理文件上传请求"""
    client_ip = request.remote_addr
    try:
        permission_err = check_access_permission(client_ip)
        if permission_err:
            return jsonify(permission_err[0]), permission_err[1]

        # 获取上传的文件
        file = request.files.get('file')
        if not file or not file.filename:
            return jsonify({'success': False, 'message': '未上传文件'}), 400

        # 获取文件名称
        filename = sanitize_filename(file.filename)

        # 获取文件大小
        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)  # 重置文件指针, 否则后续保存会是空文件

        logger.info(
            f'IP {client_ip} 尝试在 {"根" if not dirpath else dirpath} 下上传文件: {filename}, 大小: {file_size}字节')

        # 获取完整路径并校验
        full_path = safe_join(get_base_directory(), dirpath, filename)
        if not full_path:
            return jsonify({'success': False, 'message': '非法路径, 请检查文件名中是否有特殊字符'}), 400
        elif os.path.exists(full_path):
            return jsonify({'success': False, 'message': '文件已存在'}), 400

        # 服务器剩余空间检测
        try:
            # 获取当前剩余空间
            target_dir = os.path.dirname(full_path)
            free_bytes = shutil.disk_usage(target_dir).free
            free_mb = round(free_bytes / 1024 / 1024, 2)

            # 剩余空间不足阈值
            if free_bytes < UPLOAD_FILE_MIN_FREE_SPACE:
                logger.warning(
                    f"IP {client_ip} 上传文件失败：服务器存储空间不足, 剩余 {free_mb}MB")
                return jsonify({
                    'success': False,
                    'message': f'服务器存储空间不足（剩余{free_mb}MB）, 请联系管理员'
                }), 500

            # 上传后剩余空间检测
            after_upload_free = max(free_bytes - file_size, 0)
            if after_upload_free < UPLOAD_FILE_MIN_FREE_SPACE:
                after_mb = round(after_upload_free / 1024 / 1024, 2)
                logger.warning(
                    f"IP {client_ip} 上传文件被拒绝：上传后服务器剩余空间仅 {after_mb}MB, 低于安全阈值")
                return jsonify({
                    'success': False,
                    'error': f'拒绝上传：上传后服务器剩余空间仅 {after_mb}MB, 低于安全阈值'
                }), 500

        except Exception as e:
            logger.error(f'检测磁盘剩余空间失败：{e}')
            return jsonify({'success': False, 'message': '无法检测服务器剩余存储空间'}), 500

        # 尝试写入上传的文件
        try:
            file.save(full_path)
            logger.info(f'IP {client_ip} 成功上传文件：{full_path}, 大小：{file_size}字节')
            return jsonify({'success': True, 'message': '上传成功'}), 200

        except PermissionError:
            logger.warning(
                f'IP {client_ip} 上传文件 {full_path} 失败：服务器错误, 权限不足, 无法保存文件')
            return jsonify({'success': False, 'message': '服务器错误：权限不足, 无法保存文件'}), 500

        except OSError as e:
            # 清理文件
            if os.path.exists(full_path):
                os.remove(full_path)
            # 磁盘空间不足
            if e.errno == errno.ENOSPC:
                logger.warning(f'IP {client_ip} 上传文件 {full_path} 失败：磁盘空间不足')
                return jsonify({'success': False, 'message': '服务器存储空间不足'}), 500
            # 其他错误
            else:
                logger.error(f'IP {client_ip} 上传文件 {full_path} 失败：系统错误 {e}')
                return jsonify({'success': False, 'message': f'系统错误: {e}'}), 500

    except Exception as e:
        logger.warning(
            f'IP {client_ip} 在 {"根" if not dirpath else dirpath} 下上传文件失败：{e}')
        return jsonify({'success': False, 'message': f'上传失败: {e}'}), 500


@file_explorer.route('/api/chunk', methods=['POST'])
@file_explorer.route('/api/chunk/<path:dirpath>', methods=['POST'])
def upload_chunk(dirpath=''):
    """单分片上传接口"""
    client_ip = request.remote_addr
    chunk_dir = ''
    try:
        permission_err = check_access_permission(client_ip)
        if permission_err:
            return jsonify(permission_err[0]), permission_err[1]

        # 获取参数并校验
        filename = request.args.get("filename", "")
        filename = sanitize_filename(filename)
        chunk_idx = int(request.args.get("chunk", 0))
        # total_chunks = int(request.args.get("total", 1))
        file_id = request.args.get("fileId", "")
        if not all([filename, file_id]):
            return jsonify({"success": False, "message": "参数缺失"}), 400

        # 获取分片文件
        chunk_file = request.files.get("chunk")
        if not chunk_file or not chunk_file.filename:
            return jsonify({"success": False, "message": "未上传分片文件"}), 400

        # 获取分片文件大小
        chunk_file.seek(0, 2)
        file_size = chunk_file.tell()
        chunk_file.seek(0)  # 重置文件指针, 否则后续保存会是空文件

        logger.info(
            f'IP {client_ip} 尝试在 {"根" if not dirpath else dirpath} 下上传 {filename} 的第 {chunk_idx} 段分片文件, 大小: {file_size}字节')

        # 获取分片保存路径并校验
        chunk_dir = get_chunk_dir(file_id, dirpath, filename)
        chunk_save_path = safe_join(chunk_dir, f"{chunk_idx}.part")
        if not chunk_save_path:
            clean_chunk_folder(chunk_dir)
            return jsonify({'success': False, 'message': '非法路径, 请检查文件名中是否有特殊字符'}), 400

        # 保存分片
        chunk_file.save(chunk_save_path)
        logger.info(
            f'IP {client_ip} 成功上传 {filename} 的第 {chunk_idx} 段分片文件, 大小：{file_size}字节')
        return jsonify({"success": True, "message": f"分片{chunk_idx}上传完成"}), 200

    except Exception as e:
        # 清理分片临时文件
        clean_chunk_folder(chunk_dir)
        logger.warning(
            f'IP {client_ip} 在 {"根" if not dirpath else dirpath} 下上传分片文件失败：{e}')
        return jsonify({'success': False, 'message': f'上传失败: {e}'}), 500


@file_explorer.route('/api/chunk_merge', methods=['POST'])
@file_explorer.route('/api/chunk_merge/<path:dirpath>', methods=['POST'])
def merge_chunk(dirpath=''):
    """合并分片生成完整文件"""
    client_ip = request.remote_addr
    chunk_dir = ''
    try:
        permission_err = check_access_permission(client_ip)
        if permission_err:
            return jsonify(permission_err[0]), permission_err[1]

        # 获取参数并校验
        filename = request.args.get("filename", "")
        filename = sanitize_filename(filename)
        total_chunks = int(request.args.get("total", 1))
        file_id = request.args.get("fileId", "")
        if not all([filename, file_id]):
            return jsonify({"success": False, "message": "参数缺失"}), 400

        logger.info(
            f'IP {client_ip} 尝试在 {"根" if not dirpath else dirpath} 下合并 {filename} 的分片文件, 总分片数: {total_chunks}, 文件ID: {file_id}')

        # 校验分片完整性
        chunk_dir = get_chunk_dir(file_id, dirpath, filename)
        chunk_files = [safe_join(chunk_dir, f"{i}.part")
                       for i in range(total_chunks)]
        for cf in chunk_files:
            if not cf:
                clean_chunk_folder(chunk_dir)
                return jsonify({'success': False, 'message': '非法路径, 请检查文件名中是否有特殊字符'}), 400
            if not os.path.exists(cf):
                clean_chunk_folder(chunk_dir)
                return jsonify({"success": False, "message": f"分片缺失：{cf}"}), 400

        # 获取目标文件路径并校验
        full_path = safe_join(get_base_directory(), dirpath, filename)
        if not full_path:
            clean_chunk_folder(chunk_dir)
            return jsonify({'success': False, 'message': '非法路径, 请检查文件名中是否有特殊字符'}), 400
        elif os.path.exists(full_path):
            clean_chunk_folder(chunk_dir)
            return jsonify({'success': False, 'message': '文件已存在'}), 400

        # 合并分片写入最终文件
        with open(full_path, "wb") as out_f:
            for chunk_path in chunk_files:
                if not chunk_path:
                    clean_chunk_folder(chunk_dir)
                    return jsonify({'success': False, 'message': '非法路径, 请检查文件名中是否有特殊字符'}), 400
                with open(chunk_path, "rb") as in_f:
                    out_f.write(in_f.read())

        # 清理分片临时文件
        clean_chunk_folder(chunk_dir)
        logger.info(f"IP {client_ip} 分片合并完成, 生成文件：{full_path}")
        return jsonify({"success": True, "message": "分片合并完成"}), 200

    except Exception as e:
        # 清理分片临时文件
        clean_chunk_folder(chunk_dir)
        logger.error(
            f'IP {client_ip} {"根" if not dirpath else dirpath} 下的分片合并失败：{e}')
        return jsonify({"success": False, "message": f"分片合并失败: {e}"}), 500
