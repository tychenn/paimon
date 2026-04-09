import sys
from pathlib import Path

from volition.app import ConfigError, TerminalApp


def main() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
    project_root = Path(__file__).resolve().parent
    try:
        app = TerminalApp(project_root)
    except ConfigError as exc:
        print(f"【配置错误】{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    app.run()


if __name__ == "__main__":
    main()
