import os
import sys
import threading
import re
import gzip
import json
import hashlib
import shutil
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import tarfile
import urllib3
import logging
from threading import Event
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QComboBox,
    QPushButton,
    QTextEdit,
    QProgressBar,
    QMessageBox,
    QDialog,
    QWidget,
    QGridLayout
)
from PyQt6.QtGui import QIcon, QFont, QColor, QPalette
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QSize

# 禁用 SSL 警告
urllib3.disable_warnings()

# 版本号
VERSION = "v1.0.8"

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler("docker_pull_log.txt", mode="a", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 停止事件
stop_event = Event()


def create_session():
    """创建带有重试和代理配置的请求会话"""
    session = requests.Session()
    retry_strategy = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    # 设置代理
    session.proxies = {
        'http': os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy'),
        'https': os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
    }
    if session.proxies.get('http') or session.proxies.get('https'):
        logger.info('使用代理设置从环境变量')

    return session


def parse_image_input(image_input):
    """解析用户输入的镜像名称"""
    parts = image_input.split('/')
    if len(parts) == 1:
        repo = 'library'
        img_tag = parts[0]
    else:
        repo = '/'.join(parts[:-1])
        img_tag = parts[-1]

    img, *tag_parts = img_tag.split(':')
    tag = tag_parts[0] if tag_parts else 'latest'

    return repo, img, tag


def get_auth_head(session, auth_url, reg_service, repository):
    """获取认证头"""
    try:
        url = f'{auth_url}?service={reg_service}&scope=repository:{repository}:pull'
        logger.debug(f"获取认证头 CURL 命令: curl '{url}'")
        resp = session.get(url, verify=False, timeout=30)
        resp.raise_for_status()
        access_token = resp.json()['token']
        auth_head = {'Authorization': f'Bearer {access_token}', 'Accept': 'application/vnd.docker.distribution.manifest.v2+json'}
        return auth_head
    except requests.exceptions.RequestException as e:
        logger.error(f'请求认证失败: {e}')
        raise


def fetch_manifest(session, registry, repository, tag, auth_head):
    """获取镜像清单"""
    try:
        url = f'https://{registry}/v2/{repository}/manifests/{tag}'
        headers = {
            'Accept': 'application/vnd.docker.distribution.manifest.v2+json',
            'Authorization': auth_head.get('Authorization', '')
        }
        logger.debug(f'获取镜像清单 CURL 命令: curl -H "Accept: application/vnd.docker.distribution.manifest.v2+json" -H "Authorization: {auth_head.get("Authorization")}" {url}')
        resp = session.get(url, headers=headers, verify=False, timeout=30)
        resp.raise_for_status()
        return resp
    except requests.exceptions.RequestException as e:
        logger.error(f'请求清单失败: {e}')
        raise


def select_manifest(manifests, arch):
    """选择适合指定架构的清单"""
    for m in manifests:
        if (m.get('annotations', {}).get('com.docker.official-images.bashbrew.arch') == arch or
            m.get('platform', {}).get('architecture') == arch) and \
            m.get('platform', {}).get('os') == 'linux':
            return m.get('digest')
    return None


def download_layers(session, registry, repository, layers, auth_head, imgdir, resp_json, imgparts, img, tag, log_callback=None, layer_progress_callback=None, overall_progress_callback=None):
    """下载镜像层"""
    try:
        config = resp_json['config']['digest']
        url = f'https://{registry}/v2/{repository}/blobs/{config}'
        headers = {
            'Accept': 'application/vnd.docker.distribution.manifest.v2+json',
            'Authorization': auth_head.get('Authorization', '')
        }
        if log_callback:
            log_callback(f"[DEBUG] 下载配置文件 CURL 命令: {url}\n")
        with session.get(url, headers=headers, verify=False, timeout=30, stream=True) as confresp:
            confresp.raise_for_status()
            with open(f'{imgdir}/{config[7:]}.json', 'wb') as file:
                shutil.copyfileobj(confresp.raw, file)
        if log_callback:
            log_callback(f"配置文件下载完成：{config}\n")

        content = [{
            'Config': f'{config[7:]}.json',
            'RepoTags': [f'{"/".join(imgparts[:-1])}/{img}:{tag}' if imgparts[:-1] else f'{img}:{tag}'],
            'Layers': []
        }]

        empty_json = {
            "created": "1970-01-01T00:00:00Z",
            "container_config": {
                "Hostname": "",
                "Domainname": "",
                "User": "",
                "AttachStdin": False,
                "AttachStdout": False,
                "AttachStderr": False,
                "Tty": False,
                "OpenStdin": False,
                "StdinOnce": False,
                "Env": None,
                "Cmd": None,
                "Image": "",
                "Volumes": None,
                "WorkingDir": "",
                "Entrypoint": None,
                "OnBuild": None,
                "Labels": None
            }
        }

        parentid = ''
        total_layers = len(layers)
        overall_progress = 0

        for index, layer in enumerate(layers, start=1):
            if stop_event.is_set():
                log_callback("下载已停止。\n")
                log_callback("[INFO] 镜像下载中断！\n")
                return

            ublob = layer['digest']
            fake_layerid = hashlib.sha256((parentid + '\n' + ublob + '\n').encode('utf-8')).hexdigest()
            layerdir = f'{imgdir}/{fake_layerid}'
            os.makedirs(layerdir, exist_ok=True)

            with open(f'{layerdir}/VERSION', 'w') as file:
                file.write('1.0')

            try:
                url = f'https://{registry}/v2/{repository}/blobs/{ublob}'
                headers = {
                    'Accept': 'application/vnd.docker.distribution.manifest.v2+json',
                    'Authorization': auth_head.get('Authorization', '')
                }
                if log_callback:
                    log_callback(f"[DEBUG] 下载镜像层 CURL 命令: {url}\n")
                with session.get(url, headers=headers, verify=False, timeout=30, stream=True) as bresp:
                    bresp.raise_for_status()
                    total_size = int(bresp.headers.get('content-length', 0))
                    downloaded_size = 0

                    with open(f'{layerdir}/layer_gzip.tar', 'wb') as file:
                        for chunk in bresp.iter_content(chunk_size=1024):
                            if stop_event.is_set():
                                log_callback("下载已停止。\n")
                                log_callback("[INFO] 镜像下载中断！\n")
                                return
                            if chunk:
                                file.write(chunk)
                                downloaded_size += len(chunk)
                                if layer_progress_callback:
                                    layer_progress_callback(int(downloaded_size / total_size * 100))

                    if log_callback:
                        log_callback(f"镜像层下载完成：{ublob[:12]}\n")

                with gzip.open(f'{layerdir}/layer_gzip.tar', 'rb') as gz, open(f'{layerdir}/layer.tar', 'wb') as file:
                    shutil.copyfileobj(gz, file)
                os.remove(f'{layerdir}/layer_gzip.tar')

                content[0]['Layers'].append(f'{fake_layerid}/layer.tar')

                if layers[-1]['digest'] == layer['digest']:
                    with open(f'{imgdir}/{config[7:]}.json', 'rb') as file:
                        json_data = file.read()
                        json_obj = json.loads(json_data.decode('utf-8'))
                    json_obj.pop('history', None)
                    json_obj.pop('rootfs', None)
                else:
                    json_obj = empty_json.copy()
                json_obj['id'] = fake_layerid
                if parentid:
                    json_obj['parent'] = parentid
                parentid = json_obj['id']

                with open(f'{layerdir}/json', 'w') as file:
                    json.dump(json_obj, file)

            except Exception as e:
                if log_callback:
                    log_callback(f"[ERROR] 请求层失败: {e}\n")
                raise
            finally:
                overall_progress += 1
                if overall_progress_callback:
                    overall_progress_callback(int(overall_progress / total_layers * 100))
    except Exception as e:
        if log_callback:
            log_callback(f"[ERROR] 下载镜像层失败: {e}\n")
        raise

    with open(f'{imgdir}/manifest.json', 'w') as file:
        json.dump(content, file)

    repo_tag = f'{"/".join(imgparts[:-1])}/{img}' if imgparts[:-1] else img
    with open(f'{imgdir}/repositories', 'w') as file:
        json.dump({repo_tag: {tag: fake_layerid}}, file)


def create_image_tar(imgdir, repo, img, arch):
    """将镜像打包为 tar 文件"""
    docker_tar = f'{repo.replace("/", "_")}_{img}_{arch}.tar'
    try:
        with tarfile.open(docker_tar, "w") as tar:
            tar.add(imgdir, arcname='/')
        logger.info(f'Docker 镜像已拉取：{docker_tar}')
    except Exception as e:
        logger.error(f'打包镜像失败: {e}')
        raise


def cleanup_tmp_dir():
    """删除 tmp 目录"""
    tmp_dir = 'tmp'
    try:
        if os.path.exists(tmp_dir):
            logger.info(f'清理临时目录: {tmp_dir}')
            shutil.rmtree(tmp_dir)
            logger.info('临时目录已清理。')
    except Exception as e:
        logger.error(f'清理临时目录失败: {e}')


def pull_image_logic(image, registry, arch, debug=False, log_callback=None, layer_progress_callback=None, overall_progress_callback=None):
    """核心逻辑函数，接受直接传递的参数"""
    global stop_event
    stop_event.clear()  # 重置停止事件

    try:
        if debug:
            logger.setLevel(logging.DEBUG)

        def log_message(message, level="INFO"):
            if log_callback:
                log_callback(f"[{level}] {message}\n")
            else:
                print(f"[{level}] {message}")

        repo, img, tag = parse_image_input(image)
        repository = f'{repo}/{img}'

        session = create_session()

        url = f'https://{registry}/v2/'
        log_message(f"获取认证信息 CURL 命令: curl '{url}'", level="DEBUG")
        resp = session.get(url, verify=False, timeout=30)
        if resp.status_code == 401:
            auth_url = resp.headers['WWW-Authenticate'].split('"')[1]
            reg_service = resp.headers['WWW-Authenticate'].split('"')[3]
            auth_head = get_auth_head(session, auth_url, reg_service, repository)
        else:
            auth_head = {'Accept': 'application/vnd.docker.distribution.manifest.v2+json'}

        resp = fetch_manifest(session, registry, repository, tag, auth_head)
        resp_json = resp.json()
        manifests = resp_json.get('manifests')
        if manifests:
            archs = [m.get('annotations', {}).get('com.docker.official-images.bashbrew.arch') or m.get('platform', {}).get('architecture') for m in manifests if m.get('platform', {}).get('os') == 'linux']
            log_message(f'当前可用架构：{", ".join(archs)}')

            digest = select_manifest(manifests, arch)
            if digest:
                url = f'https://{registry}/v2/{repository}/manifests/{digest}'
                headers = {
                    'Accept': 'application/vnd.docker.distribution.manifest.v2+json',
                    'Authorization': auth_head.get('Authorization', '')
                }
                log_message(f'获取架构清单 CURL 命令: {url}', level="DEBUG")
                manifest_resp = session.get(url, headers=headers, verify=False, timeout=30)
                manifest_resp.raise_for_status()
                resp_json = manifest_resp.json()

        if 'layers' not in resp_json:
            log_message('错误：清单中没有层', level="ERROR")
            return

        log_message(f'仓库地址：{registry}')
        log_message(f'仓库名：{repository}')
        log_message(f'标签：{tag}')
        log_message(f'架构：{arch}')

        imgdir = 'tmp'
        os.makedirs(imgdir, exist_ok=True)
        log_message('开始下载层...')
        download_layers(session, registry, repository, resp_json['layers'], auth_head, imgdir, resp_json, [repo], img, tag, log_callback=log_callback, layer_progress_callback=layer_progress_callback, overall_progress_callback=overall_progress_callback)

        create_image_tar(imgdir, repo, img, arch)
        if not stop_event.is_set():
            log_message("镜像拉取完成！")
    except Exception as e:
        log_message(f'程序运行过程中发生异常: {e}', level="ERROR")
        raise
    finally:
        cleanup_tmp_dir()


class Worker(QObject):
    """用于拉取镜像的后台线程"""
    log_signal = pyqtSignal(str)
    layer_progress_signal = pyqtSignal(int)
    overall_progress_signal = pyqtSignal(int)

    def __init__(self, image, registry, arch, language):
        super().__init__()
        self.image = image
        self.registry = registry
        self.arch = arch
        self.language = language

    def run(self):
        try:
            log_msg = {
                "zh": f"开始拉取镜像：{self.image}\n",
                "en": f"Pulling image: {self.image}\n"
            }
            self.log_signal.emit(log_msg[self.language])

            pull_image_logic(
                self.image,
                self.registry,
                self.arch,
                log_callback=self.log_callback,
                layer_progress_callback=self.layer_progress_callback,
                overall_progress_callback=self.overall_progress_callback
            )

        except Exception as e:
            error_msg = {
                "zh": f"[ERROR] 发生错误：{e}\n",
                "en": f"[ERROR] Error occurred: {e}\n"
            }
            self.log_callback(error_msg[self.language])
        finally:
            self.layer_progress_callback(0)
            self.overall_progress_callback(0)

    def log_callback(self, message):
        self.log_signal.emit(message)

    def layer_progress_callback(self, value):
        self.layer_progress_signal.emit(value)

    def overall_progress_callback(self, value):
        self.overall_progress_signal.emit(value)


class DockerPullerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.language = "zh"
        self.theme_mode = "light"
        self.is_pulling = False  # 标记是否正在拉取镜像

        # 定义图标路径
        base_path = os.path.dirname(os.path.abspath(__file__))
        logo_icon_path = os.path.join(base_path, "logo.ico")
        settings_icon_path = os.path.join(base_path, "settings.png")

        # 调用 init_ui 并传递图标路径
        self.init_ui(logo_icon_path, settings_icon_path)
        self.apply_theme_mode()
        self.update_ui_text()

    def init_ui(self, logo_icon_path, settings_icon_path):
        # 主窗口设置
        self.setWindowTitle("Docker 镜像打包工具 v1.0.8")
        self.setGeometry(100, 100, 600, 800)
        self.setWindowIcon(QIcon(logo_icon_path))  # 使用动态路径加载图标

        # 主布局
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)

        # 设置按钮
        self.settings_button = QPushButton()
        self.settings_button.setIcon(QIcon(settings_icon_path))  # 使用动态路径加载图标
        self.settings_button.setIconSize(QSize(24, 24))
        self.settings_button.clicked.connect(self.show_settings_dialog)
        settings_layout = QHBoxLayout()
        settings_layout.addWidget(self.settings_button)
        settings_layout.addStretch()
        main_layout.addLayout(settings_layout)

        # 输入区域
        input_grid = QGridLayout()
        self.create_input_fields(input_grid)
        main_layout.addLayout(input_grid)

        # 按钮区域
        button_layout = QHBoxLayout()
        self.create_buttons(button_layout)
        main_layout.addLayout(button_layout)

        # 日志区域
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        main_layout.addWidget(self.log_text)

        # 进度条
        progress_layout = QHBoxLayout()
        self.create_progress_bars(progress_layout)
        main_layout.addLayout(progress_layout)

    def create_input_fields(self, layout):
        # 仓库地址
        self.registry_label = QLabel()
        self.registry_combobox = QComboBox()
        self.load_registries()
        layout.addWidget(self.registry_label, 0, 0)
        layout.addWidget(self.registry_combobox, 0, 1)

        # 镜像名称
        self.image_label = QLabel()
        self.image_entry = QLineEdit()
        layout.addWidget(self.image_label, 1, 0)
        layout.addWidget(self.image_entry, 1, 1)

        # 标签
        self.tag_label = QLabel()
        self.tag_entry = QLineEdit()
        self.tag_entry.setText("latest")
        layout.addWidget(self.tag_label, 2, 0)
        layout.addWidget(self.tag_entry, 2, 1)

        # 架构
        self.arch_label = QLabel()
        self.arch_combobox = QComboBox()
        self.arch_combobox.addItems([
            "amd64", "arm64", "arm32v7", "arm32v5", "i386", "ppc64le", "s390x", "mips64le"
        ])
        self.arch_combobox.setCurrentIndex(0)  # 默认选中 amd64
        layout.addWidget(self.arch_label, 3, 0)
        layout.addWidget(self.arch_combobox, 3, 1)

        # 设置字体
        font = QFont("Microsoft YaHei", 12)
        for widget in [
            self.registry_label, self.registry_combobox,
            self.image_label, self.image_entry,
            self.tag_label, self.tag_entry,
            self.arch_label, self.arch_combobox
        ]:
            widget.setFont(font)

    def create_buttons(self, layout):
        self.pull_button = QPushButton()
        self.pull_button.clicked.connect(self.pull_image)

        self.reset_button = QPushButton()
        self.reset_button.clicked.connect(self.reset_fields)

        self.manage_registry_button = QPushButton()
        self.manage_registry_button.clicked.connect(self.manage_registries)

        for btn in [self.pull_button, self.reset_button, self.manage_registry_button]:
            btn.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
            self.apply_button_style(btn)  # 应用按钮样式
            layout.addWidget(btn)

    def create_progress_bars(self, layout):
        self.layer_progress_label = QLabel()
        self.layer_progress_bar = QProgressBar()
        self.layer_progress_bar.setValue(0)  # 初始化进度条值为0
        self.layer_progress_bar.setFormat("%p%")  # 设置进度条始终显示百分比

        self.overall_progress_label = QLabel()
        self.overall_progress_bar = QProgressBar()
        self.overall_progress_bar.setValue(0)  # 初始化进度条值为0
        self.overall_progress_bar.setFormat("%p%")  # 设置进度条始终显示百分比

        for widget in [
            self.layer_progress_label, self.layer_progress_bar,
            self.overall_progress_label, self.overall_progress_bar
        ]:
            widget.setFont(QFont("Microsoft YaHei", 12))
            layout.addWidget(widget)

    def load_registries(self):
        self.registry_combobox.clear()
        self.registry_combobox.addItem("registry.hub.docker.com")
        if os.path.exists("registries.txt"):
            with open("registries.txt", "r", encoding="utf-8") as f:
                registries = [line.strip() for line in f if line.strip()]
                self.registry_combobox.addItems(registries)

    def pull_image(self):
        image = self.image_entry.text().strip()
        tag = self.tag_entry.text().strip()

        if not image or not tag:
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle({
                "zh": "错误",
                "en": "Error"
            }[self.language])
            msg_box.setText({
                "zh": "镜像名称和标签不能为空！",
                "en": "Image name and tag cannot be empty!"
            }[self.language])
            msg_box.setIcon(QMessageBox.Icon.Critical)

            # 为 QMessageBox 设置样式表
            if self.theme_mode == "dark":
                msg_box.setStyleSheet("""
                    QMessageBox {
                        background-color: #353535;
                        color: white;
                    }
                    QMessageBox QLabel {
                        color: white;
                    }
                    QMessageBox QPushButton {
                        background-color: #535353;
                        color: white;
                        border-radius: 5px;
                        padding: 5px 10px;
                    }
                    QMessageBox QPushButton:hover {
                        background-color: #636363;
                    }
                """)
            else:
                msg_box.setStyleSheet("""
                    QMessageBox {
                        background-color: #FFFFFF;  /* 修改为白色背景 */
                        color: black;
                    }
                    QMessageBox QLabel {
                        color: black;
                    }
                    QMessageBox QPushButton {
                        background-color: #f0f0f0;
                        color: black;
                        border-radius: 5px;
                        padding: 5px 10px;
                    }
                    QMessageBox QPushButton:hover {
                        background-color: #e0e0e0;
                    }
                """)

            msg_box.exec()
            return

        self.is_pulling = True
        self.pull_button.setEnabled(False)  # 禁用拉取按钮

        self.worker = Worker(
            f"{image}:{tag}",
            self.registry_combobox.currentText(),
            self.arch_combobox.currentText(),
            self.language
        )

        self.worker.log_signal.connect(self.log_callback)
        self.worker.layer_progress_signal.connect(self.layer_progress_bar.setValue)
        self.worker.overall_progress_signal.connect(self.overall_progress_bar.setValue)

        threading.Thread(target=self.worker.run).start()

    def log_callback(self, message):
        self.log_text.append(message)
        if "镜像拉取完成！" in message or "[INFO] 镜像下载中断！" in message:
            self.pull_button.setEnabled(True)  # 启用拉取按钮
            self.is_pulling = False

    def reset_fields(self):
        if self.is_pulling:
            stop_event.set()  # 设置停止事件，中断下载
            self.is_pulling = False  # 标记镜像拉取已停止
            self.pull_button.setEnabled(True)  # 启用拉取按钮

        try:
            cleanup_tmp_dir()  # 清理临时文件
            self.log_text.clear()  # 清空面板中的日志信息
            self.log_text.append({
                "zh": "已恢复到初始状态。\n",
                "en": "Reset to initial state.\n"
            }[self.language])

            if self.is_pulling:
                self.log_text.append({
                    "zh": "下载已停止。\n[INFO] 镜像下载中断！\n",
                    "en": "Download stopped.\n[INFO] Image download interrupted!\n"
                }[self.language])
        except Exception as e:
            self.log_text.append({
                "zh": f"[ERROR] 清理临时目录失败: {e}\n",
                "en": f"[ERROR] Failed to cleanup temporary directory: {e}\n"
            }[self.language])

        self.image_entry.clear()
        self.tag_entry.setText("latest")
        self.registry_combobox.setCurrentIndex(0)
        self.arch_combobox.setCurrentIndex(0)
        self.layer_progress_bar.setValue(0)
        self.overall_progress_bar.setValue(0)

    def manage_registries(self):
        dialog = QDialog(self)
        dialog.setWindowTitle({
            "zh": "管理仓库地址",
            "en": "Manage Registries"
        }[self.language])

        # 应用当前主题模式到弹窗
        dialog.setPalette(self.palette())

        text_area = QTextEdit()
        text_area.setFont(QFont("Microsoft YaHei", 12))
        # 初始化文本区域内容为当前 registries.txt 文件中的内容
        if os.path.exists("registries.txt"):
            with open("registries.txt", "r", encoding="utf-8") as f:
                text_area.setText(f.read().strip())
        else:
            text_area.setText("")

        dialog_layout = QVBoxLayout()
        dialog_layout.addWidget(text_area)

        def save_and_close():
            # 获取用户输入的仓库地址列表
            registries = text_area.toPlainText().strip().split("\n")
            valid_registries = []

            # 验证每个仓库地址是否有效
            for registry in registries:
                registry = registry.strip()
                if registry and self.is_valid_registry(registry):
                    valid_registries.append(registry)
                elif registry:
                    QMessageBox.warning(self, {
                        "zh": "无效的仓库地址",
                        "en": "Invalid Registry"
                    }[self.language], {
                        "zh": f"无效的仓库地址：{registry}\n请检查域名格式。",
                        "en": f"Invalid registry address: {registry}\nPlease check the domain format."
                    }[self.language])

            # 更新 registries.txt 文件
            with open("registries.txt", "w", encoding="utf-8") as f:
                for registry in valid_registries:
                    f.write(registry + "\n")

            # 更新 QComboBox 的内容
            self.registry_combobox.clear()
            self.registry_combobox.addItem("registry.hub.docker.com")
            self.registry_combobox.addItems(valid_registries)

            dialog.close()

        save_button = QPushButton({
            "zh": "保存",
            "en": "Save"
        }[self.language])
        save_button.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        save_button.clicked.connect(save_and_close)
        dialog_layout.addWidget(save_button)

        dialog.setLayout(dialog_layout)
        dialog.exec()

    def is_valid_registry(self, registry):
        """验证仓库地址是否符合域名格式"""
        pattern = re.compile(r"^(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}$")
        return bool(pattern.match(registry))

    def show_settings_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle({
            "zh": "设置",
            "en": "Settings"
        }[self.language])

        # 手动设置弹窗的背景色和字体颜色
        if self.theme_mode == "dark":
            dialog.setStyleSheet("background-color: #353535; color: white;")
        else:
            dialog.setStyleSheet("background-color: white; color: black;")

        # 语言设置
        lang_label = QLabel({
            "zh": "语言设置：",
            "en": "Language:"
        }[self.language])

        lang_combo = QComboBox()
        lang_combo.addItems(["中文", "English"])
        lang_combo.setCurrentText("中文" if self.language == "zh" else "English")

        # 主题设置
        theme_label = QLabel({
            "zh": "主题模式：",
            "en": "Theme:"
        }[self.language])

        theme_combo = QComboBox()
        theme_combo.addItems(["亮色", "暗色"] if self.language == "zh" else ["Light", "Dark"])
        theme_combo.setCurrentText({
            ("light", "zh"): "亮色",
            ("dark", "zh"): "暗色",
            ("light", "en"): "Light",
            ("dark", "en"): "Dark"
        }[(self.theme_mode, self.language)])

        # 应用按钮
        apply_btn = QPushButton({
            "zh": "应用",
            "en": "Apply"
        }[self.language])

        layout = QVBoxLayout()
        layout.addWidget(lang_label)
        layout.addWidget(lang_combo)
        layout.addWidget(theme_label)
        layout.addWidget(theme_combo)
        layout.addWidget(apply_btn)
        dialog.setLayout(layout)

        def apply_settings():
            self.language = "zh" if lang_combo.currentText() == "中文" else "en"
            self.theme_mode = "light" if theme_combo.currentText() in ["亮色", "Light"] else "dark"
            self.update_ui_text()
            self.apply_theme_mode()
            dialog.close()

        apply_btn.clicked.connect(apply_settings)
        dialog.exec()

    def apply_button_style(self, button):
        """根据当前主题模式设置按钮的样式表"""
        if self.theme_mode == "light":
            button.setStyleSheet("""
                QPushButton {
                    background-color: #f0f0f0; /* 浅灰色背景 */
                    border: 1px solid #ccc; /* 边框颜色 */
                    color: black; /* 文字颜色 */
                }
                QPushButton:hover {
                    background-color: #e0e0e0; /* 鼠标悬停时的背景颜色 */
                }
            """)
        else:
            button.setStyleSheet("""
                QPushButton {
                    background-color: #535353; /* 暗色背景 */
                    border: 1px solid #333; /* 边框颜色 */
                    color: white; /* 文字颜色 */
                }
                QPushButton:hover {
                    background-color: #636363; /* 鼠标悬停时的背景颜色 */
                }
            """)

    def apply_theme_mode(self):
        palette = QPalette()
        if self.theme_mode == "dark":
            palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
            palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
            palette.setColor(QPalette.ColorRole.Base, QColor(51, 51, 51))  # 输入框背景色
            palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)
            palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
            palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.white)
        else:
            palette.setColor(QPalette.ColorRole.Window, QColor(255, 255, 255))  # 设置背景颜色
            palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.black)
            palette.setColor(QPalette.ColorRole.Base, QColor(240, 240, 240))  # 设置输入框背景颜色
            palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.black)
            palette.setColor(QPalette.ColorRole.Button, QColor(240, 240, 240))
            palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.black)

        self.setPalette(palette)
        self.update()  # 强制刷新界面

        # 更新按钮样式
        self.apply_button_style(self.pull_button)
        self.apply_button_style(self.reset_button)
        self.apply_button_style(self.manage_registry_button)
        self.apply_button_style(self.settings_button)  # 为 settings_button 应用样式

        # 更新消息框样式
        QApplication.instance().setPalette(palette)

        # 显式设置输入框的背景色
        input_widgets = [self.image_entry, self.tag_entry, self.registry_combobox, self.arch_combobox]
        for widget in input_widgets:
            if self.theme_mode == "dark":
                widget.setStyleSheet("background-color: #333333; color: white;")
            else:
                widget.setStyleSheet("background-color: #f0f0f0; color: black;")

    def update_ui_text(self):
        translations = {
            "zh": {
                "window_title": "Docker 镜像打包工具 v1.0.8",
                "registry_label": "仓库地址：",
                "image_label": "镜像名称：",
                "tag_label": "标签版本：",
                "arch_label": "系统架构：",
                "pull_btn": "拉取镜像",
                "reset_btn": "重置",
                "manage_btn": "管理仓库",
                "layer_progress": "当前层进度：",
                "overall_progress": "总体进度："
            },
            "en": {
                "window_title": "Docker Image Packer v1.0.8",
                "registry_label": "Registry:",
                "image_label": "Image Name:",
                "tag_label": "Tag:",
                "arch_label": "Architecture:",
                "pull_btn": "Pull Image",
                "reset_btn": "Reset",
                "manage_btn": "Manage Registries",
                "layer_progress": "Layer Progress:",
                "overall_progress": "Overall Progress:"
            }
        }
        trans = translations[self.language]

        self.setWindowTitle(trans["window_title"])
        self.registry_label.setText(trans["registry_label"])
        self.image_label.setText(trans["image_label"])
        self.tag_label.setText(trans["tag_label"])
        self.arch_label.setText(trans["arch_label"])
        self.pull_button.setText(trans["pull_btn"])
        self.reset_button.setText(trans["reset_btn"])
        self.manage_registry_button.setText(trans["manage_btn"])
        self.layer_progress_label.setText(trans["layer_progress"])
        self.overall_progress_label.setText(trans["overall_progress"])


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = DockerPullerGUI()
    window.show()
    sys.exit(app.exec())
