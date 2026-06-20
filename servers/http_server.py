# -*- coding: utf-8 -*-
# @Author : yangsongh
# @File : HttpServer.py

import time
import errno
import select
import socket
import threading
from typing import Optional
from urllib.parse import urlparse

from utils.utils_lib import ConfigManager
from utils.utils_lib import LoggerManager
from utils.utils_lib import Utils
from utils.whitelist_manager import WhitelistManager


class HTTPProxyServer:
    def __init__(self, logger: LoggerManager, config_manager: ConfigManager):
        self.logger = logger
        self.config_manager = config_manager
        self.server_socket: Optional[socket.socket] = None
        self.is_running = False
        self.active_threads = {}
        self.thread_counter = 0
        self.thread_lock = threading.Lock()

        # 从配置中获取参数
        self.port = self.config_manager.cfgs.get('proxy_port', 8080)
        self.max_connections = self.config_manager.cfgs.get(
            'max_connections', 100)
        self.timeout = self.config_manager.cfgs.get('timeout', 30)
        self.bind_host = self.config_manager.cfgs.get('bind_host', '0.0.0.0')

        # 初始化白名单管理器
        self.whitelist_manager = WhitelistManager(logger)
        self.whitelist_manager.update_config(config_manager)

    def is_client_ip_allowed(self, client_address: tuple) -> bool:
        """检查客户端IP是否在白名单中"""
        client_ip = client_address[0] if isinstance(
            client_address, tuple) else str(client_address)
        return self.whitelist_manager.is_client_ip_allowed(client_ip)

    def is_domain_allowed(self, url: str, method: str) -> bool:
        """检查目标域名是否在白名单中"""
        hostname = self.whitelist_manager.extract_hostname_from_url(
            url, method)
        if hostname is None:
            # 无法提取域名时，直接拒绝
            return False
        return self.whitelist_manager.is_domain_allowed(hostname)

    def handle_client(self, client_socket: socket.socket, client_address: tuple, thread_id: int):
        """处理客户端连接"""
        try:
            # 检查IP白名单
            if not self.is_client_ip_allowed(client_address):
                self.logger.warning(
                    f"线程[{thread_id}] 客户端IP {client_address[0]} 不在白名单中，拒绝连接")
                client_socket.sendall(
                    b"HTTP/1.1 403 Forbidden\r\n\r\nIP not allowed")
                return

            # 接收客户端请求
            request_data = client_socket.recv(4096)
            if not request_data:
                return

            # 解析HTTP请求
            request_lines = request_data.decode(
                'utf-8', errors='ignore').split('\r\n')
            if not request_lines:
                self.logger.warning(f"线程[{thread_id}] 无法解析请求")
                return

            # 解析请求行
            first_line = request_lines[0].split()
            if len(first_line) < 2:
                self.logger.warning(f"线程[{thread_id}] 无效的请求行: {first_line}")
                return

            # 检查域名白名单
            method = first_line[0]
            url = first_line[1]
            if not self.is_domain_allowed(url, method):
                self.logger.warning(f"线程[{thread_id}] 域名不在白名单中，拒绝访问: {url}")
                client_socket.sendall(
                    b"HTTP/1.1 403 Forbidden\r\n\r\nDomain not allowed")
                return

            # 合并日志输出
            client_ip = client_address[0] if isinstance(
                client_address, tuple) else str(client_address)
            with self.thread_lock:
                current_thread_count = len(self.active_threads)
            self.logger.info(
                f"线程[{thread_id}] {client_ip} {method} {url} 活动线程数: {current_thread_count}")

            if method == "CONNECT":
                # HTTPS代理请求
                self.handle_https_proxy(client_socket, url, thread_id)
            else:
                # HTTP代理请求
                self.handle_http_proxy(
                    client_socket, request_data, url, thread_id)

        except (ConnectionRefusedError, ConnectionAbortedError, ConnectionResetError):
            self.logger.debug(f"线程[{thread_id}] 断开连接")
        except socket.timeout:
            self.logger.debug(f"线程[{thread_id}] 连接超时")
        except Exception as e:
            self.logger.error(f"线程[{thread_id}] 处理客户端请求时发生错误: {repr(e)}")
        finally:
            # 关闭连接
            try:
                client_socket.close()
            except:
                pass

            with self.thread_lock:
                if thread_id in self.active_threads:
                    del self.active_threads[thread_id]

            self.logger.debug(f"线程[{thread_id}] 连接处理完成")

    def handle_http_proxy(self, client_socket: socket.socket, request_data: bytes, url: str, thread_id: int):
        """处理HTTP代理请求"""
        target_socket = None
        try:
            # 解析目标URL
            parsed_url = urlparse(url)
            target_host = parsed_url.hostname
            target_port = parsed_url.port or 80

            if not target_host:
                self.logger.warning(f"线程[{thread_id}] 无法解析目标主机: {url}")
                return

            # 连接到目标服务器
            self.logger.debug(
                f"线程[{thread_id}] HTTP连接到 {target_host}:{target_port}")
            target_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            target_socket.settimeout(self.timeout)
            target_socket.connect((target_host, target_port))

            # 发送请求到目标服务器
            target_socket.sendall(request_data)

            # 转发响应给客户端
            while True:
                response_data = target_socket.recv(4096)
                if not response_data:
                    break
                client_socket.sendall(response_data)

        except (ConnectionRefusedError, ConnectionAbortedError, ConnectionResetError):
            self.logger.debug(f"线程[{thread_id}] 断开连接")
        except socket.timeout:
            self.logger.debug(f"线程[{thread_id}] HTTP请求超时")
        except BrokenPipeError:
            pass
        except Exception as e:
            self.logger.error(f"线程[{thread_id}] HTTP代理处理错误: {repr(e)}")
        finally:
            if target_socket:
                try:
                    target_socket.close()
                except:
                    pass

    def handle_https_proxy(self, client_socket: socket.socket, url: str, thread_id: int):
        """处理HTTPS代理请求"""
        target_socket = None
        try:
            # 解析CONNECT请求中的目标地址
            host_port = url.split(':')
            target_host = host_port[0]
            target_port = int(host_port[1]) if len(host_port) > 1 else 443

            # 连接到目标服务器
            self.logger.debug(
                f"线程[{thread_id}] HTTPS连接到 {target_host}:{target_port}")
            target_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            target_socket.settimeout(self.timeout)
            target_socket.connect((target_host, target_port))

            # 告诉客户端连接已建立
            client_socket.sendall(
                b"HTTP/1.1 200 Connection Established\r\n\r\n")

            # 双向转发数据
            self.forward_data(client_socket, target_socket)

        except (ConnectionRefusedError, ConnectionAbortedError, ConnectionResetError):
            self.logger.debug(f"线程[{thread_id}] 断开连接")
        except socket.timeout:
            self.logger.warning(f"线程[{thread_id}] HTTPS连接超时")
        except Exception as e:
            self.logger.error(f"线程[{thread_id}] HTTPS代理处理错误: {repr(e)}")
        finally:
            if target_socket:
                try:
                    target_socket.close()
                except:
                    pass

    def forward_data(self, client_socket: socket.socket, target_socket: socket.socket):
        """使用 select 实现双向数据转发，避免多线程"""
        inputs = [client_socket, target_socket]
        outputs = []

        # 标记套接字是否已关闭
        client_closed = False
        target_closed = False

        while inputs and not (client_closed and target_closed):
            try:
                readable, writable, exceptional = select.select(
                    inputs, outputs, inputs, 1.0)  # 1秒超时

                for s in readable:
                    try:
                        # 接收数据前先检查套接字是否有效
                        if s.fileno() == -1:
                            raise OSError("Socket has been closed")

                        data = s.recv(4096)
                        if not data:
                            # 连接关闭：标记对应套接字状态，移除监听
                            if s is client_socket:
                                client_closed = True
                                if target_socket.fileno() != -1:
                                    try:
                                        target_socket.shutdown(socket.SHUT_WR)
                                    except:
                                        pass
                            else:
                                target_closed = True
                                if client_socket.fileno() != -1:
                                    try:
                                        client_socket.shutdown(socket.SHUT_WR)
                                    except:
                                        pass
                            inputs.remove(s)
                            continue

                        # 转发数据时检查目标套接字状态
                        if s is client_socket and not target_closed:
                            if target_socket.fileno() != -1:
                                target_socket.sendall(data)
                        elif s is target_socket and not client_closed:
                            if client_socket.fileno() != -1:
                                client_socket.sendall(data)

                    except (ConnectionResetError, BrokenPipeError, OSError) as e:
                        # 捕获连接重置/管道破裂/套接字关闭异常
                        self.logger.debug(f"数据转发异常: {repr(e)}")
                        if s is client_socket:
                            client_closed = True
                        else:
                            target_closed = True
                        if s in inputs:
                            inputs.remove(s)
                        continue

                # 处理异常套接字（连接出错/超时等）
                for s in exceptional:
                    self.logger.debug(
                        f"套接字异常，关闭连接: {s.getpeername() if s.fileno() != -1 else 'closed socket'}")
                    if s in inputs:
                        inputs.remove(s)
                    if s in outputs:
                        outputs.remove(s)
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                        s.close()
                    except:
                        pass
                    if s is client_socket:
                        client_closed = True
                    else:
                        target_closed = True

            except (socket.error, OSError) as e:
                # 过滤已知的无害错误，仅记录未预期的异常
                errno_code = e.errno if hasattr(e, 'errno') else -1
                if errno_code in [errno.ECONNRESET, errno.EPIPE, errno.ENOTCONN]:
                    self.logger.debug(f"Select处理已知异常: {repr(e)}")
                else:
                    self.logger.error(f"Select出错: {e}")
                break

        # 最终清理连接（确保套接字关闭）
        try:
            client_socket.shutdown(socket.SHUT_RDWR)
        except:
            pass
        try:
            client_socket.close()
        except:
            pass

        try:
            target_socket.shutdown(socket.SHUT_RDWR)
        except:
            pass
        try:
            target_socket.close()
        except:
            pass

    def log_whitelist_stats(self):
        """记录白名单统计信息"""
        ip_stats = self.whitelist_manager.get_ip_whitelist_stats()
        domain_stats = self.whitelist_manager.get_domain_whitelist_stats()
        self.logger.info(
            f"IP白名单: {'启用' if ip_stats['enabled'] else '禁用'}, 规则数: {ip_stats['rule_count']}")
        self.logger.info(
            f"域名白名单: {'启用' if domain_stats['enabled'] else '禁用'}, 规则数: {domain_stats['rule_count']}")

    def start_server(self):
        """启动代理服务器"""
        try:
            # 创建服务器socket
            self.server_socket = socket.socket(
                socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.settimeout(1)

            # 绑定地址和端口
            self.server_socket.bind((self.bind_host, self.port))
            self.server_socket.listen(self.max_connections)

            self.is_running = True
            self.logger.info(
                f"HTTP代理服务器启动在 {self.bind_host}:{self.port}, 最大连接数: {self.max_connections}, 超时时间: {self.timeout}秒")

            # 记录白名单状态
            self.log_whitelist_stats()

            while self.is_running:
                try:
                    # 接受客户端连接
                    client_socket, client_address = self.server_socket.accept()
                    client_socket.settimeout(self.timeout)

                    # 检查是否超过最大连接数
                    with self.thread_lock:
                        if len(self.active_threads) >= self.max_connections:
                            self.logger.warning(
                                f"达到最大连接数限制，拒绝来自 {client_address} 的连接")
                            client_socket.close()
                            continue

                        # 创建新线程处理连接
                        self.thread_counter += 1
                        thread_id = self.thread_counter
                        client_thread = threading.Thread(
                            target=self.handle_client,
                            args=(client_socket, client_address, thread_id),
                            name=f'http-{self.thread_counter}',
                            daemon=True
                        )
                        self.active_threads[thread_id] = client_thread

                    client_thread.start()

                except socket.timeout:
                    # 超时是正常的，用于检查停止信号
                    continue
                except Exception as e:
                    if self.is_running:
                        self.logger.error(f"接受连接时发生错误: {repr(e)}")

        except Exception as e:
            self.logger.error(f"服务器运行错误: {repr(e)}")
        finally:
            self.stop_server()

    def stop_server(self):
        """停止HTTP代理服务器"""
        self.is_running = False
        self.logger.info("正在停止HTTP代理服务器...")

        if self.server_socket:
            try:
                self.server_socket.close()
            except:
                pass

        # 等待所有活动线程结束
        with self.thread_lock:
            thread_count = len(self.active_threads)
            if thread_count > 0:
                self.logger.info(f"等待 {thread_count} 个活动线程结束...")

        # 给线程一些时间自然结束
        max_wait_time = 20  # 最大等待时间（秒）
        start_time = time.time()

        while time.time() - start_time < max_wait_time:
            with self.thread_lock:
                if not self.active_threads:
                    break
            time.sleep(0.5)

        with self.thread_lock:
            if self.active_threads:
                self.logger.warning(f"强制终止 {len(self.active_threads)} 个活动线程")

        self.logger.info("HTTP代理服务器已停止")


def main():
    """主函数"""
    Utils.sync_work_dir()
    logger = LoggerManager(file_name='http_server')
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
        logger, deft_cfgs=deft_cfgs, cfg_file='config_http.jsonc')
    config_manager.load_configs()

    http_server = HTTPProxyServer(logger, config_manager)

    try:
        http_server.start_server()
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在关闭服务器...")
    except Exception as e:
        logger.error(f"服务器运行异常: {repr(e)}")
    finally:
        http_server.stop_server()


if __name__ == "__main__":
    main()
