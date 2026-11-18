"""Run the API locally: `uv run --env-file .env python -m anemos.api [--port N]`.

Needs ANEMOS_OWNER_PASSWORD (D82). The host stays fixed to 127.0.0.1 until week 11's
compose (D83): nothing needs the API on another machine before then.
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
