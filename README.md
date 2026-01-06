# Life OS v1

Local-first Life OS v1 with FastAPI + SQLModel + Jinja2 + HTMX.

## Install

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

## Database

The app auto-initializes the SQLite database on startup. You can also run:

```bash
python manage.py migrate
```

Set `LIFEOS_DATABASE_URL` to override the database location (default: `sqlite:///lifeos.db`).

## Delight Choices

- D1 计划-日志自动对齐：成本低、直接提升日志补记效率。
- D3 过载预警：快速发现过量计划与低完成率风险。

## Demo Data

首次启动会自动写入 5 组 mock 数据（目标、习惯、计划、日志、建议）。
月度复盘示例：进入 `Reviews`，选择当前年月并点击“生成”即可查看。
