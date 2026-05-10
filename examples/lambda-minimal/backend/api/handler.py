from fastapi import FastAPI
from mangum import Mangum

app = FastAPI()


@app.get("/hello")
def hello() -> dict[str, str]:
    return {"message": "hello from Lambda"}


handler = Mangum(app)
