import os
import sys
import gzip
import json
import hashlib
import shutil
import threading
import time
import warnings

warnings.filterwarnings("ignore", message="urllib3.*doesn\\'t match a supported version")
warnings.filterwarnings("ignore", category=UserWarning, module="requests")

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import tarfile
import urllib3
import argparse
import logging
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple, Any
from pathlib import Path
import io
import signal

"""
1ms Docker 镜像下载专版

交互流程（默认）：
1) 输入关键词 -> 调用 1ms 搜索接口
2) 分页浏览 / 选择镜像
3) 选择 tag（默认 latest）
4) 展示可用架构并选择
5) 直接下载并输出 docker load 可用的 tar 包
"""

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

urllib3.disable_warnings()

VERSION = "v1.0.0-1ms"

# 1ms registry（下载数据用）
DEFAULT_1MS_REGISTRY = "docker.1ms.run"
# 1ms 搜索 API（搜索镜像用）
DEFAULT_1MS_API = "https://1ms.run/api/v1/registry"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", encoding="utf-8")
logger = logging.getLogger(__name__)

stop_event = threading.Event()
progress_lock = threading.Lock()
original_sigint_handler = None

# 下载路径性能参数（偏向“快速失败 + 快速重试”）
CONNECT_TIMEOUT = 8
READ_TIMEOUT = 120
DOWNLOAD_MAX_RETRIES = 4
BACKOFF_BASE = 0.3
MAX_PARALLEL_LAYERS = 8


def signal_handler(signum, frame):
    global stop_event
    if stop_event.is_set():
        print("\n⚠️ 强制退出...")
        if original_sigint_handler:
            signal.signal(signal.SIGINT, original_sigint_handler)
            raise KeyboardInterrupt
        sys.exit(1)

    stop_event.set()
    print("\n⚠️ 收到中断信号，正在保存进度并退出...")
    print("💡 再次按 Ctrl+C 强制退出")


original_sigint_handler = signal.signal(signal.SIGINT, signal_handler)


@dataclass
class ImageInfo:
    registry: str
    repository: str  # v2 repository path，例如 library/nginx
    image_name: str  # nginx
    tag: str


@dataclass
class DownloadStats:
    total_size: int = 0
    downloaded_size: int = 0
    start_time: float = 0.0
    speeds: List[float] = field(default_factory=list)

    def get_avg_speed(self) -> float:
        if not self.speeds:
            return 0.0
        return sum(self.speeds[-10:]) / len(self.speeds[-10:])

    def format_size(self, size: int) -> str:
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f}{unit}"
            size /= 1024
        return f"{size:.1f}TB"

    def format_time(self, seconds: float) -> str:
        if seconds < 60:
            return f"{int(seconds)}秒"
        elif seconds < 3600:
            return f"{int(seconds // 60)}分{int(seconds % 60)}秒"
        else:
            return f"{int(seconds // 3600)}小时{int((seconds % 3600) // 60)}分"


class LayerProgress:
    def __init__(self, name: str, total_size: int, index: int, total_layers: int):
        self.name = name
        self.total_size = total_size
        self.downloaded_size = 0
        self.index = index
        self.total_layers = total_layers
        self.status = "waiting"
        self.total_chunks = 0
        self.current_chunk = 0
        self.retry_count = 0
        self.is_resume = False

    def set_chunk_info(self, current: int, total: int):
        self.current_chunk = current
        self.total_chunks = total

    def set_total_size(self, total_size: int):
        self.total_size = total_size

    @staticmethod
    def format_size(size: int) -> str:
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f}{unit}"
            size /= 1024
        return f"{size:.1f}TB"


class ProgressDisplay:
    def __init__(self, bar_width: int = 30):
        self.bar_width = bar_width
        self.layers: Dict[str, LayerProgress] = {}
        self.stats: Optional[DownloadStats] = None
        self.last_update = 0.0
        self.update_interval = 0.2
        self.initialized = False
        self.last_line_count = 0

    def add_layer(self, name: str, total_size: int, index: int, total_layers: int):
        with progress_lock:
            self.layers[name] = LayerProgress(name, total_size, index, total_layers)

    def update_layer(self, name: str, downloaded: int):
        with progress_lock:
            if name in self.layers:
                self.layers[name].downloaded_size = downloaded
                self.layers[name].status = "downloading"
        self._refresh_display()

    def update_layer_size(self, name: str, total_size: int):
        with progress_lock:
            if name in self.layers:
                # 仅在拿到有效大小时更新，避免把已知大小覆盖成 0
                if total_size and total_size > 0:
                    old_size = self.layers[name].total_size
                    self.layers[name].set_total_size(max(old_size, total_size))
        self._refresh_display(force=True)

    def complete_layer(self, name: str):
        with progress_lock:
            if name in self.layers:
                layer = self.layers[name]
                if layer.total_size == 0:
                    layer.total_size = layer.downloaded_size
                else:
                    layer.downloaded_size = layer.total_size
                layer.status = "completed"
        self._refresh_display(force=True)

    def set_chunk_info(self, name: str, current: int, total: int):
        with progress_lock:
            if name in self.layers:
                self.layers[name].current_chunk = current
                self.layers[name].total_chunks = total

    def print_initial(self):
        with progress_lock:
            for name, layer in sorted(self.layers.items(), key=lambda x: x[1].index):
                print(self._format_layer_line(layer))
            if self.stats:
                print("📊 速度: 计算中...")
            self.last_line_count = len(self.layers) + (1 if self.stats else 0)
            self.initialized = True

    def _refresh_display(self, force: bool = False):
        now = time.time()
        if not force and now - self.last_update < self.update_interval:
            return
        self.last_update = now

        with progress_lock:
            lines = [self._format_layer_line(layer) for _, layer in sorted(self.layers.items(), key=lambda x: x[1].index)]

            if self.stats:
                speed = self.stats.get_avg_speed()
                speed_str = self.stats.format_size(int(speed)) if speed > 0 else "0B"
                lines.append(f"📊 速度: {speed_str}/s")

            if self.initialized and self.last_line_count > 0:
                for _ in range(self.last_line_count):
                    sys.stdout.write("\033[F")
                sys.stdout.write("\033[J")

            for line in lines:
                print(line)

            self.last_line_count = len(lines)
            self.initialized = True
            sys.stdout.flush()

    def _format_layer_line(self, layer: LayerProgress) -> str:
        if layer.total_size > 0:
            progress = layer.downloaded_size / layer.total_size
            progress_text = f"{progress*100:5.1f}%"
        else:
            progress = 1.0 if layer.status == "completed" else 0.0
            progress_text = "100.0%" if layer.status == "completed" else "  --.-%"
        filled = int(self.bar_width * progress)
        empty = self.bar_width - filled
        bar = "█" * filled + "░" * empty
        total_size_str = layer.format_size(layer.total_size) if layer.total_size > 0 else "?"
        size_str = f"{layer.format_size(layer.downloaded_size)}/{total_size_str}"
        chunk_info = f" [{layer.current_chunk}/{layer.total_chunks}]" if layer.total_chunks > 0 else ""
        status_icon = "✅" if layer.status == "completed" else "⬇️"
        total_layers_str = str(layer.total_layers)
        index_str = str(layer.index).rjust(len(total_layers_str))
        layer_info = f"({index_str}/{total_layers_str})"
        return f"  {status_icon} {layer_info} {layer.name:<12} |{bar}| {progress_text} {size_str:>15}{chunk_info}"


progress_display = ProgressDisplay()


class SessionManager:
    _instance: Optional[requests.Session] = None

    @classmethod
    def get_session(cls) -> requests.Session:
        if cls._instance is None:
            cls._instance = cls._create_session()
        return cls._instance

    @classmethod
    def _create_session(cls) -> requests.Session:
        session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.3,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD", "OPTIONS"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=64, pool_maxsize=128, pool_block=False)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.timeout = (CONNECT_TIMEOUT, READ_TIMEOUT)
        session.proxies = {
            "http": os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"),
            "https": os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"),
        }
        if session.proxies.get("http") or session.proxies.get("https"):
            logger.info("🌐 使用代理设置从环境变量")
        return session


def format_big_number(n: int) -> str:
    try:
        n = int(n)
    except Exception:
        return str(n)
    if n >= 1_0000_0000:
        return f"{n/1_0000_0000:.2f}亿"
    if n >= 1_0000:
        return f"{n/1_0000:.2f}万"
    return str(n)


def get_output_dir(repository: str, tag: str, arch: str, output_path: Optional[str] = None) -> Path:
    safe_repo = repository.replace("/", "_").replace(":", "_")
    dir_name = f"{safe_repo}_{tag}_{arch}"
    output_dir = (Path(output_path) / dir_name) if output_path else (Path.cwd() / dir_name)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def get_auth_head(
    session: requests.Session,
    auth_url: str,
    reg_service: str,
    repository: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    max_retries: int = 3,
) -> Dict[str, str]:
    for attempt in range(max_retries):
        try:
            url = f"{auth_url}?service={reg_service}&scope=repository:{repository}:pull"
            headers: Dict[str, str] = {}
            if username and password:
                auth_string = f"{username}:{password}"
                encoded_auth = base64.b64encode(auth_string.encode("utf-8")).decode("utf-8")
                headers["Authorization"] = f"Basic {encoded_auth}"

            resp = session.get(url, headers=headers, verify=False, timeout=60)
            resp.raise_for_status()
            access_token = resp.json()["token"]
            return {
                "Authorization": f"Bearer {access_token}",
                "Accept": ", ".join(
                    [
                        "application/vnd.docker.distribution.manifest.v2+json",
                        "application/vnd.docker.distribution.manifest.list.v2+json",
                        "application/vnd.oci.image.index.v1+json",
                        "application/vnd.oci.image.manifest.v1+json",
                    ]
                ),
            }
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                wait_time = 2**attempt
                logger.warning(f"认证请求失败，{wait_time}秒后重试 ({attempt + 1}/{max_retries}): {e}")
                time.sleep(wait_time)
            else:
                raise
    raise RuntimeError("获取认证头失败")


def fetch_manifest(
    session: requests.Session,
    registry: str,
    repository: str,
    tag_or_digest: str,
    auth_head: Dict[str, str],
    max_retries: int = 3,
) -> Tuple[requests.Response, int]:
    for attempt in range(max_retries):
        try:
            url = f"https://{registry}/v2/{repository}/manifests/{tag_or_digest}"
            resp = session.get(url, headers=auth_head, verify=False, timeout=60)
            if resp.status_code == 401:
                return resp, 401
            resp.raise_for_status()
            return resp, 200
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                wait_time = 2**attempt
                logger.warning(f"清单请求失败，{wait_time}秒后重试 ({attempt + 1}/{max_retries}): {e}")
                time.sleep(wait_time)
            else:
                raise
    raise RuntimeError("获取 manifest 失败")


def select_manifest_digest(manifests: List[Dict[str, Any]], arch: str) -> Optional[str]:
    for m in manifests:
        if (
            (m.get("annotations", {}).get("com.docker.official-images.bashbrew.arch") == arch or m.get("platform", {}).get("architecture") == arch)
            and m.get("platform", {}).get("os") == "linux"
        ):
            return m.get("digest")
    return None


class DownloadProgressManager:
    def __init__(self, output_dir: Path, repository: str, tag: str, arch: str):
        self.output_dir = output_dir
        self.repository = repository
        self.tag = tag
        self.arch = arch
        self.progress_file = output_dir / "progress.json"
        self.progress_data = self.load_progress()

    def load_progress(self) -> Dict[str, Any]:
        if self.progress_file.exists():
            try:
                with open(self.progress_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                metadata = data.get("metadata", {})
                if metadata.get("repository") == self.repository and metadata.get("tag") == self.tag and metadata.get("arch") == self.arch:
                    logger.info(f"📋 加载已有下载进度，共 {len(data.get('layers', {}))} 个文件")
                    return data
            except Exception:
                pass
        return {
            "metadata": {"repository": self.repository, "tag": self.tag, "arch": self.arch, "created_at": time.strftime("%Y-%m-%d %H:%M:%S")},
            "layers": {},
            "config": None,
        }

    def save_progress(self):
        try:
            with open(self.progress_file, "w", encoding="utf-8") as f:
                json.dump(self.progress_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存进度文件失败: {e}")

    def update_layer_status(self, digest: str, status: str, **kwargs):
        self.progress_data["layers"].setdefault(digest, {})
        self.progress_data["layers"][digest]["status"] = status
        self.progress_data["layers"][digest].update(kwargs)
        self.save_progress()

    def is_layer_completed(self, digest: str) -> bool:
        return self.progress_data.get("layers", {}).get(digest, {}).get("status") == "completed"

    def update_config_status(self, status: str, **kwargs):
        if self.progress_data["config"] is None:
            self.progress_data["config"] = {}
        self.progress_data["config"]["status"] = status
        self.progress_data["config"].update(kwargs)
        self.save_progress()

    def is_config_completed(self) -> bool:
        c = self.progress_data.get("config")
        return bool(c and c.get("status") == "completed")

    def clear_progress(self):
        try:
            if self.progress_file.exists():
                self.progress_file.unlink()
        except Exception:
            pass


def get_file_size(session: requests.Session, url: str, headers: Dict[str, str]) -> int:
    try:
        resp = session.head(url, headers=headers, verify=False, timeout=(CONNECT_TIMEOUT, 5))
        if resp.status_code == 200:
            return int(resp.headers.get("content-length", 0))
    except Exception:
        return 0
    return 0


def download_file_in_chunks(
    session: requests.Session,
    url: str,
    headers: Dict[str, str],
    save_path: str,
    desc: str,
    total_size: int,
    expected_digest: Optional[str] = None,
    max_retries: int = DOWNLOAD_MAX_RETRIES,
    stats: Optional[DownloadStats] = None,
    chunk_size: int = 10 * 1024 * 1024,
) -> bool:
    num_chunks = (total_size + chunk_size - 1) // chunk_size
    temp_dir = save_path + ".chunks"
    progress_display.set_chunk_info(desc, 0, num_chunks)

    try:
        os.makedirs(temp_dir, exist_ok=True)
        chunk_files: List[Tuple[int, int, str]] = []
        for i in range(num_chunks):
            start = i * chunk_size
            end = min((i + 1) * chunk_size, total_size)
            chunk_files.append((start, end, os.path.join(temp_dir, f"chunk_{i:04d}")))

        completed_chunks = [False] * num_chunks
        chunk_sizes = [end - start for start, end, _ in chunk_files]

        def download_single_chunk(i: int, start: int, end: int, chunk_file: str) -> bool:
            if stop_event.is_set():
                return False
            if os.path.exists(chunk_file):
                if os.path.getsize(chunk_file) == end - start:
                    return True
                try:
                    os.remove(chunk_file)
                except Exception:
                    pass

            chunk_headers = headers.copy()
            chunk_headers["Range"] = f"bytes={start}-{end-1}"

            for attempt in range(max_retries):
                if stop_event.is_set():
                    return False
                try:
                    with session.get(
                        url, headers=chunk_headers, verify=False, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), stream=True
                    ) as resp:
                        resp.raise_for_status()
                        with open(chunk_file, "wb") as f:
                            for data in resp.iter_content(chunk_size=65536):
                                if stop_event.is_set():
                                    return False
                                if data:
                                    f.write(data)
                    return os.path.getsize(chunk_file) == end - start
                except Exception as e:
                    if attempt < max_retries - 1:
                        time.sleep(min(BACKOFF_BASE * (2**attempt), 2))
                        continue
                    logger.error(f"❌ {desc} 分片 {i+1} 下载失败: {e}")
                    return False
            return False

        max_workers = min(num_chunks, MAX_PARALLEL_LAYERS)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures: Dict[Any, int] = {}
            for i, (start, end, chunk_file) in enumerate(chunk_files):
                if os.path.exists(chunk_file) and os.path.getsize(chunk_file) == end - start:
                    completed_chunks[i] = True
                    continue
                futures[executor.submit(download_single_chunk, i, start, end, chunk_file)] = i

            while futures:
                for future in list(futures.keys()):
                    if future.done():
                        i = futures.pop(future)
                        ok = future.result()
                        if not ok:
                            return False
                        completed_chunks[i] = True

                current_completed = sum(1 for c in completed_chunks if c)
                current_size = sum(chunk_sizes[i] for i in range(num_chunks) if completed_chunks[i])
                progress_display.update_layer(desc, current_size)
                progress_display.set_chunk_info(desc, current_completed, num_chunks)
                time.sleep(0.03)

        sha256_hash = hashlib.sha256() if expected_digest else None
        with open(save_path, "wb") as outfile:
            for _, _, chunk_file in chunk_files:
                with open(chunk_file, "rb") as infile:
                    while True:
                        data = infile.read(65536)
                        if not data:
                            break
                        outfile.write(data)
                        if sha256_hash:
                            sha256_hash.update(data)

        shutil.rmtree(temp_dir, ignore_errors=True)

        if expected_digest and sha256_hash:
            actual_digest = f"sha256:{sha256_hash.hexdigest()}"
            if actual_digest != expected_digest:
                logger.error(f"❌ {desc} 校验失败！")
                try:
                    os.remove(save_path)
                except Exception:
                    pass
                return False

        progress_display.complete_layer(desc)
        return True
    except Exception as e:
        logger.error(f"❌ {desc} 分片下载失败: {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return False


def download_file_with_progress(
    session: requests.Session,
    url: str,
    headers: Dict[str, str],
    save_path: str,
    desc: str,
    expected_digest: Optional[str] = None,
    max_retries: int = DOWNLOAD_MAX_RETRIES,
    stats: Optional[DownloadStats] = None,
) -> bool:
    CHUNK_THRESHOLD = 50 * 1024 * 1024

    for attempt in range(max_retries):
        if stop_event.is_set():
            return False

        resume_pos = 0
        if os.path.exists(save_path):
            resume_pos = os.path.getsize(save_path)
            if resume_pos > 0 and attempt == 0:
                logger.info(f"📎 {desc} 检测到已下载 {LayerProgress.format_size(resume_pos)}，尝试断点续传...")

        download_headers = headers.copy()
        if resume_pos > 0:
            download_headers["Range"] = f"bytes={resume_pos}-"

        try:
            with session.get(
                url, headers=download_headers, verify=False, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), stream=True
            ) as resp:
                if resp.status_code == 416:
                    progress_display.complete_layer(desc)
                    return True
                resp.raise_for_status()

                content_range = resp.headers.get("content-range")
                if content_range:
                    total_size = int(content_range.split("/")[1])
                else:
                    total_size = int(resp.headers.get("content-length", 0)) + resume_pos

                progress_display.update_layer_size(desc, total_size)

                # 大文件用分片
                if total_size - resume_pos > CHUNK_THRESHOLD and resume_pos == 0:
                    return download_file_in_chunks(session, url, headers, save_path, desc, total_size, expected_digest, max_retries, stats)

                sha256_hash = hashlib.sha256() if expected_digest else None
                if resume_pos > 0 and sha256_hash:
                    with open(save_path, "rb") as existing_file:
                        while True:
                            chunk = existing_file.read(65536)
                            if not chunk:
                                break
                            sha256_hash.update(chunk)

                if stats:
                    stats.total_size += total_size - resume_pos
                    if stats.start_time == 0:
                        stats.start_time = time.time()

                mode = "ab" if resume_pos > 0 else "wb"
                downloaded_size = resume_pos
                last_update_time = time.time()
                last_downloaded = resume_pos

                with open(save_path, mode) as file:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if stop_event.is_set():
                            return False
                        if chunk:
                            file.write(chunk)
                            downloaded_size += len(chunk)
                            if sha256_hash:
                                sha256_hash.update(chunk)
                            progress_display.update_layer(desc, downloaded_size)

                            if stats:
                                now = time.time()
                                if now - last_update_time >= 0.5:
                                    speed = (downloaded_size - last_downloaded) / (now - last_update_time)
                                    stats.speeds.append(speed)
                                    last_downloaded = downloaded_size
                                    last_update_time = now

                if expected_digest and sha256_hash:
                    actual_digest = f"sha256:{sha256_hash.hexdigest()}"
                    if actual_digest != expected_digest:
                        logger.error(f"❌ {desc} 校验失败！")
                        try:
                            os.remove(save_path)
                        except Exception:
                            pass
                        time.sleep(min(BACKOFF_BASE * (2**attempt), 2))
                        continue

                progress_display.complete_layer(desc)
                return True

        except KeyboardInterrupt:
            return False
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < max_retries - 1:
                time.sleep(min(BACKOFF_BASE * (2**attempt), 2))
                continue
            logger.error(f"❌ {desc} 下载失败: {e}")
            return False
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code in [429, 500, 502, 503, 504] and attempt < max_retries - 1:
                time.sleep(min(BACKOFF_BASE * (2**attempt), 2))
                continue
            logger.error(f"❌ {desc} 下载失败: {e}")
            return False
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(min(BACKOFF_BASE * (2**attempt), 2))
                continue
            logger.error(f"❌ {desc} 下载失败: {e}")
            return False

    return False


def download_layers(
    session: requests.Session,
    registry: str,
    repository: str,
    layers: List[Dict[str, Any]],
    auth_head: Dict[str, str],
    imgdir: str,
    resp_json: Dict[str, Any],
    tag: str,
    arch: str,
    output_dir: Path,
    repo_tag: str,
    repo_key: str,
):
    global progress_display
    progress_display = ProgressDisplay()

    os.makedirs(imgdir, exist_ok=True)

    progress_manager = DownloadProgressManager(output_dir, repository, tag, arch)
    stats = DownloadStats()
    progress_display.stats = stats

    # Config
    config_digest = resp_json["config"]["digest"]
    config_size = int(resp_json.get("config", {}).get("size") or 0)
    config_filename = f"{config_digest[7:]}.json"
    config_path = os.path.join(imgdir, config_filename)
    config_url = f"https://{registry}/v2/{repository}/blobs/{config_digest}"

    if progress_manager.is_config_completed() and os.path.exists(config_path):
        logger.info("✅ Config 已存在，跳过下载")
    else:
        progress_manager.update_config_status("downloading", digest=config_digest)
        # 性能优先：不在下载前串行 HEAD 探测大小，避免首屏等待
        progress_display.add_layer("Config", config_size, 0, len(layers) + 1)
        if not download_file_with_progress(session, config_url, auth_head, config_path, "Config", expected_digest=config_digest, stats=stats):
            progress_manager.update_config_status("failed")
            raise RuntimeError("Config 下载失败")
        progress_manager.update_config_status("completed", digest=config_digest)

    content = [{"Config": config_filename, "RepoTags": [repo_tag], "Layers": []}]
    parentid = ""
    layer_json_map: Dict[str, Dict[str, Any]] = {}

    layers_to_download: List[Tuple[str, str, str, str, int]] = []
    skipped_count = 0

    for layer in layers:
        ublob = layer["digest"]
        fake_layerid = hashlib.sha256((parentid + "\n" + ublob + "\n").encode("utf-8")).hexdigest()
        layerdir = f"{imgdir}/{fake_layerid}"
        os.makedirs(layerdir, exist_ok=True)
        layer_json_map[fake_layerid] = {"id": fake_layerid, "parent": parentid if parentid else None}
        parentid = fake_layerid

        save_path = f"{layerdir}/layer_gzip.tar"
        if progress_manager.is_layer_completed(ublob) and os.path.exists(save_path):
            skipped_count += 1
        else:
            known_size = int(layer.get("size") or 0)
            layers_to_download.append((ublob, fake_layerid, layerdir, save_path, known_size))

    if skipped_count:
        logger.info(f"📦 跳过 {skipped_count} 个已下载的层，还需下载 {len(layers_to_download)} 个层")

    # 性能优先：直接启动下载；优先使用 manifest 已提供的 size 预填
    for idx, (ublob, _, _, _, known_size) in enumerate(layers_to_download):
        progress_display.add_layer(ublob[:12], known_size, idx + 1, len(layers_to_download))

    progress_display.print_initial()

    num_workers = min(len(layers_to_download), MAX_PARALLEL_LAYERS) if layers_to_download else 1
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures: Dict[Any, Tuple[str, str]] = {}
        for ublob, _, _, save_path, _ in layers_to_download:
            if stop_event.is_set():
                raise KeyboardInterrupt
            url = f"https://{registry}/v2/{repository}/blobs/{ublob}"
            progress_manager.update_layer_status(ublob, "downloading")
            futures[
                executor.submit(
                    download_file_with_progress,
                    session,
                    url,
                    auth_head,
                    save_path,
                    ublob[:12],
                    ublob,
                    DOWNLOAD_MAX_RETRIES,
                    stats,
                )
            ] = (ublob, save_path)

        for future in as_completed(futures):
            if stop_event.is_set():
                raise KeyboardInterrupt
            ublob, _ = futures[future]
            ok = future.result()
            if not ok:
                progress_manager.update_layer_status(ublob, "failed")
                raise RuntimeError(f"层 {ublob[:12]} 下载失败")
            progress_manager.update_layer_status(ublob, "completed")

    print()

    # 解压 + 写 json
    for fake_layerid in layer_json_map.keys():
        if stop_event.is_set():
            raise KeyboardInterrupt("用户已取消操作")
        layerdir = f"{imgdir}/{fake_layerid}"
        gz_path = f"{layerdir}/layer_gzip.tar"
        tar_path = f"{layerdir}/layer.tar"
        if os.path.exists(gz_path):
            with gzip.open(gz_path, "rb") as gz, open(tar_path, "wb") as file:
                shutil.copyfileobj(gz, file)
            os.remove(gz_path)

        with open(f"{layerdir}/json", "w", encoding="utf-8") as file:
            json.dump(layer_json_map[fake_layerid], file)
        content[0]["Layers"].append(f"{fake_layerid}/layer.tar")

    with open(os.path.join(imgdir, "manifest.json"), "w", encoding="utf-8") as file:
        json.dump(content, file)

    with open(os.path.join(imgdir, "repositories"), "w", encoding="utf-8") as file:
        json.dump({repo_key: {tag: parentid}}, file)

    if stats.start_time > 0:
        elapsed = time.time() - stats.start_time
        avg_speed = stats.get_avg_speed()
        logger.info(f"📊 平均下载速度: {stats.format_size(int(avg_speed))}/s")
        logger.info(f"⏱️  总耗时: {stats.format_time(elapsed)}")

    logger.info("✅ 下载完成！")
    progress_manager.clear_progress()


def create_image_tar(imgdir: str, repository: str, tag: str, arch: str, output_dir: Path) -> str:
    safe_repo = repository.replace("/", "_")
    docker_tar = str(output_dir / f"{safe_repo}_{tag}_{arch}.tar")
    with tarfile.open(docker_tar, "w") as tar:
        tar.add(imgdir, arcname="/")
    # 清理 layers 目录
    try:
        if os.path.exists(imgdir):
            shutil.rmtree(imgdir)
    except Exception:
        pass
    return docker_tar


def cleanup_tmp_dir():
    tmp_dir = "tmp"
    try:
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)
    except Exception:
        pass


def search_1ms(session: requests.Session, api_base: str, query: str, page: int, page_size: int) -> Dict[str, Any]:
    url = f"{api_base}/search"
    resp = session.get(url, params={"query": query, "page": page, "page_size": page_size}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"搜索接口返回异常: {data}")
    return data["data"]


def get_detail_1ms(session: requests.Session, api_base: str, repositories: str) -> Dict[str, Any]:
    url = f"{api_base}/get_detail"
    resp = session.get(url, params={"repositories": repositories}, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"详情接口返回异常: {payload}")
    return payload.get("data") or {}


def get_tags_1ms(
    session: requests.Session,
    api_base: str,
    repositories: str,
    page: int,
    page_size: int,
    search: str = "",
    sort_by: str = "last_updated",
    sort_order: str = "DESC",
) -> Dict[str, Any]:
    url = f"{api_base}/get_tags"
    params = {
        "repositories": repositories,
        "page": page,
        "page_size": page_size,
        "search": search,
        "sort_by": sort_by,
        "sort_order": sort_order,
    }
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"Tag 接口返回异常: {payload}")
    return payload.get("data") or {}


def _fmt_time(t: str) -> str:
    # 2026-04-07T19:51:13.804481832Z -> 2026-04-07 19:51:13
    if not t:
        return ""
    t = t.replace("T", " ").replace("Z", "")
    return t.split(".")[0]


def _fmt_time_compact(t: str) -> str:
    # 搜索结果里的更新时间只保留到分钟，避免列太长影响阅读
    formatted = _fmt_time(t)
    if len(formatted) >= 16:
        return formatted[:16]
    return formatted


def _max_last_pushed(images: List[Dict[str, Any]]) -> str:
    # 字符串排序对 ISO8601 基本可用（同一格式）
    times = [img.get("last_pushed") for img in images if img.get("last_pushed")]
    return _fmt_time(max(times)) if times else ""


def _fmt_arch(img: Dict[str, Any]) -> str:
    os_ = img.get("os") or "unknown"
    arch = img.get("architecture") or "unknown"
    variant = img.get("variant") or ""
    if variant:
        return f"{os_}/{arch}/{variant}"
    return f"{os_}/{arch}"


def interactive_tag_select(
    session: requests.Session,
    api_base: str,
    repositories: str,
    page_size: int = 8,
    default_tag: str = "latest",
) -> Tuple[str, Dict[str, Any]]:
    """
    返回：(tag_name, tag_item)
    tag_item 为 get_tags 的 list 内条目（含 images 列表）
    """
    page = 1
    search = ""

    while True:
        data = get_tags_1ms(session, api_base, repositories, page, page_size, search=search)
        total = int(data.get("total", 0) or 0)
        items = data.get("list", []) or []
        total_pages = max(1, (total + page_size - 1) // page_size)

        print("\n" + "=" * 80)
        title = f"🏷️ Tag 列表: {repositories}    页码: {page}/{total_pages}    总数: {total}"
        if search:
            title += f"    搜索: {search}"
        print(title)
        print("-" * 80)

        # 显示：tag_name、最近推送、架构数量、示例架构、大小（取 amd64/linux 的 size）
        for idx, it in enumerate(items, start=1):
            tag_name = it.get("tag_name") or it.get("name") or ""
            images = it.get("images") or []
            last_pushed = _max_last_pushed(images)
            linux_imgs = [im for im in images if (im.get("os") == "linux" and im.get("architecture") not in (None, "", "unknown"))]
            arch_count = len(linux_imgs)
            arch_preview = ", ".join([_fmt_arch(im) for im in linux_imgs[:3]])

            size = ""
            for im in linux_imgs:
                if im.get("architecture") == "amd64":
                    try:
                        size = DownloadStats().format_size(int(im.get("size") or 0))
                    except Exception:
                        size = ""
                    break
            default_mark = " (默认)" if tag_name == default_tag else ""

            print(f"{idx:>2}. {tag_name}{default_mark}")
            meta = []
            if last_pushed:
                meta.append(f"last_pushed={last_pushed}")
            if arch_count:
                meta.append(f"archs={arch_count}")
            if size:
                meta.append(f"amd64_size≈{size}")
            if meta:
                print(f"    {'  '.join(meta)}")
            if arch_preview:
                print(f"    {arch_preview}")

        print("-" * 80)
        print("输入：序号=选择  n=下一页  p=上一页  g=跳页  s=搜索tag  q=退出")
        cmd = input("你的选择: ").strip().lower()

        if cmd in ("q", "quit", "exit"):
            raise KeyboardInterrupt("用户退出")
        if cmd in ("n", "next"):
            if page < total_pages:
                page += 1
            else:
                print("已是最后一页")
            continue
        if cmd in ("p", "prev", "previous"):
            if page > 1:
                page -= 1
            else:
                print("已是第一页")
            continue
        if cmd in ("g", "goto"):
            try:
                new_page = int(input(f"请输入页码(1-{total_pages}): ").strip())
                if 1 <= new_page <= total_pages:
                    page = new_page
                else:
                    print("页码超出范围")
            except Exception:
                print("页码输入无效")
            continue
        if cmd in ("s", "search"):
            search = input("输入 tag 搜索关键词（留空清空搜索）: ").strip()
            page = 1
            continue

        try:
            selected = int(cmd)
            if 1 <= selected <= len(items):
                it = items[selected - 1]
                tag_name = it.get("tag_name") or it.get("name") or ""
                if not tag_name:
                    print("该条目缺少 tag_name，换一个试试")
                    continue
                return tag_name, it
            print("序号超出范围")
        except Exception:
            print("输入无效")


def interactive_search_and_select(
    session: requests.Session,
    api_base: str,
    keyword: str,
    page_size: int = 10,
) -> Tuple[Dict[str, Any], int]:
    page = 1
    while True:
        data = search_1ms(session, api_base, keyword, page, page_size)
        total = data.get("total", 0)
        items = data.get("list", [])
        total_pages = max(1, (total + page_size - 1) // page_size)

        print("\n" + "=" * 80)
        print(f"🔎 关键词: {keyword}    页码: {page}/{total_pages}    总数: {total}")
        print("-" * 80)
        for idx, it in enumerate(items, start=1):
            ns = it.get("namespace", "")
            name = it.get("name", "")
            desc = (it.get("description") or "").strip().replace("\n", " ")
            pulls = format_big_number(it.get("pull_count", 0))
            stars = format_big_number(it.get("star_count", 0))
            last = _fmt_time_compact(it.get("last_updated", "") or it.get("last_modified", ""))
            show_name = f"{ns}/{name}" if ns else name
            print(f"{idx:>2}. {show_name:<35} ⭐{stars:<8} ⬇{pulls:<10}  {last}")
            if desc:
                print(f"    {desc[:120]}")


        print("-" * 80)
        print("输入：序号=选择  n=下一页  p=上一页  g=跳页  k=换关键词  q=退出")
        cmd = input("你的选择: ").strip().lower()

        if cmd in ("q", "quit", "exit"):
            raise KeyboardInterrupt("用户退出")
        if cmd in ("n", "next"):
            if page < total_pages:
                page += 1
            else:
                print("已是最后一页")
            continue
        if cmd in ("p", "prev", "previous"):
            if page > 1:
                page -= 1
            else:
                print("已是第一页")
            continue
        if cmd in ("g", "goto"):
            try:
                new_page = int(input(f"请输入页码(1-{total_pages}): ").strip())
                if 1 <= new_page <= total_pages:
                    page = new_page
                else:
                    print("页码超出范围")
            except Exception:
                print("页码输入无效")
            continue
        if cmd in ("k", "keyword"):
            keyword = input("请输入新的关键词: ").strip()
            if not keyword:
                print("关键词不能为空")
                continue
            page = 1
            continue

        try:
            selected = int(cmd)
            if 1 <= selected <= len(items):
                return items[selected - 1], page
            print("序号超出范围")
        except Exception:
            print("输入无效")


def pick_arch_from_manifest_list(
    manifests: List[Dict[str, Any]],
    default_arch: str = "amd64",
    interactive: bool = True,
) -> str:
    archs: List[str] = []
    for m in manifests:
        if m.get("platform", {}).get("os") != "linux":
            continue
        arch = m.get("annotations", {}).get("com.docker.official-images.bashbrew.arch") or m.get("platform", {}).get("architecture")
        if arch and arch not in archs:
            archs.append(arch)

    if not archs:
        return default_arch

    # 非交互：直接返回默认架构（若不可用则用第一个）
    if not interactive:
        if default_arch in archs:
            return default_arch
        return archs[0]

    print("\n📋 当前可用架构：")
    for i, a in enumerate(archs, start=1):
        flag = " (默认)" if a == default_arch else ""
        print(f"  {i}. {a}{flag}")

    if len(archs) == 1:
        print(f"✅ 自动选择唯一可用架构: {archs[0]}")
        return archs[0]

    # 默认值如果不在列表，退回第一项
    if default_arch not in archs:
        default_arch = archs[0]

    inp = input(f"请选择架构序号（默认 {default_arch}）: ").strip()
    if not inp:
        return default_arch
    try:
        idx = int(inp)
        if 1 <= idx <= len(archs):
            return archs[idx - 1]
    except Exception:
        pass
    print("输入无效，使用默认架构")
    return default_arch


def build_image_info_from_search_item(registry: str, item: Dict[str, Any], tag: str) -> ImageInfo:
    namespace = item.get("namespace") or "library"
    name = item.get("name")
    if not name:
        raise ValueError("搜索结果中缺少 name")
    repository = f"{namespace}/{name}"
    return ImageInfo(registry=registry, repository=repository, image_name=name, tag=tag)


def main():
    try:
        parser = argparse.ArgumentParser(description="1ms Docker 镜像下载专版（关键词搜索 + 一键下载）")
        parser.add_argument("-k", "--keyword", help="关键词（不传则启动后交互输入）")
        parser.add_argument("--api", default=DEFAULT_1MS_API, help=f"1ms API 地址，默认：{DEFAULT_1MS_API}")
        parser.add_argument("--registry", default=DEFAULT_1MS_REGISTRY, help=f"1ms registry 地址，默认：{DEFAULT_1MS_REGISTRY}")
        parser.add_argument("--page-size", type=int, default=10, help="搜索分页大小，默认 10")
        parser.add_argument("-t", "--tag", default="", help="镜像 tag（不传则从 1ms tag 列表中选择）")
        parser.add_argument("-a", "--arch", default="amd64", help="默认架构（当存在多架构时作为默认值），默认 amd64")
        parser.add_argument("-o", "--output", help="输出目录，默认当前目录")
        parser.add_argument("--no-download", action="store_true", help="仅验证搜索与 manifest（不下载层）")
        parser.add_argument("--select-index", type=int, help="配合 --keyword：自动选择当前页的第 N 个结果（用于脚本化/验证）")
        parser.add_argument("--page", type=int, default=1, help="配合 --select-index：指定页码，默认 1")
        parser.add_argument("--debug", action="store_true", help="调试模式")
        args = parser.parse_args()

        if args.debug:
            logger.setLevel(logging.DEBUG)

        logger.info(f"🚀 1ms Docker 镜像下载专版 {VERSION}")

        session = SessionManager.get_session()

        # 1) 选择镜像
        if not args.keyword:
            args.keyword = input("请输入关键词（例如 nginx）: ").strip()
            if not args.keyword:
                logger.error("关键词不能为空")
                return

        selected_item: Dict[str, Any]
        if args.select_index:
            data = search_1ms(session, args.api, args.keyword, args.page, args.page_size)
            items = data.get("list", [])
            if not items:
                logger.error("没有搜索到结果")
                return
            if not (1 <= args.select_index <= len(items)):
                logger.error(f"--select-index 超出范围：1-{len(items)}")
                return
            selected_item = items[args.select_index - 1]
        else:
            selected_item, _ = interactive_search_and_select(session, args.api, args.keyword, args.page_size)

        namespace = selected_item.get("namespace") or "library"
        name = selected_item.get("name") or ""
        show_name = f"{namespace}/{name}" if namespace else name
        print(f"\n✅ 已选择镜像：{show_name}")

        repositories = f"{namespace}/{name}"

        # 2) 详情（展示一下，简化用户判断）
        try:
            detail = get_detail_1ms(session, args.api, repositories)
            desc = (detail.get("description") or "").strip()
            cats = ", ".join([c.get("name") for c in (detail.get("categories") or []) if c.get("name")]) or ""
            reg = detail.get("date_registered") or ""
            print("\n📌 镜像详情（来自 1ms）：")
            if desc:
                print(f"  描述：{desc}")
            if cats:
                print(f"  分类：{cats}")
            if reg:
                print(f"  注册：{_fmt_time(reg)}")
        except Exception as e:
            logger.warning(f"获取详情失败（可忽略）：{e}")

        # 3) tag（来自 1ms /get_tags）
        # 非交互：保持兼容，如果未传 --tag 则默认 latest
        is_interactive = (args.select_index is None) and sys.stdin.isatty()
        chosen_tag_item: Dict[str, Any] = {}
        if args.tag:
            chosen_tag = args.tag
        elif not is_interactive:
            chosen_tag = "latest"
        else:
            chosen_tag, chosen_tag_item = interactive_tag_select(
                session=session,
                api_base=args.api,
                repositories=repositories,
                page_size=8,
                default_tag="latest",
            )
        args.tag = chosen_tag
        print(f"\n✅ 已选择 tag：{args.tag}")

        image_info = build_image_info_from_search_item(args.registry, selected_item, args.tag)

        # 3) registry auth（尽量兼容有 token 的镜像站）
        auth_head: Dict[str, str] = {
            "Accept": ", ".join(
                [
                    "application/vnd.docker.distribution.manifest.v2+json",
                    "application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.oci.image.index.v1+json",
                    "application/vnd.oci.image.manifest.v1+json",
                ]
            )
        }
        try:
            ping_url = f"https://{image_info.registry}/v2/"
            resp = session.get(ping_url, verify=False, timeout=30)
            if resp.status_code == 401 and "WWW-Authenticate" in resp.headers:
                www = resp.headers["WWW-Authenticate"]
                auth_url = www.split('"')[1]
                reg_service = www.split('"')[3]
                auth_head = get_auth_head(session, auth_url, reg_service, image_info.repository)
        except Exception as e:
            logger.warning(f"连接 registry 探测认证失败，将继续尝试直接拉取 manifest: {e}")

        # 4) 拉取 manifest（tag）
        resp, code = fetch_manifest(session, image_info.registry, image_info.repository, image_info.tag, auth_head)
        if code == 401:
            logger.error("registry 需要认证，但未提供登录方式（当前专版默认拉取公开镜像）")
            return
        resp_json = resp.json()

        # 5) 多架构 -> 选择架构 -> 再请求对应 digest 的 manifest
        manifests = resp_json.get("manifests")
        if manifests is not None:
            # 当使用 --select-index 或 stdin 非交互时，自动选择架构，不再询问
            args.arch = pick_arch_from_manifest_list(manifests, args.arch, interactive=is_interactive)
            digest = select_manifest_digest(manifests, args.arch)
            if not digest:
                logger.error(f"在清单中找不到指定架构 {args.arch}")
                return
            manifest_resp, code2 = fetch_manifest(session, image_info.registry, image_info.repository, digest, auth_head)
            if code2 != 200:
                logger.error("获取架构清单失败")
                return
            resp_json = manifest_resp.json()

        if "layers" not in resp_json or "config" not in resp_json:
            logger.error("错误：清单格式不完整，缺少 layers/config")
            return

        # 6) 输出信息
        logger.info(f"📦 registry：{image_info.registry}")
        logger.info(f"📦 repository：{image_info.repository}")
        logger.info(f"📦 tag：{image_info.tag}")
        logger.info(f"📦 arch：{args.arch}")

        # RepoTags：对 library/xxx 进行“官方风格”处理，避免 docker load 后出现 library/nginx
        if image_info.repository.startswith("library/"):
            repo_tag = f"{image_info.image_name}:{image_info.tag}"
            repo_key = image_info.image_name
        else:
            repo_tag = f"{image_info.repository}:{image_info.tag}"
            repo_key = image_info.repository

        output_dir = get_output_dir(image_info.repository, image_info.tag, args.arch, args.output)
        imgdir = str(output_dir / "layers")
        os.makedirs(imgdir, exist_ok=True)
        logger.info(f"📁 输出目录：{output_dir}")

        if args.no_download:
            logger.info("🧪 --no-download 已开启：已完成搜索与 manifest 验证，未下载任何层。")
            return

        logger.info("📥 开始下载...")
        download_layers(
            session=session,
            registry=image_info.registry,
            repository=image_info.repository,
            layers=resp_json["layers"],
            auth_head=auth_head,
            imgdir=imgdir,
            resp_json=resp_json,
            tag=image_info.tag,
            arch=args.arch,
            output_dir=output_dir,
            repo_tag=repo_tag,
            repo_key=repo_key,
        )

        output_file = create_image_tar(imgdir, image_info.repository, image_info.tag, args.arch, output_dir)
        logger.info(f"✅ 镜像已保存为: {output_file}")
        logger.info(f"💡 导入命令: docker load -i {output_file}")
        logger.info(f"💡 如需改名/打 tag: docker tag {repo_tag} 你的新名字:tag")

    except KeyboardInterrupt:
        logger.info("⚠️ 用户取消操作。")
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ 网络连接失败: {e}")
    except json.JSONDecodeError as e:
        logger.error(f"❌ JSON解析失败: {e}")
    except Exception as e:
        logger.error(f"❌ 程序运行过程中发生异常: {e}")
        import traceback

        logger.debug(traceback.format_exc())
    finally:
        cleanup_tmp_dir()


if __name__ == "__main__":
    main()
