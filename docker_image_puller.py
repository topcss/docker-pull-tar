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
urllib3.disable_warnings()

def create_session():
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    http_proxy = os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')
    https_proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
    
    if http_proxy or https_proxy:
        session.proxies = {
            'http': http_proxy,
            'https': https_proxy
        }
        print('使用代理设置从环境变量')
    
    return session

def get_user_input():
    print("欢迎使用 Docker 镜像拉取工具！")
    print("请输入以下信息：")
    
    image_input = input("请输入 Docker 镜像名称（例如：library/ubuntu:latest）：").strip()
    if not image_input:
        print("错误：镜像名称是必填项。")
        sys.exit(1)
    
    arch = input("请输入架构（默认：amd64）：").strip() or 'amd64'
    registry = input("请输入 Docker 仓库地址（默认：docker.xuanyuan.me）：").strip() or 'docker.xuanyuan.me'
    
    return image_input, arch, registry

def parse_image_input(image_input):
    repo = 'library'
    tag = 'latest'
    imgparts = image_input.split('/')
    try:
        img, tag = imgparts[-1].split(':')
    except ValueError:
        img = imgparts[-1]
    
    if len(imgparts) > 1 and ('.' in imgparts[0] or ':' in imgparts[0]):
        registry = imgparts[0]
        repo = '/'.join(imgparts[1:-1])
    else:
        if len(imgparts[:-1]) != 0:
            repo = '/'.join(imgparts[:-1])
    
    return repo, img, tag, imgparts

def get_auth_head(session, auth_url, reg_service, repository):
    try:
        resp = session.get(f'{auth_url}?service={reg_service}&scope=repository:{repository}:pull', verify=False, timeout=30)
        access_token = resp.json()['token']
        auth_head = {'Authorization': f'Bearer {access_token}', 'Accept': 'application/vnd.docker.distribution.manifest.v2+json'}
        return auth_head
    except requests.exceptions.RequestException as e:
        print(f'身份验证错误：{e}')
        exit(1)

def fetch_manifest(session, registry, repository, tag, auth_head):
    try:
        resp = session.get(f'https://{registry}/v2/{repository}/manifests/{tag}', headers=auth_head, verify=False, timeout=30)
        resp.raise_for_status()
        return resp
    except requests.exceptions.RequestException as e:
        print(f'获取清单错误：{e}')
        exit(1)

def select_manifest(resp_json, arch):
    if 'manifests' in resp_json:
        for m in resp_json['manifests']:
            platform = m.get('platform', {})
            if platform.get('os') == 'linux' and platform.get('architecture') == arch:
                return m
        for m in resp_json['manifests']:
            platform = m.get('platform', {})
            if platform.get('os') == 'windows' and platform.get('architecture') == 'amd64':
                return m
        return resp_json['manifests'][0]
    else:
        return None

def download_layers(session, registry, repository, layers, auth_head, imgdir, resp_json, imgparts, img, tag):
    config = resp_json['config']['digest']
    confresp = session.get(f'https://{registry}/v2/{repository}/blobs/{config}', headers=auth_head, verify=False, timeout=30)
    with open(f'{imgdir}/{config[7:]}.json', 'wb') as file:
        file.write(confresp.content)
    
    content = [{
        'Config': f'{config[7:]}.json',
        'RepoTags': [],
        'Layers': []
    }]
    if len(imgparts[:-1]) != 0:
        content[0]['RepoTags'].append('/'.join(imgparts[:-1]) + '/' + img + ':' + tag)
    else:
        content[0]['RepoTags'].append(img + ':' + tag)
    
    empty_json = '{"created":"1970-01-01T00:00:00Z","container_config":{"Hostname":"","Domainname":"","User":"","AttachStdin":false, "AttachStdout":false,"AttachStderr":false,"Tty":false,"OpenStdin":false, "StdinOnce":false,"Env":null,"Cmd":null,"Image":"", "Volumes":null,"WorkingDir":"","Entrypoint":null,"OnBuild":null,"Labels":null}}'
    
    parentid = ''
    for layer in layers:
        ublob = layer['digest']
        fake_layerid = hashlib.sha256((parentid + '\n' + ublob + '\n').encode('utf-8')).hexdigest()
        layerdir = f'{imgdir}/{fake_layerid}'
        os.mkdir(layerdir)
        
        with open(f'{layerdir}/VERSION', 'w') as file:
            file.write('1.0')
        
        try:
            bresp = session.get(f'https://{registry}/v2/{repository}/blobs/{ublob}', headers=auth_head, stream=True, verify=False, timeout=30)
            bresp.raise_for_status()
            
            # 使用 tqdm 显示下载进度
            total_size = int(bresp.headers.get('content-length', 0))
            with tqdm(total=total_size, unit='B', unit_scale=True, desc=f'Downloading {ublob[:12]}') as pbar:
                with open(f'{layerdir}/layer_gzip.tar', 'wb') as file:
                    for chunk in bresp.iter_content(chunk_size=1024):
                        if chunk:
                            file.write(chunk)
                            pbar.update(len(chunk))
        
        except requests.exceptions.RequestException as e:
            print(f'下载层错误：{e}')
            exit(1)
        
        with open(f'{layerdir}/layer.tar', 'wb') as file:
            with gzip.open(f'{layerdir}/layer_gzip.tar', 'rb') as gz:
                shutil.copyfileobj(gz, file)
        os.remove(f'{layerdir}/layer_gzip.tar')
        
        content[0]['Layers'].append(f'{fake_layerid}/layer.tar')
        
        if layers[-1]['digest'] == layer['digest']:
            json_obj = json.loads(confresp.content)
            del json_obj['history']
            try:
                del json_obj['rootfs']
            except KeyError:
                del json_obj['rootfS']
        else:
            json_obj = json.loads(empty_json)
        json_obj['id'] = fake_layerid
        if parentid:
            json_obj['parent'] = parentid
        parentid = json_obj['id']
        
        with open(f'{layerdir}/json', 'w') as file:
            file.write(json.dumps(json_obj))
    
    with open(f'{imgdir}/manifest.json', 'w') as file:
        file.write(json.dumps(content))
    
    if len(imgparts[:-1]) != 0:
        content = { '/'.join(imgparts[:-1]) + '/' + img : { tag : fake_layerid } }
    else:
        content = { img : { tag : fake_layerid } }
    with open(f'{imgdir}/repositories', 'w') as file:
        file.write(json.dumps(content))

def create_image_tar(imgdir, repo, img):
    docker_tar = f'{repo.replace("/", "_")}_{img}.tar'
    with tarfile.open(docker_tar, "w") as tar:
        tar.add(imgdir, arcname=os.path.sep)
    shutil.rmtree(imgdir)
    print(f'Docker 镜像已拉取：{docker_tar}')

def main():
    if len(sys.argv) < 2:
        image_input, arch, registry = get_user_input()
    else:
        image_input = sys.argv[1]
        arch = 'amd64' if len(sys.argv) < 3 else sys.argv[2]
        registry = 'registry-1.docker.io' if len(sys.argv) < 4 else sys.argv[3]
    
    repo, img, tag, imgparts = parse_image_input(image_input)
    repository = f'{repo}/{img}'
    
    print(f'仓库地址：{registry}')
    print(f'仓库名：{repository}')
    print(f'标签：{tag}')
    print(f'架构：{arch}')
    
    session = create_session()
    
    try:
        resp = session.get(f'https://{registry}/v2/', verify=False, timeout=30)
        if resp.status_code == 401:
            auth_url = resp.headers['WWW-Authenticate'].split('"')[1]
            reg_service = resp.headers['WWW-Authenticate'].split('"')[3]
    except requests.exceptions.RequestException as e:
        print(f'连接错误：{e}')
        exit(1)
    
    auth_head = get_auth_head(session, auth_url, reg_service, repository)
    
    resp = fetch_manifest(session, registry, repository, tag, auth_head)
    resp_json = resp.json()
    
    selected_manifest = select_manifest(resp_json, arch)
    if selected_manifest:
        manifest_auth_head = get_auth_head(session, auth_url, reg_service, repository)
        manifest_resp = session.get(f'https://{registry}/v2/{repository}/manifests/{selected_manifest["digest"]}', headers=manifest_auth_head, verify=False, timeout=30)
        manifest_resp.raise_for_status()
        resp_json = manifest_resp.json()
    else:
        resp_json = resp.json()  # 如果没有选择新的清单，使用原始的resp_json
    
    if 'layers' not in resp_json:
        print('错误：清单中没有层')
        exit(1)
    
    layers = resp_json['layers']
    
    imgdir = 'tmp'
    if not os.path.exists(imgdir):
        os.makedirs(imgdir)
    
    print('开始下载层...')
    download_layers(session, registry, repository, layers, auth_head, imgdir, resp_json, imgparts, img, tag)
    
    create_image_tar(imgdir, repo, img)

if __name__ == '__main__':
    main()