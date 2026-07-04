# -*- coding: utf-8 -*-
# @Author : yangsh
# @File : socks_proxy_server.py

import time
import errno
import struct
import select
import socket
import threading

from typing import Optional, Tuple

from utils.utils_lib import ConfigManager
from utils.utils_lib import LoggerManager
from utils.utils_lib import Utils
from utils.whitelist_manager import WhitelistManager


class SocksProxyServer:
    def __init__(self, logger: LoggerManager, config_manager: ConfigManager):
        self.logger = logger
        self.config_manager = config_manager
        self.server_socket: Optional[socket.socket] = None
        self.is_running = False
        self.active_threads = {}
        self.thread_counter = 0
        self.thread_lock = threading.Lock()

        # 从配置中获取参数
        self.port = self.config_manager.cfgs.get('proxy_port', 1080)
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

    def is_domain_allowed(self, target_host: str) -> bool:
        """检查目标域名是否在白名单中"""
        try:
            # IPv4
            socket.inet_aton(target_host)
            return self.whitelist_manager.is_domain_allowed(target_host)
        except socket.error:
            try:
                # IPv6, 直接允许
                socket.inet_pton(socket.AF_INET6, target_host)
                return True
            except socket.error:
                # 域名
                return self.whitelist_manager.is_domain_allowed(target_host)

    def handle_client(self, client_socket: socket.socket, client_address: tuple, thread_id: int):
        """处理客户端连接"""
        target_socket = None
        try:
            # 检查IP白名单
            if not self.is_client_ip_allowed(client_address):
                self.logger.warning(
                    f"线程[{thread_id}] 客户端IP {client_address[0]} 不在白名单中，拒绝连接")
                return

            # SOCKS5握手协商
            if not self.socks5_handshake(client_socket, thread_id):
                self.logger.warning(f"线程[{thread_id}] SOCKS5握手失败")
                return

            # 解析客户端请求
            target_host, target_port = self.parse_socks5_request(
                client_socket, thread_id)
            if not target_host:
                self.logger.warning(f"线程[{thread_id}] 无法解析SOCKS5请求")
                return

            # 检查域名白名单
            if not self.is_domain_allowed(target_host):
                self.logger.warning(
                    f"线程[{thread_id}] 域名不在白名单中，拒绝访问: {target_host}:{target_port}")
                self.send_socks5_error(client_socket, 0x02)  # 0x02 = 不允许的连接
                return

            # 连接到目标服务器
            target_socket = self.create_target_socket(target_host)
            target_socket.settimeout(self.timeout)
            target_socket.connect((target_host, target_port))

            # 合并日志输出
            client_ip = client_address[0] if isinstance(
                client_address, tuple) else str(client_address)
            with self.thread_lock:
                current_thread_count = len(self.active_threads)
            self.logger.info(
                f"线程[{thread_id}] {client_ip} -> {target_host}:{target_port} 活动线程数: {current_thread_count}")

            # 发送连接成功响应
            self.send_socks5_response(
                client_socket, target_host, target_port, thread_id)

            # 双向转发数据
            self.forward_data(client_socket, target_socket)

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

            if target_socket:
                try:
                    target_socket.close()
                except:
                    pass

            with self.thread_lock:
                if thread_id in self.active_threads:
                    del self.active_threads[thread_id]

            current_thread_count = len(self.active_threads)
            self.logger.debug(f"线程[{thread_id}] 连接处理完成，活动线程数: {current_thread_count}")

    @staticmethod
    def create_target_socket(target_host: str) -> socket.socket:
        """根据目标主机地址类型创建对应的socket"""
        try:
            # 尝试解析为IP地址
            socket.inet_aton(target_host)  # IPv4
            return socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except socket.error:
            try:
                socket.inet_pton(socket.AF_INET6, target_host)  # IPv6
                return socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            except socket.error:
                # 域名，优先使用IPv4
                return socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    def socks5_handshake(self, client_socket: socket.socket, thread_id: int) -> bool:
        """SOCKS5握手协商"""
        try:
            handshake_data = client_socket.recv(1024)
            if len(handshake_data) < 3:
                return False

            version, nmethods = struct.unpack('!BB', handshake_data[:2])
            if version != 5:
                self.logger.warning(f"线程[{thread_id}] 不支持的SOCKS版本: {version}")
                return False

            methods = list(handshake_data[2:2 + nmethods])

            if 0x00 in methods:
                response = struct.pack('!BB', 5, 0x00)
                client_socket.sendall(response)
                return True
            else:
                response = struct.pack('!BB', 5, 0xFF)
                client_socket.sendall(response)
                return False

        except Exception as e:
            self.logger.error(f"线程[{thread_id}] SOCKS5握手错误: {repr(e)}")
            return False

    def parse_socks5_request(self, client_socket: socket.socket, thread_id: int) -> Tuple[str, int]:
        """解析SOCKS5请求"""
        try:
            request_data = client_socket.recv(1024)
            if len(request_data) < 10:
                self.logger.warning(f"线程[{thread_id}] 请求数据过短")
                return "", 0

            version, cmd, _, addr_type = struct.unpack(
                '!BBBB', request_data[:4])

            if version != 5:
                self.logger.warning(f"线程[{thread_id}] 无效的SOCKS版本: {version}")
                return "", 0

            if cmd != 1:
                self.logger.warning(f"线程[{thread_id}] 不支持的SOCKS命令: {cmd}")
                self.send_socks5_error(client_socket, 0x07)
                return "", 0

            offset = 4

            if addr_type == 1:  # IPv4
                if len(request_data) < offset + 6:
                    return "", 0
                target_host = socket.inet_ntoa(request_data[offset:offset + 4])
                offset += 4
            elif addr_type == 3:  # 域名
                domain_len = request_data[offset]
                offset += 1
                if len(request_data) < offset + domain_len + 2:
                    return "", 0
                target_host = request_data[offset:offset +
                                           domain_len].decode('utf-8', errors='ignore')
                offset += domain_len
            elif addr_type == 4:  # IPv6
                if len(request_data) < offset + 18:
                    self.logger.warning(f"线程[{thread_id}] IPv6请求数据长度不足")
                    return "", 0
                try:
                    target_host = socket.inet_ntop(
                        socket.AF_INET6, request_data[offset:offset + 16])
                except OSError as e:
                    self.logger.error(f"线程[{thread_id}] IPv6地址转换失败: {repr(e)}")
                    self.send_socks5_error(client_socket, 0x08)
                    return "", 0
                offset += 16
            else:
                self.logger.warning(f"线程[{thread_id}] 不支持的地址类型: {addr_type}")
                self.send_socks5_error(client_socket, 0x08)
                return "", 0

            target_port = struct.unpack(
                '!H', request_data[offset:offset + 2])[0]
            return target_host, target_port

        except (ConnectionRefusedError, ConnectionAbortedError, ConnectionResetError):
            self.logger.debug(f"线程[{thread_id}] 客户端在请求解析期间断开连接")
            return "", 0
        except Exception as e:
            self.logger.error(f"线程[{thread_id}] 解析SOCKS5请求错误: {repr(e)}")
            return "", 0

    def send_socks5_response(self, client_socket: socket.socket, target_host: str, target_port: int, thread_id: int):
        """发送SOCKS5连接成功响应"""
        try:
            # 获取本地的绑定地址
            bind_addr = client_socket.getsockname()[0]

            # 根据目标地址类型构建响应
            try:
                # 尝试作为IPv4地址
                bind_ip = socket.inet_aton(bind_addr)
                addr_type = 1
            except socket.error:
                try:
                    # 尝试作为IPv6地址
                    bind_ip = socket.inet_pton(socket.AF_INET6, bind_addr)
                    addr_type = 4
                except socket.error:
                    # 默认使用IPv4
                    bind_ip = socket.inet_aton('0.0.0.0')
                    addr_type = 1

            response = struct.pack(f'!BBBB{len(bind_ip)}sH',
                                   5, 0, 0, addr_type, bind_ip, 0)
            client_socket.sendall(response)
        except Exception as e:
            self.logger.error(f"线程[{thread_id}] 发送SOCKS5响应错误: {repr(e)}")

    def send_socks5_error(self, client_socket: socket.socket, error_code: int):
        """发送SOCKS5错误响应"""
        try:
            response = struct.pack(
                '!BBBB4sH', 5, error_code, 0, 1, socket.inet_aton('0.0.0.0'), 0)
            client_socket.sendall(response)
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

    def log_thread_count(self):
        """记录当前线程数量"""
        with self.thread_lock:
            self.logger.debug(f"当前活动线程数量: {len(self.active_threads)}")

    def log_whitelist_stats(self):
        """记录白名单统计信息"""
        ip_stats = self.whitelist_manager.get_ip_whitelist_stats()
        domain_stats = self.whitelist_manager.get_domain_whitelist_stats()
        self.logger.info(
            f"IP白名单: {'启用' if ip_stats['enabled'] else '禁用'}, 规则数: {ip_stats['rule_count']}")
        self.logger.info(
            f"域名白名单: {'启用' if domain_stats['enabled'] else '禁用'}, 规则数: {domain_stats['rule_count']}")

    def start_server(self):
        """启动SOCKS5代理服务器"""
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
                f"SOCKS5代理服务器启动在 {self.bind_host}:{self.port}, 最大连接数: {self.max_connections}, 超时时间: {self.timeout}秒")

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
                            name=f'socks-{self.thread_counter}',
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
        """停止SOCKS5代理服务器"""
        self.is_running = False
        self.logger.info("正在停止SOCKS5代理服务器...")

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

        self.logger.info("SOCKS5代理服务器已停止")


def main():
    """主函数"""
    Utils.sync_work_dir()
    logger = LoggerManager(file_name='socks_server')
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
        logger, deft_cfgs=deft_cfgs, cfg_file='config_socks.jsonc')
    config_manager.load_configs()

    socks_server = SocksProxyServer(logger, config_manager)

    try:
        socks_server.start_server()
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在关闭服务器...")
    except Exception as e:
        logger.error(f"服务器运行异常: {repr(e)}")
    finally:
        socks_server.stop_server()


if __name__ == "__main__":
    main()
