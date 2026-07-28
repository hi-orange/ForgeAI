from fastapi import FastAPI

app = FastAPI(title="ForgeAI", version="0.1.0")


@app.get("/health", tags=["system"])
def health_check() -> dict[str, str]:
    return {"status": "ok"}
