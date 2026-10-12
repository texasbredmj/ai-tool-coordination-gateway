import argparse

import uvicorn

from .app import create_app, from_environment


def main():
    parser = argparse.ArgumentParser(description="Serve the authorized tool gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(create_app(from_environment()), host=args.host, port=args.port)
