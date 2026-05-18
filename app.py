import gzip
import hashlib
import json
import os
import shutil
import threading
import time
import urllib.request
import warnings

warnings.filterwarnings("ignore", message="urllib3.*doesn\\'t match a supported version")
warnings.filterwarnings("ignore", category=UserWarning, module="requests")

import requests
from urllib.request import getproxies
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import tarfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List
from pathlib import Path
import gradio as gr

# 配置参数
VERSION = "v2.0.1-Gradio7-Web"
DEFAULT_1MS_API = "https://1ms.run/api/v1/registry"
CONNECT_TIMEOUT = 8
READ_TIMEOUT = 120
DOWNLOAD_MAX_RETRIES = 4
BACKOFF_BASE = 0.3
MAX_PARALLEL_LAYERS = 8

# 防止回环代理问题
local_proxies = getproxies()
os.environ["HTTP_PROXY"] = os.environ["http_proxy"] = local_proxies.get("http", "")
os.environ["HTTPS_PROXY"] = os.environ["https_proxy"] = local_proxies.get("https", "")
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "localhost, 127.0.0.1/8, ::1"


# --------------------------
# 状态与进度管理 (适配 Web)
# --------------------------
@dataclass
class DownloadStats:
    total_size: int = 0
    downloaded_size: int = 0
    start_time: float = 0.0
    speeds: List[float] = field(default_factory=list)

    def get_avg_speed(self) -> float:
        if not self.speeds: return 0.0
        return sum(self.speeds[-10:]) / len(self.speeds[-10:])

    def format_size(self, size: int) -> str:
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024: return f"{size:.1f}{unit}"
            size /= 1024
        return f"{size:.1f}TB"


class LayerProgress:
    def __init__(self, name: str, total_size: int, index: int, total_layers: int):
        self.name = name
        self.total_size = total_size
        self.downloaded_size = 0
        self.index = index
        self.total_layers = total_layers
        self.status = "waiting"  # waiting, downloading, completed


class WebProgressDisplay:
    def __init__(self):
        self.layers: Dict[str, LayerProgress] = {}
        self.stats = DownloadStats()
        self.lock = threading.Lock()
        self.is_done = False
        self.error_msg = ""
        self.final_path = ""

    def add_layer(self, name: str, total_size: int, index: int, total_layers: int):
        with self.lock:
            self.layers[name] = LayerProgress(name, total_size, index, total_layers)

    def update_layer(self, name: str, downloaded: int):
        with self.lock:
            if name in self.layers:
                self.layers[name].downloaded_size = downloaded
                self.layers[name].status = "downloading"

    def update_layer_size(self, name: str, total_size: int):
        with self.lock:
            if name in self.layers and total_size and total_size > 0:
                self.layers[name].total_size = max(self.layers[name].total_size, total_size)

    def complete_layer(self, name: str):
        with self.lock:
            if name in self.layers:
                layer = self.layers[name]
                if layer.total_size == 0:
                    layer.total_size = layer.downloaded_size
                else:
                    layer.downloaded_size = layer.total_size
                layer.status = "completed"

    def get_html_content(self) -> str:
        """生成美观的 HTML & CSS 进度条日志，消除闪烁"""
        with self.lock:
            if self.error_msg:
                return f"""
                <div style="padding: 15px; border-radius: 8px; background-color: #ffebee; color: #c62828; font-weight: bold; border: 1px solid #ef5350;">
                    ❌ 发生错误: {self.error_msg}
                </div>
                """

            html = ["""
            <div style="font-family: 'Consolas', 'Courier New', monospace; background-color: #1e1e1e; color: #e0e0e0; padding: 16px; border-radius: 8px; min-height: 250px; font-size: 13px;">
                <div style="margin-bottom: 15px; font-weight: 600; color: #4fc3f7;">🐳 Docker Image Pull Status</div>
            """]

            for _, layer in sorted(self.layers.items(), key=lambda x: x[1].index):
                if layer.total_size > 0:
                    progress_pct = (layer.downloaded_size / layer.total_size) * 100
                else:
                    progress_pct = 100.0 if layer.status == "completed" else 0.0

                if layer.status == "completed":
                    icon_color, bar_color, icon = "#4caf50", "#4caf50", "✔"
                elif layer.status == "downloading":
                    icon_color, bar_color, icon = "#29b6f6", "#29b6f6", "⬇"
                else:
                    icon_color, bar_color, icon = "#757575", "#616161", "⏳"

                t_str = self.stats.format_size(layer.total_size) if layer.total_size > 0 else "?"
                c_str = self.stats.format_size(layer.downloaded_size)

                html.append(f"""
                <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;">
                    <div style="width: 140px; color: {icon_color};">
                        <span style="display: inline-block; width: 20px;">{icon}</span>
                        ({layer.index}/{layer.total_layers}) {layer.name:<12}
                    </div>
                    <div style="flex-grow: 1; margin: 0 15px; background-color: #333333; height: 10px; border-radius: 5px; overflow: hidden;">
                        <div style="width: {progress_pct}%; background-color: {bar_color}; height: 100%; transition: width 0.3s ease;"></div>
                    </div>
                    <div style="width: 160px; text-align: right; color: #b0bec5;">
                        <span style="display:inline-block; width: 50px;">{progress_pct:5.1f}%</span> | {c_str}/{t_str}
                    </div>
                </div>
                """)

            speed = self.stats.get_avg_speed()
            speed_str = self.stats.format_size(int(speed)) if speed > 0 else "0B"

            html.append(f"""
                <hr style="border: none; border-top: 1px dashed #424242; margin: 15px 0;">
                <div style="color: #ffb74d;">📊 预估下载速度: {speed_str}/s</div>
            """)

            if self.is_done:
                # 🟡 优化：高对比度、清晰可见的完成提示卡片
                html.append(f"""
                    <div style="margin-top: 15px; padding: 15px; background-color: #e8f5e9; color: #1b5e20; border: 1px solid #a5d6a7; border-radius: 6px; font-family: sans-serif;">
                        <div style="font-size: 16px; font-weight: bold; margin-bottom: 10px;">🎉 所有组件下载并打包完成!</div>
                        <div style="margin-bottom: 8px;">
                            📦 <strong>保存路径:</strong> 
                            <code style="background: #ffffff; color: #d81b60; padding: 3px 6px; border-radius: 4px; border: 1px solid #f48fb1; font-family: monospace;">{self.final_path}</code>
                        </div>
                        <div>
                            💡 <strong>导入命令:</strong> 
                            <code style="background: #ffffff; color: #1976d2; padding: 3px 6px; border-radius: 4px; border: 1px solid #90caf9; font-family: monospace;">docker load -i {self.final_path}</code>
                        </div>
                    </div>
                """)

            html.append("</div>")
            return "".join(html)


# --------------------------
# 核心网络与下载逻辑
# --------------------------
def apply_proxy_config(proxy_mode, p_host, p_user, p_pass):
    proxies = {}
    if proxy_mode == "系统代理":
        sys_proxies = urllib.request.getproxies()
        if "http" in sys_proxies:
            os.environ["HTTP_PROXY"] = os.environ["http_proxy"] = sys_proxies["http"]
            proxies["http"] = sys_proxies["http"]
        if "https" in sys_proxies:
            os.environ["HTTPS_PROXY"] = os.environ["https_proxy"] = sys_proxies["https"]
            proxies["https"] = sys_proxies["https"]
    elif proxy_mode == "自定义代理":
        auth = f"{p_user}:{p_pass}@" if p_user else ""
        url = f"http://{auth}{p_host}" if p_host else ""
        if url:
            os.environ["HTTP_PROXY"] = os.environ["http_proxy"] = url
            os.environ["HTTPS_PROXY"] = os.environ["https_proxy"] = url
            proxies = {"http": url, "https": url}
    return proxies


def get_session(verify_ssl, proxies) -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.3, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry, pool_connections=64, pool_maxsize=128)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.verify = verify_ssl
    session.proxies = proxies
    return session


def get_auth_head(session, registry, repository):
    try:
        ping_url = f"https://{registry}/v2/"
        resp = session.get(ping_url, timeout=10)
        if resp.status_code == 401 and "WWW-Authenticate" in resp.headers:
            www = resp.headers["WWW-Authenticate"]
            auth_url = www.split('"')[1]
            reg_service = www.split('"')[3]
            token_url = f"{auth_url}?service={reg_service}&scope=repository:{repository}:pull"
            t_resp = session.get(token_url, timeout=10)
            t_resp.raise_for_status()
            access_token = t_resp.json()["token"]
            return {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json"
            }
    except Exception as e:
        print(f"Auth Ping Fallback: {e}")
    return {
        "Accept": "application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json"}


def download_file_chunked(session, url, headers, save_path, desc, expected_digest, progress: WebProgressDisplay):
    resume_pos = os.path.getsize(save_path) if os.path.exists(save_path) else 0
    fetch_headers = headers.copy()
    if resume_pos > 0: fetch_headers["Range"] = f"bytes={resume_pos}-"

    for attempt in range(DOWNLOAD_MAX_RETRIES):
        if progress.error_msg: return False
        try:
            with session.get(url, headers=fetch_headers, stream=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)) as resp:
                if resp.status_code == 416:
                    progress.complete_layer(desc)
                    return True
                resp.raise_for_status()

                content_range = resp.headers.get("content-range")
                total_size = int(content_range.split("/")[1]) if content_range else int(
                    resp.headers.get("content-length", 0)) + resume_pos
                progress.update_layer_size(desc, total_size)

                mode = "ab" if resume_pos > 0 else "wb"
                downloaded_size = resume_pos
                last_update = time.time()
                last_size = resume_pos

                with open(save_path, mode) as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if progress.error_msg: return False
                        if chunk:
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            progress.update_layer(desc, downloaded_size)
                            progress.stats.total_size += len(chunk)

                            now = time.time()
                            if now - last_update >= 0.5:
                                speed = (downloaded_size - last_size) / (now - last_update)
                                progress.stats.speeds.append(speed)
                                last_size = downloaded_size
                                last_update = now
            progress.complete_layer(desc)
            return True
        except Exception as e:
            if attempt < DOWNLOAD_MAX_RETRIES - 1:
                time.sleep(1)
                continue
            raise e
    return False


def pull_image_logic(progress: WebProgressDisplay, registry, repository, tag, arch, output_dir, proxy_args):
    try:
        proxies = apply_proxy_config(*proxy_args[:4])
        session = get_session(proxy_args[4], proxies)

        if "/" not in repository: repository = f"library/{repository}"
        auth_head = get_auth_head(session, registry, repository)

        mani_url = f"https://{registry}/v2/{repository}/manifests/{tag}"
        resp = session.get(mani_url, headers=auth_head)
        resp.raise_for_status()
        resp_json = resp.json()

        if resp_json.get("manifests"):
            digest = None
            for m in resp_json["manifests"]:
                m_arch = m.get("platform", {}).get("architecture")
                m_os = m.get("platform", {}).get("os")
                if m_arch == arch and m_os == "linux":
                    digest = m.get("digest")
                    break
            if not digest:
                raise ValueError(f"该镜像在 {tag} 下未找到 {arch} 架构。")

            mani_url = f"https://{registry}/v2/{repository}/manifests/{digest}"
            resp = session.get(mani_url, headers=auth_head)
            resp.raise_for_status()
            resp_json = resp.json()

        layers = resp_json.get("layers", [])
        config_digest = resp_json["config"]["digest"]
        config_size = resp_json["config"].get("size", 0)

        safe_repo = repository.replace("/", "_")
        target_dir = Path(output_dir) / f"{safe_repo}_{tag}_{arch}"
        imgdir = target_dir / "layers"
        imgdir.mkdir(parents=True, exist_ok=True)

        config_path = str(imgdir / f"{config_digest[7:]}.json")
        config_url = f"https://{registry}/v2/{repository}/blobs/{config_digest}"

        progress.add_layer("Config", config_size, 1, len(layers) + 1)

        parentid = ""
        for idx, l in enumerate(layers):
            ublob = l["digest"]
            fake_layerid = hashlib.sha256((parentid + "\n" + ublob + "\n").encode("utf-8")).hexdigest()
            l["fake_layerid"] = fake_layerid
            l["parent_layerid"] = parentid
            parentid = fake_layerid
            progress.add_layer(ublob[:12], l.get("size", 0), idx + 2, len(layers) + 1)

        download_file_chunked(session, config_url, auth_head, config_path, "Config", config_digest, progress)

        def download_worker(layer_obj):
            ublob = layer_obj["digest"]
            url = f"https://{registry}/v2/{repository}/blobs/{ublob}"
            fake_layerid = layer_obj["fake_layerid"]
            ldir = imgdir / fake_layerid
            ldir.mkdir(parents=True, exist_ok=True)

            save_path = str(ldir / "layer_gzip.tar")
            success = download_file_chunked(session, url, auth_head, save_path, ublob[:12], ublob, progress)
            return success, fake_layerid, ldir

        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_LAYERS) as pool:
            futures = [pool.submit(download_worker, l) for l in layers]
            for fut in as_completed(futures):
                try:
                    suc, fid, ldir = fut.result()
                    if not suc:
                        raise Exception("分片网络下载失败")
                except Exception as e:
                    progress.error_msg = str(e)
                    return

        content = [{"Config": f"{config_digest[7:]}.json", "RepoTags": [f"{repository}:{tag}"], "Layers": []}]

        for l in layers:
            fake_layerid = l["fake_layerid"]
            parent_layerid = l["parent_layerid"]
            ldir = imgdir / fake_layerid
            gz_path = ldir / "layer_gzip.tar"
            tar_path = ldir / "layer.tar"

            if gz_path.exists():
                with gzip.open(gz_path, "rb") as gz, open(tar_path, "wb") as file:
                    shutil.copyfileobj(gz, file)
                os.remove(gz_path)

            with open(ldir / "json", "w") as f:
                json.dump({"id": fake_layerid, "parent": parent_layerid if parent_layerid else None}, f)

            content[0]["Layers"].append(f"{fake_layerid}/layer.tar")

        with open(imgdir / "manifest.json", "w") as f:
            json.dump(content, f)
        with open(imgdir / "repositories", "w") as f:
            json.dump({repository: {tag: layers[-1]["fake_layerid"]}}, f)

        tar_final = Path(output_dir) / f"{safe_repo}_{tag}_{arch}.tar"
        with tarfile.open(str(tar_final), "w") as tar:
            tar.add(str(imgdir), arcname="/")

        shutil.rmtree(str(imgdir), ignore_errors=True)

        progress.final_path = str(tar_final.absolute())
        progress.is_done = True

    except Exception as e:
        import traceback
        traceback.print_exc()
        progress.error_msg = str(e)


# --------------------------
# UI 交互函数与文件管理
# --------------------------
def get_downloaded_tars(out_dir):
    """扫描目录下的所有 .tar 文件给前端展示"""
    if not out_dir: out_dir = "downloads"
    os.makedirs(out_dir, exist_ok=True)
    files = []
    try:
        for f in os.listdir(out_dir):
            if f.endswith(".tar"):
                files.append(os.path.join(out_dir, f))
        # 按修改时间排序，最新的在前面
        files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
    except Exception:
        pass

    choices = [os.path.basename(f) for f in files]
    return files, gr.update(choices=choices, value=None)


def delete_local_tar(filename, out_dir):
    """删除指定的 tar 文件"""
    if filename:
        filepath = os.path.join(out_dir, filename)
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
                gr.Info(f"🗑️ 成功删除: {filename}")
            except Exception as e:
                gr.Warning(f"删除失败: {e}")
    return get_downloaded_tars(out_dir)


def fn_search(keyword, p_mode, p_host, p_user, p_pass, vSSL):
    if not keyword: return []
    proxies = apply_proxy_config(p_mode, p_host, p_user, p_pass)
    session = get_session(vSSL, proxies)
    try:
        resp = session.get(f"{DEFAULT_1MS_API}/search", params={"query": keyword, "page": 1, "page_size": 20})
        resp.raise_for_status()
        items = resp.json().get("data", {}).get("list", [])
        res = []
        for it in items:
            ns = it.get("namespace", "library")
            name = it.get("name", "")
            repo = f"{ns}/{name}" if ns else name
            res.append([
                repo,
                it.get("star_count", 0),
                it.get("pull_count", 0),
                it.get("last_modified", "")[:10],
                (it.get("description") or "").replace("\n", "")[:50]
            ])
        return res
    except Exception as e:
        raise gr.Error(f"搜索失败: {str(e)}")


def fn_get_tags(selected_repo, p_mode, p_host, p_user, p_pass, vSSL):
    proxies = apply_proxy_config(p_mode, p_host, p_user, p_pass)
    session = get_session(vSSL, proxies)
    try:
        resp = session.get(f"{DEFAULT_1MS_API}/get_tags",
                           params={"repositories": selected_repo, "page": 1, "page_size": 100})
        resp.raise_for_status()
        items = resp.json().get("data", {}).get("list", [])
        tags = [it.get("tag_name") for it in items if it.get("tag_name")]
        if not tags: tags = ["latest"]
        return gr.update(choices=tags, value="latest", interactive=True)
    except Exception:
        return gr.update(choices=["latest"], value="latest", interactive=True)


def fn_download_manager(repo, tag, arch, registry, out_dir, p_mode, p_host, p_user, p_pass, vSSL):
    if not repo:
        yield '<div style="color:red;font-weight:bold;">❌ 请先在上方搜索并在表格中点击选择一个镜像！</div>', gr.skip(), gr.skip()
        return

    yield '<div style="color:#666;font-family:monospace;padding:10px;">🕒 初始化下载任务中，请稍候...</div>', gr.skip(), gr.skip()

    reg_map = {
        "Docker 官方 (registry-1.docker.io)": "registry-1.docker.io",
        "1ms 专属加速 (docker.1ms.run)": "docker.1ms.run",
        "国内源: 南大 (docker.nju.edu.cn)": "docker.nju.edu.cn"
    }
    actual_registry = reg_map.get(registry, "docker.1ms.run")
    proxy_args = (p_mode, p_host, p_user, p_pass, vSSL)

    progress = WebProgressDisplay()

    t = threading.Thread(target=pull_image_logic,
                         args=(progress, actual_registry, repo, tag, arch, out_dir, proxy_args))
    t.start()

    while t.is_alive():
        time.sleep(0.5)
        # Yield 日志 HTML 以及跳过文件列表的更新
        yield progress.get_html_content(), gr.skip(), gr.skip()

    # 🟡 优化：下载完成后自动重新加载第三步的文件管理器列表
    files, drop_update = get_downloaded_tars(out_dir)
    yield progress.get_html_content(), files, drop_update


# --------------------------
# Gradio 界面设计
# --------------------------
with gr.Blocks(
        title="Docker 镜像离线下载工具",
        theme=gr.themes.Soft(primary_hue="blue")
) as demo:
    gr.Markdown(
        f"# 🐳 Docker 镜像快捷离线拉取器 ({VERSION})\n搜索、选择架构、一键导出 `.tar` 镜像包供离线环境 `docker load`。")

    with gr.Accordion("⚙️ 全局设置项 (代理、源与路径)", open=False):
        with gr.Row():
            out_dir_ui = gr.Textbox(label="📦 镜像保存目录", value="downloads",
                                    info="默认下载到当前文件夹的 downloads 目录下", scale=2)
            registry_ui = gr.Dropdown(
                label="🌐 拉取数据源",
                choices=["1ms 专属加速 (docker.1ms.run)", "Docker 官方 (registry-1.docker.io)",
                         "国内源: 南大 (docker.nju.edu.cn)"],
                value="1ms 专属加速 (docker.1ms.run)",
                scale=2
            )
            ssl_ui = gr.Checkbox(label="🔐 开启 SSL 证书验证", value=True, info="如果遇到代理证书问题可取消勾选",
                                 scale=1)

        with gr.Row():
            proxy_mode_ui = gr.Radio(label="📡 代理模式", choices=["无代理", "系统代理", "自定义代理"], value="无代理")
        with gr.Row(visible=False) as custom_proxy_row:
            proxy_host_ui = gr.Textbox(label="代理地址 (例如 127.0.0.1:7890)")
            proxy_user_ui = gr.Textbox(label="代理用户名 (可选)")
            proxy_pass_ui = gr.Textbox(label="代理密码 (可选)", type="password")


    def toggle_proxy_visibility(mode):
        return gr.update(visible=(mode == "自定义代理"))


    proxy_mode_ui.change(fn=toggle_proxy_visibility, inputs=[proxy_mode_ui], outputs=[custom_proxy_row])

    with gr.Row():
        with gr.Column(scale=4):
            gr.Markdown("### 🔍 步骤 1：全网查镜像")
            with gr.Row():
                kw_ui = gr.Textbox(label="输入镜像关键词", placeholder="eg: nginx, redis, pytorch", show_label=False)
                search_btn = gr.Button("🔍 搜 索", variant="primary")

            df_results = gr.Dataframe(
                headers=["镜像名称 (Repo)", "⭐ Stars", "⬇️ Pulls", "更新日期", "描述"],
                interactive=False, wrap=True, type="array", max_height=300
            )

        with gr.Column(scale=3):
            gr.Markdown("### 🏷️ 步骤 2：选择并下载")
            selected_repo_ui = gr.Textbox(label="当前选中目标", placeholder="请从左侧表格中点击选择一项...",
                                          interactive=False)

            with gr.Row():
                tag_ui = gr.Dropdown(label="选择 Tag", choices=["latest"], value="latest", interactive=False)
                arch_ui = gr.Dropdown(label="选择架构 Arch", choices=["amd64", "arm64", "arm/v7", "s390x", "ppc64le"],
                                      value="amd64")

            dl_btn = gr.Button("⬇️ 开始下载 & 导出", variant="primary", size="lg")

            gr.Markdown("#### 运行日志 & 进度")
            log_box = gr.HTML(
                value="<div style='color:#999; font-size:14px; text-align:center; padding: 20px; border: 1px dashed #ccc; border-radius: 8px;'>等待开始...</div>"
            )

    # 🟡 优化：增加步骤 3 —— 独立的本地 TAR 文件管理器面板
    gr.Markdown("---\n### 📁 步骤 3：本地镜像包管理 (.tar)")
    with gr.Row():
        with gr.Column(scale=3):
            file_list_ui = gr.File(
                label="📦 已下载的镜像 (在浏览器中点击文件名即可直接下载至当前设备)",
                file_count="multiple",
                interactive=False
            )
        with gr.Column(scale=1):
            refresh_btn = gr.Button("🔄 刷新本地列表")
            delete_dropdown = gr.Dropdown(label="🗑️ 选择要删除的文件")
            delete_btn = gr.Button("❌ 删除选中文件", variant="stop")

    # --------------------------
    # 事件绑定逻辑
    # --------------------------
    search_btn.click(
        fn=fn_search,
        inputs=[kw_ui, proxy_mode_ui, proxy_host_ui, proxy_user_ui, proxy_pass_ui, ssl_ui],
        outputs=[df_results]
    )


    def on_df_select(evt: gr.SelectData, current_df, p_mode, p_host, p_user, p_pass, vSSL):
        try:
            row_idx = evt.index[0]
            if isinstance(current_df, dict) and "data" in current_df:
                repo_name = current_df["data"][row_idx][0]
            elif hasattr(current_df, "iloc"):
                repo_name = current_df.iloc[row_idx, 0]
            else:
                repo_name = current_df[row_idx][0]
            tag_update = fn_get_tags(repo_name, p_mode, p_host, p_user, p_pass, vSSL)
            return repo_name, tag_update
        except Exception as e:
            import traceback
            traceback.print_exc()
            return "", gr.update(choices=["latest"], value="latest", interactive=True)


    df_results.select(
        fn=on_df_select,
        inputs=[df_results, proxy_mode_ui, proxy_host_ui, proxy_user_ui, proxy_pass_ui, ssl_ui],
        outputs=[selected_repo_ui, tag_ui]
    )

    dl_btn.click(
        fn=fn_download_manager,
        inputs=[
            selected_repo_ui, tag_ui, arch_ui, registry_ui, out_dir_ui,
            proxy_mode_ui, proxy_host_ui, proxy_user_ui, proxy_pass_ui, ssl_ui
        ],
        outputs=[log_box, file_list_ui, delete_dropdown]  # 同步刷新文件面板
    )

    # 文件管理器事件绑定
    demo.load(  # 页面初始加载时获取已下载文件
        fn=get_downloaded_tars, inputs=[out_dir_ui], outputs=[file_list_ui, delete_dropdown]
    )
    refresh_btn.click(
        fn=get_downloaded_tars, inputs=[out_dir_ui], outputs=[file_list_ui, delete_dropdown]
    )
    delete_btn.click(
        fn=delete_local_tar, inputs=[delete_dropdown, out_dir_ui], outputs=[file_list_ui, delete_dropdown]
    )

    demo.queue(default_concurrency_limit=4)

if __name__ == "__main__":
    print(f"🚀 服务启动中...\n💡 请在浏览器访问下方 URL 打开 UI 界面。")
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        inbrowser=True
    )
