"""Measure a chosen endpoint; selecting a tracking URL records real clicks."""

import argparse
import asyncio
from collections import Counter
import json
import statistics
import time
import httpx


async def run(args):
    semaphore = asyncio.Semaphore(args.concurrency)
    latency = []
    statuses = Counter()
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:

        async def request():
            async with semaphore:
                started = time.perf_counter()
                try:
                    response = await client.get(args.url)
                    statuses[str(response.status_code)] += 1
                except httpx.HTTPError:
                    statuses["network_error"] += 1
                latency.append((time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        await asyncio.gather(*(request() for _ in range(args.requests)))
        elapsed = time.perf_counter() - started
    ordered = sorted(latency)
    print(
        json.dumps(
            {
                "requests": args.requests,
                "concurrency": args.concurrency,
                "seconds": round(elapsed, 2),
                "requests_per_second": round(args.requests / elapsed, 2),
                "status_counts": dict(statuses),
                "latency_ms": {
                    "median": round(statistics.median(latency), 2),
                    "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 2),
                    "p99": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))], 2),
                },
            },
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000/health")
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.requests <= 100000 or not 1 <= args.concurrency <= 500:
        parser.error("Use 1–100000 requests and 1–500 concurrent requests.")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
