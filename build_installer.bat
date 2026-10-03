@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

echo ============================================================
echo   PrimiGenius Build Script
echo   Build Windows Installer (NSIS)
echo ============================================================
echo.

:: ---- 配置区 ----
set "PROJECT_DIR=%~dp0"
cd /d "%PROJECT_DIR%"

:: ---- Step 1/5: 环境检测 ----
echo [Step 1/5] 检测构建环境...

where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] 未找到 Python，请先安装 Python 并添加到 PATH。
    pause
    exit /b 1
)

where node >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] 未找到 Node.js，请先安装 Node.js 并添加到 PATH。
    pause
    exit /b 1
)

where npm >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] 未找到 npm，请先安装 Node.js 并添加到 PATH。
    pause
    exit /b 1
)

:: 检查 PyInstaller
python -m PyInstaller --version >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [WARN] PyInstaller 未安装，正在自动安装...
    python -m pip install pyinstaller
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] PyInstaller 安装失败。
        pause
        exit /b 1
    )
)

:: 检查 Docker SDK (必须)
python -c "import docker" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [WARN] Docker SDK 未安装，正在自动安装...
    python -m pip install docker
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] Docker SDK 安装失败，这是 Podman 连接的必要依赖。
        pause
        exit /b 1
    )
)

:: 检查 Podman Python Bindings (可选)
python -c "from podman import PodmanClient" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [WARN] Podman Python Bindings 未安装，正在尝试安装 podman-py...
    python -m pip install podman-py 2>nul
    if %ERRORLEVEL% neq 0 (
        echo [WARN] podman-py 安装失败，将使用 Docker SDK 作为 fallback。
    )
)

:: 检查 node_modules
if not exist "%PROJECT_DIR%node_modules" (
    echo [WARN] node_modules 不存在，正在安装依赖...
    call npm install
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] npm install 失败。
        pause
        exit /b 1
    )
)

echo [OK] 环境检测通过。
echo.

:: ---- Step 2/5: 检查内置 Podman 和 WSL2 内核更新 ----
echo [Step 2/5] Checking built-in resources...

if exist "%PROJECT_DIR%podman\podman.exe" (
    echo [OK] podman.exe ready: podman\podman.exe
) else (
    echo [WARN] Built-in podman.exe not found.
    echo        Place Podman Windows binaries in podman\ directory.
    echo        Download from https://github.com/containers/podman/releases
)

if exist "%PROJECT_DIR%build\wsl.2.7.3.0.x64.msi" (
    echo [OK] WSL2 full installer ready: build\wsl.2.7.3.0.x64.msi
) else (
    echo [WARN] wsl.2.7.3.0.x64.msi not found in build\, WSL2 will be installed via wsl --install on first launch.
)

if exist "%PROJECT_DIR%build\jre8.zip" (
    echo [OK] Embedded JRE8 ready: build\jre8.zip
) else (
    echo [WARN] jre8.zip not found in build\, Java plugins will not work offline.
)

echo.

:: ---- Step 3/5: 清理旧构建产物并打包后端 ----
echo [Step 3/5] 使用 PyInstaller 打包 Python 后端 (app.py -^> app.exe)...

:: 清理旧构建产物
if exist "%PROJECT_DIR%backend\dist" (
    rmdir /s /q "%PROJECT_DIR%backend\dist"
    echo   已清理 backend\dist
)
if exist "%PROJECT_DIR%backend\build" (
    rmdir /s /q "%PROJECT_DIR%backend\build"
    echo   已清理 backend\build
)
if exist "%PROJECT_DIR%dist_output" (
    rmdir /s /q "%PROJECT_DIR%dist_output"
    echo   已清理 dist_output
)
if exist "%PROJECT_DIR%dist" (
    rmdir /s /q "%PROJECT_DIR%dist"
    echo   已清理 dist
)

:: 打包后端
cd /d "%PROJECT_DIR%backend"
python -m PyInstaller app.spec --noconfirm
if %ERRORLEVEL% neq 0 (
    echo [ERROR] PyInstaller 打包后端失败！
    cd /d "%PROJECT_DIR%"
    pause
    exit /b 1
)

:: 验证产物
if not exist "%PROJECT_DIR%backend\dist\app\app.exe" (
    echo [ERROR] 打包后端失败 - 未找到 backend\dist\app\app.exe
    cd /d "%PROJECT_DIR%"
    pause
    exit /b 1
)

cd /d "%PROJECT_DIR%"
echo [OK] 后端打包完成: backend\dist\app\app.exe
echo.

:: ---- Step 4/5: 验证构建资源 ----
echo [Step 4/5] 验证构建资源...

if not exist "%PROJECT_DIR%build\icon.ico" (
    echo [WARN] 未找到 build\icon.ico，安装包将使用默认图标。
)

if not exist "%PROJECT_DIR%build\installer.nsh" (
    echo [WARN] 未找到 build\installer.nsh，NSIS 自定义安装脚本缺失。
)

if not exist "%PROJECT_DIR%podman\podman.exe" (
    echo [WARN] 未找到 podman\podman.exe，深度集成需要内置 Podman 二进制。
) else (
    echo [OK] 内置 Podman 二进制已就绪: podman\podman.exe
)

echo [OK] 资源检查完成。
echo.

:: ---- Step 5/5: 使用 electron-builder 打包安装包 ----
echo [Step 5/5] 使用 electron-builder 构建 Windows 安装包...
echo   (插件目录 plugins\、R 库目录 r_libs*\、输出目录 outputs\ 已在 package.json 中排除)
echo   用户需安装后自行导入所需插件和R包。
echo.

:: 杀掉可能锁定 dist 目录的进程
taskkill /F /IM "PrimiGenius.exe" >nul 2>&1
taskkill /F /IM "electron.exe" >nul 2>&1
taskkill /F /IM "app.exe" >nul 2>&1
taskkill /F /IM "win-sshproxy.exe" >nul 2>&1
timeout /t 3 /nobreak >nul

:: 清理旧的 dist_output 和 dist 目录
if exist "%PROJECT_DIR%dist_output" (
    echo   正在清理旧的 dist_output 目录...
    rmdir /s /q "%PROJECT_DIR%dist_output" 2>nul
    if exist "%PROJECT_DIR%dist_output" (
        echo   [WARN] 无法完全删除 dist_output 目录，尝试重命名...
        ren "%PROJECT_DIR%dist_output" "dist_output_old_%RANDOM%" 2>nul
    )
    timeout /t 2 /nobreak >nul
)
if exist "%PROJECT_DIR%dist" (
    echo   正在清理旧的 dist 目录...
    rmdir /s /q "%PROJECT_DIR%dist" 2>nul
    if exist "%PROJECT_DIR%dist" (
        echo   [WARN] 无法完全删除 dist 目录，尝试重命名...
        ren "%PROJECT_DIR%dist" "dist_old_%RANDOM%" 2>nul
    )
    timeout /t 2 /nobreak >nul
)

:: 尝试打包，最多重试3次
set DIST_RETRY=0
:DIST_RETRY_LOOP
if %DIST_RETRY% geq 3 (
    echo [ERROR] electron-builder 打包失败（已重试3次）！
    pause
    exit /b 1
)
if %DIST_RETRY% gtr 0 (
    echo   [RETRY] 第 %DIST_RETRY% 次重试...
    timeout /t 5 /nobreak >nul
)
call npm run dist
if %ERRORLEVEL% equ 0 goto DIST_SUCCESS
echo   [WARN] electron-builder 打包失败，正在清理输出目录并重试...
if exist "%PROJECT_DIR%dist_output" (
    rmdir /s /q "%PROJECT_DIR%dist_output" 2>nul
    if exist "%PROJECT_DIR%dist_output" ren "%PROJECT_DIR%dist_output" "dist_output_old_%RANDOM%" 2>nul
    timeout /t 3 /nobreak >nul
)
if exist "%PROJECT_DIR%dist" (
    rmdir /s /q "%PROJECT_DIR%dist" 2>nul
    if exist "%PROJECT_DIR%dist" ren "%PROJECT_DIR%dist" "dist_old_%RANDOM%" 2>nul
    timeout /t 3 /nobreak >nul
)
set /a DIST_RETRY=%DIST_RETRY%+1
goto DIST_RETRY_LOOP

:DIST_SUCCESS

:: ---- 完成 ----
echo.
echo ============================================================
echo   打包完成！
echo ============================================================
echo.

if exist "%PROJECT_DIR%dist_output\*.exe" (
    echo   安装包位于: %PROJECT_DIR%dist_output\
    echo.
    for %%f in ("%PROJECT_DIR%dist_output\*.exe") do (
        echo   - %%~nxf  ^(%%~zf bytes^)
    )
) else if exist "%PROJECT_DIR%dist\*.exe" (
    echo   安装包位于: %PROJECT_DIR%dist\
    echo.
    for %%f in ("%PROJECT_DIR%dist\*.exe") do (
        echo   - %%~nxf  ^(%%~zf bytes^)
    )
) else (
    echo   [WARN] 未在 dist_output\ 或 dist\ 目录找到 .exe 安装包，请检查构建日志。
)

echo.
echo   注意事项:
echo   - 插件（plugins\）未包含在安装包中，用户需通过"安装插件"功能自行导入
echo   - R 库（r_libs_*\）未包含在安装包中，用户首次使用 R 工具时会自动安装
echo   - 镜像未包含在安装包中，用户运行插件时会自动拉取
echo   - 内置 Podman 二进制（podman\）会自动集成到安装程序中
echo   - 内置 JRE8（build\jre8.zip）会自动集成到安装程序中，Java 插件无需联网即可运行
echo   - 首次启动时会自动更新 WSL 并创建 Podman Machine
echo.
pause
