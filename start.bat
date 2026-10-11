@echo off
chcp 65001 >nul
cd /d "%~dp0"

python -c "import flask, requests, bs4, trafilatura, playwright" 2>nul
if errorlevel 1 (
    echo [1/2] 首次运行,正在安装依赖...
    python -m pip install --no-index --find-links wheels -r requirements.txt
    if errorlevel 1 (
        echo.
        echo 依赖安装失败。请查看 README.md 的「装不上依赖怎么办」一节。
        pause
        exit /b 1
    )
) else (
    echo [1/2] 依赖已就绪
)

echo [2/2] 启动服务,浏览器将自动打开 http://127.0.0.1:5000
echo 关闭本窗口即可停止服务。
start "" http://127.0.0.1:5000
python app.py
pause
