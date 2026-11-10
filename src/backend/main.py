from fastapi import FastAPI

app = FastAPI(title='월급지도 API', version='0.1.0')


@app.get('/health')
def health() -> dict[str, str]:
    return {'status': 'ok'}
