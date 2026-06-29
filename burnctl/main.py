import asyncio
import aiohttp
import argparse
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
    except Exception:
        return time.time() - start, 0

async def get_pod_metrics_async(namespace):
    process = await asyncio.create_subprocess_exec(
        "kubectl", "top", "pods", "-n", namespace, "--no-headers",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await process.communicate()
    cpu_values = []
    mem_values = []
    for line in stdout.decode().strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        cpu_str = parts[1]
        mem_str = parts[2]
        try:
            if cpu_str.endswith("m"):
                cpu_values.append(int(cpu_str[:-1]))
            mem_raw = mem_str.lower()
            if mem_raw.endswith("mi"):
                mem_values.append(int(mem_raw[:-2]))
            elif mem_raw.endswith("ki"):
                mem_values.append(int(mem_raw[:-2]) // 1024)
        except ValueError:
            continue
    avg_cpu = sum(cpu_values) / len(cpu_values) if cpu_values else 0
    avg_mem = sum(mem_values) / len(mem_values) if mem_values else 0
    return avg_cpu, avg_mem, len(cpu_values)

async def watch_pods(namespace, stop_event, pod_log):
    prev_count = 0
    metrics_samples = []

    while not stop_event.is_set():
        elapsed = time.time() - pod_log["start_time"]
        try:
            # non-blocking pod count
            process = await asyncio.create_subprocess_exec(
                "kubectl", "get", "pods", "-n", namespace,
                "--field-selector=status.phase=Running", "--no-headers",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await process.communicate()
            count = len([l for l in stdout.decode().strip().split("\n") if l.strip()])

            if count != prev_count:
                ts = f"[{int(elapsed):02d}s]"
                if prev_count == 0:
                    print(f"\n{ts} Pods: {count}")
                else:
                    arrow = "↑" if count > prev_count else "↓"
                    print(f"\n{ts} Pods: {prev_count} {arrow} {count}")
                pod_log["events"].append((elapsed, prev_count, count))
                prev_count = count

            # non-blocking metrics collection
            avg_cpu, avg_mem, pod_count = await get_pod_metrics_async(namespace)
            if avg_cpu > 0:
                metrics_samples.append({
                    "elapsed": elapsed,
                    "avg_cpu_m": avg_cpu,
                    "avg_mem_mi": avg_mem,
                    "pods": pod_count
                })
                print(
                    f"[{int(elapsed):02d}s] "
                    f"CPU avg: {avg_cpu:.0f}m | "
                    f"MEM avg: {avg_mem:.0f}Mi | "
                    f"Pods: {prev_count}   ",
                )

        except Exception:
            pass

        await asyncio.sleep(5)

    pod_log["metrics_samples"] = metrics_samples

async def load_worker(session, url, headers, end_time, results):
    while time.time() < end_time:
        latency, status = await send_request(session, url, headers)
        results.append((latency, status))

async def run(url, user, password, concurrency, duration, namespace):
    headers = make_auth_header(user, password)
    stop_event = asyncio.Event()
    pod_log = {"start_time": time.time(), "events": [], "metrics_samples": []}

    print(f"Starting load test → {url}")
    print(f"Concurrency: {concurrency} | Duration: {duration}s | Auth: {user}:***")
    print("─" * 50)

    watcher = asyncio.create_task(watch_pods(namespace, stop_event, pod_log))

    results = []
    end_time = time.time() + duration

    connector = aiohttp.TCPConnector(limit=concurrency)
    async with aiohttp.ClientSession(connector=connector) as session:
        workers = [
            asyncio.create_task(load_worker(session, url, headers, end_time, results))
            for _ in range(concurrency)
        ]
        await asyncio.gather(*workers)

    stop_event.set()
    await watcher

    # calculate stats
    latencies = []
    errors = 0
    total = len(results)
    status_counts = {}

    for latency, status in results:
        latencies.append(latency)
        status_counts[status] = status_counts.get(status, 0) + 1
        if status not in (200, 202, 409):
            errors += 1

    latencies.sort()
    p50 = latencies[int(len(latencies) * 0.50)] * 1000 if latencies else 0
    p95 = latencies[int(len(latencies) * 0.95)] * 1000 if latencies else 0
    p99 = latencies[int(len(latencies) * 0.99)] * 1000 if latencies else 0
    error_rate = (errors / total * 100) if total > 0 else 0

    peak_pods = max((e[2] for e in pod_log["events"]), default=1)
    scale_out_at = next(
        (f"{int(e[0])}s" for e in pod_log["events"] if e[2] > e[1] and e[1] > 0),
        "no scale event"
    )

    samples = pod_log["metrics_samples"]
    if samples:
        overall_avg_cpu = sum(s["avg_cpu_m"] for s in samples) / len(samples)
        overall_avg_mem = sum(s["avg_mem_mi"] for s in samples) / len(samples)
        peak_cpu = max(s["avg_cpu_m"] for s in samples)
        peak_mem = max(s["avg_mem_mi"] for s in samples)
    else:
        overall_avg_cpu = overall_avg_mem = peak_cpu = peak_mem = 0

    print("\n\n" + "─" * 50)
    print("Summary")
    print("─" * 50)
    print(f"Duration:            {duration}s")
    print(f"Concurrency:         {concurrency}")
    print()
    print(f"Requests:            {total}")
    print(f"Successful (202):    {status_counts.get(202, 0)}")
    print(f"Busy (409):          {status_counts.get(409, 0)}  (pods already burning — expected)")
    print(f"Errors:              {errors}  ({error_rate:.1f}%)")
    print()
    print(f"Latency p50:         {p50:.0f}ms")
    print(f"Latency p95:         {p95:.0f}ms")
    print(f"Latency p99:         {p99:.0f}ms")
    print()
    print(f"Avg CPU (all pods):  {overall_avg_cpu:.0f}m  ({len(samples)} samples)")
    print(f"Peak CPU (all pods): {peak_cpu:.0f}m")
    print(f"Avg MEM (all pods):  {overall_avg_mem:.0f}Mi")
    print(f"Peak MEM (all pods): {peak_mem:.0f}Mi")
    print()
    print(f"Peak pods:           {peak_pods}")
    print(f"Scale-out at:        {scale_out_at}")
    print("─" * 50)

def main():
    parser = argparse.ArgumentParser(
        description="burnctl — load test CLI for the platform challenge",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
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