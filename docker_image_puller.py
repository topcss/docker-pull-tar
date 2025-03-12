import os
import sys
import gzip
import json
import hashlib
import shutil
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from tqdm import tqdm
import tarfile
import urllib3
import argparse
import logging
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed

# Set default encoding to UTF-8
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 禁用 SSL 警告
urllib3.disable_warnings()

# 版本号
VERSION = "v1.1.0"

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s', encoding='utf-8')
logger = logging.getLogger(__name__)

stop_event = threading.Event()


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
    if session.proxies.get('http') or session.proxies.get('https'):
        logger.info('使用代理设置从环境变量')

    return session

def parse_image_input(args):
    """解析用户输入的镜像名称，支持私有仓库格式"""
    image_input = args.image
    # 检查是否包含私有仓库地址
    if '/' in image_input and ('.' in image_input.split('/')[0] or ':' in image_input.split('/')[0]):
        # 私有仓库格式: harbor.abc.com/abc/nginx:1.26.0
        registry, remainder = image_input.split('/', 1)
        parts = remainder.split('/')
        if len(parts) == 1:
            repo = ''
            img_tag = parts[0]
        else:
            repo = '/'.join(parts[:-1])
            img_tag = parts[-1]
        
        # 解析镜像名和标签
        img, *tag_parts = img_tag.split(':')
        tag = tag_parts[0] if tag_parts else 'latest'
        
        # 组合成完整的仓库路径
        repository = remainder.split(':')[0]
        
        return registry, repository, img, tag
    else:
        # 标准Docker Hub格式
        parts = image_input.split('/')
        if len(parts) == 1:
            repo = 'library'
            img_tag = parts[0]
        else:
            repo = '/'.join(parts[:-1])
            img_tag = parts[-1]

        # 解析镜像名和标签
        img, *tag_parts = img_tag.split(':')
        tag = tag_parts[0] if tag_parts else 'latest'
        
        # 组合成完整的仓库路径
        repository = f'{repo}/{img}'
        if not args.custom_registry:
            registry = 'registry-1.docker.io'
        else:
            registry = args.custom_registry
        return registry, repository, img, tag

def get_auth_head(session, auth_url, reg_service, repository, username=None, password=None):
    """获取认证头，支持用户名密码认证"""
    try:
        url = f'{auth_url}?service={reg_service}&scope=repository:{repository}:pull'
        
        headers = {}
        # 如果提供了用户名和密码，添加到请求头
        if username and password:
            auth_string = f"{username}:{password}"
            encoded_auth = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
            headers['Authorization'] = f'Basic {encoded_auth}'
        
        # 打印 curl 命令
        logger.debug(f"获取认证头 CURL 命令: curl '{url}'")
        
        resp = session.get(url, headers=headers, verify=False, timeout=30)
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
        # 打印 curl 命令
        headers = ' '.join([f"-H '{key}: {value}'" for key, value in auth_head.items()])
        curl_command = f"curl '{url}' {headers}"
        logger.debug(f'获取镜像清单 CURL 命令: {curl_command}')
        resp = session.get(url, headers=auth_head, verify=False, timeout=30)
        if resp.status_code == 401:
            logger.info('需要认证。')
            return resp, 401
        resp.raise_for_status()
        return resp, 200
    except requests.exceptions.RequestException as e:
        logger.error(f'请求清单失败: {e}')
        raise

def select_manifest(manifests, arch):
    """选择适合指定架构的清单"""
    selected_manifest = None
    for m in manifests:
        if (m.get('annotations', {}).get('com.docker.official-images.bashbrew.arch') == arch or \
            m.get('platform',{}).get('architecture') == arch) and \
            m.get('platform', {}).get('os') == 'linux':
            selected_manifest = m.get('digest')
            break
    return selected_manifest

def download_file_with_progress(session, url, headers, save_path, desc):
    """下载文件"""
    try:
        with session.get(url, headers=headers, verify=False, timeout=30, stream=True) as resp:
            resp.raise_for_status()
            total_size = int(resp.headers.get('content-length', 0))

            with open(save_path, 'wb') as file, tqdm(
                total=total_size, unit='B', unit_scale=True, desc=desc, position=0, leave=True
            ) as pbar:
                for chunk in resp.iter_content(chunk_size=1024):
                    if stop_event.is_set():
                        raise KeyboardInterrupt
                    if chunk:
                        file.write(chunk)
                        pbar.update(len(chunk))
        return True
    except KeyboardInterrupt:
        logging.debug(f'⚠️  下载 {url} 被用户取消')
        if os.path.exists(save_path):
            os.remove(save_path)  # 删除部分下载的文件
        return False
    except Exception as e:
        logging.error(f'❌ 下载 {url} 失败: {e}')
        return False

def download_layers(session, registry, repository, layers, auth_head, imgdir, resp_json, imgparts, img, tag):
    """多线程下载镜像层"""
    os.makedirs(imgdir, exist_ok=True)

    try:
        config_digest = resp_json['config']['digest']
        config_filename = f'{config_digest[7:]}.json'
        config_path = os.path.join(imgdir, config_filename)
        config_url = f'https://{registry}/v2/{repository}/blobs/{config_digest}'

        logger.debug(f'下载 Config: {config_filename}')
        if not download_file_with_progress(session, config_url, auth_head, config_path, "Config"):
            raise Exception(f'Config JSON {config_filename} 下载失败')

    except Exception as e:
        logging.error(f'请求配置失败: {e}')
        return

    repo_tag = f'{"/".join(imgparts)}/{img}:{tag}' if imgparts else f'{img}:{tag}'
    content = [{'Config': config_filename, 'RepoTags': [repo_tag], 'Layers': []}]
    parentid = ''
    layer_json_map = {}

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {}
        try:
            for layer in layers:
                if stop_event.is_set():
                    raise KeyboardInterrupt  # 检测到终止信号

                ublob = layer['digest']
                fake_layerid = hashlib.sha256((parentid + '\n' + ublob + '\n').encode('utf-8')).hexdigest()
                layerdir = f'{imgdir}/{fake_layerid}'
                os.makedirs(layerdir, exist_ok=True)
                layer_json_map[fake_layerid] = {"id": fake_layerid, "parent": parentid if parentid else None}
                parentid = fake_layerid
                
                url = f'https://{registry}/v2/{repository}/blobs/{ublob}'
                save_path = f'{layerdir}/layer_gzip.tar'
                futures[executor.submit(download_file_with_progress, session, url, auth_head, save_path, ublob[:12])] = save_path
            
            for future in as_completed(futures):
                if stop_event.is_set():
                    raise KeyboardInterrupt  # 退出
                future.result()
        except KeyboardInterrupt:
            logging.error("用户终止下载，清理已下载文件...")
            stop_event.set()  # 设置终止标志
            executor.shutdown(wait=False)
            for future, save_path in futures.items():
                if os.path.exists(save_path):
                    os.remove(save_path)  # 删除部分下载的文件
            sys.exit(1)

    for fake_layerid in layer_json_map.keys():
        if stop_event.is_set():
            sys.exit(1)  # 检测到终止信号，提前退出

        layerdir = f'{imgdir}/{fake_layerid}'
        gz_path = f'{layerdir}/layer_gzip.tar'
        tar_path = f'{layerdir}/layer.tar'

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

    logging.info(f'✅ 镜像 {img}:{tag} 下载完成！')

def create_image_tar(imgdir, repository, tag, arch):
    """将镜像打包为 tar 文件"""
    safe_repo = repository.replace("/", "_")
    docker_tar = f'{safe_repo}_{tag}_{arch}.tar'
    try:
        with tarfile.open(docker_tar, "w") as tar:
            tar.add(imgdir, arcname='/')
        logger.debug(f'Docker 镜像已拉取：{docker_tar}')
        return docker_tar
    except Exception as e:
        logger.error(f'打包镜像失败: {e}')
        raise

def cleanup_tmp_dir():
    """删除 tmp 目录"""
    tmp_dir = 'tmp'
    try:
        if os.path.exists(tmp_dir):
            logger.debug(f'清理临时目录: {tmp_dir}')
            shutil.rmtree(tmp_dir)
            logger.debug('临时目录已清理。')
    except Exception as e:
        logger.error(f'清理临时目录失败: {e}')

def main():
    """主函数"""
    try:
        parser = argparse.ArgumentParser(description="Docker 镜像拉取工具")
        parser.add_argument("-i", "--image", required=False, help="Docker 镜像名称（例如：nginx:latest 或 harbor.abc.com/abc/nginx:1.26.0）")
        parser.add_argument("-q", "--quiet", action="store_true", help="静默模式，减少交互")
        parser.add_argument("-r", "--custom_registry", help="自定义仓库地址（例如：harbor.abc.com）")
        parser.add_argument("-a", "--arch", help="架构,默认：amd64,常见：amd64, arm64v8等")
        parser.add_argument("-u", "--username", help="Docker 仓库用户名")
        parser.add_argument("-p", "--password", help="Docker 仓库密码")
        parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {VERSION}", help="显示版本信息")
        parser.add_argument("--debug", action="store_true", help="启用调试模式，打印请求 URL 和连接状态")

        # 显示程序的信息
        logger.info(f'欢迎使用 Docker 镜像拉取工具 {VERSION}')

        args = parser.parse_args()

        if args.debug:
            logger.setLevel(logging.DEBUG)

        # 获取镜像名称
        if not args.image:
            args.image = input("请输入 Docker 镜像名称（例如：nginx:latest 或 harbor.abc.com/abc/nginx:1.26.0）：").strip()
            if not args.image:
                logger.error("错误：镜像名称是必填项。")
                return
        
        # # 获取架构
        # if not args.arch and not args.quiet:
        #     args.arch = input("请输入架构（常见: amd64, arm64v8等，默认: amd64）：").strip() or 'amd64'
        
        # 获取自定义仓库地址
        if not args.custom_registry and not args.quiet:
            # use_custom_registry = input("是否使用自定义仓库地址？(y/n, 默认: y): ").strip().lower() or 'y'
            # if use_custom_registry == 'y':
            #     args.custom_registry = input("请输入自定义仓库地址: )").strip()
            args.custom_registry = input("请输入自定义仓库地址: （默认 dockerhub）").strip()

        # 解析镜像信息
        registry, repository, img, tag = parse_image_input(args)
        
        # 获取认证信息
        if not args.username and not args.quiet:
            args.username = input("请输入镜像仓库用户名: ").strip()
        if not args.password and not args.quiet:
            args.password = input("请输入镜像仓库密码: ").strip()
        session = create_session()
        auth_head = None
        try:
            url = f'https://{registry}/v2/'
            logger.debug(f"获取认证信息 CURL 命令: curl '{url}'")
            resp = session.get(url, verify=False, timeout=30)
            auth_url = resp.headers['WWW-Authenticate'].split('"')[1]
            reg_service = resp.headers['WWW-Authenticate'].split('"')[3]
            auth_head = get_auth_head(session, auth_url, reg_service, repository, args.username, args.password)
            # 获取清单
            resp, http_code = fetch_manifest(session, registry, repository, tag, auth_head)
            if http_code == 401:
                use_auth = input(f"当前仓库 {registry}，需要登录？(y/n, 默认: y): ").strip().lower() or 'y'
                if use_auth == 'y':
                    args.username = input("请输入用户名: ").strip()
                    args.password = input("请输入密码: ").strip()
                auth_head = get_auth_head(session, auth_url, reg_service, repository, args.username, args.password)
        
            resp, http_code = fetch_manifest(session, registry, repository, tag, auth_head)
        except requests.exceptions.RequestException as e:
            logger.error(f'连接仓库失败: {e}')
            raise

        resp_json = resp.json()
        
        # 处理多架构镜像
        manifests = resp_json.get('manifests')
        if manifests is not None:
            archs = [m.get('annotations', {}).get('com.docker.official-images.bashbrew.arch') or 
                     m.get('platform',{}).get('architecture') 
                     for m in manifests if m.get('platform',{}).get('os') == 'linux']
            
            # 打印架构列表
            if archs:
                logger.debug(f'当前可用架构：{", ".join(archs)}')

            if len(archs) == 1:
                args.arch = archs[0]
                logger.info(f'自动选择唯一可用架构: {args.arch}')

            # 获取架构
            if not args.arch or args.arch not in archs:
                args.arch = input(f"请输入架构（可选: {', '.join(archs)}，默认: amd64）：").strip() or 'amd64'

            digest = select_manifest(manifests, args.arch)
            if not digest:
                logger.error(f'在清单中找不到指定的架构 {args.arch}')
                return

            # 构造请求
            url = f'https://{registry}/v2/{repository}/manifests/{digest}'
            headers = ' '.join([f"-H '{key}: {value}'" for key, value in auth_head.items()])
            curl_command = f"curl '{url}' {headers}"
            logger.debug(f'获取架构清单 CURL 命令: {curl_command}')

            # 获取清单
            manifest_resp = session.get(url, headers=auth_head, verify=False, timeout=30)
            try:
                manifest_resp.raise_for_status()
                resp_json = manifest_resp.json()
            except Exception as e:
                logger.error(f'获取架构清单失败: {e}')
                return

            if 'layers' not in resp_json:
                logger.error('错误：清单中没有层')
                return


        logger.info(f'仓库地址：{registry}')
        logger.info(f'镜像：{repository}')
        logger.info(f'标签：{tag}')
        logger.info(f'架构：{args.arch}')

        # 下载镜像层
        imgdir = 'tmp'
        os.makedirs(imgdir, exist_ok=True)
        logger.info('开始下载')

        # 根据镜像类型，提供正确的imgparts
        if registry == 'registry-1.docker.io' and repository.startswith('library/'):
            # Docker Hub
            imgparts = []  # 官方镜像不需要前缀
        else:
            # 
            imgparts = repository.split('/')[:-1]  
        
        download_layers(session, registry, repository, resp_json['layers'], auth_head, imgdir, resp_json, imgparts, img, tag)

        # 打包镜像
        output_file = create_image_tar(imgdir, repository, tag, args.arch)
        logger.info(f'镜像已保存为: {output_file}')
        logger.info(f'可使用以下命令导入镜像: docker load -i {output_file}')
        if registry not in ("registry-1.docker.io", "docker.io"):
            logger.info(f'您可能需要: docker tag {repository}:{tag} {registry}/{repository}:{tag}')



    except KeyboardInterrupt:
        logger.info('用户取消操作。')
    except requests.exceptions.RequestException as e:
        logger.error(f'网络连接失败: {e}')
    except json.JSONDecodeError as e:
        logger.error(f'JSON解析失败: {e}')
    except FileNotFoundError as e:
        logger.error(f'文件操作失败: {e}')
    except argparse.ArgumentError as e:
        logger.error(f'命令行参数错误: {e}')
    except Exception as e:
        logger.error(f'程序运行过程中发生异常: {e}')
        import traceback
        logger.debug(traceback.format_exc())

    finally:
        cleanup_tmp_dir()
        input("按任意键退出程序...")
        sys.exit(0)

if __name__ == '__main__':
    main()