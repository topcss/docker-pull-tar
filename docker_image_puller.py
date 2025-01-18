import os
import sys
import gzip
import json
import hashlib
import shutil
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from tqdm import tqdm
import tarfile
import urllib3
import argparse
import logging

# Set default encoding to UTF-8
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 禁用 SSL 警告
urllib3.disable_warnings()

# 版本号
VERSION = "v1.0.5"

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s', encoding='utf-8')
logger = logging.getLogger(__name__)

def create_session():
    """创建带有重试和代理配置的请求会话"""
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
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
    if session.proxies:
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
        logger.debug(f'获取认证头 URL: {url}')
        resp = session.get(url, verify=False, timeout=30)
        resp.raise_for_status()
        access_token = resp.json()['token']
        return {'Authorization': f'Bearer {access_token}', 'Accept': 'application/vnd.docker.distribution.manifest.v2+json'}
    except requests.exceptions.RequestException as e:
        logger.error(f'请求认证失败: {e}')
        raise

def fetch_manifest(session, registry, repository, tag, auth_head):
    """获取镜像清单"""
    try:
        url = f'https://{registry}/v2/{repository}/manifests/{tag}'
        logger.debug(f'获取镜像清单 URL: {url}')
        resp = session.get(url, headers=auth_head, verify=False, timeout=30)
        resp.raise_for_status()
        return resp
    except requests.exceptions.RequestException as e:
        logger.error(f'请求清单失败: {e}')
        raise

def select_manifest(resp_json, arch):
    """选择适合指定架构的清单"""
    for m in resp_json.get('manifests', []):
        platform = m.get('platform', {})
        if platform.get('os') == 'linux' and platform.get('architecture') == arch:
            return m
    return resp_json.get('manifests', [{}])[0]

def download_layers(session, registry, repository, layers, auth_head, imgdir, resp_json, imgparts, img, tag):
    """下载镜像层"""
    try:
        config = resp_json['config']['digest']
        url = f'https://{registry}/v2/{repository}/blobs/{config}'
        logger.debug(f'请求配置 URL: {url}')
        with session.get(url, headers=auth_head, verify=False, timeout=30, stream=True) as confresp:
            confresp.raise_for_status()
            with open(f'{imgdir}/{config[7:]}.json', 'wb') as file:
                shutil.copyfileobj(confresp.raw, file)
    except requests.exceptions.RequestException as e:
        logger.error(f'请求配置失败: {e}')
        raise

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
    for layer in layers:
        ublob = layer['digest']
        fake_layerid = hashlib.sha256((parentid + '\n' + ublob + '\n').encode('utf-8')).hexdigest()
        layerdir = f'{imgdir}/{fake_layerid}'
        os.makedirs(layerdir, exist_ok=True)

        with open(f'{layerdir}/VERSION', 'w') as file:
            file.write('1.0')

        try:
            # 下载压缩的镜像层
            url = f'https://{registry}/v2/{repository}/blobs/{ublob}'
            logger.debug(f'请求层 URL: {url}')
            with session.get(url, headers=auth_head, verify=False, timeout=30, stream=True) as bresp:
                bresp.raise_for_status()
                total_size = int(bresp.headers.get('content-length', 0))
                with tqdm(total=total_size, unit='B', unit_scale=True, desc=f'Downloading {ublob[:12]}') as pbar:
                    with open(f'{layerdir}/layer_gzip.tar', 'wb') as file:
                        for chunk in bresp.iter_content(chunk_size=1024):
                            if chunk:
                                file.write(chunk)
                                pbar.update(len(chunk))

                # 解压缩镜像层
                with gzip.open(f'{layerdir}/layer_gzip.tar', 'rb') as gz, open(f'{layerdir}/layer.tar', 'wb') as file:
                    shutil.copyfileobj(gz, file)
                os.remove(f'{layerdir}/layer_gzip.tar')

                content[0]['Layers'].append(f'{fake_layerid}/layer.tar')

                # 生成层元数据
                if layers[-1]['digest'] == layer['digest']:
                    json_obj = json.loads(open(f'{imgdir}/{config[7:]}.json').read())
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
        except requests.exceptions.RequestException as e:
            logger.error(f'请求层失败: {e}')
            raise

    # 保存清单和仓库信息
    with open(f'{imgdir}/manifest.json', 'w') as file:
        json.dump(content, file)

    repo_tag = f'{"/".join(imgparts[:-1])}/{img}' if imgparts[:-1] else img
    with open(f'{imgdir}/repositories', 'w') as file:
        json.dump({repo_tag: {tag: fake_layerid}}, file)

def create_image_tar(imgdir, repo, img):
    """将镜像打包为 tar 文件"""
    docker_tar = f'{repo.replace("/", "_")}_{img}.tar'
    with tarfile.open(docker_tar, "w") as tar:
        tar.add(imgdir, arcname='/')
    shutil.rmtree(imgdir)
    logger.info(f'Docker 镜像已拉取：{docker_tar}')

def cleanup_tmp_dir():
    """删除 tmp 目录"""
    tmp_dir = 'tmp'
    if os.path.exists(tmp_dir):
        logger.info(f'清理临时目录: {tmp_dir}')
        shutil.rmtree(tmp_dir)
        logger.info('临时目录已清理。')

def main():
    """主函数"""
    try:
        parser = argparse.ArgumentParser(description="Docker 镜像拉取工具")
        parser.add_argument("-i", "--image", required=False, help="Docker 镜像名称（例如：library/ubuntu:latest）")
        parser.add_argument("-a", "--arch", help="架构（默认：amd64）")
        parser.add_argument("-r", "--registry", help="Docker 仓库地址（默认：docker.xuanyuan.me）")
        parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {VERSION}", help="显示版本信息")
        parser.add_argument("--debug", action="store_true", help="启用调试模式，打印请求 URL 和连接状态")

        # 显示程序的信息
        logger.info(f'欢迎使用 Docker 镜像拉取工具 {VERSION}')

        args = parser.parse_args()

        if not args.image:
            args.image = input("请输入 Docker 镜像名称（例如：library/ubuntu:latest）：").strip()
            if not args.image:
                logger.error("错误：镜像名称是必填项。")
                return

        # 获取架构
        if not args.arch:
            args.arch = input("请输入架构（默认：amd64）：").strip() or 'amd64'
        
        # 获取仓库地址
        if not args.registry:
            args.registry = input("请输入 Docker 仓库地址（默认：docker.xuanyuan.me）：").strip() or 'docker.xuanyuan.me'

        if args.debug:
            logger.setLevel(logging.DEBUG)

        repo, img, tag = parse_image_input(args.image)
        repository = f'{repo}/{img}'

        logger.info(f'仓库地址：{args.registry}')
        logger.info(f'仓库名：{repository}')
        logger.info(f'标签：{tag}')
        logger.info(f'架构：{args.arch}')

        session = create_session()

        # 获取认证信息
        try:
            url = f'https://{args.registry}/v2/'
            logger.debug(f'获取认证信息 URL: {url}')
            resp = session.get(url, verify=False, timeout=30)
            if resp.status_code == 401:
                auth_url = resp.headers['WWW-Authenticate'].split('"')[1]
                reg_service = resp.headers['WWW-Authenticate'].split('"')[3]
                auth_head = get_auth_head(session, auth_url, reg_service, repository)
            else:
                auth_head = {'Accept': 'application/vnd.docker.distribution.manifest.v2+json'}
        except requests.exceptions.RequestException as e:
            logger.error(f'连接仓库失败: {e}')
            raise

        # 获取清单
        resp = fetch_manifest(session, args.registry, repository, tag, auth_head)
        resp_json = resp.json()

        # 选择适合的清单
        selected_manifest = select_manifest(resp_json, args.arch)
        if selected_manifest:
            url = f'https://{args.registry}/v2/{repository}/manifests/{selected_manifest["digest"]}'
            logger.debug(f'获取清单 URL: {url}')
            manifest_resp = session.get(url, headers=auth_head, verify=False, timeout=30)
            manifest_resp.raise_for_status()
            resp_json = manifest_resp.json()

        if 'layers' not in resp_json:
            logger.error('错误：清单中没有层')
            return

        # 下载镜像层
        imgdir = 'tmp'
        os.makedirs(imgdir, exist_ok=True)
        logger.info('开始下载层...')
        download_layers(session, args.registry, repository, resp_json['layers'], auth_head, imgdir, resp_json, [repo], img, tag)

        # 打包镜像
        create_image_tar(imgdir, repo, img)

    except KeyboardInterrupt:
        logger.info('用户取消操作。')
        cleanup_tmp_dir()

    except requests.exceptions.RequestException as e:
        logger.error(f'网络连接失败: {e}')

    except Exception as e:
        logger.error(f'程序运行过程中发生异常: {e}')

    finally:
        input("按任意键退出程序...")
        sys.exit(0)

if __name__ == '__main__':
    main()