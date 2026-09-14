from playwright.sync_api import sync_playwright

HOME = "https://efdsearch.senate.gov/search/home/"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)

    page = browser.new_page(
        viewport={"width": 1400, "height": 1000}
    )

    page.goto(
        HOME,
        wait_until="domcontentloaded",
        timeout=60000
    )

    print("INITIAL TITLE:", page.title())
    print("INITIAL URL:", page.url)

    agreement = page.get_by_label(
        "I understand the prohibitions on obtaining and use of financial disclosure reports."
    )

    print("AGREEMENT FOUND:", agreement.count())

    if agreement.count() != 1:
        print("FAILED: agreement page unavailable.")
        print(page.locator("body").inner_text()[:2000])
        browser.close()
        raise SystemExit(1)

    agreement.check()

    # Wait several seconds for the Senate site to transition.
    page.wait_for_timeout(5000)

    print("AFTER AGREEMENT TITLE:", page.title())
    print("AFTER AGREEMENT URL:", page.url)

    # Real proof that we reached the search form.
    first_name = page.locator('input[name="first_name"]')
    last_name = page.locator('input[name="last_name"]')
    report_type = page.locator('input[name="report_type"][value="11"]')

    print("FIRST NAME INPUT:", first_name.count())
    print("LAST NAME INPUT:", last_name.count())
    print("PTR CHECKBOX:", report_type.count())

    if (
        first_name.count() == 1
        and last_name.count() == 1
        and report_type.count() == 1
    ):
        print("SUCCESS: real Senate search form loaded in GitHub Actions.")
        browser.close()
        raise SystemExit(0)

    print()
    print("FAILED: agreement loaded, but search form did not.")
    print("PAGE TEXT:")
    print(page.locator("body").inner_text()[:3000])

    browser.close()
    raise SystemExit(1)
