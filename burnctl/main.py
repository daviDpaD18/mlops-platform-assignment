import asyncio
import aiohttp
import argparse
import subprocess
import time
import base64

def make_auth_header(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}

async def send_request(session, url, headers):
    start = time.time()
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=35)) as resp:
            await resp.text()
            return time.time() - start, resp.status
    except Exception as e:
        return time.time() - start, 0

async def watch_pods(namespace, stop_event, pod_log):
    prev_count = 0
    while not stop_event.is_set():
        try:
            result = subprocess.run(
                ["kubectl", "get", "pods", "-n", namespace,
                 "--field-selector=status.phase=Running", "--no-headers"],
                capture_output=True, text=True
            )
            count = len([l for l in result.stdout.strip().split("\n") if l])
            elapsed = time.time() - pod_log["start_time"]
            if count != prev_count:
                ts = f"[{int(elapsed):02d}s]"
                if prev_count == 0:
                    print(f"\n{ts} Pods: {count}")
                else:
                    arrow = "↑" if count > prev_count else "↓"
                    print(f"\n{ts} Pods: {prev_count} {arrow} {count}")
                pod_log["events"].append((elapsed, prev_count, count))
                prev_count = count
        except Exception:
            pass
        await asyncio.sleep(5)

async def run(url, user, password, concurrency, duration, namespace):
    headers = make_auth_header(user, password)
    stop_event = asyncio.Event()
    pod_log = {"start_time": time.time(), "events": []}

    print(f"Starting load test → {url}")
    print(f"Concurrency: {concurrency} | Duration: {duration}s | Auth: {user}:***")
    print("─" * 50)

    watcher = asyncio.create_task(watch_pods(namespace, stop_event, pod_log))

    latencies = []
    errors = 0
    total = 0
    status_counts = {}
    end_time = time.time() + duration

    connector = aiohttp.TCPConnector(limit=concurrency)
    async with aiohttp.ClientSession(connector=connector) as session:
        while time.time() < end_time:
            batch = [
                send_request(session, url, headers)
                for _ in range(concurrency)
            ]
            results = await asyncio.gather(*batch)
            for latency, status in results:
                total += 1
                latencies.append(latency)
                status_counts[status] = status_counts.get(status, 0) + 1
                if status not in (200, 202, 409):
                    errors += 1

    stop_event.set()
    await watcher

    latencies.sort()
    p50 = latencies[int(len(latencies) * 0.50)] * 1000
    p95 = latencies[int(len(latencies) * 0.95)] * 1000
    p99 = latencies[int(len(latencies) * 0.99)] * 1000
    error_rate = (errors / total * 100) if total > 0 else 0

    peak_pods = max((e[2] for e in pod_log["events"]), default=1)
    scale_out_at = next(
        (f"{int(e[0])}s" for e in pod_log["events"] if e[2] > e[1] and e[1] > 0),
        "no scale event"
    )

    print("\n" + "─" * 50)
    print("Summary")
    print("─" * 50)
    print(f"Requests:      {total}")
    print(f"Errors:        {errors}  ({error_rate:.1f}%)")
    print(f"Status codes:  {dict(sorted(status_counts.items()))}")
    print(f"Latency p50:   {p50:.0f}ms")
    print(f"Latency p95:   {p95:.0f}ms")
    print(f"Latency p99:   {p99:.0f}ms")
    print(f"Peak pods:     {peak_pods}")
    print(f"Scale-out at:  {scale_out_at}")
    print("─" * 50)

def main():
    parser = argparse.ArgumentParser(description="burnctl — load test CLI for the platform challenge")
    parser.add_argument("--url", default="http://loadtester.localhost/burn", help="Target URL")
    parser.add_argument("--user", default="user", help="Basic auth username")
    parser.add_argument("--password", default="password", help="Basic auth password")
    parser.add_argument("--concurrency", type=int, default=20, help="Concurrent requests per batch")
    parser.add_argument("--duration", type=int, default=120, help="Test duration in seconds")
    parser.add_argument("--namespace", default="loadtester", help="Kubernetes namespace to watch")
    args = parser.parse_args()

    asyncio.run(run(
        url=args.url,
        user=args.user,
        password=args.password,
        concurrency=args.concurrency,
        duration=args.duration,
        namespace=args.namespace,
    ))

if __name__ == "__main__":
    main()