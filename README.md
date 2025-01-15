# Docker Image Puller

## 项目简介

Docker Image Puller 是一个强大的工具，用于从 Docker 仓库拉取镜像，支持国内镜像源加速和多架构支持。该工具采用 MIT 许可证，开放源代码，方便用户根据需要进行定制和扩展。

## 特点

- **国内镜像源加速**: 通过配置国内镜像源，大幅提高镜像下载速度。
- **多架构支持**: 支持多种架构（如 amd64, arm64），满足不同环境需求。
- **兼容最新 Docker Hub API**: 确保与 Docker Hub 的最新接口兼容，获取最新的镜像信息。
- **单文件 Python 脚本**: 便于携带和使用，无需复杂安装。
- **无依赖 EXE 执行**: 编译为独立 EXE 文件，无需安装 Python 环境，在 Releases 下载。
- **用户友好**: 提供交互式输入，简化操作流程。
- **优化性能**: 提高下载速度和可靠性。

## 安装

### 通过 Git 克隆

```bash
git clone https://github.com/topcss/docker-pull-tar.git
```


### 依赖安装

确保已安装 Python 3.x 版本。如果需要将脚本编译为 EXE，可以使用 PyInstaller：

```bash
pip install pyinstaller
pyinstaller --onefile docker_image_puller.py
```

## 使用

### 基本用法

```bash
python docker_image_puller.py [镜像名称] [架构] [仓库地址]
```

### 示例

```bash
D:\> DockerPull.exe

欢迎使用 Docker 镜像拉取工具！
请输入以下信息：
请输入 Docker 镜像名称（例如：library/ubuntu:latest）：alpine
请输入架构（默认：amd64）：
请输入 Docker 仓库地址（默认：docker.xuanyuan.me）：
仓库地址：docker.xuanyuan.me
仓库名：library/alpine
标签：latest
架构：amd64
Docker 镜像已拉取：library_alpine.tar
```

```bash
python docker_image_puller.py library/ubuntu:latest amd64 docker.xuanyuan.me
```


## 许可证

本项目采用 MIT 许可证，详情见 [LICENSE](LICENSE) 文件。

## 联系方式

如有任何问题或建议，请通过 [GitHub Issues](https://github.com/topcss/docker-pull-tar/issues) 提出。

## 为什么选择这个工具？

- **速度快**: 国内镜像源加速，下载更快。
- **架构灵活**: 支持多架构，适应各种环境。
- **易于使用**: 单文件脚本，无需复杂配置。
- **开放源代码**: 自由定制和扩展。

## 常见问题

**Q**: 如何配置国内镜像源？  
**A**: 在命令行中指定仓库地址参数，例如 `docker.xuanyuan.me`。

**Q**: 支持哪些架构？  
**A**: 目前支持 amd64 和 arm64 架构。

希望通过这个工具能为您的 Docker 镜像管理带来便利！ 🚀