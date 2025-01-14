# Docker Pull Tool

这是一个基于 `docker-drag` 实现的 Python 工具，用于从 Docker Hub 或其他镜像源下载 Docker 镜像并保存为 `.tar` 文件。支持以下功能：

- **新版 Docker Hub API 支持**：兼容 Docker Hub 的最新 API。
- **多架构支持**：可以指定下载的镜像架构（如 `amd64`、`arm64` 等）。
- **镜像源加速**：支持指定镜像源以加速下载。
- **简单易用**：通过命令行参数快速下载镜像。


---

## 使用方法

### 基本命令
```bash
python docker_pull.py <image_name> [options]
```

### 示例
下载 `alpine` 镜像的 `arm64` 架构版本，使用自定义镜像源加速下载：
   ```bash
   python docker_pull.py alpine arm64 docker.xuanyuan.me
   ```
---

## 示例输出

```bash
$ python docker_pull.py alpine --arch arm64 --registry docker.xuanyuan.me
[INFO] 开始下载镜像: alpine:latest (arm64)
[INFO] 使用镜像源: docker.xuanyuan.me
[INFO] 下载 manifest 文件...
[INFO] 下载 config 文件...
[INFO] 下载 layer 1/3: sha256:123456...
[INFO] 下载 layer 2/3: sha256:abcdef...
[INFO] 下载 layer 3/3: sha256:987654...
[INFO] 打包镜像为 alpine_latest_arm64.tar...
[INFO] 镜像下载完成: alpine_latest_arm64.tar
```

---

## 注意事项

1. 确保网络连接正常，尤其是访问 Docker Hub 或自定义镜像源时。
2. 如果下载速度较慢，建议使用国内镜像源（如 `docker.xuanyuan.me`）。
3. 下载的 `.tar` 文件可以通过 `docker load -i <file>.tar` 命令加载到本地 Docker 环境。

---

## 许可证

本项目基于 MIT 许可证开源。详情请参阅 [LICENSE](LICENSE) 文件。

---

## 贡献

欢迎提交 Issue 或 Pull Request 以改进本工具！
