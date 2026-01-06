# Life OS v1

Life OS v1 with FastAPI + SQLModel + Jinja2 + HTMX.

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

For schema changes, create a new Alembic revision and apply it with `python manage.py migrate`.

## App Info

Set `LIFEOS_VERSION` and `LIFEOS_DEVELOPER_ID` to override the version and developer info.

## Delight Choices

- D1 计划-日志自动对齐：成本低、直接提升日志补记效率。
- D3 过载预警：快速发现过量计划与低完成率风险。

