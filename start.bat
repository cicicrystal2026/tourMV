@echo off
REM 一键启动：后端 8000 + 前端 5173（各自独立窗口）
cd /d "%~dp0"

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo [info] 已从 .env.example 创建 .env（默认 FORCE_MOCK=1，可无 Key 跑通）
)

if not exist "backend\.venv\Scripts\python.exe" (
  echo [setup] 创建后端虚拟环境...
  python -m venv backend\.venv
  backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
)

if not exist "frontend\node_modules" (
  echo [setup] 安装前端依赖...
  pushd frontend && call npm install && popd
)

echo [init] 初始化数据库与示例数据...
pushd backend && .venv\Scripts\python.exe -m app.seed && popd

start "wenlv-backend" cmd /k "cd /d %~dp0backend && .venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000"
start "wenlv-frontend" cmd /k "cd /d %~dp0frontend && npm run dev"

echo.
echo 后端 http://127.0.0.1:8000/docs
echo 前端 http://localhost:5173
