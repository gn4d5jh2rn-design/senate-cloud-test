import json
import os
import re
import requests
from urllib.parse import parse_qsl, urlencode
from playwright.sync_api import sync_playwright

HOME = "https://efdsearch.senate.gov/search/home/"
DATA_URL = "https://efdsearch.senate.gov/search/report/data/"
BASE_URL = "https://efdsearch.senate.gov"

STATE_FILE = "seen_senate_filings.json"
START_DATE = "01/01/2026"
BATCH_SIZE = 100
MINIMUM_ALERT = 100001

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram(message):
    if not BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set."
        )

    if not CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID is not set."
        )

    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=30,
    )

    response.raise_for_status()


def load_seen():
    if not os.path.exists(STATE_FILE):
        return set()

    with open(STATE_FILE, "r") as f:
        data = json.load(f)

    return set(data)


def save_seen(seen):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)


def filing_id_from_link(link):
    match = re.search(
        r"/(?:ptr|paper)/([^/]+)/",
        link
    )

    if not match:
        return None

    return match.group(1)


def minimum_amount(amount):
    numbers = re.findall(r"\$([\d,]+)", amount)

    if not numbers:
        return 0

    first = int(numbers[0].replace(",", ""))

    if "Over" in amount:
        return first + 1

    return first


def effective_ticker(ticker, asset_name):
    ticker = ticker.strip()

    if ticker and ticker != "--":
        return ticker.upper()

    match = re.search(
        r"\(([A-Z][A-Z0-9.\-]{0,14})\)",
        asset_name
    )

    if match:
        return match.group(1).upper()

    return None


def parse_ptr(page):
    rows = page.locator("table tbody tr")

    transactions = []

    for i in range(rows.count()):
        cells = [
            text.strip()
            for text in rows.nth(i).locator("td").all_inner_texts()
        ]

        if len(cells) < 9:
            continue

        transactions.append({
            "date": cells[1],
            "owner": cells[2],
            "ticker": cells[3],
            "asset": cells[4],
            "asset_type": cells[5],
            "transaction_type": cells[6],
            "amount": cells[7],
            "comment": cells[8],
        })

    return transactions


def find_qualifying_groups(transactions):
    purchases = [
        transaction
        for transaction in transactions
        if transaction["transaction_type"]
        .strip()
        .lower()
        .startswith("purchase")
    ]

    groups = {}

    for index, transaction in enumerate(purchases):
        ticker = effective_ticker(
            transaction["ticker"],
            transaction["asset"]
        )

        if ticker:
            key = ("ticker", ticker)
        else:
            # No reliable ticker: do not combine it with
            # unrelated tickerless assets.
            key = ("single", index)

        if key not in groups:
            groups[key] = {
                "ticker": ticker,
                "transactions": [],
                "minimum_total": 0,
            }

        groups[key]["transactions"].append(transaction)

        groups[key]["minimum_total"] += minimum_amount(
            transaction["amount"]
        )

    return [
        group
        for group in groups.values()
        if group["minimum_total"] >= MINIMUM_ALERT
    ]


with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()

    captured_body = None

    def capture_request(request):
        global captured_body

        if (
            "/search/report/data/" in request.url
            and request.method == "POST"
            and captured_body is None
        ):
            captured_body = request.post_data

    page.on("request", capture_request)

    # --------------------------------------------------
    # Open Senate eFD and perform normal browser search
    # --------------------------------------------------

    page.goto(
        HOME,
        wait_until="domcontentloaded",
        timeout=60000
    )

    page.get_by_label(
        "I understand the prohibitions on obtaining and use of financial disclosure reports."
    ).check()

    page.wait_for_url("**/search/**", timeout=30000)

    # Senators
    page.locator(
        'input[name="filer_type"][value="1"]'
    ).check()

    # Periodic Transaction Reports
    page.locator(
        'input[name="report_type"][value="11"]'
    ).check()

    # Only filings submitted from 2026 onward
    page.locator("#fromDate").fill(START_DATE)

    page.get_by_role(
        "button",
        name="Search Reports"
    ).click()

    page.locator("table tbody tr").first.wait_for(
        state="visible",
        timeout=30000
    )

    page.wait_for_timeout(1500)

    if not captured_body:
        raise RuntimeError(
            "Could not capture Senate search request."
        )

    csrf = next(
        (
            cookie["value"]
            for cookie in page.context.cookies()
            if cookie["name"] == "csrftoken"
        ),
        None
    )

    if not csrf:
        raise RuntimeError("CSRF token not found.")

    # --------------------------------------------------
    # Retrieve ALL result batches directly
    # --------------------------------------------------

    def fetch_batch(start):
        params = parse_qsl(
            captured_body,
            keep_blank_values=True
        )

        modified = []

        for key, value in params:
            if key == "start":
                value = str(start)

            if key == "length":
                value = str(BATCH_SIZE)

            modified.append((key, value))

        body = urlencode(modified)

        text = page.evaluate(
            """async ({url, body, csrf}) => {
                const response = await fetch(url, {
                    method: "POST",
                    credentials: "include",
                    headers: {
                        "Content-Type":
                            "application/x-www-form-urlencoded; charset=UTF-8",
                        "X-Requested-With":
                            "XMLHttpRequest",
                        "X-CSRFToken": csrf
                    },
                    body: body
                });

                if (!response.ok) {
                    throw new Error(
                        "HTTP " + response.status
                    );
                }

                return await response.text();
            }""",
            {
                "url": DATA_URL,
                "body": body,
                "csrf": csrf,
            }
        )

        return json.loads(text)

    # --------------------------------------------------
    # Senate pagination can be unstable between requests.
    # Fetch the result set several times and take the union
    # of all filing links we observe.
    # --------------------------------------------------

    rows_by_link = {}
    total = None

    for discovery_round in range(1, 6):
        first_batch = fetch_batch(0)

        if total is None:
            total = first_batch.get(
                "recordsFiltered",
                0
            )

        round_rows = first_batch.get("data", [])

        start = BATCH_SIZE

        while start < total:
            batch = fetch_batch(start)
            round_rows.extend(
                batch.get("data", [])
            )
            start += BATCH_SIZE

        round_links = set()

        for row in round_rows:
            if len(row) < 4:
                continue

            match = re.search(
                r'href="([^"]+)"',
                row[3]
            )

            if not match:
                continue

            link = match.group(1)

            round_links.add(link)
            rows_by_link[link] = row

        print(
            f"Discovery round {discovery_round}: "
            f"{len(round_links)} unique this round | "
            f"{len(rows_by_link)} unique total | "
            f"Senate reports {total}"
        )

        # Two consecutive rounds are normally enough,
        # but continue a little longer if we have not yet
        # observed at least the reported total.
        if (
            discovery_round >= 2
            and len(rows_by_link) >= total
        ):
            break

        page.wait_for_timeout(1000)

    rows = list(rows_by_link.values())

    # --------------------------------------------------
    # Convert result rows to unique filings
    # --------------------------------------------------

    filings = {}

    for row in rows:
        if len(row) < 5:
            continue

        match = re.search(
            r'href="([^"]+)"',
            row[3]
        )

        if not match:
            continue

        link = match.group(1)

        filing_id = filing_id_from_link(link)

        if not filing_id:
            continue

        # Deduplicate duplicate Senate result rows.
        if filing_id in filings:
            continue

        if "/view/paper/" in link:
            filing_type = "paper"

        elif "/view/ptr/" in link:
            filing_type = "electronic"

        else:
            continue

        filings[filing_id] = {
            "id": filing_id,
            "member": row[2],
            "filed_date": row[4],
            "link": link,
            "type": filing_type,
        }

    electronic_count = sum(
        1
        for filing in filings.values()
        if filing["type"] == "electronic"
    )

    paper_count = sum(
        1
        for filing in filings.values()
        if filing["type"] == "paper"
    )

    print()
    print("Search rows:", len(rows))
    print("Unique filings:", len(filings))
    print("Electronic PTRs:", electronic_count)
    print("Paper PTRs:", paper_count)

    # --------------------------------------------------
    # Load state
    # --------------------------------------------------

    seen = load_seen()

    print("Already seen:", len(seen))

    # FIRST RUN:
    # Seed everything that already exists.
    if not os.path.exists(STATE_FILE):
        seen.update(filings.keys())
        save_seen(seen)

        print()
        print(
            f"Seeded {len(seen)} existing Senate filings."
        )
        print(
            "No historical filings were processed."
        )

        browser.close()
        raise SystemExit

    # --------------------------------------------------
    # Find genuinely new filings
    # --------------------------------------------------

    new_filings = [
        filing
        for filing_id, filing in filings.items()
        if filing_id not in seen
    ]

    print("New filings:", len(new_filings))

    # --------------------------------------------------
    # Process only new filings
    # --------------------------------------------------

    for filing in new_filings:
        print()
        print("=" * 70)
        print("NEW SENATE PTR")
        print("Member:", filing["member"])
        print("Filed:", filing["filed_date"])
        print("Type:", filing["type"])

        full_url = BASE_URL + filing["link"]

        print("URL:", full_url)

        try:
            # PAPER:
            # No automatic inspection.
            if filing["type"] == "paper":
                print(
                    "PAPER PTR — manual review required."
                )

                message = (
                    "📄 SENATE PAPER PTR\n\n"
                    "A new paper Periodic Transaction Report "
                    "was filed.\n\n"
                    f"Filed: {filing['filed_date']}\n"
                    "Chamber: Senate\n\n"
                    "Manual review required:\n"
                    f"{full_url}"
                )

                send_telegram(message)

                print(
                    "Telegram paper alert sent."
                )

            # ELECTRONIC:
            # Parse transactions automatically.
            else:
                page.goto(
                    full_url,
                    wait_until="domcontentloaded",
                    timeout=60000
                )

                page.locator(
                    "table tbody tr"
                ).first.wait_for(
                    state="visible",
                    timeout=30000
                )

                transactions = parse_ptr(page)

                qualifying = find_qualifying_groups(
                    transactions
                )

                print(
                    "Transactions:",
                    len(transactions)
                )

                print(
                    "Qualifying groups:",
                    len(qualifying)
                )

                for group in qualifying:
                    print()
                    print("QUALIFYING PURCHASE")
                    print(
                        "Ticker:",
                        group["ticker"] or
                        "(no ticker)"
                    )
                    print(
                        "Combined minimum:",
                        f"${group['minimum_total']:,}"
                    )
                    print(
                        "Transactions:",
                        len(group["transactions"])
                    )

                    transaction_lines = []

                    for transaction in group["transactions"]:
                        transaction_lines.append(
                            "\n".join([
                                f"Asset: {transaction['asset']}",
                                f"Asset type: {transaction['asset_type']}",
                                f"Trade date: {transaction['date']}",
                                f"Amount: {transaction['amount']}",
                            ])
                        )

                    ticker_display = (
                        group["ticker"]
                        or "(no ticker)"
                    )

                    message = (
                        "🚨 LARGE SENATE PURCHASE\n\n"
                        f"Member: {filing['member']}\n"
                        "Chamber: Senate\n"
                        f"Filed: {filing['filed_date']}\n"
                        f"Ticker: {ticker_display}\n"
                        f"Combined minimum disclosed: "
                        f"${group['minimum_total']:,}\n"
                        f"Transactions in filing: "
                        f"{len(group['transactions'])}\n\n"
                        + "\n\n".join(transaction_lines)
                        + "\n\n"
                        + f"Filing: {full_url}"
                    )

                    send_telegram(message)

                    print(
                        "Telegram purchase alert sent."
                    )

            # Only mark as seen after successful
            # processing.
            seen.add(filing["id"])
            save_seen(seen)

        except Exception as error:
            print(
                "ERROR — filing NOT marked seen:"
            )
            print(error)

    print()
    print("Monitor complete.")

    browser.close()
