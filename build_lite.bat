@echo off
chcp 65001 >nul
echo ========================================
echo Docker Image Puller 精简打包脚本
echo ========================================

echo.
echo [1/3] 检查 Python 环境...
python --version
if errorlevel 1 (
    echo 错误: 未找到 Python，请先安装 Python
    pause
    exit /b 1
)

echo.
echo [2/3] 清理旧的构建文件...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "DockerPull.spec" del /q "DockerPull.spec"

echo.
echo [3/3] 开始精简打包...
pyinstaller --onefile ^
    --name DockerPull ^
    --icon favicon.ico ^
    --version-file version.txt ^
    --console ^
    --clean ^
    --noconfirm ^
    --exclude-module matplotlib ^
    --exclude-module numpy ^
    --exclude-module pandas ^
    --exclude-module scipy ^
    --exclude-module torch ^
    --exclude-module tensorflow ^
    --exclude-module PIL ^
    --exclude-module cv2 ^
    --exclude-module sklearn ^
    --exclude-module transformers ^
    --exclude-module gradio ^
    --exclude-module jieba ^
    --exclude-module nltk ^
    --exclude-module h5py ^
    --exclude-module pyarrow ^
    --exclude-module sqlalchemy ^
    --exclude-module openpyxl ^
    --exclude-module lxml ^
    --exclude-module uvicorn ^
    --exclude-module fastapi ^
    --exclude-module flask ^
    --exclude-module django ^
    --exclude-module tornado ^
    --exclude-module twisted ^
    --exclude-module cryptography ^
    --exclude-module nacl ^
    --exclude-module PIL ^
    --exclude-module tkinter ^
    --exclude-module turtle ^
    --exclude-module test ^
    --exclude-module tests ^
    --exclude-module unittest ^
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
for %%I in (dist\DockerPull.exe) do echo 文件大小: %%~zI 字节 (约 %%~zI / 1048576 MB)
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
