from pydantic import BaseModel


class User(BaseModel, frozen=True):
    id: str
    manager: str | None = None
