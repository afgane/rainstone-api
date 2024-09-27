FROM python:3.12-slim

ARG APP_DIR=/rainstone-api

WORKDIR $APP_DIR

COPY requirements.txt $APP_DIR/requirements.txt

RUN pip install --no-cache-dir --upgrade -r $APP_DIR/requirements.txt

COPY ./app $APP_DIR/app
COPY ./static $APP_DIR/static

EXPOSE 8000

CMD ["uvicorn", "--app-dir", "app", "main:app", "--host", "0.0.0.0", "--port", "8000"]
