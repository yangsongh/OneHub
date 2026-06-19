import os
import sys
import time
import json
import shutil
import logging
import asyncio
import waitress
import threading
import websockets

import pyftpdlib.log
from pyftpdlib.servers import FTPServer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.authorizers import DummyAuthorizer

from typing import Set
from flask import Flask

from servers.http_server import HTTPProxyServer
from servers.socks_server import SocksProxyServer

import servers.web.file_explorer as file_explorer
import servers.web.web_console as web_console
import servers.localproxy_server as localproxy_server
import servers.news_downloader as news_downloader

from assets.config_server import *
from utils.utils_lib import Utils
from utils.utils_lib import ConfigManager
from utils.utils_lib import LoggerManager

Utils.sync_work_dir()
logger = LoggerManager(
    logger_name='server_launcher',
    file_name='server_launcher',
    console_format_str='\033[32m[%(asctime)s]\033[0m %(funcName)s-%(lineno)d %(log_color)s[服务器] %(message)s'
)
Utils.setup_except_hook(logger)


class ServerManager:
    """服务器进程管理器"""

    def __init__(self):
        self.is_running = True
        self.start_time = time.time()

        self.system_monitor_thread = None  # 系统监控线程

        # CPU监控相关属性
        self.cpu_prev_times = None
        self.cpu_usage = 0.0
        self.cpu_usage_lock = threading.Lock()

        self.flask_app = None  # 网页服务器Flask App
        self.web_server_thread = None  # 网页服务器线程

        # 网页控制台相关属性
        self.web_console_output = []
        self.max_output_lines = 2000
        self.web_console_commands = []
        self.command_lock = threading.Lock()
        self.output_lock = threading.Lock()

        # WebSocket服务器相关属性
        self.websocket_clients: Set = set()
        self.websocket_clients_lock = threading.Lock()
        self.websocket_server_thread = None
        self.websocket_loop = None

        self.ftp_server_thread = None  # FTP服务器线程

        self.http_server = None  # HTTP服务器
        self.http_server_thread = None  # HTTP服务器线程

        self.socks_server = None  # SOCKS服务器
        self.socks_server_thread = None  # SOCKS服务器线程

        self.news_downloader_thread = None  # 新闻下载器线程

    def add_output_to_web_console(self, output_line):
        """添加输出到网页控制台，并广播给所有WebSocket客户端"""
        with self.output_lock:
            output_entry = {
                'content': output_line
            }
            self.web_console_output.append(output_entry)
            # 限制最大记录数
            if len(self.web_console_output) > self.max_output_lines:
                self.web_console_output = self.web_console_output[-self.max_output_lines:]

            # 广播给所有WebSocket客户端
            self.broadcast_to_websocket_clients({
                'type': 'output',
                'data': output_entry
            })

    def get_full_output(self):
        """获取所有输出"""
        with self.output_lock:
            return self.web_console_output.copy(), len(self.web_console_output)

    def get_next_command(self):
        """获取下一个待执行的命令"""
        with self.command_lock:
            if self.web_console_commands:
                return self.web_console_commands.pop(0)
            return None

    def add_command_to_queue(self, command):
        """添加命令到执行队列"""
        with self.command_lock:
            self.web_console_commands.append(command)

    def broadcast_to_websocket_clients(self, message):
        """广播消息给所有WebSocket客户端"""
        with self.websocket_clients_lock:
            clients_copy = list(self.websocket_clients)

        if not clients_copy:
            return

        # 在异步环境中广播
        if self.websocket_loop and self.websocket_loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._broadcast_message(clients_copy, message),
                self.websocket_loop
            )

    async def _broadcast_message(self, clients, message):
        """异步广播消息"""
        message_json = json.dumps(message)
        disconnected = []

        for client in clients:
            try:
                await client.send(message_json)
            except:
                disconnected.append(client)

        # 移除断开的客户端
        if disconnected:
            with self.websocket_clients_lock:
                for client in disconnected:
                    self.websocket_clients.discard(client)

    async def handle_websocket(self, websocket):
        """处理WebSocket连接"""
        # 添加客户端
        with self.websocket_clients_lock:
            self.websocket_clients.add(websocket)

        try:
            # 发送历史记录
            all_output, total_count = self.get_full_output()
            await websocket.send(json.dumps({
                'type': 'history',
                'data': all_output,
                'total': total_count
            }))

            # 处理客户端消息
            async for message in websocket:
                try:
                    data = json.loads(message)
                    if data.get('type') == 'command':
                        command = data.get('command', '').strip()
                        if command:
                            self.add_command_to_queue(command)
                            await websocket.send(json.dumps({
                                'type': 'command_ack',
                                'command': command,
                                'status': 'queued'
                            }))
                except json.JSONDecodeError:
                    pass

        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            # 移除客户端
            with self.websocket_clients_lock:
                self.websocket_clients.discard(websocket)

    async def start_websocket_server(self):
        """启动WebSocket服务器"""
        async with websockets.serve(
            self.handle_websocket,
            '0.0.0.0',
            WEBSOCKET_PORT,
            ping_interval=30,
            ping_timeout=10
        ):
            logger.info(f"WebSocket服务器已启动在端口: {WEBSOCKET_PORT}")
            await asyncio.Future()  # 永久运行

    def run_websocket_server(self):
        """运行WebSocket服务器"""
        self.websocket_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.websocket_loop)

        try:
            self.websocket_loop.run_until_complete(
                self.start_websocket_server())
        except Exception as e:
            logger.error(f"WebSocket服务器运行异常: {e}")

    def get_cpu_times(self):
        """获取当前CPU时间数据"""
        if not sys.platform.startswith("linux"):
            return [0, 0, 0, 0, 0, 0, 0, 0]

        try:
            with open('/proc/stat', 'r') as f:
                lines = f.readlines()

            # 查找CPU汇总行
            for line in lines:
                if line.startswith('cpu '):  # 注意cpu后面有个空格，这是总CPU统计
                    parts = line.split()
                    # 提取前8个时间值（user, nice, system, idle, iowait, irq, softirq, steal）
                    times = [int(x) for x in parts[1:9]]
                    return times
            return None

        except Exception as e:
            logger.error(f"读取CPU时间数据失败: {e}")
            return None

    def calculate_cpu_percentage(self, prev_times, current_times):
        """根据两次CPU时间数据计算使用率百分比"""
        if prev_times is None or current_times is None:
            return 0.0

        # 计算总时间差
        total_diff = sum(current_times) - sum(prev_times)

        # 计算空闲时间差（idle + iowait）
        idle_diff = (current_times[3] - prev_times[3]) + \
            (current_times[4] - prev_times[4])

        if total_diff == 0:
            return 0.0

        # 防止使用率超过100%或小于0%
        usage = 100.0 * (total_diff - idle_diff) / total_diff
        return max(0.0, min(100.0, usage))  # 限制在0-100之间

    def get_memory_usage(self):
        """读取系统内存占用情况"""
        if not sys.platform.startswith("linux"):
            # logger.debug(f"非Linux系统，不支持读取内存占用情况")
            return None

        try:
            with open('/proc/meminfo', 'r') as f:
                mem_data = f.readlines()

            mem_info = {}
            for line in mem_data:
                if ':' in line:
                    key, value = line.split(':', 1)
                    mem_info[key.strip()] = value.strip()

            # 计算总内存和可用内存（单位：MB）
            total_mem = int(mem_info.get(
                'MemTotal', '0 kB').replace('kB', '').strip()) / 1024
            free_mem = int(mem_info.get('MemFree', '0 kB').replace(
                'kB', '').strip()) / 1024
            cached_mem = int(mem_info.get(
                'Cached', '0 kB').replace('kB', '').strip()) / 1024
            buffers_mem = int(mem_info.get(
                'Buffers', '0 kB').replace('kB', '').strip()) / 1024

            # 计算已用内存
            used_mem = total_mem - free_mem - cached_mem - buffers_mem
            memory_percent = (used_mem / total_mem) * \
                100 if total_mem > 0 else 0

            return {
                'total': round(total_mem, 1),
                'used': round(used_mem, 1),
                'free': round(free_mem, 1),
                'cached': round(cached_mem, 1),
                'buffers': round(buffers_mem, 1),
                'percent': round(memory_percent, 1)
            }

        except Exception as e:
            logger.error(f"读取内存信息失败: {e}")
            return None

    def get_disk_usage(self, path='./'):
        """获取磁盘存储空间使用情况"""
        try:
            # 获取磁盘使用情况
            usage = shutil.disk_usage(path)

            # 转换为 GB 单位（保留2位小数）
            total_gb = usage.total / (1024 ** 3)
            used_gb = usage.used / (1024 ** 3)
            free_gb = usage.free / (1024 ** 3)
            percent = (usage.used / usage.total) * 100

            return {
                'total': round(total_gb, 2),
                'used': round(used_gb, 2),
                'free': round(free_gb, 2),
                'percent': round(percent, 1),
                'path': os.path.abspath(path)
            }
        except Exception as e:
            logger.error(f"获取磁盘空间信息失败: {e}")
            return None

    def get_cpu_usage_str(self):
        """获取字符串格式的CPU使用率"""
        with self.cpu_usage_lock:
            return f"""CPU使用率: {self.cpu_usage:.2f}%"""

    def get_memory_info_str(self):
        """获取字符串格式的内存使用情况"""
        mem_info = self.get_memory_usage()
        if mem_info:
            return f"""
内存使用情况:
    内存使用: {mem_info['used']} MB / {mem_info['total']} MB ({mem_info['percent']}%)
    空闲内存: {mem_info['free']} MB, 缓存内存: {mem_info['cached']} MB, 缓冲内存: {mem_info['buffers']} MB"""
        else:
            return ""

    def get_disk_info_str(self):
        """获取字符串格式的储存空间使用情况"""
        disk_info = self.get_disk_usage()
        if disk_info:
            return f"""
存储空间使用情况:
    存储空间: 已用 {disk_info['used']} GB / {disk_info['total']} GB ({disk_info['percent']}%)
    剩余空间: {disk_info['free']} GB, 路径: {disk_info['path']}"""
        else:
            return ""

    def run_ftp_server(self):
        """运行FTP服务器"""
        try:
            # 确保FTP目录存在
            if not os.path.exists(FTP_DIRECTORY):
                os.makedirs(FTP_DIRECTORY)
                logger.info(f"创建FTP目录: {FTP_DIRECTORY}")

            # 创建授权器
            authorizer = DummyAuthorizer()
            # 添加主用户，用于访问主目录
            authorizer.add_user(
                FTP_USERNAME, FTP_PASSWORD, FTP_DIRECTORY, perm="elradfmwMT",
            )
            # 添加USB用户，用于访问USB
            authorizer.add_user(
                FTP_USB_USERNAME, FTP_USB_PASSWORD, FTP_USB_DIRECTORY, perm="elradfmwMT",
            )

            # 配置FTP日志输出
            pyftpdlib.log.config_logging(
                level=logging.ERROR, prefix='[%(levelname)1.1s %(filename)s:%(lineno)d]')
            logging.getLogger('pyftpdlib').propagate = False

            # 创建FTP服务器并运行
            address = ("0.0.0.0", FTP_PORT)
            FTPHandler.authorizer = authorizer
            server = FTPServer(address, FTPHandler)
            server.serve_forever()

        except Exception as e:
            logger.error(f"FTP服务器运行异常: {e}")

    def run_system_monitor(self):
        """运行系统监控"""
        # 初始化第一次CPU时间
        self.cpu_prev_times = self.get_cpu_times()

        # 记录上次输出时间
        last_output_time = time.time()

        # 初始化当前日期
        current_date = time.localtime().tm_mday
        logger.info(f"系统监控初始化，当前日期: {current_date}")

        while self.is_running:
            try:
                time.sleep(1)  # 等待1秒以便计算CPU使用率

                # 计算CPU使用率
                current_cpu_times = self.get_cpu_times()
                cpu_usage = self.calculate_cpu_percentage(
                    self.cpu_prev_times, current_cpu_times
                )

                # 更新全局CPU数据
                with self.cpu_usage_lock:
                    self.cpu_usage = cpu_usage
                self.cpu_prev_times = current_cpu_times

                # 检查日期是否切换
                new_date = time.localtime().tm_mday
                if new_date != current_date:
                    logger.info(
                        f"检测到日期切换: {current_date} -> {new_date}，自动执行服务器重启...")
                    current_date = new_date
                    # 执行重启命令
                    self.execute_command('restart')

                # 检查是否需要输出系统信息
                current_time = time.time()
                if current_time - last_output_time >= SYSTEM_MONITOR_FREQ:
                    # 更新上次输出时间
                    last_output_time = current_time

                    self.execute_command('status')

            except Exception as e:
                logger.error(f"系统资源监控运行异常: {e}")

    def run_web_server(self):
        """运行网页服务器 (文件浏览器+网页控制台+LocalProxy服务器)"""
        try:
            self.flask_app = Flask(__name__)

            # 初始化基本配置和日志输出
            self.flask_app.config['BASE_DIRECTORY'] = FILE_EXPLORER_BASE_DIR
            self.flask_app.config['MAX_CONTENT_LENGTH'] = 10 * \
                1024 * 1024 * 1024
            file_explorer.logger = LoggerManager(
                logger_name='file_explorer', file_name='file_explorer',
                web_callback=self.add_output_to_web_console,
                console_format_str='\033[32m[%(asctime)s]\033[0m %(funcName)s-%(lineno)d %(log_color)s[文件浏览器] %(message)s'
            )
            web_console.logger = LoggerManager(
                logger_name='web_console', file_name='web_console',
                web_callback=self.add_output_to_web_console,
                console_format_str='\033[32m[%(asctime)s]\033[0m %(funcName)s-%(lineno)d %(log_color)s[网页控制台] %(message)s'
            )
            localproxy_server.logger = LoggerManager(
                logger_name='localproxy_server', file_name='localproxy_server',
                web_callback=self.add_output_to_web_console,
                console_format_str='\033[32m[%(asctime)s]\033[0m %(funcName)s-%(lineno)d %(log_color)s[LocalProxy] %(message)s'
            )

            # 注册蓝图
            self.flask_app.register_blueprint(file_explorer.file_explorer)
            self.flask_app.register_blueprint(web_console.web_console)
            self.flask_app.register_blueprint(
                localproxy_server.localproxy_server)

            waitress.serve(
                self.flask_app, host='0.0.0.0',
                port=WEB_SERVER_PORT, threads=WEB_SERVER_THREADS,
                max_request_body_size=10 * 1024 * 1024 * 1024,  # 10GB 最大请求体大小
                connection_limit=200,  # 提高连接限制
                channel_timeout=300,   # 连接超时时间（秒），默认120秒，改为5分钟
                inbuf_overflow=1024 * 1024,  # 输入缓冲区溢出阈值
                outbuf_overflow=1024 * 1024  # 输出缓冲区溢出阈值
            )

        except Exception as e:
            logger.error(f"网页服务器运行异常: {repr(e)}")

    def run_http_server(self):
        """运行HTTP服务器"""
        try:
            http_logger = LoggerManager(
                logger_name='http_server', file_name='http_server',
                web_callback=self.add_output_to_web_console,
                console_format_str='\033[32m[%(asctime)s]\033[0m %(funcName)s-%(lineno)d %(log_color)s[HTTP] %(message)s'
            )
            deft_cfgs = {
                "proxy_port": 8080,
                "max_connections": 3000,
                "timeout": 30,
                "bind_host": "0.0.0.0",
                "enable_ip_whitelist": False,
                "ip_whitelist": [],
                "enable_domain_whitelist": False,
                "domain_whitelist": []
            }
            config_manager = ConfigManager(
                http_logger, deft_cfgs=deft_cfgs, cfg_file='config_http.jsonc')
            config_manager.load_configs()

            self.http_server = HTTPProxyServer(http_logger, config_manager)
            self.http_server.start_server()

        except Exception as e:
            logger.error(f"HTTP服务器运行异常: {repr(e)}")

    def run_socks_server(self):
        """运行SOCKS服务器"""
        try:
            socks_logger = LoggerManager(
                logger_name='socks_server', file_name='socks_server',
                web_callback=self.add_output_to_web_console,
                console_format_str='\033[32m[%(asctime)s]\033[0m %(funcName)s-%(lineno)d %(log_color)s[SOCKS] %(message)s'
            )
            deft_cfgs = {
                "proxy_port": 1080,
                "max_connections": 3000,
                "timeout": 30,
                "bind_host": "0.0.0.0",
                "enable_ip_whitelist": False,
                "ip_whitelist": [],
                "enable_domain_whitelist": False,
                "domain_whitelist": []
            }
            config_manager = ConfigManager(
                socks_logger, deft_cfgs=deft_cfgs, cfg_file='config_socks.jsonc')
            config_manager.load_configs()

            self.socks_server = SocksProxyServer(socks_logger, config_manager)
            self.socks_server.start_server()

        except Exception as e:
            logger.error(f"SOCKS服务器运行异常: {repr(e)}")

    def run_news_downloader(self):
        """运行新闻下载器"""
        try:
            news_downloader.logger = LoggerManager(
                logger_name='news_downloader', file_name='news_downloader',
                web_callback=self.add_output_to_web_console,
                console_format_str='\033[32m[%(asctime)s]\033[0m %(funcName)s-%(lineno)d %(log_color)s[新闻下载器] %(message)s'
            )
            news_downloader.run_downloader(
                output_dir=NEWS_SAVE_DIR, speed=NEWS_SPEED, proxy="",
                download_past_days=0, max_workers=1, schedule_time=NEWS_SCHEDULE_TIME
            )

        except Exception as e:
            logger.error(f"新闻下载器运行异常: {repr(e)}")

    def start_all_servers(self) -> bool:
        """启动所有服务器"""
        try:
            logger.info("开始启动所有服务器...")

            # 启动系统监控线程
            self.system_monitor_thread = threading.Thread(
                target=self.run_system_monitor, name='system_monitor', daemon=True)
            self.system_monitor_thread.start()
            logger.info("系统监控已启动")

            # 启动网页服务器
            logger.info("正在启动网页服务器 (文件浏览器+网页控制台+LocalProxy服务器)...")
            self.web_server_thread = threading.Thread(
                target=self.run_web_server, name='web_server', daemon=True)
            self.web_server_thread.start()
            logger.info(
                f"网页服务器已启动在端口: {WEB_SERVER_PORT}, 控制台用户名: {WEB_CONSOLE_USERNAME}, 控制台密码: {WEB_CONSOLE_PASSWORD}; " +
                f"LocalProxy官网用户名: {LOCALPROXY_USERNAME}, LocalProxy官网密码: {LOCALPROXY_PASSWORD}"
            )

            # 启动WebSocket服务器
            logger.info("正在启动WebSocket服务器...")
            self.websocket_server_thread = threading.Thread(
                target=self.run_websocket_server, name='websocket_server', daemon=True)
            self.websocket_server_thread.start()

            # 启动FTP服务器
            logger.info("正在启动FTP服务器...")
            self.ftp_server_thread = threading.Thread(
                target=self.run_ftp_server, name='ftp_server', daemon=True)
            self.ftp_server_thread.start()
            logger.info(
                f"FTP服务器已启动在端口: {FTP_PORT}, 用户名: {FTP_USERNAME}, 密码: {FTP_PASSWORD}, 根目录: {FTP_DIRECTORY}, USB目录: {FTP_USB_DIRECTORY}")

            # 启动HTTP服务器
            logger.info("正在启动HTTP服务器...")
            self.http_server_thread = threading.Thread(
                target=self.run_http_server, name='http_server', daemon=True)
            self.http_server_thread.start()

            # 启动SOCKS服务器
            logger.info("正在启动SOCKS服务器...")
            self.socks_server_thread = threading.Thread(
                target=self.run_socks_server, name='socks_server', daemon=True)
            self.socks_server_thread.start()

            # 启动新闻下载器
            logger.info("正在启动新闻下载器...")
            self.news_downloader_thread = threading.Thread(
                target=self.run_news_downloader, name='news_downloader', daemon=True)
            self.news_downloader_thread.start()
            logger.info("新闻下载器已启动")

            logger.info("服务器启动完成")

            return True

        except Exception as e:
            logger.error(f"服务器启动失败: {repr(e)}")
            return False

    def interactive_console(self):
        """交互式控制台"""
        logger.info("启动完成，进入交互式控制台...")
        self.show_help()

        # 启动网页命令处理线程
        def process_web_commands():
            """专门处理网页命令的线程"""
            while self.is_running:
                web_command = self.get_next_command()
                if web_command:
                    logger.info(f"[网页控制台] 执行命令: {web_command}")
                    self.execute_command(web_command.lower().strip())
                time.sleep(0.1)  # 避免CPU占用过高

        web_command_thread = threading.Thread(
            target=process_web_commands, name='process_web_commands', daemon=True)
        web_command_thread.start()

        while self.is_running:
            try:
                # 获取本地命令
                command = input("\n输入命令 (help 查看帮助): ").strip().lower()
                if command:
                    self.execute_command(command)
            except KeyboardInterrupt:
                logger.info("收到中断信号，正在停止...")
                break
            except EOFError:
                # 遇到EOF，只打日志，不退出，忽略异常继续循环
                logger.warning("检测到EOF输入，忽略本次输入")
                continue

    def execute_command(self, command: str):
        """统一执行命令（网页端和本地）"""
        # status: 显示服务器运行状态
        if command == 'status':
            # 获取CPU信息
            output = f"""\n系统资源使用情况:\n    {self.get_cpu_usage_str()}"""
            # 获取内存信息
            output += self.get_memory_info_str()
            # 获取存储空间信息
            output += self.get_disk_info_str()
            # 最终输出
            logger.info(output)

        # restart: 重启服务器
        elif command == 'restart':
            logger.info("正在重启服务器...")

            # 脚本模式下需要先从数据目录回退到根目录
            if not Utils.is_package_mode():
                base_dir = os.path.dirname(Utils.get_bundle_dir())
                os.chdir(base_dir)

            # 重启当前脚本
            python = sys.executable
            os.execv(python, [python] + sys.argv)

        # uptime: 显示服务器运行时间
        elif command == 'uptime':
            uptime = time.time() - self.start_time
            hours = int(uptime // 3600)
            minutes = int((uptime % 3600) // 60)
            seconds = int(uptime % 60)
            logger.info(f"服务器运行时间: {hours:02d}:{minutes:02d}:{seconds:02d}")

        # memory: 获取内存使用情况
        elif command == 'memory':
            logger.info(self.get_memory_info_str())

        # cpu: 获取CPU使用率
        elif command == 'cpu':
            logger.info(self.get_cpu_usage_str())

        # disk/storage: 获取存储空间使用情况
        elif command in ['disk', 'storage']:
            logger.info(self.get_disk_info_str())

        # exit/quit/stop: 停止所有服务器并退出管理控制台
        elif command in ['exit', 'quit', 'stop']:
            logger.info("正在退出...")
            self.is_running = False

        # help: 显示帮助信息
        elif command in ['help', '?']:
            self.show_help()

        # 其他: 未知命令
        elif command:
            logger.info(f"未知命令: {command}")
            logger.info("输入 'help' 查看可用命令")

    def show_help(self):
        """显示帮助信息"""
        help_text = """
可用命令:
    status          - 显示服务器CPU、内存、存储空间信息
    restart         - 重启服务器
    uptime          - 显示运行时间
    memory          - 显示内存使用情况
    cpu             - 显示CPU使用率
    disk/storage    - 显示存储空间使用情况
    exit/quit/stop  - 停止所有服务器并退出管理控制台
    help/?          - 显示此帮助信息
        """
        logger.info(help_text)


def main():
    # 创建服务器管理器
    manager = ServerManager()
    # 添加网页控制台输出回调
    logger.add_web_callback(manager.add_output_to_web_console)

    # 启动所有服务器
    if not manager.start_all_servers():
        sys.exit(1)

    # 进入交互式控制台
    manager.interactive_console()


if __name__ == "__main__":
    main()
