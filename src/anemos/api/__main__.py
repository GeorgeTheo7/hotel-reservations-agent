"""Run the API locally: `uv run --env-file .env python -m anemos.api [--port N]`.

The host is fixed to 127.0.0.1 on purpose: until the week-7 password exists, every
request is treated as the owner (D19), so the API must not be reachable from other
machines.
"""

import argparse

import uvicorn

HOST = "127.0.0.1"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m anemos.api")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    uvicorn.run("anemos.api.app:production_app", factory=True, host=HOST, port=args.port)


if __name__ == "__main__":
    main()
