#!/usr/bin/env python3
"""Starter App. Create a copy with: python3 _tools/new_app.py <kebab-name> "Display Name"."""

from pathlib import Path

from appbase import AppContext, app

import ui
from applog import AppLog

APP_DIR = Path(__file__).resolve().parent
LOG = AppLog(APP_DIR)


@app()
def main(ctx: AppContext) -> None:
    screen = ui.screen(ctx)  # 284x240, fully visible
    header, body = screen.split_top(28)
    state = {"dirty": True}

    def tick(dt: float) -> None:
        if not state["dirty"]:
            return  # text drawing is slow; redraw only when something changed
        ctx.fb.clear(ui.BG)
        ui.draw_header(ctx.fb, header, "My App")
        ui.draw_text_center(ctx.fb, body, "Hello", ui.TEXT, 3)
        state["dirty"] = False

    LOG.info("started")
    ctx.run(LOG.guard(tick), fps=10)


if __name__ == "__main__":
    main()
