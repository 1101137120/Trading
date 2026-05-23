#!/usr/bin/env python3
"""
Test MOPS API: batch concurrency, OTC stocks, error cases.
"""
from playwright.sync_api import sync_playwright
import json, time

URL = "https://mops.twse.com.tw/mops/#/web/t05st10_ifrs"

def batch_query(page, queries: list[dict]) -> list[dict]:
    """Run multiple revenue queries concurrently via Promise.all."""
    queries_json = json.dumps(queries)
    results = page.evaluate(f"""
        async () => {{
            const queries = {queries_json};
            const fetches = queries.map(q => fetch('/mops/api/t05st10_ifrs', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{
                    companyId: q.code,
                    dataType: '2',
                    month: String(q.month),
                    year: String(q.year),
                    subsidiaryCompanyId: ''
                }})
            }}).then(r => r.json()).catch(e => ({{error: e.toString()}})));
            return await Promise.all(fetches);
        }}
    """)
    return results

def parse_revenue(data: dict, code: str, year: int, month: int):
    """Extract monthly revenue from API response."""
    if data.get("code") != 200:
        return None
    rows = data.get("result", {}).get("data", [])
    for row in rows:
        if row[0] in ("本月", "本月："):
            val_str = str(row[1]).replace(",", "").strip()
            try:
                return int(val_str)
            except ValueError:
                return None
    return None

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        print("Loading MOPS...")
        page.goto(URL, wait_until="networkidle", timeout=30000)
        print("Ready\n")

        # Test batch of 10 queries
        queries = [
            {"code": "2330", "year": 113, "month": m} for m in range(1, 11)
        ]
        t0 = time.time()
        results = batch_query(page, queries)
        elapsed = time.time() - t0
        print(f"Batch of {len(queries)} in {elapsed:.2f}s ({elapsed/len(queries)*1000:.0f}ms each)")

        for q, r in zip(queries, results):
            rev = parse_revenue(r, q["code"], q["year"], q["month"])
            print(f"  {q['code']} {q['year']}/{q['month']:02d}: {rev:,}" if rev else f"  {q['code']} {q['year']}/{q['month']:02d}: None  (code={r.get('code')})")

        # Test OTC stock (6505 or 2330 vs 3105)
        print("\n--- OTC stock test ---")
        otc_queries = [
            {"code": "6533", "year": 113, "month": 6},  # OTC stock
            {"code": "0050", "year": 113, "month": 6},  # ETF - should fail
            {"code": "9999", "year": 113, "month": 6},  # invalid
            {"code": "3008", "year": 113, "month": 6},  # 大立光 TSE
        ]
        results2 = batch_query(page, otc_queries)
        for q, r in zip(otc_queries, results2):
            rev = parse_revenue(r, q["code"], q["year"], q["month"])
            print(f"  {q['code']}: rev={rev}  api_code={r.get('code')}  msg={r.get('message','')[:50]}")

        # Test speed: large batch
        print("\n--- Speed test: 50 concurrent ---")
        big_batch = []
        for code in ["2330","2317","2454","2412","1301"]:
            for month in range(1, 11):
                big_batch.append({"code": code, "year": 113, "month": month})
        t0 = time.time()
        results3 = batch_query(page, big_batch)
        elapsed = time.time() - t0
        ok = sum(1 for r in results3 if r.get("code") == 200)
        print(f"50 queries in {elapsed:.2f}s ({elapsed/len(big_batch)*1000:.0f}ms each), {ok} success")

        browser.close()

if __name__ == "__main__":
    main()
