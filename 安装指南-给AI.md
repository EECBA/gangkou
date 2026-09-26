# 港口 · 安装指南（给 AI 编程助手）

> 你（AI 助手）的任务：在这台 Windows 电脑上把"港口"跑起来，让用户双击 exe 就能见到她。
> 全程只需要装环境和启动，**不需要写代码**。遇到和本指南不符的情况，先排查再变通。

## 你要达成的最终状态

双击 `港口.exe` → 弹出一个窗口（她），地址栏是 `http://127.0.0.1:8000`。
用户在 设置→大脑 填入自己的 DeepSeek API Key 后，聊天能出字。

## 步骤

1. **确认 Python**：`python --version` 需要 3.10+。没有就装：
   `winget install -e --id Python.Python.3.12`（或官网安装，勾 Add to PATH）。
   装完重开终端让 PATH 生效。
2. **进入项目目录**（本文件所在的文件夹，下称"港口目录"）。
3. **建虚拟环境**：`python -m venv .venv`
4. **装依赖**：`.venv\Scripts\python.exe -m pip install -r requirements.txt`
   （国内网络慢可加清华镜像：`-i https://pypi.tuna.tsinghua.edu.cn/simple`）
5. **启动验证**：`.venv\Scripts\python.exe -m uvicorn server:app --port 8000`
   - 打开 http://127.0.0.1:8000 能看到界面 = 成功，Ctrl+C 停掉。
   - 报错缺文件/缺库按提示补。端口被占用就先找到占用者（可能是已启动的她，复用即可）。
6. **交给用户**：告诉用户"双击 港口.exe 就行，第一次进设置页填 API Key"。

## 常见坑（按命中概率排序）

- **PATH 里没有 python**：winget 装完不重开终端就是没有。重开。
- **防火墙拦 8000**：本机访问不受影响；手机局域网访问才需要放行（右键管理员运行 `放行8000端口.bat`）。
- **杀毒误报 exe**：PyInstaller onefile 的常见误报，让用户加白。
- **首次聊天卡住**：第一次对话她要下载 400MB 本地记忆模型（bge-small-zh，进 `data/embed_cache/`），
  需要联网+耐心；下载失败她会照常聊天，只是记忆检索降级。
- **想改壳的源码（port_app.py）**：exe 之外跑壳需要额外 `.venv\Scripts\pip install pywebview pystray`
  （exe 里已打包，不装也能用 exe）。
- **她是空房子**：用户要自己写 `data/soul/SOUL.md`（人设/名字）和在设置页填 Key，这步 AI 别代劳
  ——Key 是用户的隐私，人设是用户的主权。

## 不要做的事

- 不要动 `data/` 里任何文件的内容（那是她的身体）
- 不要把用户的 config.json / data 上传到任何地方（里面有 Key 和私人记忆）
- 不要替用户"测试性格"或改她的人设
