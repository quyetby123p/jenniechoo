from __future__ import annotations

from app.settings import load_settings
from app.web_report_app import create_app


settings = load_settings()
app = create_app(settings=settings)


def main() -> int:
    app.run(host=settings.web_report_host, port=settings.web_report_port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
