import os
import sys
import gzip
import json
import hashlib
import shutil
import threading
import time
import warnings

warnings.filterwarnings('ignore', message='urllib3.*doesn\'t match a supported version')
warnings.filterwarnings('ignore', category=UserWarning, module='requests')

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

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

urllib3.disable_warnings()

VERSION = "v1.8.0"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    encoding='utf-8'
)
logger = logging.getLogger(__name__)

stop_event = threading.Event()
progress_lock = threading.Lock()
original_sigint_handler = None


def signal_handler(signum, frame):
    global stop_event
    if stop_event.is_set():
        print('\n⚠️ 强制退出...')
        if original_sigint_handler:
            signal.signal(signal.SIGINT, original_sigint_handler)
            raise KeyboardInterrupt
        sys.exit(1)
    
    stop_event.set()
    print('\n⚠️ 收到中断信号，正在保存进度并优雅退出...')
    print('💡 再次按 Ctrl+C 强制退出')


original_sigint_handler = signal.signal(signal.SIGINT, signal_handler)


@dataclass
class ImageInfo:
    registry: str
    repository: str
    image_name: str
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
        for unit in ['B', 'KB', 'MB', 'GB']:
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
        self.status = 'waiting'
        self.chunk_count = 0
        self.total_chunks = 0
        self.current_chunk = 0
        self.retry_count = 0
        self.is_resume = False

    def update(self, downloaded: int, chunk_info: str = ''):
        self.downloaded_size = downloaded
        self.chunk_info = chunk_info

    def set_chunk_info(self, current: int, total: int):
        self.current_chunk = current
        self.total_chunks = total

    @staticmethod
    def format_size(size: int) -> str:
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f}{unit}"
            size /= 1024
        return f"{size:.1f}TB"


class ProgressDisplay:
    def __init__(self, bar_width: int = 30):
        self.bar_width = bar_width
        self.layers: Dict[str, LayerProgress] = {}
        self.stats: Optional[DownloadStats] = None
        self.last_update = 0
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
                self.layers[name].status = 'downloading'
        self._refresh_display()

    def complete_layer(self, name: str):
        with progress_lock:
            if name in self.layers:
                self.layers[name].downloaded_size = self.layers[name].total_size
                self.layers[name].status = 'completed'
        self._refresh_display()

    def set_chunk_info(self, name: str, current: int, total: int):
        with progress_lock:
            if name in self.layers:
                self.layers[name].current_chunk = current
                self.layers[name].total_chunks = total

    def _refresh_display(self):
        current_time = time.time()
        if current_time - self.last_update < self.update_interval:
            return
        self.last_update = current_time

        with progress_lock:
            lines = []
            for name, layer in sorted(self.layers.items(), key=lambda x: x[1].index):
                line = self._format_layer_line(layer)
                lines.append(line)
            
            if self.stats:
                speed = self.stats.get_avg_speed()
                speed_str = self.stats.format_size(int(speed)) if speed > 0 else "0B"
                lines.append(f"📊 速度: {speed_str}/s")

            if self.initialized and self.last_line_count > 0:
                for _ in range(self.last_line_count):
                    sys.stdout.write('\033[F')
                sys.stdout.write('\033[J')
            
            for line in lines:
                print(line)
            
            self.last_line_count = len(lines)
            self.initialized = True
            sys.stdout.flush()

    def _format_layer_line(self, layer: LayerProgress) -> str:
        if layer.total_size > 0:
            progress = layer.downloaded_size / layer.total_size
        else:
            progress = 0

        filled = int(self.bar_width * progress)
        empty = self.bar_width - filled
        
        bar = '█' * filled + '░' * empty
        
        size_str = f"{layer.format_size(layer.downloaded_size)}/{layer.format_size(layer.total_size)}"
        
        chunk_info = ""
        if layer.total_chunks > 0:
            chunk_info = f" [{layer.current_chunk}/{layer.total_chunks}]"
        
        status_icon = "✅" if layer.status == 'completed' else "⬇️"
        
        retry_info = ""
        if layer.retry_count > 0:
            retry_info = f" 🔄{layer.retry_count}"
        
        resume_info = ""
        if layer.is_resume:
            resume_info = " 📎"
        
        return f"  {status_icon} ({layer.index}/{layer.total_layers}) {layer.name[:12]:<12} |{bar}| {progress*100:5.1f}% {size_str:>15}{chunk_info}{retry_info}{resume_info}"

    def print_initial(self):
        with progress_lock:
            for name, layer in sorted(self.layers.items(), key=lambda x: x[1].index):
                line = self._format_layer_line(layer)
                print(line)
            if self.stats:
                print(f"📊 速度: 计算中...")
            self.last_line_count = len(self.layers) + 1
            self.initialized = True


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
            total=10,
            backoff_factor=3,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD", "OPTIONS"]
        )

        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=20,
            pool_maxsize=50,
            pool_block=False
        )

        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.timeout = (60, 600)

        session.proxies = {
            'http': os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy'),
            'https': os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
        }
        if session.proxies.get('http') or session.proxies.get('https'):
            logger.info('🌐 使用代理设置从环境变量')

        return session


def get_output_dir(repository: str, tag: str, arch: str, output_path: Optional[str] = None) -> Path:
    safe_repo = repository.replace("/", "_").replace(":", "_")
    dir_name = f"{safe_repo}_{tag}_{arch}"

    if output_path:
        output_dir = Path(output_path) / dir_name
    else:
        output_dir = Path.cwd() / dir_name

    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def parse_image_input(image_input: str, custom_registry: Optional[str] = None) -> ImageInfo:
    if '/' in image_input and ('.' in image_input.split('/')[0] or ':' in image_input.split('/')[0]):
        registry, remainder = image_input.split('/', 1)
        parts = remainder.split('/')

        if len(parts) == 1:
            repo = ''
            img_tag = parts[0]
        else:
            repo = '/'.join(parts[:-1])
            img_tag = parts[-1]

        img, *tag_parts = img_tag.split(':')
        tag = tag_parts[0] if tag_parts else 'latest'
        repository = remainder.split(':')[0]

        return ImageInfo(registry, repository, img, tag)
    else:
        parts = image_input.split('/')
        if len(parts) == 1:
            repo = 'library'
            img_tag = parts[0]
        else:
            repo = '/'.join(parts[:-1])
            img_tag = parts[-1]

        img, *tag_parts = img_tag.split(':')
        tag = tag_parts[0] if tag_parts else 'latest'
        repository = f'{repo}/{img}'

        if not custom_registry:
            registry = 'registry-1.docker.io'
        else:
            registry = custom_registry

        return ImageInfo(registry, repository, img, tag)


def get_auth_head(
    session: requests.Session,
    auth_url: str,
    reg_service: str,
    repository: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    max_retries: int = 3
) -> Dict[str, str]:
    for attempt in range(max_retries):
        try:
            url = f'{auth_url}?service={reg_service}&scope=repository:{repository}:pull'

            headers = {}
            if username and password:
                auth_string = f"{username}:{password}"
                encoded_auth = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
                headers['Authorization'] = f'Basic {encoded_auth}'

            logger.debug(f"获取认证头: {url}")

            resp = session.get(url, headers=headers, verify=False, timeout=60)
            resp.raise_for_status()
            access_token = resp.json()['token']
            auth_head = {
                'Authorization': f'Bearer {access_token}',
                'Accept': 'application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json'
            }

            return auth_head
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                logger.warning(f'认证请求失败，{wait_time}秒后重试 ({attempt + 1}/{max_retries}): {e}')
                time.sleep(wait_time)
            else:
                logger.error(f'请求认证失败: {e}')
                raise


def fetch_manifest(
    session: requests.Session,
    registry: str,
    repository: str,
    tag: str,
    auth_head: Dict[str, str],
    max_retries: int = 3
) -> Tuple[requests.Response, int]:
    for attempt in range(max_retries):
        try:
            url = f'https://{registry}/v2/{repository}/manifests/{tag}'
            logger.debug(f'获取镜像清单: {url}')

            resp = session.get(url, headers=auth_head, verify=False, timeout=60)
            if resp.status_code == 401:
                logger.info('需要认证。')
                return resp, 401
            resp.raise_for_status()
            return resp, 200
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                logger.warning(f'清单请求失败，{wait_time}秒后重试 ({attempt + 1}/{max_retries}): {e}')
                time.sleep(wait_time)
            else:
                logger.error(f'请求清单失败: {e}')
                raise


def select_manifest(manifests: List[Dict], arch: str) -> Optional[str]:
    for m in manifests:
        if (m.get('annotations', {}).get('com.docker.official-images.bashbrew.arch') == arch or
            m.get('platform', {}).get('architecture') == arch) and \
                m.get('platform', {}).get('os') == 'linux':
            return m.get('digest')
    return None


class DownloadProgressManager:
    def __init__(self, output_dir: Path, repository: str, tag: str, arch: str):
        self.output_dir = output_dir
        self.repository = repository
        self.tag = tag
        self.arch = arch
        self.progress_file = output_dir / 'progress.json'
        self.progress_data = self.load_progress()

    def load_progress(self) -> Dict[str, Any]:
        if self.progress_file.exists():
            try:
                with open(self.progress_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                    metadata = data.get('metadata', {})
                    if (metadata.get('repository') == self.repository and
                            metadata.get('tag') == self.tag and
                            metadata.get('arch') == self.arch):

                        logger.info(f'📋 加载已有下载进度，共 {len(data.get("layers", {}))} 个文件')
                        return data
                    else:
                        logger.warning(f'进度文件镜像信息不匹配，将创建新的进度')
                        return self._create_new_progress()

            except Exception as e:
                logger.warning(f'加载进度文件失败: {e}')

        return self._create_new_progress()

    def _create_new_progress(self) -> Dict[str, Any]:
        return {
            'metadata': {
                'repository': self.repository,
                'tag': self.tag,
                'arch': self.arch,
                'created_at': time.strftime('%Y-%m-%d %H:%M:%S')
            },
            'layers': {},
            'config': None
        }

    def save_progress(self):
        try:
            with open(self.progress_file, 'w', encoding='utf-8') as f:
                json.dump(self.progress_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f'保存进度文件失败: {e}')

    def update_layer_status(self, digest: str, status: str, **kwargs):
        if digest not in self.progress_data['layers']:
            self.progress_data['layers'][digest] = {}

        self.progress_data['layers'][digest]['status'] = status
        self.progress_data['layers'][digest].update(kwargs)
        self.save_progress()

    def get_layer_status(self, digest: str) -> Dict[str, Any]:
        return self.progress_data['layers'].get(digest, {})

    def is_layer_completed(self, digest: str) -> bool:
        layer_info = self.get_layer_status(digest)
        return layer_info.get('status') == 'completed'

    def update_config_status(self, status: str, **kwargs):
        if self.progress_data['config'] is None:
            self.progress_data['config'] = {}
        self.progress_data['config']['status'] = status
        self.progress_data['config'].update(kwargs)
        self.save_progress()

    def is_config_completed(self) -> bool:
        config_data = self.progress_data.get('config')
        if config_data is None:
            return False
        return config_data.get('status') == 'completed'

    def clear_progress(self):
        if self.progress_file.exists():
            try:
                self.progress_file.unlink()
                logger.debug('进度文件已清除')
            except Exception as e:
                logger.error(f'清除进度文件失败: {e}')


def get_file_size(session: requests.Session, url: str, headers: Dict[str, str]) -> int:
    try:
        resp = session.head(url, headers=headers, verify=False, timeout=30)
        if resp.status_code == 200:
            return int(resp.headers.get('content-length', 0))
    except:
        pass
    return 0


def download_file_with_progress(
    session: requests.Session,
    url: str,
    headers: Dict[str, str],
    save_path: str,
    desc: str,
    expected_digest: Optional[str] = None,
    max_retries: int = 10,
    stats: Optional[DownloadStats] = None,
    chunk_size: int = 10 * 1024 * 1024
) -> bool:
    CHUNK_THRESHOLD = 50 * 1024 * 1024
    
    for attempt in range(max_retries):
        if stop_event.is_set():
            return False

        resume_pos = 0
        if os.path.exists(save_path):
            resume_pos = os.path.getsize(save_path)

        download_headers = headers.copy()
        if resume_pos > 0:
            download_headers['Range'] = f'bytes={resume_pos}-'

        try:
            with session.get(url, headers=download_headers, verify=False, timeout=120, stream=True) as resp:
                if resp.status_code == 416:
                    progress_display.complete_layer(desc)
                    return True

                resp.raise_for_status()

                content_range = resp.headers.get('content-range')
                if content_range:
                    total_size = int(content_range.split('/')[1])
                else:
                    total_size = int(resp.headers.get('content-length', 0)) + resume_pos

                if total_size - resume_pos > CHUNK_THRESHOLD and resume_pos == 0:
                    return download_file_in_chunks(
                        session, url, headers, save_path, desc, 
                        total_size, expected_digest, max_retries, stats, chunk_size
                    )

                mode = 'ab' if resume_pos > 0 else 'wb'
                sha256_hash = hashlib.sha256() if expected_digest else None

                if resume_pos > 0 and sha256_hash:
                    with open(save_path, 'rb') as existing_file:
                        while True:
                            chunk = existing_file.read(65536)
                            if not chunk:
                                break
                            sha256_hash.update(chunk)

                if stats:
                    stats.total_size += total_size - resume_pos
                    if stats.start_time == 0:
                        stats.start_time = time.time()

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
                                current_time = time.time()
                                if current_time - last_update_time >= 0.5:
                                    speed = (downloaded_size - last_downloaded) / (current_time - last_update_time)
                                    stats.speeds.append(speed)
                                    last_downloaded = downloaded_size
                                    last_update_time = current_time

                if expected_digest and sha256_hash:
                    actual_digest = f'sha256:{sha256_hash.hexdigest()}'
                    if actual_digest != expected_digest:
                        logger.error(f'❌ {desc} 校验失败！')
                        if os.path.exists(save_path):
                            os.remove(save_path)
                        if attempt < max_retries - 1:
                            wait_time = min(2 ** attempt, 60)
                            time.sleep(wait_time)
                        continue

                progress_display.complete_layer(desc)
                return True

        except KeyboardInterrupt:
            return False
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < max_retries - 1:
                wait_time = min(2 ** attempt, 60)
                time.sleep(wait_time)
                continue
            else:
                logger.error(f'❌ {desc} 下载失败')
                return False
        except requests.exceptions.HTTPError as e:
            if e.response.status_code in [429, 500, 502, 503, 504] and attempt < max_retries - 1:
                wait_time = min(2 ** attempt, 60)
                time.sleep(wait_time)
                continue
            else:
                logger.error(f'❌ {desc} 下载失败: {e}')
                return False
        except Exception as e:
            logger.error(f'❌ {desc} 下载失败: {e}')
            if attempt < max_retries - 1:
                wait_time = min(2 ** attempt, 60)
                time.sleep(wait_time)
                continue
            return False

    return False


def download_file_in_chunks(
    session: requests.Session,
    url: str,
    headers: Dict[str, str],
    save_path: str,
    desc: str,
    total_size: int,
    expected_digest: Optional[str] = None,
    max_retries: int = 10,
    stats: Optional[DownloadStats] = None,
    chunk_size: int = 10 * 1024 * 1024
) -> bool:
    num_chunks = (total_size + chunk_size - 1) // chunk_size
    temp_dir = save_path + '.chunks'
    
    progress_display.set_chunk_info(desc, 0, num_chunks)
    
    try:
        os.makedirs(temp_dir, exist_ok=True)
        
        chunk_files = []
        for i in range(num_chunks):
            start = i * chunk_size
            end = min((i + 1) * chunk_size, total_size)
            chunk_file = os.path.join(temp_dir, f'chunk_{i:04d}')
            chunk_files.append((start, end, chunk_file))
        
        completed_size = 0
        for existing_start, existing_end, existing_chunk_file in chunk_files:
            if os.path.exists(existing_chunk_file):
                completed_size += os.path.getsize(existing_chunk_file)
        
        if stats:
            stats.total_size += total_size - completed_size
            if stats.start_time == 0:
                stats.start_time = time.time()
        
        sha256_hash = hashlib.sha256() if expected_digest else None
        
        for i, (start, end, chunk_file) in enumerate(chunk_files):
            if stop_event.is_set():
                return False
            
            progress_display.set_chunk_info(desc, i + 1, num_chunks)
            
            if os.path.exists(chunk_file):
                existing_size = os.path.getsize(chunk_file)
                if existing_size == end - start:
                    if sha256_hash:
                        with open(chunk_file, 'rb') as f:
                            while True:
                                data = f.read(65536)
                                if not data:
                                    break
                                sha256_hash.update(data)
                    completed_size += (end - start)
                    progress_display.update_layer(desc, completed_size)
                    continue
            
            chunk_headers = headers.copy()
            chunk_headers['Range'] = f'bytes={start}-{end-1}'
            
            for attempt in range(max_retries):
                if stop_event.is_set():
                    return False
                
                try:
                    chunk_downloaded = 0
                    with session.get(url, headers=chunk_headers, verify=False, timeout=120, stream=True) as resp:
                        resp.raise_for_status()
                        
                        with open(chunk_file, 'wb') as f:
                            for data in resp.iter_content(chunk_size=65536):
                                if stop_event.is_set():
                                    return False
                                if data:
                                    f.write(data)
                                    chunk_downloaded += len(data)
                                    completed_size += len(data)
                                    if sha256_hash:
                                        sha256_hash.update(data)
                                    progress_display.update_layer(desc, completed_size)
                        break
                except Exception as e:
                    if attempt < max_retries - 1:
                        wait_time = min(2 ** attempt, 60)
                        time.sleep(wait_time)
                    else:
                        logger.error(f'❌ {desc}: 分片 {i+1} 下载失败')
                        raise
        
        with open(save_path, 'wb') as outfile:
            for i, (_, _, chunk_file) in enumerate(chunk_files):
                if stop_event.is_set():
                    return False
                
                with open(chunk_file, 'rb') as infile:
                    while True:
                        data = infile.read(65536)
                        if not data:
                            break
                        outfile.write(data)
        
        shutil.rmtree(temp_dir, ignore_errors=True)
        
        if expected_digest and sha256_hash:
            actual_digest = f'sha256:{sha256_hash.hexdigest()}'
            if actual_digest != expected_digest:
                logger.error(f'❌ {desc} 校验失败！')
                if os.path.exists(save_path):
                    os.remove(save_path)
                return False
        
        progress_display.complete_layer(desc)
        return True
        
    except Exception as e:
        logger.error(f'❌ {desc} 分片下载失败: {e}')
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        return False


def download_layers(
    session: requests.Session,
    registry: str,
    repository: str,
    layers: List[Dict],
    auth_head: Dict[str, str],
    imgdir: str,
    resp_json: Dict,
    imgparts: List[str],
    img: str,
    tag: str,
    arch: str,
    output_dir: Path
):
    global progress_display
    progress_display = ProgressDisplay()

    os.makedirs(imgdir, exist_ok=True)

    progress_manager = DownloadProgressManager(output_dir, repository, tag, arch)
    stats = DownloadStats()
    progress_display.stats = stats

    try:
        config_digest = resp_json['config']['digest']
        config_filename = f'{config_digest[7:]}.json'
        config_path = os.path.join(imgdir, config_filename)
        config_url = f'https://{registry}/v2/{repository}/blobs/{config_digest}'

        if progress_manager.is_config_completed() and os.path.exists(config_path):
            logger.info(f'✅ Config 已存在，跳过下载')
        else:
            progress_manager.update_config_status('downloading', digest=config_digest)
            config_size = get_file_size(session, config_url, auth_head)
            progress_display.add_layer('Config', config_size, 0, len(layers) + 1)
            
            if not download_file_with_progress(
                session, config_url, auth_head, config_path, "Config",
                expected_digest=config_digest, stats=stats
            ):
                progress_manager.update_config_status('failed')
                raise Exception(f'Config 下载失败')

            progress_manager.update_config_status('completed', digest=config_digest)

    except Exception as e:
        logging.error(f'请求配置失败: {e}')
        return

    repo_tag = f'{"/".join(imgparts)}/{img}:{tag}' if imgparts else f'{img}:{tag}'
    content = [{'Config': config_filename, 'RepoTags': [repo_tag], 'Layers': []}]
    parentid = ''
    layer_json_map: Dict[str, Dict] = {}

    layers_to_download = []
    skipped_count = 0

    for layer in layers:
        ublob = layer['digest']
        fake_layerid = hashlib.sha256((parentid + '\n' + ublob + '\n').encode('utf-8')).hexdigest()
        layerdir = f'{imgdir}/{fake_layerid}'
        os.makedirs(layerdir, exist_ok=True)
        layer_json_map[fake_layerid] = {"id": fake_layerid, "parent": parentid if parentid else None}
        parentid = fake_layerid

        save_path = f'{layerdir}/layer_gzip.tar'

        if progress_manager.is_layer_completed(ublob) and os.path.exists(save_path):
            skipped_count += 1
        else:
            layers_to_download.append((ublob, fake_layerid, layerdir, save_path))

    if skipped_count > 0:
        logger.info(f'📦 跳过 {skipped_count} 个已下载的层，还需下载 {len(layers_to_download)} 个层')

    for idx, (ublob, fake_layerid, layerdir, save_path) in enumerate(layers_to_download):
        url = f'https://{registry}/v2/{repository}/blobs/{ublob}'
        layer_size = get_file_size(session, url, auth_head)
        progress_display.add_layer(ublob[:12], layer_size, idx + 1, len(layers_to_download))

    progress_display.print_initial()

    num_workers = min(len(layers_to_download), 4) if layers_to_download else 1

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {}
        try:
            for idx, (ublob, fake_layerid, layerdir, save_path) in enumerate(layers_to_download):
                if stop_event.is_set():
                    raise KeyboardInterrupt

                url = f'https://{registry}/v2/{repository}/blobs/{ublob}'
                progress_manager.update_layer_status(ublob, 'downloading')

                futures[executor.submit(
                    download_file_with_progress,
                    session,
                    url,
                    auth_head,
                    save_path,
                    ublob[:12],
                    expected_digest=ublob,
                    stats=stats
                )] = (ublob, save_path)

            for future in as_completed(futures):
                if stop_event.is_set():
                    raise KeyboardInterrupt

                ublob, save_path = futures[future]
                result = future.result()

                if not result:
                    progress_manager.update_layer_status(ublob, 'failed')
                    raise Exception(f'层 {ublob[:12]} 下载失败')
                else:
                    progress_manager.update_layer_status(ublob, 'completed')

        except KeyboardInterrupt:
            logging.error("用户终止下载，保存当前进度...")
            stop_event.set()
            executor.shutdown(wait=False)
            raise

    print()

    for fake_layerid in layer_json_map.keys():
        if stop_event.is_set():
            raise KeyboardInterrupt("用户已取消操作")

        layerdir = f'{imgdir}/{fake_layerid}'
        gz_path = f'{layerdir}/layer_gzip.tar'
        tar_path = f'{layerdir}/layer.tar'

        if os.path.exists(gz_path):
            with gzip.open(gz_path, 'rb') as gz, open(tar_path, 'wb') as file:
                shutil.copyfileobj(gz, file)
            os.remove(gz_path)

        json_path = f'{layerdir}/json'
        with open(json_path, 'w') as file:
            json.dump(layer_json_map[fake_layerid], file)

        content[0]['Layers'].append(f'{fake_layerid}/layer.tar')

    manifest_path = os.path.join(imgdir, 'manifest.json')
    with open(manifest_path, 'w') as file:
        json.dump(content, file)

    repositories_path = os.path.join(imgdir, 'repositories')
    with open(repositories_path, 'w') as file:
        json.dump({repository if '/' in repository else img: {tag: parentid}}, file)

    if stats.start_time > 0:
        elapsed = time.time() - stats.start_time
        avg_speed = stats.get_avg_speed()
        logger.info(f'📊 平均下载速度: {stats.format_size(int(avg_speed))}/s')
        logger.info(f'⏱️  总耗时: {stats.format_time(elapsed)}')

    logging.info(f'✅ 镜像 {img}:{tag} 下载完成！')
    progress_manager.clear_progress()


def create_image_tar(imgdir: str, repository: str, tag: str, arch: str, output_dir: Path) -> str:
    safe_repo = repository.replace("/", "_")
    docker_tar = str(output_dir / f'{safe_repo}_{tag}_{arch}.tar')
    try:
        with tarfile.open(docker_tar, "w") as tar:
            tar.add(imgdir, arcname='/')
        logger.debug(f'Docker 镜像已拉取：{docker_tar}')
        
        try:
            if os.path.exists(imgdir):
                shutil.rmtree(imgdir)
                logger.debug(f'已清理 layers 目录: {imgdir}')
        except Exception as e:
            logger.warning(f'清理 layers 目录失败: {e}')
        
        return docker_tar
    except Exception as e:
        logger.error(f'打包镜像失败: {e}')
        raise


def cleanup_tmp_dir():
    tmp_dir = 'tmp'
    try:
        if os.path.exists(tmp_dir):
            logger.debug(f'清理临时目录: {tmp_dir}')
            shutil.rmtree(tmp_dir)
            logger.debug('临时目录已清理。')
    except Exception as e:
        logger.error(f'清理临时目录失败: {e}')


def main():
    try:
        parser = argparse.ArgumentParser(
            description="Docker 镜像拉取工具 - 无需Docker环境直接下载镜像",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
示例:
  %(prog)s -i nginx:latest
  %(prog)s -i harbor.example.com/library/nginx:1.26.0 -u admin -p password
  %(prog)s -i alpine:latest -a arm64v8 -o ./downloads
            """
        )
        parser.add_argument("-i", "--image", required=False,
                            help="Docker 镜像名称（例如：nginx:latest 或 harbor.abc.com/abc/nginx:1.26.0）")
        parser.add_argument("-q", "--quiet", action="store_true", help="静默模式，减少交互")
        parser.add_argument("-r", "--custom-registry", help="自定义仓库地址（例如：harbor.abc.com）")
        parser.add_argument("-a", "--arch", default="amd64", help="架构,默认：amd64,常见：amd64, arm64v8等")
        parser.add_argument("-u", "--username", help="Docker 仓库用户名")
        parser.add_argument("-p", "--password", help="Docker 仓库密码")
        parser.add_argument("-o", "--output", help="输出目录，默认为当前目录下的镜像名_tag_arch目录")
        parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {VERSION}", help="显示版本信息")
        parser.add_argument("--debug", action="store_true", help="启用调试模式，打印请求 URL 和连接状态")
        parser.add_argument("--workers", type=int, default=4, help="并发下载线程数，默认4")

        logger.info(f'🚀 Docker 镜像拉取工具 {VERSION}')

        args = parser.parse_args()

        if args.debug:
            logger.setLevel(logging.DEBUG)

        if not args.image:
            args.image = input("请输入 Docker 镜像名称（例如：nginx:latest 或 harbor.abc.com/abc/nginx:1.26.0）：").strip()
            if not args.image:
                logger.error("错误：镜像名称是必填项。")
                return

        if not args.custom_registry and not args.quiet:
            args.custom_registry = input("请输入自定义仓库地址（默认 dockerhub）：").strip() or None

        image_info = parse_image_input(args.image, args.custom_registry)

        if not args.username and not args.quiet:
            args.username = input("请输入镜像仓库用户名：").strip() or None
        if not args.password and not args.quiet:
            args.password = input("请输入镜像仓库密码：").strip() or None

        session = SessionManager.get_session()
        auth_head = None

        try:
            url = f'https://{image_info.registry}/v2/'
            logger.debug(f"获取认证信息: {url}")
            resp = session.get(url, verify=False, timeout=60)
            auth_url = resp.headers['WWW-Authenticate'].split('"')[1]
            reg_service = resp.headers['WWW-Authenticate'].split('"')[3]
            auth_head = get_auth_head(
                session, auth_url, reg_service, image_info.repository,
                args.username, args.password
            )

            resp, http_code = fetch_manifest(
                session, image_info.registry, image_info.repository,
                image_info.tag, auth_head
            )

            if http_code == 401:
                use_auth = input(f"当前仓库 {image_info.registry}，需要登录？(y/n, 默认: y): ").strip().lower() or 'y'
                if use_auth == 'y':
                    args.username = input("请输入用户名: ").strip()
                    args.password = input("请输入密码: ").strip()
                auth_head = get_auth_head(
                    session, auth_url, reg_service, image_info.repository,
                    args.username, args.password
                )

            resp, http_code = fetch_manifest(
                session, image_info.registry, image_info.repository,
                image_info.tag, auth_head
            )
        except requests.exceptions.RequestException as e:
            logger.error(f'连接仓库失败: {e}')
            raise

        resp_json = resp.json()

        manifests = resp_json.get('manifests')
        if manifests is not None:
            archs = [
                m.get('annotations', {}).get('com.docker.official-images.bashbrew.arch') or
                m.get('platform', {}).get('architecture')
                for m in manifests if m.get('platform', {}).get('os') == 'linux'
            ]

            if archs:
                logger.info(f'📋 当前可用架构：{", ".join(archs)}')

            if len(archs) == 1:
                args.arch = archs[0]
                logger.info(f'✅ 自动选择唯一可用架构: {args.arch}')
            elif not args.quiet:
                default_arch = args.arch if args.arch in archs else 'amd64'
                user_arch = input(f"请输入架构（可选: {', '.join(archs)}，默认: {default_arch}）：").strip()
                args.arch = user_arch if user_arch else default_arch

            if args.arch not in archs:
                logger.error(f'在清单中找不到指定的架构 {args.arch}')
                logger.info(f'可用架构: {", ".join(archs)}')
                return

            digest = select_manifest(manifests, args.arch)
            if not digest:
                logger.error(f'在清单中找不到指定的架构 {args.arch}')
                return

            url = f'https://{image_info.registry}/v2/{image_info.repository}/manifests/{digest}'
            logger.debug(f'获取架构清单: {url}')

            manifest_resp = session.get(url, headers=auth_head, verify=False, timeout=60)
            try:
                manifest_resp.raise_for_status()
                resp_json = manifest_resp.json()
            except Exception as e:
                logger.error(f'获取架构清单失败: {e}')
                return

            if 'layers' not in resp_json:
                logger.error('错误：清单中没有层')
                return

            if 'config' not in resp_json:
                logger.error('错误：清单中没有配置信息')
                return
        else:
            config_digest = resp_json.get('config', {}).get('digest')
            if config_digest:
                config_url = f'https://{image_info.registry}/v2/{image_info.repository}/blobs/{config_digest}'
                logger.debug(f'获取镜像配置: {config_url}')
                try:
                    config_resp = session.get(config_url, headers=auth_head, verify=False, timeout=60)
                    config_resp.raise_for_status()
                    config_json = config_resp.json()
                    actual_arch = config_json.get('architecture', 'unknown')
                    actual_os = config_json.get('os', 'unknown')
                    logger.info(f'📋 镜像实际架构: {actual_os}/{actual_arch}')
                    
                    if actual_arch != args.arch:
                        logger.warning(f'⚠️  镜像架构为 {actual_arch}，与请求的 {args.arch} 不匹配')
                        if not args.quiet:
                            use_actual = input(f'是否使用镜像实际架构 {actual_arch}？(y/n, 默认: y): ').strip().lower() or 'y'
                            if use_actual == 'y':
                                args.arch = actual_arch
                    else:
                        if not args.quiet:
                            confirm = input(f'确认下载 {actual_os}/{actual_arch} 架构的镜像？(y/n, 默认: y): ').strip().lower() or 'y'
                            if confirm != 'y':
                                logger.info('用户取消下载')
                                return
                except Exception as e:
                    logger.warning(f'获取镜像配置失败: {e}')

        if 'layers' not in resp_json or 'config' not in resp_json:
            logger.error('错误：清单格式不完整，缺少必要字段')
            logger.debug(f'清单内容: {resp_json.keys()}')
            return

        logger.info(f'📦 仓库地址：{image_info.registry}')
        logger.info(f'📦 镜像：{image_info.repository}')
        logger.info(f'📦 标签：{image_info.tag}')
        logger.info(f'📦 架构：{args.arch}')

        output_dir = get_output_dir(image_info.repository, image_info.tag, args.arch, args.output)
        imgdir = str(output_dir / 'layers')
        os.makedirs(imgdir, exist_ok=True)
        logger.info(f'📁 输出目录：{output_dir}')
        logger.info('📥 开始下载...')

        if image_info.registry == 'registry-1.docker.io' and image_info.repository.startswith('library/'):
            imgparts = []
        else:
            imgparts = image_info.repository.split('/')[:-1]

        download_layers(
            session, image_info.registry, image_info.repository,
            resp_json['layers'], auth_head, imgdir, resp_json,
            imgparts, image_info.image_name, image_info.tag, args.arch,
            output_dir
        )

        output_file = create_image_tar(imgdir, image_info.repository, image_info.tag, args.arch, output_dir)
        logger.info(f'✅ 镜像已保存为: {output_file}')
        logger.info(f'💡 导入命令: docker load -i {output_file}')
        if image_info.registry not in ("registry-1.docker.io", "docker.io"):
            logger.info(f'💡 标签命令: docker tag {image_info.repository}:{image_info.tag} {image_info.registry}/{image_info.repository}:{image_info.tag}')

    except KeyboardInterrupt:
        logger.info('⚠️ 用户取消操作。')
    except requests.exceptions.RequestException as e:
        logger.error(f'❌ 网络连接失败: {e}')
    except json.JSONDecodeError as e:
        logger.error(f'❌ JSON解析失败: {e}')
    except FileNotFoundError as e:
        logger.error(f'❌ 文件操作失败: {e}')
    except argparse.ArgumentError as e:
        logger.error(f'❌ 命令行参数错误: {e}')
    except Exception as e:
        logger.error(f'❌ 程序运行过程中发生异常: {e}')
        import traceback
        logger.debug(traceback.format_exc())

    finally:
        cleanup_tmp_dir()
        try:
            input("\n按回车键退出程序...")
        except (KeyboardInterrupt, EOFError):
            pass
        sys.exit(0)


if __name__ == '__main__':
    main()
