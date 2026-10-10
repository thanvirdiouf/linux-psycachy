#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Run a build, preserving its exit status and recording Linux host resources."""

import argparse
import csv
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import time


def sample():
    memory = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        memory[key] = int(value.split()[0]) * 1024
    cpu = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
    vm = dict(line.split() for line in Path('/proc/vmstat').read_text().splitlines())
    pressure = {}
    for resource in ('memory', 'io', 'cpu'):
        path = Path('/proc/pressure') / resource
        if path.exists():
            for line in path.read_text().splitlines():
                fields = line.split()
                pressure[f'{resource}_{fields[0]}_us'] = int(dict(
                    field.split('=') for field in fields[1:])['total'])
    return {
        'elapsed_seconds': 0.0,
        'cpu_total_ticks': sum(cpu), 'cpu_idle_ticks': cpu[3],
        'cpu_iowait_ticks': cpu[4],
        'available_memory_bytes': memory['MemAvailable'],
        'swap_used_bytes': memory['SwapTotal'] - memory['SwapFree'],
        'swap_in_pages': int(vm['pswpin']), 'swap_out_pages': int(vm['pswpout']),
        'disk_free_bytes': shutil.disk_usage('.').free,
        **pressure,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--interval', type=float, default=10)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or args.interval <= 0:
        parser.error('Provide a command and a positive sampling interval.')
    args.output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    initial = sample()
    rows = [initial]
    measured = ['/usr/bin/time', '-v', '-o', str(args.output / 'time.txt'), '--', *command]
    process = subprocess.Popen(measured, start_new_session=True)

    def forward(signum, _frame):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, forward)
    with (args.output / 'resources.csv').open('w') as stream:
        writer = csv.DictWriter(stream, fieldnames=initial.keys())
        writer.writeheader()
        writer.writerow(initial)
        while True:
            try:
                status = process.wait(timeout=args.interval)
            except subprocess.TimeoutExpired:
                status = None
            current = sample()
            current['elapsed_seconds'] = round(time.monotonic() - started, 3)
            rows.append(current)
            writer.writerow(current)
            stream.flush()
            if status is not None:
                break
    status = status if status >= 0 else 128 - status
    total_ticks = current['cpu_total_ticks'] - initial['cpu_total_ticks']
    idle_ticks = current['cpu_idle_ticks'] - initial['cpu_idle_ticks']
    io_ticks = current['cpu_iowait_ticks'] - initial['cpu_iowait_ticks']
    models = [line.split(':', 1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines()
              if line.startswith('model name')]
    summary = {
        'command': command, 'exit_status': status,
        'host': platform.platform(), 'cpu_model': models[0] if models else 'unknown',
        'logical_cpus': os.cpu_count(), 'usable_cpus': len(os.sched_getaffinity(0)),
        'elapsed_seconds': current['elapsed_seconds'], 'sample_interval_seconds': args.interval,
        'host_cpu_busy_percent': round(100 * (total_ticks - idle_ticks - io_ticks) / total_ticks, 2) if total_ticks else 0,
        'host_cpu_iowait_percent': round(100 * io_ticks / total_ticks, 2) if total_ticks else 0,
        'min_sampled_available_memory_bytes': min(row['available_memory_bytes'] for row in rows),
        'max_sampled_swap_used_bytes': max(row['swap_used_bytes'] for row in rows),
        'swap_in_pages': current['swap_in_pages'] - initial['swap_in_pages'],
        'swap_out_pages': current['swap_out_pages'] - initial['swap_out_pages'],
        'min_sampled_disk_free_bytes': min(row['disk_free_bytes'] for row in rows),
        'pressure_stall_us': {key: current[key] - initial[key] for key in initial if key.endswith('_us')},
    }
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    gib = 1024 ** 3
    report = (
        f"### Build resource measurements\n\n"
        f"- Exit status: {status}; elapsed: {summary['elapsed_seconds'] / 60:.1f} minutes\n"
        f"- CPU: {summary['cpu_model']}; usable CPUs: {summary['usable_cpus']}\n"
        f"- Host CPU busy: {summary['host_cpu_busy_percent']}%; I/O wait: {summary['host_cpu_iowait_percent']}%\n"
        f"- Lowest sampled available RAM: {summary['min_sampled_available_memory_bytes'] / gib:.2f} GiB\n"
        f"- Highest sampled swap usage: {summary['max_sampled_swap_used_bytes'] / gib:.2f} GiB\n"
        f"- Swap pages in/out: {summary['swap_in_pages']}/{summary['swap_out_pages']}\n\n"
        "CPU, memory, swap and pressure are host-wide measurements. Sampling can miss brief peaks. "
        "time.txt includes process resource usage; its maximum RSS is not aggregate build memory.\n"
    )
    (args.output / 'summary.md').write_text(report)
    print(report, flush=True)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
