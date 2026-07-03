import time
import yt_dlp
import requests
import argparse
import functools
import subprocess
import concurrent.futures

from pathlib import Path
from datetime import datetime, timedelta
from utils.utils_lib import LoggerManager
from typing import Dict, List, Optional, Tuple

logger: LoggerManager = LoggerManager(no_file_handler=True)


class VideoDownloader:
    """新闻视频下载器类"""

    def __init__(self, output_dir: Path, speed: float, proxy: str, max_retry_times: int, retry_interval_min: int):
        self.output_dir = output_dir.absolute().resolve()
        self.speed = speed
        self.proxy = proxy
        self.month_cache: Dict[str, Dict] = {}

        # 下载重试配置
        self.max_retry_times = max_retry_times
        self.retry_interval_sec = retry_interval_min * 60
        # 视频源配置
        self.video_sources = [
            {
                "type": "今日说法",
                "api_url": "https://api.cntv.cn/NewVideo/getVideoListByColumn?id=TOPC1451464665008914&n=200&sort=desc&p=1&d={}&mode=0&serviceId=tvcctv",
            },
            {
                "type": "新闻30分",
                "api_url": "https://api.cntv.cn/NewVideo/getVideoListByColumn?id=TOPC1451559097947700&n=200&sort=desc&p=1&d={}&mode=0&serviceId=tvcctv",
            }
        ]

        # 裁剪参数配置
        self.trim_config = {
            "今日说法": {"start": 50, "end": 30},
            "新闻30分": {"start": 60, "end": 10}
        }

    @staticmethod
    def parse_duration_to_seconds(duration_str: str) -> float:
        """将HH:MM:SS格式的时长转换为秒数"""
        try:
            parts = duration_str.split(':')
            if len(parts) == 3:  # HH:MM:SS
                hours, minutes, seconds = map(float, parts)
                return hours * 3600 + minutes * 60 + seconds
            elif len(parts) == 2:  # MM:SS
                minutes, seconds = map(float, parts)
                return minutes * 60 + seconds
            else:  # 只有秒
                return float(duration_str)
        except Exception as e:
            logger.error(f"解析时长 '{duration_str}' 出错: {repr(e)}")
            return 0.0

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """清理文件名，移除或替换不安全的字符"""
        # 移除Windows文件名中不允许的字符
        illegal_chars = r'<>:"/\|?*'
        for char in illegal_chars:
            filename = filename.replace(char, '')
        return filename

    def retry_wrapper(self, max_retry: int, sleep_sec: int):
        """通用重试装饰器，捕获异常循环重试"""
        def decorator(func):
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                cnt = 0
                while cnt < max_retry:
                    try:
                        return func(*args, **kwargs)
                    except Exception as e:
                        cnt += 1
                        logger.warning(
                            f"{func.__name__} 执行失败，重试{cnt}/{max_retry}，等待{sleep_sec}s：{repr(e)}")
                        time.sleep(sleep_sec)
                e = f"{func.__name__} 达到最大重试次数{max_retry}，放弃执行"
                logger.error(e)
                raise Exception(e)
            return wrapper
        return decorator

    def get_month_programs(self, year: int, month: int) -> Dict[str, Dict]:
        """获取指定月份的所有节目信息，返回按日期索引的字典"""
        month_str = f"{year}{month:02d}"
        all_programs = {}

        try:
            # 获取代理配置
            if self.proxy:
                proxies = {
                    "http": self.proxy,
                    "https": self.proxy,
                }
            else:
                proxies = None

            # 获取所有视频源的节目列表
            for source in self.video_sources:
                logger.info(f"正在获取 {source['type']} 的 {year}年{month}月节目列表...")

                try:
                    @self.retry_wrapper(max_retry=self.max_retry_times, sleep_sec=self.retry_interval_sec//60)
                    def fetch_api():
                        api_url = source["api_url"].format(month_str)
                        resp = requests.get(
                            api_url, timeout=30, proxies=proxies)
                        resp.raise_for_status()
                        return resp.json()
                    data = fetch_api()
                    programs = data.get("data", {}).get("list", [])

                    logger.info(f"成功获取 {source['type']} 的 {len(programs)} 个节目")

                    for program in programs:
                        # 提取日期（格式：2026-04-24 12:38:00 -> 2026-04-24）
                        date_str = program["time"].split()[0]

                        # 统一存储格式
                        program_info = {
                            "title": program.get("title"),
                            "length": program.get("length"),
                            "url": program.get("url"),
                            "duration_seconds": self.parse_duration_to_seconds(program.get("length", "00:00:00")),
                            "video_type": source["type"],
                            "sanitized_title": self.sanitize_filename(program.get("title", "")),
                        }

                        # 如果同一天有多个节目，优先保留今日说法，其次是新闻30分
                        if date_str not in all_programs:
                            all_programs[date_str] = program_info
                        elif source["type"] == "今日说法" and all_programs[date_str]["video_type"] != "今日说法":
                            # 如果当前是今日说法且已有的是新闻30分，则替换
                            all_programs[date_str] = program_info

                except Exception as e:
                    logger.warning(
                        f"获取 {source['type']} 的 {year}年{month}月节目列表时出错: {repr(e)}"
                    )
                    continue

            logger.info(f"总共获取到 {len(all_programs)} 天的节目信息")
            return all_programs

        except Exception as e:
            logger.error(f"获取{year}年{month}月节目列表时出错: {repr(e)}")
            return {}

    def get_date_program(self, target_date: datetime) -> Optional[Dict]:
        """获取指定日期的节目信息，使用缓存的月度数据"""
        date_str = target_date.strftime("%Y-%m-%d")
        year = target_date.year
        month = target_date.month
        cache_key = f"{year}-{month:02d}"

        # 使用缓存或重新获取月度数据
        @self.retry_wrapper(max_retry=2, sleep_sec=10)
        def load_month_cache():
            self.month_cache[cache_key] = self.get_month_programs(year, month)
        if cache_key not in self.month_cache:
            logger.info(f"首次获取 {year}年{month}月的节目数据...")
            load_month_cache()

        # 从月度数据中查找指定日期的节目
        program = self.month_cache[cache_key].get(date_str)

        # if program:
        #     logger.info(f"找到节目: {program['video_type']} - {program['title']}")
        #     logger.info(
        #         f"视频类型: {program['video_type']}, 时长: {program['length']} ({program['duration_seconds']}秒)")
        # else:
        #     logger.info(f"没有找到 {date_str} 的今日说法或新闻30分节目")

        return program

    @staticmethod
    def _progress_hook(d):
        """yt-dlp进度回调函数"""
        filename = d.get('filename', '未知文件')
        if d['status'] == 'downloading':
            if '_percent_str' in d:
                percent = d['_percent_str']
                speed = d.get('_speed_str', '未知速度')
                eta = d.get('_eta_str', '未知')
                print(
                    f"[{Path(filename).name}] 下载进度:{percent} 速度:{speed} 剩余时间:{eta}\t\t\r", end="")
        elif d['status'] == 'error':
            logger.error(f"[{Path(filename).name}] 下载出错: {d}")

    def download_video(self, url: str, output_file: Path, date_str: str, video_type: str) -> Tuple[str, Optional[Path], str]:
        """使用yt-dlp下载视频（单次下载逻辑，无重试）"""
        logger.info(f"开始下载 {date_str} 的 {video_type} 视频, url: {url}")

        temp_video = output_file.with_suffix('.temp_video.mp4')
        ydl_opts = {
            # 优先选无分片的视频
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            # "format": "best",
            "outtmpl": str(temp_video),                 # 输出文件名模板
            "proxy": self.proxy,                        # 代理
            "encoding": "utf-8",                        # 编码
            "windowsfilenames": True,                   # Windows文件名
            "progress_hooks": [self._progress_hook],    # 下载进度回调
            # "quiet": True,                            # 安静模式
            "no_color": True,                           # 禁用彩色输出
            "noprogress": True,                         # 不显示自带进度条
            "concurrent_fragments": 1,                  # 单分片并发
            "no_part": True,                            # 禁用.part文件
            "keep_fragments": False,                    # 不保留分片
            "hls_prefer_native": False,                 # 禁用 yt-dlp 原生 HLS 下载器
            # 改用 FFmpeg 直接拉流合并，不产生分片文件
            "downloader": {"hls": "ffmpeg"},
            # "external_downloader_args": {               # ffmpeg网络参数，关闭持久连接、开启自动重连
            #     "ffmpeg": [
            #         "-http_persistent", "0",            # 关闭HLS HTTP长连接
            #         "-reconnect", "1",
            #         "-reconnect_streamed", "1",
            #         "-reconnect_on_network_error", "1",
            #         "-reconnect_delay_max", "5",
            #         "-rw_timeout", "10000000",
            #     ]
            # }
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore
                info = ydl.extract_info(url, download=True)
                if 'filepath' in info:
                    downloaded_path = Path(info['filepath'])
                else:
                    downloaded_path = Path(ydl.prepare_filename(info))

                logger.info(f"{date_str} 视频下载完成: {downloaded_path}")
                return (date_str, downloaded_path, video_type)

        except Exception as e:
            logger.error(f"单次下载 {date_str} 视频时出错: {repr(e)}")
            return (date_str, None, video_type)

    def download_video_with_retry(self, url: str, output_file: Path, date_str: str, video_type: str) -> Tuple[str, Optional[Path], str]:
        """带自动重试的下载封装，循环重试直到成功或达到最大次数"""
        retry_count = 0
        while retry_count < self.max_retry_times:
            res = self.download_video(url, output_file, date_str, video_type)
            date_ret, path_ret, typ_ret = res
            if path_ret is not None:
                # 下载成功，直接返回
                return res
            retry_count += 1
            remain_retry = self.max_retry_times - retry_count
            logger.warning(
                f"{date_str} 下载失败，当前重试次数 {retry_count}/{self.max_retry_times}，剩余重试{remain_retry}次，等待{self.retry_interval_sec//60}分钟后重试")
            # 等待指定间隔
            time.sleep(self.retry_interval_sec)
        # 全部重试用完仍失败
        logger.error(f"{date_str} 已达到最大重试次数{self.max_retry_times}，放弃下载")
        return (date_str, None, video_type)

    def process_video(self, input_path: Path, output_path: Path, duration_seconds: float, video_type: str, date_str: str) -> bool:
        """使用ffmpeg处理视频：先裁剪（直接复制流），再加速，最后重命名

            ffmpeg下载临时文件（临时文件）
                ↓
            输出裁剪文件（临时文件，裁剪文件）
                ↓
            输出加速文件（临时文件，裁剪文件，加速文件）
                ↓
            加速文件重命名最终输出（临时文件，裁剪文件，最终输出）
                ↓
            删除临时文件、裁剪文件（最终输出）
        """
        try:
            # 获取裁剪参数
            trim_params = self.trim_config.get(
                video_type, {"start": 0, "end": 0})
            trim_start = trim_params["start"]
            trim_end = trim_params["end"]

            # ===== 第一步：裁剪视频（优先复制流）=====
            logger.info(f"裁剪 {date_str} 视频中（移除前{trim_start}秒和后{trim_end}秒）...")
            temp_trimmed = output_path.with_suffix('.temp_trimmed.mp4')

            # 计算裁剪结束时间 = 总时长 - trim_end
            end_time = duration_seconds - trim_end
            if end_time <= trim_start:  # 确保结束时间大于开始时间
                logger.error(f"视频时长 {duration_seconds}秒 太短，无法裁剪")
                return False

            # 尝试快速复制流裁剪，如果失败（如非关键帧），降级为重新编码
            try:
                @self.retry_wrapper(max_retry=2, sleep_sec=3)
                def run_trim_cmd(cmd, check=True):
                    ret = subprocess.run(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=check,
                        shell=True,
                        encoding='utf-8',
                        errors='replace'
                    )
                    return ret
                
                # 优化裁剪命令：-c copy 直接复制流（快速无损失），-avoid_negative_ts make_zero 修复时间戳问题
                cmd_trim = (
                    f'ffmpeg -ss {str(trim_start)} -i "{str(input_path)}" -to {str(end_time)} '
                    f'-c:v copy -c:a copy -avoid_negative_ts make_zero -y '
                    f'"{str(temp_trimmed)}" -loglevel error -hide_banner'
                )
                try:
                    result = run_trim_cmd(cmd_trim)
                except Exception as e:
                    logger.warning(f"快速裁剪失败（非关键帧），降级为重新编码裁剪: {e}")
                    cmd_trim_fallback = (
                        f'ffmpeg -ss {str(trim_start)} -i "{str(input_path)}" -to {str(end_time)} '
                        f'-c:v libx264 -preset fast -crf 23 -c:a aac -y '
                        f'"{str(temp_trimmed)}" -loglevel error -hide_banner'
                    )
                    result = run_trim_cmd(cmd_trim_fallback, check=True)

                logger.info(
                    f"ffmpeg裁剪命令执行成功（{(cmd_trim.split('-c:v')[1].split()[0]).strip()}模式）")
            except Exception as e:
                logger.error(f"ffmpeg裁剪失败:\n{e}")
                raise
            # finally:
            #     input_path.unlink(missing_ok=True)

            # ===== 第二步：加速视频（使用滤镜必须重新编码）=====
            logger.info(f"加速 {date_str} 视频中...")
            temp_accelerated = output_path.with_suffix('.temp_accelerated.mp4')

            speed = self.speed
            # 音频倍速处理
            if speed < 0.5 or speed > 2.0:
                factors = []
                remaining = speed
                # 处理倍速大于2.0的情况
                while remaining > 2.0:
                    factors.append(2.0)
                    remaining /= 2.0
                # 处理倍速小于0.5的情况
                while remaining < 0.5:
                    factors.append(0.5)
                    remaining /= 0.5
                factors.append(remaining)
                audio_filter = ",".join([f"atempo={f}" for f in factors])
            else:
                audio_filter = f"atempo={speed}"

            # 用了滤镜就不能流复制，直接重新编码
            cmd_speed = (
                f'ffmpeg -i "{str(temp_trimmed)}" '
                f'-filter_complex "[0:v]setpts={1/speed}*PTS[v];[0:a]{audio_filter}[a]" '
                f'-map "[v]" -map "[a]" '
                f'-c:v libx264 -preset fast -crf 22 -c:a aac -b:a 128k -y '
                f'"{str(temp_accelerated)}" -loglevel error -hide_banner'
            )

            try:
                @self.retry_wrapper(max_retry=2, sleep_sec=3)
                def run_speed_cmd(cmd):
                    subprocess.run(
                        cmd_speed,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=True,
                        shell=True,
                        encoding='utf-8',
                        errors='replace'
                    )
                run_speed_cmd(cmd_speed)
                logger.info(f"ffmpeg加速命令执行成功")
            except Exception as e:
                logger.error(f"ffmpeg加速失败:\n{e}")
                raise
            # finally:
            #     temp_trimmed.unlink(missing_ok=True)

            # ===== 第三步：将加速文件重命名为最终目标文件 =====
            logger.info(f"将 {date_str} 加速文件重命名为最终输出: {output_path}")
            if output_path.exists():
                output_path.unlink()  # 如果目标文件已存在，先删除
            temp_accelerated.rename(output_path)

            return True

        except Exception as e:
            logger.error(f"处理 {date_str} 视频时出错: {repr(e)}")
            return False
        finally:
            # 清理临时文件
            input_path.unlink(missing_ok=True)
            temp_trimmed = output_path.with_suffix('.temp_trimmed.mp4')
            temp_accelerated = output_path.with_suffix('.temp_accelerated.mp4')
            temp_trimmed.unlink(missing_ok=True)
            temp_accelerated.unlink(missing_ok=True)

    def process_downloaded_video(self, temp_video_path: Path, output_path: Path,
                                 duration_seconds: float, video_type: str, date_str: str) -> bool:
        """处理已下载的视频"""
        logger.info(f"开始处理 {date_str} 的视频")

        if not temp_video_path or not temp_video_path.exists():
            logger.error(f"{date_str} 的视频文件不存在，跳过处理")
            return False

        if self.process_video(temp_video_path, output_path, duration_seconds, video_type, date_str):
            logger.info(f"{date_str} 视频处理完成: {output_path}")
            return True
        else:
            logger.error(f"{date_str} 视频处理失败")
            return False

    def download_and_process_videos(self, dates_to_download: List[datetime], max_workers: int) -> int:
        """下载和处理指定日期的视频"""
        logger.info(f"开始处理 {len(dates_to_download)} 个视频")

        # 每次执行前强制删除当月缓存（需要每日刷新节目时用）
        today = datetime.now()
        cache_key = f"{today.year}-{today.month:02d}"
        if cache_key in self.month_cache:
            del self.month_cache[cache_key]

        # 预先获取需要的月份数据
        months_needed = set((date.year, date.month)
                            for date in dates_to_download)
        for year, month in months_needed:
            cache_key = f"{year}-{month:02d}"
            if cache_key not in self.month_cache:
                logger.info(f"获取 {year}年{month}月的节目数据...")
                self.month_cache[cache_key] = self.get_month_programs(
                    year, month)

        # 第一阶段：多线程获取视频信息
        logger.info("第一阶段：获取视频信息...")
        video_infos = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_date = {
                executor.submit(self.get_date_program, date): date
                for date in dates_to_download
            }

            for future in concurrent.futures.as_completed(future_to_date):
                target_date = future_to_date[future]
                date_str = target_date.strftime("%Y%m%d")

                try:
                    program = future.result()
                    if program:
                        filename = f"{date_str} {program['sanitized_title']}.mp4"
                        video_infos.append({
                            "date_str": date_str,
                            "date": target_date,
                            "program": program,
                            "output_file": self.output_dir / filename
                        })
                        logger.info(
                            f"获取到 {date_str} 的视频信息: {program['title']}")
                    else:
                        logger.info(f"没有找到 {date_str} 的视频")
                except Exception as e:
                    logger.error(f"获取 {date_str} 视频信息时出错: {repr(e)}")

        # 第二阶段：多线程下载视频（使用带重试的方法）
        logger.info("第二阶段：多线程下载视频（自动重试开启）...")
        download_results = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_downloads = {}

            for info in video_infos:
                date_str = info["date_str"]
                output_file = info["output_file"]

                # 检查文件是否已存在
                if output_file.exists():
                    logger.info(f"视频已存在，跳过: {output_file}")
                    continue

                # 提交带重试的下载任务
                future = executor.submit(
                    self.download_video_with_retry,
                    info["program"]["url"],
                    output_file,
                    date_str,
                    info["program"]["video_type"]
                )
                future_downloads[future] = info

            # 收集下载结果
            for future in concurrent.futures.as_completed(future_downloads):
                info = future_downloads[future]
                date_str = info["date_str"]

                try:
                    result = future.result()
                    download_results.append((info, result))
                    logger.info(f"{date_str} 下载任务（含重试）执行完毕")
                except Exception as e:
                    logger.error(f"下载 {date_str} 任务异常: {repr(e)}")

        # 第三阶段：多线程处理视频
        logger.info("第三阶段：多线程处理视频...")

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_processes = []

            for info, download_result in download_results:
                if download_result[1] is None:  # 下载失败（重试耗尽）
                    logger.error(f"跳过处理，下载重试全部失败: {info['date_str']}")
                    continue

                _, temp_video_path, video_type = download_result
                future = executor.submit(
                    self.process_downloaded_video,
                    temp_video_path,
                    info["output_file"],
                    info["program"]["duration_seconds"],
                    video_type,
                    info["date_str"]
                )
                future_processes.append(future)

            # 等待所有处理完成
            successful_count = 0
            for future in concurrent.futures.as_completed(future_processes):
                try:
                    if future.result():
                        successful_count += 1
                except Exception as e:
                    logger.error(f"视频批量处理失败: {repr(e)}")

        logger.info(
            f"任务执行完成，成功处理 {successful_count}/{len(download_results)} 个视频")
        return successful_count


def run_task(output_dir: Path, speed: float, proxy: str,
             download_past_days: int, max_workers: int, max_retry_times: int, retry_interval_min: int) -> bool:
    """运行任务：下载和处理视频"""
    downloader = VideoDownloader(
        output_dir, speed, proxy, max_retry_times, retry_interval_min)
    today = datetime.now()

    logger.info(f"开始执行下载任务，日期: {today.strftime('%Y-%m-%d')}")

    # 生成日期列表：包含过去几天和今天
    dates_to_download = [
        today - timedelta(days=i)
        for i in range(download_past_days, -1, -1)
    ]

    # 执行下载和处理
    successful_count = downloader.download_and_process_videos(
        dates_to_download, max_workers)

    return successful_count > 0


def run_downloader(output_dir: Path, speed: float, proxy: str,
                   download_past_days: int, max_workers: int, schedule_time: str,
                   max_retry_times: int, retry_interval_min: int, run_immediately: bool):
    """启动下载器"""
    output_dir = Path(output_dir).absolute()
    output_dir.mkdir(parents=True, exist_ok=True)

    # 解析目标时间字符串
    try:
        target_time_obj = datetime.strptime(schedule_time, "%H:%M:%S").time()
    except ValueError:
        logger.error(f"错误：时间格式无效，应为 HH:MM:SS，当前传入: {schedule_time}")
        return

    logger.info(
        f"下载器配置：输出目录 = {output_dir}, 视频速度 = {speed}, 代理 = {proxy}, "
        f"下载过去天数: {download_past_days}, 最大线程数 = {max_workers}, 计划执行时间 = {schedule_time}, "
        f"下载最大重试次数={max_retry_times}, 重试间隔={retry_interval_min}分钟"
    )

    # 记录上次执行的时间戳（用于防止同一天内多次触发）
    last_execution_date = None

    # 立即执行一次下载任务
    if run_immediately:
        successful_count = run_task(
            output_dir, speed, proxy,
            download_past_days, max_workers, max_retry_times, retry_interval_min
        )
    else:
        successful_count = 0
        
    # 今日视频已下载，不再重复下载
    if successful_count == 1 and download_past_days == 0:
        last_execution_date = datetime.now().date()

    # 主循环：每秒检查时间
    try:
        while True:
            now = datetime.now()
            current_date = now.date()
            current_time = now.time()

            # 获取当前日期的字符串作为唯一标识
            today_str = current_date.isoformat()

            # 检查是否需要执行
            is_today_executed = (last_execution_date == current_date)

            if not is_today_executed and current_time >= target_time_obj:
                logger.info(
                    f"检测到时间已到 ({current_time} >= {target_time_obj})，执行今日任务...")
                try:
                    run_task(
                        output_dir, speed, proxy,
                        download_past_days, max_workers, max_retry_times, retry_interval_min
                    )
                    last_execution_date = current_date  # 标记今天已执行
                    # logger.info(f"{today_str} 的任务执行完毕")
                except Exception as e:
                    logger.error(f"{today_str} 的任务执行出错: {repr(e)}")

            time.sleep(1)

    except Exception as e:
        logger.error(f"新闻下载器运行出错: {repr(e)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="新闻下载器")
    parser.add_argument("--output-dir", "-o", type=str,
                        default="新闻", help="视频保存路径 (默认：'新闻')")
    parser.add_argument("--speed", type=float, default=1.8,
                        help="视频加速倍数, 默认: 1.8")
    parser.add_argument("--proxy", type=str, default="",
                        help="代理服务器地址 (例如: http://127.0.0.1:8080)")
    parser.add_argument("--download-past-days", type=int,
                        default=0, help="程序启动时下载过去几天的视频, 默认: 0 (不下载)")
    parser.add_argument("--max-workers", type=int,
                        default=2, help="最大线程数, 默认: 2")
    parser.add_argument("--schedule-time", type=str, default="17:00:00",
                        help="每日定时执行时间 (格式: HH:MM:SS), 默认: 17:00:00")
    parser.add_argument("--max-retry", type=int, default=4,
                        help="下载失败最大重试次数, 默认4次")
    parser.add_argument("--retry-interval", type=int, default=30,
                        help="重试间隔 (单位: 分钟), 默认30分钟")
    parser.add_argument("--run-immediately", default=True, action="store_true",
                        help="启动程序后立即执行一次下载, 不等待定时时间, 默认打开")

    try:
        logger: LoggerManager = LoggerManager(no_file_handler=True)
        args = parser.parse_args()
        run_downloader(
            output_dir=args.output_dir,
            speed=args.speed,
            proxy=args.proxy,
            download_past_days=args.download_past_days,
            max_workers=args.max_workers,
            schedule_time=args.schedule_time,
            max_retry_times=args.max_retry,
            retry_interval_min=args.retry_interval,
            run_immediately=args.run_immediately
        )
    except KeyboardInterrupt:
        logger.info("程序被用户终止")
    except Exception as e:
        logger.error(f"新闻下载器运行出错: {repr(e)}")
