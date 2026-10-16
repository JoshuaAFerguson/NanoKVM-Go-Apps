#!/usr/bin/env python3
"""Smoke-test App: proves this repo installs and runs from the Store."""

from appbase import AppContext, app

import ui


@app()
def main(ctx: AppContext) -> None:
    screen = ui.screen(ctx)
    version = main.__app_info__.version
    state = {"taps": 0, "dirty": True}

    def on_tap(x: int, y: int) -> None:
        state["taps"] += 1
        state["dirty"] = True

    def tick(dt: float) -> None:
        if not state["dirty"]:
            return
        fb = ctx.fb
        fb.clear(ui.BG)
        top, rest = screen.split_top(screen.h // 2)
        mid, bottom = rest.split_top(40)
        ui.draw_text_center(fb, top.inset(0, 20), "Hello", ui.ACCENT, 4)
        ui.draw_text_center(fb, mid, "NanoKVM", ui.TEXT, 2)
        ui.draw_text_center(fb, bottom.split_top(30)[0], f"taps: {state['taps']}", ui.TEXT_DIM, 2)
        ui.draw_text_center(fb, bottom.split_bottom(16)[1], f"v{version}", ui.TEXT_DIM, 1)
        state["dirty"] = False

    ctx.run(tick, fps=10, on_tap=on_tap)


if __name__ == "__main__":
    main()
