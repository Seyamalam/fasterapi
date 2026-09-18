"""Drop-in migration demo — change one import, keep everything else."""

# from fastapi import FastAPI, Depends, HTTPException, Query  # before
from fasterapi import Depends, FastAPI, HTTPException, Query  # after

from pydantic import BaseModel

app = FastAPI(title="quickstart")


class Item(BaseModel):
    name: str
    price: float


async def common(q: str | None = Query(default=None)):
    return {"q": q}


@app.get("/items/{item_id}")
async def read_item(item_id: int, common: dict = Depends(common)):
    if item_id == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return {"item_id": item_id, **common}


@app.post("/items")
async def create_item(item: Item) -> Item:
    return item


if __name__ == "__main__":
    app.build()  # AOT-compile Depends DAG + Rust radix router
    app.serve(port=8000)
