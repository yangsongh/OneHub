# OneHub

> 一站式网络服务管理平台 —— 文件共享、代理服务、新闻下载、远程管理

[![Python](https://img.shields.io/badge/Python-3.8+-blue?logo=python)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.1+-black?logo=flask)](https://flask.palletsprojects.com/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Active-brightgreen)](https://github.com/your-repo)

---

## 📖 项目简介

**OneHub** 是一个轻量级、功能丰富的网络服务聚合服务器，专为局域网环境打造。它集成了文件浏览器、HTTP/SOCKS5代理、WebSocket实时控制台、定时新闻下载器、FTP服务器和LocalProxy配套服务端等多种功能，能够在一台设备上快速搭建完整的网络服务中枢。

项目特别适合以下场景：
- 🏫 **校园局域网服务**：为教室、实验室提供文件共享和代理访问
- 🏠 **家庭网络中心**：搭建私人网盘和下载服务
- 🖥️ **设备管理终端**：通过网页控制台远程管理服务器
- 📡 **代理服务网关**：为内网设备提供统一的网络访问出口

---

## ✨ 功能特性

| 功能模块 | 描述 |
|---------|------|
| 📁 **文件浏览器** | 基于Web的文件管理系统，支持目录浏览、文件上传/下载、在线预览文本文件、新建文件夹 |
| 📺 **新闻自动下载器** | 定时从央视网自动下载《新闻30分》《今日说法》等视频，支持加速播放和自动裁剪片头片尾 |
| 🖥️ **WebSocket实时控制台** | 通过网页执行服务器管理命令（status/restart/uptime等），实时查看日志输出 |
| 🔌 **HTTP代理服务器** | 支持HTTP/HTTPS代理转发，配备IP白名单和域名白名单双重过滤机制 |
| 🔒 **SOCKS5代理服务器** | 标准SOCKS5代理协议支持，同样具备白名单管理能力，兼容所有SOCKS5客户端 |
| 📂 **FTP服务器** | 多用户FTP服务，可分别配置主目录和USB外接存储目录访问 |
| 🔗 **LocalProxy服务端** | 为配套的LocalProxy客户端提供配置文件下发、设备激活记录和软件更新服务 |
| 🖱️ **拖拽式上传** | 网页文件浏览器支持点击上传和拖拽上传，上传过程带有实时进度条 |
| 🎯 **IP + 星期白名单** | 灵活的双重访问控制：IP地址白名单 + 星期开放策略，确保服务安全 |

---

## 🛠️ 技术栈

| 类别 | 技术 |
|------|------|
| **Web框架** | Flask + Waitress (生产级WSGI服务器) |
| **代理服务** | 原生Socket + Select (无第三方依赖) |
| **实时通信** | WebSocket (websockets库) |
| **视频处理** | yt-dlp + FFmpeg |
| **FTP服务** | pyftpdlib |
| **配置管理** | JSON5 (支持注释的JSON) |
| **日志系统** | ColorLog (彩色控制台输出) |

> **前置依赖**：FFmpeg（用于视频裁剪和加速处理）

---

## 🚀 快速开始

### 前置条件

- Python 3.8+
- FFmpeg (用于新闻视频处理)
- 推荐使用虚拟环境

### 安装步骤

```bash
# 1. 克隆项目
git clone https://github.com/yangsongh/OneHub.git
cd onehub

# 2. 创建并激活虚拟环境 (推荐)
python -m venv venv
source venv/bin/activate  # Linux/Mac
# 或 venv\Scripts\activate  # Windows

# 3. 安装依赖
pip install -r requirements.txt
```

### 基本使用

```bash
# 启动所有服务
python ../server_launcher.py
```

启动成功后，您将看到：

```
[INFO] 开始启动所有服务器...
[INFO] 网页服务器已启动在端口: 8888
[INFO] FTP服务器已启动在端口: 2121
[INFO] HTTP代理服务器启动在 0.0.0.0:8080
[INFO] SOCKS5代理服务器启动在 0.0.0.0:1080
[INFO] 服务器启动完成
```

### 访问服务

| 服务 | 地址 | 说明 |
|------|------|------|
| 文件浏览器 | `http://<服务器IP>:8888/` | 网盘文件管理 |
| 网页控制台 | `http://<服务器IP>:8888/console` | 远程命令执行 (需认证) |
| LocalProxy官网 | `http://<服务器IP>:8888/localproxy` | 客户端更新服务 (需认证) |
| FTP服务 | `ftp://<服务器IP>:2121` | FTP文件传输 |
| HTTP代理 | `<服务器IP>:8080` | HTTP/HTTPS代理 |
| SOCKS5代理 | `<服务器IP>:1080` | SOCKS5代理 |

---

## 📁 项目结构

```
onehub-server/
├── server_launcher.py        # 🚀 主入口文件：服务启动与进程管理
├── requirements.txt          # 📦 Python依赖清单
├── assets/                   # 📂 配置文件与静态资源目录
│   ├── config_server.jsonc   # 主服务配置 (网盘/控制台/FTP/新闻)
│   ├── config_http.jsonc     # HTTP代理配置
│   ├── config_socks.jsonc    # SOCKS5代理配置
│   ├── config_client.jsonc   # LocalProxy客户端配置
│   ├── localproxy/           # LocalProxy静态资源 (官网页面/下载文件)
│   └── web/                  # Web前端页面
│       ├── console.html      # 网页控制台前端
│       └── file_explorer.html # 文件浏览器前端
├── servers/                  # 🔧 核心服务模块
│   ├── http_server.py        # HTTP代理服务器实现
│   ├── socks_server.py       # SOCKS5代理服务器实现
│   ├── localproxy_server.py  # LocalProxy官网服务 (Flask蓝图)
│   ├── news_downloader.py    # 新闻视频下载与处理
│   └── web/                  # Web服务子模块
│       ├── file_explorer.py  # 文件浏览器 (Flask蓝图)
│       └── web_console.py    # 网页控制台 (Flask蓝图)
└── utils/                    # 🛠️ 工具模块
    ├── utils_lib.py          # 通用工具类 (日志/配置/系统监控)
    └── whitelist_manager.py  # IP/域名白名单管理
```

---

## ⚙️ 配置说明

项目采用 JSON5 格式配置文件（支持注释），所有配置文件位于 `assets/` 目录下。

### 主配置 (`config_server.jsonc`)

```jsonc
{
    // 系统监控频率 (秒)
    "system_monitor_freq": 60,

    // 上传文件后最小磁盘空闲空间 (字节)
    "upload_file_min_free_space": 314572800, // 300MB

    // 网盘访问权限
    "file_explorer_allowed_ips": ["192.168.1.100", "127.0.0.1"],
    "file_explorer_allowed_weekdays": [1, 2, 3, 4, 5], // 周一至周五
    "file_explorer_base_dir": "../../服务器文件",

    // FTP服务
    "ftp_port": 2121,
    "ftp_username": "admin",
    "ftp_password": "your_password",
    "ftp_directory": "/",

    // 新闻下载器
    "news_save_dir": "../../服务器文件/新闻",
    "news_speed": 1.8,          // 播放速度倍率
    "news_schedule_time": "16:00:00", // 每日下载时间

    // Web控制台
    "web_console_username": "admin",
    "web_console_password": "your_password",
    "web_server_port": 8888,
    "websocket_port": 8889
}
```

### HTTP/SOCKS代理配置

两个代理服务共用类似结构，主要配置项包括：

```jsonc
{
    "proxy_port": 8080,           // 监听端口
    "max_connections": 3000,      // 最大并发连接数
    "timeout": 30,                // 超时时间 (秒)
    "bind_host": "0.0.0.0",       // 绑定地址
    "enable_ip_whitelist": true,
    "ip_whitelist": ["127.0.0.1", "192.168.1.0/24"],
    "enable_domain_whitelist": true,
    "domain_whitelist": [
        "*.example.com",
        "music.163.com",
        "*.bilibili.com"
    ]
}
```

---

## 📡 API 概览

### 文件浏览器 API

| 端点 | 方法 | 描述 |
|------|------|------|
| `/api/list` | GET | 列出当前目录内容 (JSON) |
| `/api/list/<path>` | GET | 列出指定目录内容 |
| `/api/file/<path>` | GET | 下载或预览文件 |
| `/api/mkdir` | POST | 创建文件夹 |
| `/api/mkdir/<path>` | POST | 在指定路径创建文件夹 |
| `/api/upload` | POST | 上传文件到当前目录 |
| `/api/upload/<path>` | POST | 上传文件到指定目录 |

### Web控制台 API

| 端点 | 方法 | 描述 |
|------|------|------|
| `/console/` | GET | 控制台页面 (需认证) |
| `/console/get_websocket_config` | GET | 获取WebSocket连接地址 |

### LocalProxy API

| 端点 | 方法 | 描述 |
|------|------|------|
| `/localproxy/` | GET | LocalProxy官网 (需认证) |
| `/localproxy/get_config` | GET | 获取客户端配置文件 (支持版本控制) |
| `/localproxy/post_activate` | POST | 设备激活记录 |
| `/localproxy/downloads/<file>` | GET | 下载软件包 |
| `/localproxy/icons/<file>` | GET | 图标资源 |

---

## 🤝 贡献指南

欢迎提交Issue和Pull Request！

### 代码规范

- 使用 PEP 8 代码风格
- 所有新功能需包含类型注解 (Type Hints)
- 新增模块需添加 docstring
- 提交前请确保代码可正常运行

### 提交 Pull Request

1. 确保 PR 描述清晰，说明解决的问题或新增的功能
2. 通过所有现有测试（如有）
3. 更新相关文档

---

## 📄 许可证

本项目采用 MIT 许可证。详见 [LICENSE](LICENSE) 文件。

---

## 🙏 致谢

- [Flask](https://flask.palletsprojects.com/) - 轻量级Web框架
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) - 强大的视频下载工具
- [FFmpeg](https://ffmpeg.org/) - 多媒体处理引擎
- [pyftpdlib](https://github.com/giampaolo/pyftpdlib) - Python FTP服务器库

---

## 📮 联系方式

如有问题或建议，请通过以下方式联系：

- 提交 [GitHub Issue](https://github.com/yangsongh/OneHub/issues)
- 邮件联系：18675864731@163.com

---
