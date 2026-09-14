from playwright.sync_api import sync_playwright

URL = "https://efdsearch.senate.gov/search/home/"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)

    page = browser.new_page(
        viewport={"width": 1400, "height": 1000}
    )

    page.goto(
        URL,
        wait_until="domcontentloaded",
        timeout=60000
    )

    print("TITLE:", page.title())
    print("URL:", page.url)

    agreement = page.get_by_label(
        "I understand the prohibitions on obtaining and use of financial disclosure reports."
    )

    print("AGREEMENT FOUND:", agreement.count())

    if agreement.count() == 0:
        print("FAILED: Senate access page did not load.")
        print("PAGE TEXT:")
        print(page.locator("body").inner_text()[:2000])
        browser.close()
        raise SystemExit(1)

    agreement.check()

    page.wait_for_url(
        "**/search/**",
        timeout=30000
    )

    print("AFTER AGREEMENT TITLE:", page.title())
    print("AFTER AGREEMENT URL:", page.url)

    if "/search/" in page.url:
        print("SUCCESS: Senate works from GitHub headed browser.")

    browser.close()
