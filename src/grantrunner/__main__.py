import argparse
import os


def main():
    parser = argparse.ArgumentParser(description="Run GrantRunner on the PC that will perform browser work")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--demo", action="store_true", help="Enable the local example grant portal")
    parser.add_argument("--headed", action="store_true", help="Show the worker browser (requires a desktop session)")
    parser.add_argument("--show-token", action="store_true", help="Print the dashboard login token and exit")
    args = parser.parse_args()
    os.environ["GRANTRUNNER_PORT"] = str(args.port)
    if args.demo:
        os.environ["GRANTRUNNER_DEMO"] = "1"
    if args.headed:
        os.environ["GRANTRUNNER_HEADLESS"] = "0"
    from .config import Settings
    settings = Settings.load()
    if args.show_token:
        print(settings.token)
        return
    print(f"GrantRunner data: {settings.data_dir}")
    print("Run `grantrunner --show-token` in another terminal to get your dashboard login token.")
    import uvicorn
    from .app import create_app
    uvicorn.run(create_app(settings), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
