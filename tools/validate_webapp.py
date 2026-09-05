#!/usr/bin/env python3
"""Playwright validation for ARS v5.0 against file:// (headless Chromium)."""
import base64, json, sys, time
from playwright.sync_api import sync_playwright

URL = "file:///mnt/agents/output/ars-repo/app/src/main/assets/www/index.html"
errors, failures = [], []

def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(f"{name}: {detail}")

with sync_playwright() as pw:
    browser = pw.chromium.launch(args=[
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
    ])
    page = browser.new_page(viewport={"width": 1200, "height": 900})
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(URL, wait_until="load")
    page.wait_for_timeout(800)

    # 1. no console/page errors on load
    check("no console errors on load", len(errors) == 0, "; ".join(errors[:5]))

    # 2. window.ARS exists with state + api
    has_ars = page.evaluate("!!(window.ARS && window.ARS.state && window.ARS.api)")
    check("window.ARS { state, api } exists", has_ars)

    # 3. all 6 tabs render and switch
    tabs = page.locator("nav#tabs button[data-tab]")
    check("6 tab buttons present", tabs.count() == 6, f"count={tabs.count()}")
    for tab in ["monitor", "beacon", "sensors", "rng", "journal", "tiers"]:
        page.click(f'nav#tabs button[data-tab="{tab}"]')
        page.wait_for_timeout(120)
        active = page.evaluate(
            f'document.getElementById("panel-{tab}").classList.contains("active")')
        check(f'tab {tab} activates its panel', active)
    page.click('nav#tabs button[data-tab="monitor"]')

    # 4. header clock ticking
    t1 = page.text_content("#hdr-utc")
    page.wait_for_timeout(1200)
    t2 = page.text_content("#hdr-utc")
    check("UTC clock updates", t1 != t2, f"{t1!r} -> {t2!r}")

    # 5. SHA-256 known vector + subtle/sync consistency
    sha = page.evaluate("window.ARS.api.sha256Sync('abc')")
    check("sha256Sync('abc') known vector",
          sha == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", sha)
    consistent = page.evaluate(
        "window.ARS.api.sha256('abc').then(h => h === window.ARS.api.sha256Sync('abc'))")
    check("subtle digest matches sync fallback", consistent)

    # 6. stats helpers sanity (reference: scipy gammaincc(127.5,127.5)=0.48822252)
    chi_ok = page.evaluate("Math.abs(window.ARS.api.chiSquareP(255, 255) - 0.4882225) < 1e-6")
    check("chiSquareP matches scipy reference", chi_ok)
    pear_ok = page.evaluate(
        "Math.abs(window.ARS.api.pearson([1,2,3],[2,4,6]) - 1) < 1e-12")
    check("pearson perfect correlation = 1", pear_ok)

    # 7. BEACON: TX click (no JS error), node indicator lights, abort works
    page.click('nav#tabs button[data-tab="beacon"]')
    page.click("#btn-tx")
    page.wait_for_timeout(1500)
    lit = page.evaluate("document.querySelectorAll('#tx-nodes .node-cell.lit').length")
    check("TX lights a node indicator", lit == 1, f"lit={lit}")
    status = page.text_content("#tx-status")
    check("TX status shows transmitting", "TRANSMITTING" in status, status.strip())
    page.click("#btn-tx-stop")
    page.wait_for_timeout(200)

    # 8. WAV export: render + base64 fallback populated
    page.click("#btn-tx-wav")
    try:
        page.wait_for_function(
            "document.getElementById('wav-b64').value.length > 1000", timeout=15000)
        b64len = page.evaluate("document.getElementById('wav-b64').value.length")
        b64 = page.evaluate("document.getElementById('wav-b64').value")
        meta = page.text_content("#wav-meta")
        check("WAV rendered + base64 fallback populated", b64len > 1500000,
              f"b64 len={b64len}, meta={meta.strip()}")
        href = page.get_attribute("#wav-link", "href") or ""
        check("WAV download blob URL set", href.startswith("blob:"), href[:30])
        # base64 must be valid end-to-end (regression: chunk size % 3 bug)
        raw = base64.b64decode(b64, validate=True)
        check("base64 decodes to full 13 s WAV",
              raw[:4] == b"RIFF" and (len(raw) - 44) // 2 == 573300,
              f"decoded {len(raw)} bytes")
    except Exception as e:
        check("WAV rendered + base64 fallback populated", False, str(e))

    # 9. REPLY composer: trit cells cycle 0->1->2->0
    cell = page.locator("#reply-trits .trit-cell").first
    seq = []
    for _ in range(3):
        cell.click()
        seq.append(cell.text_content())
    check("trit cell cycles 0->1->2->0", seq == ["1", "2", "0"], str(seq))
    page.click("#btn-reply-play")
    page.wait_for_timeout(300)
    rstat = page.text_content("#reply-status")
    check("reply play runs without error", "PLAYING REPLY" in rstat, rstat.strip())

    # 10. MONITOR: mic start (fake device) — capture or clean MIC UNAVAILABLE
    page.click('nav#tabs button[data-tab="monitor"]')
    page.click("#btn-mic-start")
    page.wait_for_timeout(3000)
    mic_status = page.text_content("#mon-mic-status")
    mic_ok = "CAPTURING" in mic_status
    mic_unavail = "MIC UNAVAILABLE" in mic_status
    check("mic capture starts (or cleanly reports unavailable)", mic_ok or mic_unavail,
          mic_status.strip()[:120])
    if mic_ok:
        corr = page.text_content("#mon-corr-val")
        check("lattice detector producing correlation values", corr not in ("—", ""),
              f"r={corr}")
        sr = page.text_content("#mon-samplerate")
        check("sample rate displayed", "SR " in sr and "—" not in sr, sr.strip())
        page.click("#btn-mic-stop")
        page.wait_for_timeout(300)
        check("mic stop returns to idle", "IDLE" in page.text_content("#mon-mic-status"))

    # 11. RNG: stream produces samples, p-value computed
    page.click('nav#tabs button[data-tab="rng"]')
    page.click("#btn-rng-start")
    page.wait_for_timeout(2500)
    count = int(page.text_content("#rng-count") or "0")
    ptxt = page.text_content("#rng-p")
    check("RNG stream produces samples", count >= 3, f"count={count}")
    check("RNG p-value displayed", ptxt.startswith("p = "), ptxt)
    page.click("#btn-rng-stop")

    # 12. JOURNAL: note append, hash chain verify PASS, export renders
    page.click('nav#tabs button[data-tab="journal"]')
    page.fill("#jr-note-text", "field validation note 1")
    page.click("#btn-jr-note")
    page.wait_for_timeout(400)
    n_entries = page.evaluate("window.ARS.state.journal.length")
    check("journal note appended", n_entries >= 2, f"entries={n_entries}")
    page.click("#btn-jr-verify")
    page.wait_for_timeout(1500)
    vstat = page.text_content("#jr-verify-status")
    check("hash chain verify PASS", "INTEGRITY PASS" in vstat, vstat.strip())
    # tamper test -> must FAIL
    page.evaluate("window.ARS.state.journal[0].content = 'tampered'")
    page.click("#btn-jr-verify")
    page.wait_for_timeout(1500)
    vstat2 = page.text_content("#jr-verify-status")
    check("tamper detected (chain FAIL)", "INTEGRITY FAIL" in vstat2, vstat2.strip())
    page.evaluate("window.ARS.state.journal[0].content = 'ARS v5.0 booted (storage persistent)'")

    # prediction pre-commit flow
    page.fill("#jr-pred-text", "test prediction text")
    page.click("#btn-jr-hash")
    page.wait_for_timeout(500)
    h = page.text_content("#jr-pred-hash")
    check("prediction hash shown before commit", len(h) == 64, h[:20])
    page.click("#btn-jr-commit")
    page.wait_for_timeout(400)
    has_pred = page.evaluate(
        "window.ARS.state.journal.some(e => e.type === 'PREDICTION')")
    check("prediction committed to journal", has_pred)

    # export-all renders JSON bundle
    page.click("#btn-export-all")
    page.wait_for_timeout(400)
    bundle = page.evaluate("JSON.parse(document.getElementById('jr-export').value)")
    check("export-all JSON has journal + settings",
          "journal" in bundle and "settings" in bundle and bundle["version"] == "5.0",
          f"keys={list(bundle.keys())}")

    # 13. TIERS: 12 items render, manual check persists, auto-tick works
    page.click('nav#tabs button[data-tab="tiers"]')
    n_items = page.locator("#tier-list .tier-item").count()
    check("12 tier checklist items render", n_items == 12, f"count={n_items}")
    page.locator("#tier-list .tier-item input[type=checkbox]").first.click()
    page.wait_for_timeout(200)
    banner = page.text_content("#tier-banner")
    check("composite banner reflects T1 activity", "TIER-1 CRITERIA ACTIVE" in banner,
          banner.strip())
    page.evaluate("window.ARS.api.tierAutoTick('t2-13hz')")
    page.wait_for_timeout(200)
    ticked = page.evaluate("!!window.ARS.state.tierChecks['t2-13hz']")
    check("auto-tick wires into tier state", ticked)

    # 14. SENSORS panel renders states (headless: likely unavailable, must not error)
    page.click('nav#tabs button[data-tab="sensors"]')
    page.wait_for_timeout(4500)  # let fallback timers fire
    sens = page.text_content("#sen-mag-status") + "|" + page.text_content("#sen-acc-status")
    check("sensors panel reports state without errors", len(sens) > 5, sens.strip()[:100])

    # 15. persistence across reload
    page.reload(wait_until="load")
    page.wait_for_timeout(800)
    kept = page.evaluate("window.ARS.state.journal.length")
    kept_tier = page.evaluate("!!window.ARS.state.tierChecks['t2-13hz']")
    check("journal persists across reload", kept >= n_entries, f"entries={kept}")
    check("tier checks persist across reload", kept_tier)

    check("no console errors at end", len(errors) == 0, "; ".join(errors[:8]))
    page.screenshot(path="/mnt/agents/output/ars-repo/validation-screenshot.png",
                    full_page=False)
    browser.close()

print("\n=== console errors ===")
print("\n".join(errors) if errors else "(none)")
print(f"\n=== RESULT: {'ALL PASS' if not failures else str(len(failures)) + ' FAILURES'} ===")
for f in failures:
    print(" -", f)
sys.exit(1 if failures else 0)
