"""Minimal identical apps used by docs and smoke tests."""

from fasterapi import FastAPI

app = FastAPI(title="fasterapi demo")


@app.get("/ping")
async def ping():
    return {"pong": True}
