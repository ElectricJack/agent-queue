"""Minimal interpreter, wheel and generated-client source dependency check."""

import sys

import pydantic
import sqlalchemy
from agent_queue_api_client import Client

assert sys.version_info[:3] == (3, 12, 12), sys.version
assert Client(base_url="http://127.0.0.1")._base_url == "http://127.0.0.1"
assert pydantic.TypeAdapter(list[int]).validate_python([1, 2]) == [1, 2]
assert str(sqlalchemy.select(sqlalchemy.literal(1))).startswith("SELECT")
print("Python 3.12.12 / locked wheels / generated client source: OK")
