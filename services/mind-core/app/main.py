from fastapi import FastAPI

app = FastAPI(
    title="M.I.N.D.",
    description="Multimodal Intelligence & Neural Diagnostics",
    version="0.1.0",
)


@app.get("/")
def root():
    return {
        "system": "M.I.N.D.",
        "call_name": "WEB",
        "status": "online",
    }


@app.get("/health")
def health():
    return {
        "system": "M.I.N.D.",
        "call_name": "WEB",
        "status": "online",
    }