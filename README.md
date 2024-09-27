This is the backend API for the Rainstone app - an estimator of cloud costs for
bioinformatics tools and Galaxy workflows. See the [rainstone-ui
repo](https://github.com/afgane/rainstone-ui/) for the web UI.

## Run in dev mode

Clone this repo install requirements

```sh
conda create -n rainstone python=3.12
conda activate rainstone
cd rainstone-api
pip install -r requirements.txt
```

Start the backend server with

```sh
uvicorn --app-dir app main:app --reload
```

The API will be available at http://127.0.0.1:8000, with the API docs at
`/docs`.

## Run via Docker

If building a new image, use the following command:

```sh
docker build -t afgane/rainstone-api . --platform linux/amd64
```

If the frontend is running on a different host or port than localhost:8000,
change the `ALLOWED_ORIGINS` environment variable accordingly in the below
command and run the container with:

```sh
docker run --rm --publish 8000:8000 -e ALLOWED_ORIGINS='http://localhost:8080' afgane/rainstone-api:latest
```

The backend will be available at http://localhost:8000, with the API docs at
`/docs`.

## Deploy on Kubernetes

See the [rainstone-helm repo](https://github.com/afgane/rainstone-helm).
