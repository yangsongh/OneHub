from functools import wraps
import os
import json5
from flask import Blueprint, Response, request, jsonify, send_from_directory
from utils.utils_lib import LoggerManager

WEB_FOLDER = 'localproxy'
ICONS_FOLDER = os.path.join(WEB_FOLDER, 'icons')
NOTICE_BOARD_FOLDER = os.path.join(WEB_FOLDER, 'noticeboard')

CONFIG_FILE = 'config_client.jsonc'
CONFIG_OLD_FILE = 'config_client_old.jsonc'

VERSION_CODE = 202606121

LOCALPROXY_USERNAME = 'user'
LOCALPROXY_PASSWORD = '123456'

localproxy_server = Blueprint(
    'localproxy_server', __name__, url_prefix='/localproxy'
)
logger: LoggerManager = LoggerManager(no_file_handler=True)


def require_auth(f):
    """
    认证装饰器 - 用于保护需要认证的路由
    """
    def check_auth(username, password):
        authed = username == LOCALPROXY_USERNAME and password == LOCALPROXY_PASSWORD
        if not authed:
            logger.warning(
                f"IP {request.remote_addr} 认证失败：用户名 {username}, 密码 {password}")
        return authed

    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return Response(
                '需要认证',
                401,
                {'WWW-Authenticate': 'Basic realm="Login Required"'}
            )
        return f(*args, **kwargs)

    return decorated_function


@localproxy_server.route('/')
@require_auth
def index():
    """主页内容"""
    index_path = os.path.join(WEB_FOLDER, 'index.html')
    logger.info(f"IP {request.remote_addr} 访问受保护的主页")

    if os.path.exists(index_path):
        return send_from_directory(WEB_FOLDER, 'index.html')
    else:
        return "主页文件未找到，请联系管理员", 404


@localproxy_server.route('/get_config', methods=['GET'])
def get_config():
    """
    GET /get_config
    读取本地客户端配置文件，并根据ver参数返回不同版本。

    /get_config?ver=最新版本号: 返回config_client.jsonc
    /get_config?ver=其他值或不带version，且旧配置文件存在: 返回config_client_old.jsonc
    """
    try:
        client_ip = request.remote_addr
        logger.info(f"IP {client_ip} 请求配置文件，参数: {dict(request.args)}")

        # 获取版本参数
        ver = request.args.get('ver', '')

        # 根据版本参数选择配置文件
        config_file = CONFIG_FILE

        # 旧配置文件存在就用旧的，不存在就继续用新的
        if ver != str(VERSION_CODE) and os.path.exists(CONFIG_OLD_FILE):
            config_file = CONFIG_OLD_FILE

        if not os.path.exists(config_file):
            error_msg = f"错误：配置文件 '{config_file}' 不存在。"
            logger.error(error_msg)
            return jsonify({"error": error_msg}), 500

        with open(config_file, 'r', encoding='utf-8') as f:
            config_data = json5.load(f)

        logger.info(f"配置文件 '{config_file}' 已成功发送给客户端：{client_ip}")
        return jsonify(config_data), 200

    except Exception as e:
        error_msg = f"错误：解析配置文件时发生错误：{e}"
        logger.error(error_msg, exc_info=True)
        return jsonify({"error": error_msg}), 500


@localproxy_server.route('/post_activate', methods=['POST'])
def post_activate():
    """API接口：接收客户端设备激活请求"""
    try:
        data = request.get_json()
        device_id = data.get('device_id')
        if not device_id:
            logger.warning("无效的设备激活请求（缺少设备ID）")
            return jsonify({"error": "invalid_request"}), 400

        logger.warning(
            f"收到来自 {request.remote_addr} 的设备激活请求 - 设备ID: {device_id}")
        return jsonify({"status": "success"}), 200

    except Exception as e:
        logger.error(f"处理激活请求时发生错误: {str(e)}", exc_info=True)
        return jsonify({"error": "server_error"}), 500


@localproxy_server.route('/downloads/<path:filename>')
def download_file(filename):
    """
    GET /downloads/<filename>
    提供 web/downloads 文件夹下的文件下载服务。
    """
    downloads_folder = os.path.join(WEB_FOLDER, 'downloads')
    target_file = os.path.join(downloads_folder, filename)
    client_ip = request.remote_addr

    logger.info(
        f"IP {client_ip} 请求下载文件：{filename}")

    if os.path.exists(target_file) and os.path.isfile(target_file):
        logger.info(f"成功为 {client_ip} 提供下载文件：{target_file}")
        return send_from_directory(downloads_folder, filename, as_attachment=True)
    else:
        error_msg = f"文件未找到：{target_file}"
        logger.warning(error_msg)
        return error_msg, 404


@localproxy_server.route('/icons/<path:filename>')
def serve_icon(filename):
    """
    GET /icons/<filename>
    提供 web/icons 文件夹下的图标或静态资源访问服务。
    """
    target_file = os.path.join(ICONS_FOLDER, filename)
    client_ip = request.remote_addr

    logger.info(
        f"IP {client_ip} 请求图标资源：{filename}")

    if os.path.exists(target_file) and os.path.isfile(target_file):
        logger.info(f"成功为 {client_ip} 返回图标资源：{target_file}")
        return send_from_directory(ICONS_FOLDER, filename, as_attachment=False)
    else:
        error_msg = f"图标资源未找到：{target_file}"
        logger.warning(error_msg)
        return error_msg, 404


@localproxy_server.route('/noticeboard/<path:filename>')
def serve_notice_file(filename):
    """
    GET /noticeboard/<filename>
    提供 web/noticeboard 文件夹下的文件访问服务。
    """
    target_file = os.path.join(NOTICE_BOARD_FOLDER, filename)
    client_ip = request.remote_addr

    logger.info(f"IP {client_ip} 请求公告板文件: {filename}")

    if os.path.exists(target_file) and os.path.isfile(target_file):
        logger.info(f"成功为 {client_ip} 返回公告板文件: {target_file}")
        return send_from_directory(NOTICE_BOARD_FOLDER, filename, as_attachment=False)
    else:
        error_msg = f"公告板文件未找到: {target_file}"
        logger.warning(error_msg)
        return error_msg, 404
