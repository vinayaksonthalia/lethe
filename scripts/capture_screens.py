"""Capture polished app screenshots for the README (dark theme, retina). Helper script — keep it,
re-run any time the UI changes:  ./.venv/bin/python capture_screens.py
Requires the app running on :8077 (local mode) and playwright chromium installed.
"""
import asyncio, os
from playwright.async_api import async_playwright

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "screenshots")
os.makedirs(OUT, exist_ok=True)
BASE = "http://127.0.0.1:8077"


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": 1440, "height": 900},
                                  device_scale_factor=2, color_scheme="dark")
        pg = await ctx.new_page()

        # 1) Landing hero (top fold)
        await pg.goto(BASE, wait_until="networkidle")
        await pg.wait_for_timeout(1200)
        await pg.screenshot(path=f"{OUT}/landing.png", clip={"x": 0, "y": 0, "width": 1440, "height": 900})
        print("✓ landing.png")

        # dashboard
        await pg.goto(f"{BASE}/app", wait_until="networkidle")
        await pg.wait_for_function("typeof show === 'function'")
        await pg.wait_for_timeout(800)

        # 2) Triage answer (the hero question)
        try:
            await pg.evaluate("ask('If auth-service latency is high, what should I check?')")
            await pg.wait_for_function(
                "(document.getElementById('log')||{}).innerText && document.getElementById('log').innerText.toLowerCase().includes('legacy-cache')",
                timeout=60000)
            await pg.wait_for_timeout(800)
            await pg.screenshot(path=f"{OUT}/triage.png", clip={"x": 0, "y": 0, "width": 1440, "height": 900})
            print("✓ triage.png")
        except Exception as e:
            print("✗ triage:", repr(e)[:120])

        # 3) Knowledge graph + node detail panel
        try:
            await pg.evaluate("show('graph')")
            await pg.wait_for_function("window.gnet && window.gnet.body && window.gnet.body.data.nodes.length>0", timeout=30000)
            await pg.wait_for_timeout(2500)  # let it stabilize + fit
            await pg.evaluate("""(() => {
                let id=null; window.gnet.body.data.nodes.forEach(n=>{const l=(n.label||'').toLowerCase(); if(l==='legacy-cache'||(l.includes('legacy')&&l.includes('cache')))id=n.id;});
                if(id){window.gpinned=id; (window.gFocusHops||window.gFocus)(id,2); if(window.gPanelShow)gPanelShow(id); window.gnet.focus(id,{scale:1.1,animation:false});} })()""")
            await pg.wait_for_timeout(1100)
            await pg.screenshot(path=f"{OUT}/graph.png", clip={"x": 0, "y": 0, "width": 1440, "height": 900})
            print("✓ graph.png")
        except Exception as e:
            print("✗ graph:", repr(e)[:120])

        # 4) Curation (auto-runs the free scans on open)
        try:
            await pg.evaluate("show('curation')")
            await pg.wait_for_timeout(2500)
            await pg.screenshot(path=f"{OUT}/curation.png", clip={"x": 0, "y": 0, "width": 1440, "height": 900})
            print("✓ curation.png")
        except Exception as e:
            print("✗ curation:", repr(e)[:120])

        # 5) Timeline
        try:
            await pg.evaluate("show('timeline')")
            await pg.wait_for_timeout(1200)
            await pg.screenshot(path=f"{OUT}/timeline.png", clip={"x": 0, "y": 0, "width": 1440, "height": 900})
            print("✓ timeline.png")
        except Exception as e:
            print("✗ timeline:", repr(e)[:120])

        await b.close()


asyncio.run(main())
