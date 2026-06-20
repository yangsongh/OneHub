import os
from functools import wraps
from flask import Blueprint, Response, jsonify, request, send_file
from utils.utils_lib import LoggerManager, Utils

web_console = Blueprint('web_console', __name__, url_prefix='/console')
logger: LoggerManager = LoggerManager(no_file_handler=True)

WEB_CONSOLE_USERNAME = 'user'
WEB_CONSOLE_PASSWORD = '123456'
WEBSOCKET_PORT = 8889


def require_auth(f):
    """
    认证装饰器 - 用于保护需要认证的路由
    """
    def check_auth(username, password):
        authed = username == WEB_CONSOLE_USERNAME and password == WEB_CONSOLE_PASSWORD
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


@web_console.route('/')
@require_auth
def index():
    """控制台主页路由"""
    logger.info(f'IP {request.remote_addr} 尝试访问网页控制台')

    html_file = os.path.join(
        Utils.get_bundle_dir(), 'web', 'console.html'
    )
    if os.path.exists(html_file):
        return send_file(html_file)
    else:
        logger.warning("网页控制台页面未找到")
        return "网页控制台页面未找到，请联系管理员", 404


@web_console.route('/get_websocket_config')
@require_auth
def websocket_config():
    """获取WebSocket配置"""
    host = request.host.split(':')[0]
    return jsonify({
        'ws_url': f'ws://{host}:{WEBSOCKET_PORT}'
    })
