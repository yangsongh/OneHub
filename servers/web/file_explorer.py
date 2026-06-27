import errno
import os
import re
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
MAX_UPLOAD_SIZE = 1.8 * 1024 * 1024 * 1024  # 限制单文件上传最大大小

FILE_EXPLORER_NEWS_DIR_BASENAME = '新闻'
FILE_EXPLORER_ALLOWED_IPS = []
FILE_EXPLORER_ALLOWED_WEEKDAYS = []
UPLOAD_FILE_MIN_FREE_SPACE = 0


def get_base_directory() -> str:
    """从配置中获取基础目录，增加空值校验"""
    base_dir = current_app.config.get('BASE_DIRECTORY', '')
    if not base_dir:
        logger.error("BASE_DIRECTORY 配置未设置")
        raise ValueError("BASE_DIRECTORY 配置不能为空")
    # 确保基础目录存在
    os.makedirs(base_dir, exist_ok=True)
    return os.path.abspath(base_dir)


def sanitize_filename(filename: str) -> str:
    """清理文件名，移除或替换不安全的字符"""
    if not isinstance(filename, str):
        return ''
    # 移除Windows非法字符
    for char in ILLEGAL_FILENAME_CHARS:
        filename = filename.replace(char, '')
    # 移除首尾空白字符
    filename = filename.strip()
    # 防止空文件名
    return filename if filename else 'unnamed_file'


def check_access_permission(client_ip: Optional[str], path: str = '') -> Optional[Tuple[Dict[str, Any], int]]:
    """统一权限校验函数，返回None表示有权限，否则返回错误响应数据"""
    # 新闻目录始终允许访问
    if FILE_EXPLORER_NEWS_DIR_BASENAME in path:
        return None

    # IP白名单校验
    if client_ip not in FILE_EXPLORER_ALLOWED_IPS:
        logger.warning(f'IP {client_ip} 访问被禁止：不在白名单')
        return {
            'success': False,
            'error': '禁止访问：IP不在白名单'
        }, 403

    # 星期限制校验
    weekday_num = datetime.now().isoweekday()
    if weekday_num not in FILE_EXPLORER_ALLOWED_WEEKDAYS:
        logger.warning(f'IP {client_ip} 访问被禁止：网站当前未开放')
        return {
            'success': False,
            'error': f'本网站仅在星期{FILE_EXPLORER_ALLOWED_WEEKDAYS}开放。'
        }, 400

    return None


def get_directory_items(full_path: str) -> List[Dict[str, Any]]:
    """获取目录项列表，封装为独立函数"""
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

        # 排序：文件夹在前，文件在后，按名称小写排序
        items.sort(key=lambda x: (x['type'] != 'dir', x['name'].lower()))
        return items
    except PermissionError:
        logger.error(f'无权限访问目录：{full_path}')
        raise
    except Exception as e:
        logger.error(f'读取目录 {full_path} 失败：{str(e)}')
        raise


def is_text_file(filepath: str) -> bool:
    """判断是否为文本文件"""
    ext = os.path.splitext(filepath)[1].lower()
    return ext in TEXT_FILE_EXTENSIONS


def convert_and_send_text_file(filepath: str) -> Response:
    """检测编码并转换为UTF-8后发送。若检测为UTF-8系列编码，直接返回原始数据，不重复转码"""
    try:
        with open(filepath, 'rb') as f:
            raw_data = f.read()

        # 检测编码
        detected = chardet.detect(raw_data)
        encoding = detected.get('encoding', 'utf-8')
        confidence = detected.get('confidence', 0.0)

        # 低置信度 / 非UTF8，使用ANSI兜底转UTF8
        if confidence < 0.7 or encoding is None:
            encoding = 'ansi'
            logger.warning(f'文件 {filepath} 编码检测置信度低({confidence})，使用ANSI兜底')

        # UTF-8 系列直接返回原文件，无需转码
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
    logger.info(f'IP {client_ip} 尝试访问文件浏览器')

    permission_err = check_access_permission(client_ip)
    if permission_err:
        return jsonify(permission_err[0]), permission_err[1]

    html_file = os.path.join(
        Utils.get_bundle_dir(), 'web', 'file_explorer.html'
    )
    if os.path.exists(html_file):
        return send_file(html_file)
    else:
        logger.warning("文件浏览器页面未找到")
        return "文件浏览器页面未找到，请联系管理员", 404


@file_explorer.route('/news')
def news_page():
    """新闻页路由"""
    client_ip = request.remote_addr
    logger.info(f'IP {client_ip} 尝试访问新闻播放页')

    if client_ip not in FILE_EXPLORER_ALLOWED_IPS:
        logger.warning(f'IP {client_ip} 对新闻播放页的访问被禁止：不在白名单')
        return "禁止访问：IP不在白名单", 403

    html_file = os.path.join(
        Utils.get_bundle_dir(), 'web', 'file_explorer.html'
    )
    if os.path.exists(html_file):
        return send_file(html_file)
    else:
        logger.warning("文件浏览器页面未找到")
        return "文件浏览器页面未找到，请联系管理员", 404


@file_explorer.route('/api/list')
@file_explorer.route('/api/list/<path:dirpath>')
def list_directory(dirpath=''):
    """获取目录内容（JSON格式）"""
    client_ip = request.remote_addr
    logger.info(f'IP {client_ip} 尝试获取 {"根" if not dirpath else dirpath} 目录内容')

    # 权限校验
    permission_err = check_access_permission(client_ip, dirpath)
    if permission_err:
        return jsonify(permission_err[0]), permission_err[1]

    # 获取完整路径并校验
    try:
        full_path = safe_join(get_base_directory(), dirpath)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    if full_path is None:
        return jsonify({'success': False, 'error': '路径为空'}), 400
    elif not os.path.exists(full_path):
        return jsonify({'success': False, 'error': '路径不存在'}), 400
    elif not os.path.isdir(full_path):
        return jsonify({'success': False, 'error': '路径非文件夹'}), 400

    try:
        items = get_directory_items(full_path)
        return jsonify({'success': True, 'items': items})
    except PermissionError:
        return jsonify({'success': False, 'error': '无权限访问该目录'}), 403
    except Exception as e:
        return jsonify({'success': False, 'error': f'读取目录失败: {str(e)}'}), 500


@file_explorer.route('/api/mkdir', methods=['POST'])
@file_explorer.route('/api/mkdir/<path:dirpath>', methods=['POST'])
def mkdir(dirpath=''):
    """处理新建文件夹请求"""
    client_ip = request.remote_addr

    # 权限校验
    permission_err = check_access_permission(client_ip)
    if permission_err:
        return jsonify(permission_err[0]), permission_err[1]

    # 获取要新建的文件夹名称
    data = request.get_json() or {}
    folder_name = data.get('folder_name', '').strip()
    if not folder_name:
        return jsonify({'success': False, 'error': '文件夹名称不能为空'}), 400

    folder_name = sanitize_filename(folder_name)
    logger.info(
        f'IP {client_ip} 尝试在 {"根" if not dirpath else dirpath} 下新建文件夹: {folder_name}')

    # 获取完整路径并校验
    try:
        full_path = safe_join(get_base_directory(), dirpath, folder_name)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    if full_path is None:
        return jsonify({'success': False, 'error': '非法路径，请检查文件名中是否有特殊字符'}), 400
    elif os.path.exists(full_path):
        return jsonify({'success': False, 'error': '文件夹已存在'}), 400

    # 尝试新建文件夹
    try:
        os.makedirs(full_path, exist_ok=False)
        logger.info(f'IP {client_ip} 成功新建文件夹：{full_path}')
        return jsonify({'success': True, 'message': '创建成功'}), 200
    except PermissionError:
        logger.warning(f'IP {client_ip} 新建文件夹 {full_path} 失败：权限不足')
        return jsonify({'success': False, 'error': '权限不足，无法创建文件夹'}), 403
    except Exception as e:
        logger.warning(f'IP {client_ip} 新建文件夹 {full_path} 失败：{str(e)}')
        return jsonify({'success': False, 'error': f'创建失败: {str(e)}'}), 500


@file_explorer.route('/api/file')
@file_explorer.route('/api/file/<path:filepath>')
def serve_file(filepath=''):
    """提供文件服务：浏览器支持预览的文件会内联显示，否则下载"""
    client_ip = request.remote_addr
    logger.info(f'IP {client_ip} 尝试获取文件: {filepath}')

    # 权限校验
    permission_err = check_access_permission(client_ip, filepath)
    if permission_err:
        return jsonify(permission_err[0]), permission_err[1]

    # 获取完整路径并校验
    try:
        full_path = safe_join(get_base_directory(), filepath)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    if full_path is None:
        return jsonify({'success': False, 'error': '文件路径为空'}), 400
    elif os.path.isdir(full_path):
        return jsonify({'success': False, 'error': '不支持文件夹'}), 400
    elif not os.path.exists(full_path):
        return jsonify({'success': False, 'error': '文件不存在'}), 400

    try:
        filename = os.path.basename(full_path)  # 提取文件名
        # 对文件名进行URL编码（兼容RFC 5987）
        encoded_filename = urllib.parse.quote(filename, encoding='utf-8')
        file_size = os.path.getsize(full_path)
        # 1GB 字节阈值
        GB_THRESHOLD = 1 * 1024 * 1024 * 1024

        # 文本格式，转码+浏览器内预览
        if is_text_file(full_path):
            response = convert_and_send_text_file(full_path)
            # 替换原有Content-Disposition，使用编码后的文件名
            response.headers['Content-Disposition'] = f'inline; filename="{encoded_filename}"; filename*=UTF-8\'\'{encoded_filename}'
            return response
        # 判断文件大小：小于1GB直接使用send_file
        elif file_size < GB_THRESHOLD:
            resp = send_file(full_path)
            return resp
        # 大文件自定义流式输出，直接下载
        else:
            def stream_large_file():
                chunk_size = 1024 * 1024  # 1MB分片
                with open(full_path, "rb") as f:
                    while chunk := f.read(chunk_size):
                        yield chunk

            # 响应头使用编码后的文件名，兼容latin-1编码
            headers = {
                "Content-Length": str(file_size),
                "Content-Disposition": f'attachment; filename="{encoded_filename}"; filename*=UTF-8\'\'{encoded_filename}'
            }
            return Response(stream_large_file(), headers=headers)
    except Exception as e:
        logger.error(f'提供文件 {full_path} 失败：{str(e)}')
        return jsonify({'success': False, 'error': f'文件读取失败: {str(e)}'}), 500


@file_explorer.route('/api/upload', methods=['POST'])
@file_explorer.route('/api/upload/<path:dirpath>', methods=['POST'])
def upload(dirpath=''):
    """处理文件上传请求，改为流式上传，控制内存占用"""
    client_ip = request.remote_addr

    # 权限校验
    permission_err = check_access_permission(client_ip)
    if permission_err:
        return jsonify(permission_err[0]), permission_err[1]

    # 获取上传的文件
    file = request.files.get('file')
    if file is None or not file.filename:
        return jsonify({'success': False, 'error': '未上传文件'}), 400

    filename = sanitize_filename(file.filename)
    logger.info(
        f'IP {client_ip} 尝试在 {"根" if not dirpath else dirpath} 下上传文件: {filename}')

    # 获取完整路径并校验
    try:
        full_path = safe_join(get_base_directory(), dirpath, filename)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    if full_path is None:
        return jsonify({'success': False, 'error': '非法路径，请检查文件名中是否有特殊字符'}), 400
    elif os.path.exists(full_path):
        return jsonify({'success': False, 'error': '文件已存在'}), 400

    # 服务器剩余空间检测
    target_dir = os.path.dirname(full_path)
    try:
        free_bytes = shutil.disk_usage(target_dir).free
    except Exception as e:
        logger.error(f'获取磁盘空间失败：{str(e)}')
        return jsonify({'success': False, 'error': '无法检测服务器存储空间'}), 500

    free_mb = round(free_bytes / 1024 / 1024, 2)

    # 剩余空间不足阈值
    if free_bytes < UPLOAD_FILE_MIN_FREE_SPACE:
        logger.warning(f"IP {client_ip} 上传文件失败：服务器存储空间不足，剩余 {free_mb}MB")
        return jsonify({
            'success': False,
            'error': f'服务器存储空间不足（剩余{free_mb}MB），请联系管理员'
        }), 500

    # 流式上传核心逻辑
    chunk_size = 10 * 1024 * 1024  # 10MB分块（可根据内存调整，确保总占用<300MB）
    total_size = 0
    try:
        # 分块写入文件
        with open(full_path, 'wb') as f:
            while True:
                chunk = file.stream.read(chunk_size)
                if not chunk:
                    break

                # 实时校验文件大小，避免超过限制
                total_size += len(chunk)
                if total_size > MAX_UPLOAD_SIZE:
                    # 删除已写入的临时文件
                    os.remove(full_path)
                    max_size_gb = MAX_UPLOAD_SIZE / 1024 / 1024 / 1024
                    return jsonify({
                        'success': False,
                        'error': f'文件大小超过限制（最大{max_size_gb}GB）'
                    }), 400

                # 校验剩余空间（上传中）
                if free_bytes - total_size < UPLOAD_FILE_MIN_FREE_SPACE:
                    os.remove(full_path)
                    after_mb = round(
                        (free_bytes - total_size) / 1024 / 1024, 2)
                    return jsonify({
                        'success': False,
                        'error': f'拒绝上传：上传中剩余空间仅 {after_mb}MB，低于安全阈值'
                    }), 500

                # 写入分块
                f.write(chunk)
                # 强制刷新缓冲区，释放内存
                f.flush()

        # 上传完成后校验最终大小
        after_upload_free = free_bytes - total_size
        if after_upload_free < UPLOAD_FILE_MIN_FREE_SPACE:
            os.remove(full_path)
            after_mb = round(after_upload_free / 1024 / 1024, 2)
            logger.warning(f"IP {client_ip} 上传文件被拒绝：上传后剩余空间仅 {after_mb}MB")
            return jsonify({
                'success': False,
                'error': f'拒绝上传：上传后服务器剩余空间仅 {after_mb}MB，低于安全阈值'
            }), 500

        logger.info(f'IP {client_ip} 成功上传文件：{full_path}，大小：{total_size}字节')
        return jsonify({
            'success': True,
            'message': '上传成功',
            'filename': filename,
            'size': total_size
        }), 200

    except PermissionError:
        if os.path.exists(full_path):
            os.remove(full_path)
        logger.warning(f'IP {client_ip} 上传文件 {full_path} 失败：权限不足')
        return jsonify({'success': False, 'error': '服务器错误：权限不足，无法保存文件'}), 500
    except OSError as e:
        if os.path.exists(full_path):
            os.remove(full_path)
        if e.errno == errno.ENOSPC:
            logger.warning(f'IP {client_ip} 上传文件 {full_path} 失败：磁盘空间不足')
            return jsonify({'success': False, 'error': '服务器存储空间不足'}), 500
        else:
            logger.error(f'IP {client_ip} 上传文件 {full_path} 失败：系统错误 {e}')
            return jsonify({'success': False, 'error': f'系统错误: {str(e)}'}), 500
    except Exception as e:
        if os.path.exists(full_path):
            os.remove(full_path)
        logger.warning(f'IP {client_ip} 上传文件 {full_path} 失败：{str(e)}')
        return jsonify({'success': False, 'error': f'上传失败: {str(e)}'}), 500
