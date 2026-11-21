"""Explicit desktop smoke modes; never exposed through the renderer bridge."""
from __future__ import annotations

import json
from pathlib import Path
import struct
import sys
import threading
import time
import zlib

from .native_desktop import evaluate_json, read_preferences


def png(width=16, height=12):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    rows = b"".join(b"\0" + bytes((23, 101, 211)) * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def wait_for(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.1)
    raise RuntimeError("smoke check timed out")


def javascript(window, expression, timeout=30):
    window.run_js("globalThis.__nativeSmokeValue=null;Promise.resolve((async()=>{" + expression +
                       "})()).then(value=>globalThis.__nativeSmokeValue={value},error=>globalThis.__nativeSmokeValue={error:String(error)});null")
    result = wait_for(lambda: evaluate_json(window, "globalThis.__nativeSmokeValue"), timeout)
    if "error" in result:
        raise RuntimeError(result["error"])
    return result["value"]


def write_report(path, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def application_smoke(app, output):
    """One real UI flow: start, select an image, change exposure, export PNG, exit."""
    output = Path(output)
    checks = {}
    report = {"platform": sys.platform, "checks": checks}
    try:
        if not app.ready.wait(20):
            raise RuntimeError("frontend did not load")
        window = app.window
        wait_for(lambda: evaluate_json(window, "!!document.querySelector('#app .topbar')"), timeout=15)
        checks["frontend"] = True
        checks["no_python_api"] = evaluate_json(window, "Object.keys(window.pywebview.api).length") == 0
        checks["private_token_not_in_page"] = app.backend._token not in evaluate_json(window, "document.documentElement.outerHTML+location.href+String(window.pywebview.token)")
        expected = read_preferences(app.preferences_path).get("wc-theme")
        checks["preferences_restored"] = evaluate_json(window, "localStorage.getItem('wc-theme')") == expected
        checks["sandbox_enabled"] = sys.platform != "linux" or all(context.get_sandbox_enabled() for context in app._contexts)
        fixture_dir = output.parent / "fixtures"
        fixture_dir.mkdir(parents=True, exist_ok=True)
        image = fixture_dir / "native-smoke.png"
        image.write_bytes(png(320, 240))
        downloads = output.parent / "downloads"
        downloads.mkdir(exist_ok=True)
        exported = downloads / "webcompare-page-1.png"
        exported.unlink(missing_ok=True)
        app._smoke_download_dir = downloads
        values = javascript(window, f"""
          async function until(test){{for(let n=0;n<100;n++){{const value=test();if(value)return value;await new Promise(r=>setTimeout(r,100));}}throw Error('UI condition timed out');}}
          const field=await until(()=>document.querySelector({json.dumps('input[aria-label="目录路径"]')}));
          field.value={json.dumps(str(fixture_dir))};field.dispatchEvent(new Event('input',{{bubbles:true}}));
          field.dispatchEvent(new KeyboardEvent('keydown',{{key:'Enter',bubbles:true}}));
          const file=await until(()=>document.querySelector({json.dumps('input[aria-label="选择 native-smoke.png"]')}));
          file.click();
          const compare=await until(()=>document.querySelector('.selection-actions button.primary:not(:disabled)'));
          compare.click();
          const canvas=await until(()=>document.querySelector('.view-compare #compare-canvas'));
          const top=document.querySelector('.view-compare .topbar');
          const topbarHeight=top.getBoundingClientRect().height;
          const topbarControls=top.firstElementChild.matches('.media-toggle') &&
            ['.view-toggle','.ssh-manage','.theme-toggle','.fullscreen-toggle','.guide-link']
              .every(selector=>top.querySelector(selector)?.getClientRects().length);
          const sample=()=>{{if(!canvas.width||!canvas.height)return null;return [...canvas.getContext('2d').getImageData(Math.floor(canvas.width/2),Math.floor(canvas.height/2),1,1).data];}};
          const before=await until(()=>{{const p=sample();return p&&p[0]===23&&p[1]===101&&p[2]===211?p:null;}});
          const exposure=document.querySelector('input[data-state-key="exposure"]');
          if(!exposure)throw Error('exposure control missing');
          exposure.value='1';exposure.dispatchEvent(new Event('input',{{bubbles:true}}));
          const after=await until(()=>{{const p=sample();return p&&p[0]>before[0]&&p[1]>before[1]?p:null;}});
          const save=[...document.querySelectorAll('button')].find(button=>button.textContent==={json.dumps('导出 PNG')});
          if(!save)throw Error('PNG export button missing');save.click();
          localStorage.setItem('wc-theme','dark');
          return {{image_displayed:true,exposure_changed:true,canvas_width:canvas.width,canvas_height:canvas.height,
            before,after,topbarHeight,topbarControls,topbarUnbranded:!top.querySelector('.brand')}};
        """, timeout=25)
        checks["image_displayed"] = values["image_displayed"]
        checks["exposure_changed"] = values["exposure_changed"]
        checks["desktop_topbar_unbranded"] = values["topbarUnbranded"]
        checks["desktop_topbar_controls"] = values["topbarControls"]
        checks["desktop_topbar_compact"] = 40 <= values["topbarHeight"] <= 44
        report["desktop_topbar_height_px"] = values["topbarHeight"]
        report["pixels"] = {"before": values["before"], "after": values["after"]}
        wait_for(lambda: exported.exists() and exported.read_bytes().endswith(b"IEND\xaeB`\x82"), timeout=10)
        data = exported.read_bytes()
        dimensions = struct.unpack(">II", data[16:24]) if data.startswith(b"\x89PNG\r\n\x1a\n") else (0, 0)
        checks["png_export"] = dimensions == (values["canvas_width"], values["canvas_height"])
        if sys.platform == "linux":
            from gi.repository import Gdk, GLib
            captured = threading.Event()
            def screenshot():
                try:
                    native = window.native
                    pixels = Gdk.pixbuf_get_from_window(native.get_window(), 0, 0,
                        native.get_allocated_width(), native.get_allocated_height())
                    pixels.savev(str(output.with_suffix(".png")), "png", [], [])
                    checks["window_screenshot"] = True
                except Exception as error:
                    report["screenshot_error"] = str(error)
                    checks["window_screenshot"] = False
                finally:
                    captured.set()
                return False
            GLib.idle_add(screenshot)
            if not captured.wait(5):
                checks["window_screenshot"] = False
        report["origin"] = app.backend.origin
        report["backend_pid"] = app.backend.process.pid
    except Exception as error:
        report["error"] = str(error).replace(app.backend._token, "[REDACTED]")
        try:
            report["page_text"] = evaluate_json(app.window, "document.body.innerText.slice(0,3000)").replace(app.backend._token, "[REDACTED]")
        except Exception:
            pass
    finally:
        report["passed"] = bool(checks) and all(checks.values()) and "error" not in report
        write_report(output, report)
        app.shutdown()
