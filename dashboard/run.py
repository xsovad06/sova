#!/usr/bin/env python3
"""Morning Agent Dashboard — entry point."""

import argparse
import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Morning Agent Dashboard")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Path to .claude/ directory (default: auto-detect from cwd)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8111)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    import os
    if args.data_dir:
        os.environ["AGENT_DATA_DIR"] = args.data_dir

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
