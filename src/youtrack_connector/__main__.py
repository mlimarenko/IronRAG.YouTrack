from __future__ import annotations

import uvicorn
from ironrag_connector import build_app

from .adapter import YouTrackAdapter
from .config import YouTrackSettings


def main() -> None:
    settings = YouTrackSettings()  # type: ignore[call-arg]
    adapter = YouTrackAdapter(settings)
    app = build_app(settings, adapter)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    main()
