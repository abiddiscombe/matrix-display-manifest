import gc
import sys
import time
import asyncio
import network
import requests
import secrets
from interstate75 import Interstate75, DISPLAY_INTERSTATE75_64X64
from secrets import WIFI_SSID, WIFI_PASS, MANIFEST_URL

# RP2350 Interstate 75 Code for
# rendering a Manifest JSON document
# ---
# [ 2026 ] https://github.com/abiddiscombe

# ----------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------

# WIFI_SSID, WIFI_PASS and MANIFEST_URL are required in secrets.py (no sensible
# default exists, so a missing key fails loudly at import).
# The values below are optional in secrets.py and fall back if absent.

NETWORK_TIMEOUT = getattr(secrets, "NETWORK_TIMEOUT", 15)  # Seconds to wait for Wi-Fi
REQUEST_TIMEOUT = getattr(secrets, "REQUEST_TIMEOUT", 10)  # Seconds per HTTP request
RETRY_DELAY = getattr(secrets, "RETRY_DELAY", 10)  # Seconds between retries

# Manifest fallbacks and limits (milliseconds).
# Out-of-range values are clamped so a typo cannot park the device for hours.
DEFAULT_DURATION_MS = 5000
MIN_DURATION_MS = 250
MAX_DURATION_MS = 10 * 60 * 1000

DEFAULT_CACHE_MS = 60000
MIN_CACHE_MS = 5000
MAX_CACHE_MS = 24 * 60 * 60 * 1000

DEFAULT_COLOR = (255, 255, 255)
DEFAULT_BACKGROUND = (0, 0, 0)

wlan = network.WLAN(network.STA_IF)

i75 = Interstate75(display=DISPLAY_INTERSTATE75_64X64)
i75_display = i75.display

manifest_cache = None


async def network_init():
    # Async so a (re)connect does not stall the renderer.
    if wlan.isconnected():
        return

    wlan.active(True)
    wlan.connect(WIFI_SSID, WIFI_PASS)

    for _ in range(NETWORK_TIMEOUT):
        status = wlan.status()
        if status < 0 or status >= 3:
            break
        await asyncio.sleep(1)

    status = wlan.status()

    if status != 3:
        raise Exception(f"[ Network ] Connection Failure (status {status})")


# ----------------------------------------------------------------
# MANIFEST PARSING
# ----------------------------------------------------------------


def clamp(value, low, high):
    return max(low, min(value, high))


def int_or_default(value, default):
    # None, wrong types, non-numeric strings and inf/nan all fall back.
    try:
        return default if value is None else int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def parse_scale(value):
    # Optional. Missing/null/malformed falls back to 1.
    # Cells and bitmap fonts only scale in whole steps.
    if value is None:
        return 1

    try:
        return max(1, round(value))
    except (TypeError, ValueError, OverflowError):
        return 1


def parse_color(value, default=DEFAULT_COLOR):
    # "#rrggbb" or "#rrggbbaa" -> (r, g, b). Alpha is discarded.
    # Missing/null/malformed falls back to the given default (white).
    if value is None:
        return default

    try:
        if not isinstance(value, str) or len(value) not in (7, 9) or value[0] != "#":
            raise ValueError("bad format")

        return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))
    except ValueError:
        print(f"[ Manifest ] Invalid color {value}. Using default.")
        return default


def clamp_origin(value, limit):
    # Missing, malformed or out-of-range origins fall back to the top-left edge (0), per axis.
    value = int_or_default(value, 0)
    return value if 0 <= value < limit else 0


def clip_extent(size, origin, limit):
    # Trim a width/height so the shape stops at the display edge.
    return max(0, min(int(size), limit - origin))


def parse_block(d, x, y, disp_w, disp_h, default_rgb):
    # Expands a flat array of palette indices (0 = empty) into a list of clipped (x, y, w, h, rgb) rectangles.
    #
    # "width" is the wrap point in cells and is independent of the display edge:
    # cells past the right edge are clipped, never wrapped onto the next row.
    # Consecutive cells with the same index in a row are merged into one rectangle to keep
    # the number of draw calls (and tuples held in memory) down.
    content = d["content"]
    cols = int(d["width"])
    scale = parse_scale(d.get("scale"))
    palette = d.get("palette")

    if not isinstance(content, list):
        raise ValueError("content must be an array")

    if cols < 1:
        raise ValueError("width must be at least 1")

    if not isinstance(palette, dict):
        palette = {}

    colors = {key: parse_color(value) for key, value in palette.items()}
    rects = []

    for row_i, row_start in enumerate(range(0, len(content), cols)):
        row = [int_or_default(cell, 0) for cell in content[row_start:row_start + cols]]  # Final row may be short
        py = y + row_i * scale

        if py >= disp_h:
            break  # Every later row sits further down, so is also off-screen

        h = clip_extent(scale, py, disp_h)
        col = 0

        while col < len(row):
            cell = row[col]

            if cell < 1:
                col += 1
                continue

            run_start = col
            while col < len(row) and row[col] == cell:
                col += 1

            px = x + run_start * scale

            if px >= disp_w:
                break  # Remaining runs on this row sit further right

            w = clip_extent((col - run_start) * scale, px, disp_w)
            rects.append((px, py, w, h, colors.get(str(cell), default_rgb)))

    return rects


def parse_drawing(d, disp_w, disp_h):
    kind = d.get("type")
    x = clamp_origin(d.get("posX"), disp_w)
    y = clamp_origin(d.get("posY"), disp_h)
    rgb = parse_color(d.get("color"))

    if kind == "rect":
        return {
            "type": "rect",
            "x": x,
            "y": y,
            "w": clip_extent(d["width"], x, disp_w),
            "h": clip_extent(d["height"], y, disp_h),
            "rgb": rgb,
        }

    if kind == "block":
        return {
            "type": "block",
            "rects": parse_block(d, x, y, disp_w, disp_h, rgb),
        }

    if kind == "text":
        if not isinstance(d["prose"], str):
            raise ValueError("prose must be a string")

        return {
            "type": "text",
            "x": x,
            "y": y,
            "scale": parse_scale(d.get("scale")),
            "prose": d["prose"],
            "rgb": rgb,
        }

    raise ValueError(f"unknown drawing type {kind}")


def parse_slide(slide, slide_i, disp_w, disp_h):
    # Returns None when a slide had drawings but none were valid, so a broken
    # slide is dropped instead of being shown as a black frame.
    raw_drawings = slide.get("drawings")

    if not isinstance(raw_drawings, list):
        raw_drawings = []  # Missing/malformed is treated as a deliberately blank slide

    drawings = []

    for draw_i, d in enumerate(raw_drawings):
        try:
            drawings.append(parse_drawing(d, disp_w, disp_h))
        except Exception as e:
            print(f"[ Manifest ] Skipping slide {slide_i + 1}, drawing {draw_i + 1}: {type(e).__name__}: {e}")

    if raw_drawings and not drawings:
        print(f"[ Manifest ] Dropping slide {slide_i + 1}: no valid drawings")
        return None

    duration = int_or_default(slide.get("duration"), DEFAULT_DURATION_MS)

    return {
        "duration": clamp(duration, MIN_DURATION_MS, MAX_DURATION_MS),
        "drawings": drawings,
    }


def parse_manifest(raw):
    if not isinstance(raw, dict):
        raise ValueError("root must be an object")

    raw_slides = raw.get("slides")

    if not isinstance(raw_slides, list):
        raise ValueError("slides must be an array")

    meta = raw.get("meta")

    if not isinstance(meta, dict):
        meta = {}  # Everything read from meta has a fallback

    disp_w, disp_h = i75_display.get_bounds()

    canvas = (
        int_or_default(meta.get("width"), disp_w),
        int_or_default(meta.get("height"), disp_h),
    )

    if canvas != (disp_w, disp_h):
        print(f"[ Manifest ] Canvas size differs from display ({disp_w}x{disp_h}). Clipping to display.")

    slides = []

    for slide_i, raw_slide in enumerate(raw_slides):
        try:
            slide = parse_slide(raw_slide, slide_i, disp_w, disp_h)
        except Exception as e:
            print(f"[ Manifest ] Skipping slide {slide_i + 1}: {type(e).__name__}: {e}")
            continue

        if slide is not None:
            slides.append(slide)

    if not slides:
        raise ValueError("no playable slides")

    cache_ms = int_or_default(meta.get("cacheLifespan"), DEFAULT_CACHE_MS)
    background = parse_color(meta.get("background"), DEFAULT_BACKGROUND)

    return {
        "background": background,
        "cache_ms": clamp(cache_ms, MIN_CACHE_MS, MAX_CACHE_MS),
        "slides": slides,
    }


# ----------------------------------------------------------------
# RENDERING
# ----------------------------------------------------------------


def render_slide(slide, background):
    i75_display.set_pen(i75_display.create_pen(*background))
    i75_display.clear()

    # Array order is paint order: later drawings sit on top of earlier ones.
    for d in slide["drawings"]:
        kind = d["type"]

        if kind == "rect":
            i75_display.set_pen(i75_display.create_pen(*d["rgb"]))
            i75_display.rectangle(d["x"], d["y"], d["w"], d["h"])
        elif kind == "block":
            # Unset cells ("0") are never drawn, so whatever is beneath shows through.
            for rx, ry, rw, rh, rgb in d["rects"]:
                i75_display.set_pen(i75_display.create_pen(*rgb))
                i75_display.rectangle(rx, ry, rw, rh)
        elif kind == "text":
            i75_display.set_pen(i75_display.create_pen(*d["rgb"]))
            i75_display.text(d["prose"], d["x"], d["y"], -1, d["scale"])  # -1 = no word wrap

    i75.update()


# ----------------------------------------------------------------
# TASKS
# ----------------------------------------------------------------


def fetch_manifest():
    gc.collect()  # Free the previous response before allocating for the next

    res = requests.get(MANIFEST_URL, timeout=REQUEST_TIMEOUT)

    try:
        if res.status_code != 200:
            raise Exception(f"[ Network ] Response Error (HTTP {res.status_code})")

        raw = res.json()
    finally:
        res.close()

    return parse_manifest(raw)


async def main_updater():
    global manifest_cache

    while True:
        delay_ms = RETRY_DELAY * 1000

        try:
            await network_init()

            print("[ Network ] Fetching Manifest...")
            parsed = fetch_manifest()

            manifest_cache = parsed
            delay_ms = parsed["cache_ms"]
            i75.set_led(0, 0, 0)

            print(f"[ Network ] Updated. Sleeping for {delay_ms} ms.")

        except Exception as e:
            # Keep rendering the last good manifest (if any) and retry sooner
            # than the cache lifespan. The LED stays red while content is stale.
            i75.set_led(255, 0, 0)
            print(f"[ Network ] Update failed: {type(e).__name__}: {e}. Retrying in {RETRY_DELAY} s.")

        gc.collect()
        await asyncio.sleep_ms(delay_ms)


async def main_renderer():
    while True:
        manifest_snapshot = manifest_cache

        if manifest_snapshot is None:
            print("[ Display ] Nothing to render. Skipping...")
            await asyncio.sleep(1)
            continue

        slide_count = len(manifest_snapshot["slides"])

        for slide_i, slide in enumerate(manifest_snapshot["slides"]):
            print(f"[ Display ] Rendering: {slide_i + 1} of {slide_count}")

            try:
                render_slide(slide, manifest_snapshot["background"])
            except Exception as e:
                # Still wait out the duration so a failing slide cannot spin the loop.
                print(f"[ Display ] Slide {slide_i + 1} failed: {type(e).__name__}: {e}")

            await asyncio.sleep_ms(slide["duration"])


async def run_tasks():
    await asyncio.gather(main_updater(), main_renderer())


def signal_error():
    for _ in range(RETRY_DELAY):
        i75.set_led(255, 150, 0)
        time.sleep(0.25)
        i75.set_led(255, 0, 0)
        time.sleep(0.25)
        i75.set_led(0, 0, 0)
        time.sleep(0.5)


def main():
    i75_display.set_font("bitmap8")

    while True:
        try:
            asyncio.run(run_tasks())

        except Exception as e:
            # Last resort: both tasks handle their own errors, so reaching
            # here means something unexpected. manifest_cache survives the restart.
            sys.print_exception(e)
            signal_error()

        finally:
            # Drop any task left over from a failed gather() before retrying.
            asyncio.new_event_loop()


if __name__ == "__main__":
    main()
