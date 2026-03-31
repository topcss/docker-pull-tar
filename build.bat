@echo off
chcp 65001 >nul
echo ========================================
echo Docker Image Puller 打包脚本
echo ========================================

echo.
echo [1/4] 检查 Python 环境...
python --version
if errorlevel 1 (
    echo 错误: 未找到 Python，请先安装 Python
    pause
    exit /b 1
)

echo.
echo [2/4] 安装依赖...
pip install -r requirements.txt
pip install pyinstaller

echo.
echo [3/4] 清理旧的构建文件...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "*.spec" del /q "*.spec"

echo.
echo [4/4] 开始打包...
pyinstaller --onefile ^
    --name DockerPull ^
    --icon favicon.ico ^
    --add-data "favicon.ico;." ^
    --version-file version.txt ^
    --console ^
    --clean ^
    --noconfirm ^
    docker_image_puller.py

if errorlevel 1 (
    echo.
    echo ❌ 打包失败！
    pause
    exit /b 1
)

echo.
echo ========================================
echo ✅ 打包完成！
echo 输出文件: dist\DockerPull.exe
echo ========================================
echo.

echo 是否压缩发布？(y/n)
set /p compress=
if /i "%compress%"=="y" (
    echo.
    echo 压缩文件...
    if exist "DockerPull.zip" del /q "DockerPull.zip"
    powershell Compress-Archive -Path "dist\DockerPull.exe" -DestinationPath "DockerPull.zip"
    echo ✅ 已创建 DockerPull.zip
)

echo.
pause
