# JW-DSAR

JW-DSAR（金乌每日太阳活动报告智能体）用于聚合 NOAA/SWPC 数据并生成中文日报（HTML/PDF）。  
项目拆分为两部分：

- **JW-DSAR 主体**：数据下载、处理、报告生成、Web 界面
- **JW-Flare 推理服务**：通过 OpenAI 兼容 HTTP 接口提供耀斑预测

两者可同机或跨机部署。

## 1. 快速开始

```bash
conda create -n jw-dsar python=3.10 -y
conda activate jw-dsar
pip install -U pip
pip install -r requirements_scheduled.txt
# 如需 JW-Flare 预报链路：
# pip install -r requirements_jwflare.txt
```

已验证可用的 Gradio 版本：**6.12.0**。

复制配置模板：

```bash
cp .env.example .env
```

## 1.1 通过 GitHub 一键获取（含数据 + Git LFS）

由于本项目会包含 `data/` 下的压缩包（建议使用 Git LFS 存储），首次克隆后请执行：

```bash
git clone <your_repo_url>
cd JW-DSAR
git lfs install
git lfs pull
cp .env.example .env
```

## 2. 核心配置（.env）

```env
DASHSCOPE_API_KEY_FILE=./secrets/dashscope_api_key.txt
DASHSCOPE_VL_MODEL=qwen3.6-plus

JWDSAR_DISABLE_QWEN=0
JWDSAR_PERSIST_LATEST=1
JWDSAR_STRICT_LATEST_LOCAL=1
JWDSAR_STRICT_BROWSE_LOCAL=1
JWDSAR_ENABLE_MATHJAX_CDN=0

JWFLARE_ENABLED=1
JWFLARE_DATA_ROOT=./data
JWFLARE_API_BASE=http://127.0.0.1:2222/v1
JWFLARE_MODEL=qwen2-vl-7b-instruct
JWFLARE_MAX_REGIONS=3
JWFLARE_TRANSPORT=upload
JWFLARE_UPLOAD_FORMAT=base64
JWFLARE_ALLOW_PATH_FALLBACK=0
```

### JW-Flare 传输模式说明

- `JWFLARE_TRANSPORT=upload`：默认推荐，跨机部署可用
- `JWFLARE_TRANSPORT=path`：兼容旧服务，要求服务端可读同一路径
- `JWFLARE_TRANSPORT=auto`：先 `upload`，失败后按 `JWFLARE_ALLOW_PATH_FALLBACK` 决定是否回退 `path`

## 3. 一行启动

### JW-DSAR 主体

- Linux（长期运行，推荐 systemd）：
  ```bash
  bash scripts/install_systemd_units.sh
  ```
- Linux（Web）：
  ```bash
  python app_scheduled.py
  ```
- Linux（单次日报）：
  ```bash
  python app_scheduled.py --generate-once
  ```
- Windows（Web）：
  ```bat
  scripts\start_jwdsar_windows.bat
  ```
- Windows（单次日报）：
  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts/run_daily_report_windows.ps1
  ```
- Windows（长期运行，计划任务，一键安装）：
  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts/install_windows_tasks.ps1
  ```

### JW-Flare 服务（一行启动，仅 Linux 服务器）

- Linux：
  ```bash
  # 默认会尝试用 conda run -n qwen 执行（可用 JWFLARE_CONDA_ENV 覆盖）
  # 下面给出一个你当前环境的 ckpt_dir 示例（按实际替换）：
  # /data/shaomf/JW-Flare/output/qwen2-vl-7b-instruct/v43-20241025-184811/checkpoint-180-merged
  JWFLARE_CKPT_DIR=/path/to/checkpoint bash scripts/start_jwflare.sh
  ```

## 4. systemd（Linux 生产）

使用仓库内模板 + 自动渲染安装：

```bash
bash scripts/install_systemd_units.sh
```

可选覆盖变量：

- `JWDSAR_PYTHON_BIN`
- `JWDSAR_SERVICE_USER`
- `JWDSAR_SERVICE_GROUP`
- `JWDSAR_SECRETS_DIR`

## 5. 数据目录约定

- 日级目录：`data/YYYY-MM-DD/`
- 年归档目录：`data/SRS/`、`data/events/`
- 报告目录：默认 `reports/`（可通过 `JWDSAR_REPORTS_DIR` 或 `--output-dir` 指定）

## 6. 常用命令

```bash
# 生成指定日期
python app_scheduled.py --generate-once --date 2026-04-02

# 输出到测试目录
python app_scheduled.py --generate-once --date 2026-04-02 --output-dir test/reports
```

## 7. 关键文件

- `app_scheduled.py`：主入口
- `jwflare_pipeline.py`：JW-Flare 预报编排
- `jwflare_client.py`：JW-Flare HTTP 客户端（upload/path/auto）
- `deploy/systemd/`：systemd 模板
- `scripts/`：一键启动与运维脚本
- `部署指南.md`：完整部署与排障

## 8. 生产部署与排障

详见 [部署指南.md](部署指南.md)。
