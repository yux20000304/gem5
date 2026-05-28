#!/usr/bin/env python3
#
# Batch runner for GAPBS multi-host SE experiments:
#   1. native: no CXL device, graph uses normal heap/private memory
#   2. cxl: graph allocated from shared /dev/gem5_cxl_mem
#   3. dsmtee: graph allocated from /dev/gem5_dsm_tee with DSM-TEE datapath
#
# The scheduler limits parallel gem5 processes using a conservative memory
# estimate and the current Linux MemAvailable value.

import argparse
import csv
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

STAT_RE = re.compile(
    r"^(?P<name>\S+)\s+(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
)

PAPER_CXL_DDR5_READ_LATENCY = "42ns"
PAPER_CXL_LINK_ONE_WAY_DELAY = "35ns"
PAPER_CXL_BANDWIDTH = "25.6GB/s"

PAGE_SIZE = 4096
KIB = 1024
MIB = 1024**2
GIB = 1024**3


def repo_default_paths():
    gem5_dir = Path(__file__).resolve().parents[1]
    workspace_dir = gem5_dir.parent
    return gem5_dir, workspace_dir, workspace_dir / "gapbs"


def parse_csv(raw, cast=str):
    values = [item.strip() for item in raw.split(",") if item.strip()]
    if not values:
        raise ValueError(f"empty CSV value: {raw}")
    return [cast(value) for value in values]


def align_up(value, alignment):
    return ((value + alignment - 1) // alignment) * alignment


def parse_size_to_bytes(raw):
    if isinstance(raw, int):
        return raw
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*", raw)
    if not match:
        raise ValueError(f"invalid size: {raw}")
    value = float(match.group(1))
    unit = match.group(2).lower()
    units = {
        "": 1,
        "b": 1,
        "k": 1024,
        "kb": 1000,
        "kib": 1024,
        "m": 1024**2,
        "mb": 1000**2,
        "mib": 1024**2,
        "g": 1024**3,
        "gb": 1000**3,
        "gib": 1024**3,
        "t": 1024**4,
        "tb": 1000**4,
        "tib": 1024**4,
    }
    if unit not in units:
        raise ValueError(f"unsupported size unit in {raw}")
    return int(value * units[unit])


def format_gem5_size(num_bytes):
    if num_bytes == 0:
        return "0B"
    for suffix, unit in (
        ("TiB", 1024**4),
        ("GiB", GIB),
        ("MiB", MIB),
        ("KiB", KIB),
    ):
        if num_bytes % unit == 0:
            return f"{num_bytes // unit}{suffix}"
    return f"{num_bytes}B"


def format_env_size(num_bytes):
    # GAPBS cxl_allocator accepts plain integer bytes reliably.
    return str(num_bytes)


def read_mem_available():
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * KIB
    except OSError:
        return None
    return None


def read_stats(path):
    stats = {}
    if not path.exists():
        return stats
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            match = STAT_RE.match(line)
            if not match:
                continue
            raw = match.group("value")
            value = float(raw)
            stats[match.group("name")] = (
                int(value) if value.is_integer() else value
            )
    return stats


def stat(stats, name, default=0):
    return stats.get(name, default)


def stat_any(stats, names, default=0):
    for name in names:
        if name in stats:
            return stats[name]
    return default


def read_cpu_cycles(stats):
    cycles = {}
    for name, value in stats.items():
        match = re.fullmatch(r"system\.cpu(\d+)\.numCycles", name)
        if match:
            cycles[int(match.group(1))] = int(value)
    return cycles


def parse_clock_to_period_ticks(raw):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]+)\s*", raw)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    multipliers = {
        "hz": 1,
        "khz": 1e3,
        "mhz": 1e6,
        "ghz": 1e9,
        "thz": 1e12,
    }
    if unit not in multipliers or value <= 0:
        return None
    # gem5 ticks use one tick per picosecond.
    return 1e12 / (value * multipliers[unit])


def sim_cycles(stats, cpu_clock):
    sim_ticks = stat(stats, "simTicks", 0)
    clock_ticks = stat(stats, "system.cpu_clk_domain.clock", None)
    if clock_ticks is None:
        clock_ticks = stat(stats, "system.clk_domain.clock", None)
    if clock_ticks:
        return sim_ticks / clock_ticks
    clock_ticks = parse_clock_to_period_ticks(cpu_clock)
    if clock_ticks:
        return sim_ticks / clock_ticks
    return sim_ticks


def parse_program_output(path):
    result = {
        "generate_time": "",
        "build_time": "",
        "trial_time": "",
        "average_time": "",
        "graph_nodes": "",
        "graph_edges": "",
        "graph_degree": "",
        "allocator_line": "",
    }
    if not path.exists():
        return result
    text = path.read_text(encoding="utf-8", errors="replace")
    patterns = {
        "generate_time": r"Generate Time:\s*([0-9.]+)",
        "build_time": r"Build Time:\s*([0-9.]+)",
        "trial_time": r"Trial Time:\s*([0-9.]+)",
        "average_time": r"Average Time:\s*([0-9.]+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            result[key] = match.group(1)
    match = re.search(
        r"Graph has\s+(\d+)\s+nodes and\s+(\d+)\s+undirected edges for degree:\s+(\d+)",
        text,
    )
    if match:
        result["graph_nodes"] = match.group(1)
        result["graph_edges"] = match.group(2)
        result["graph_degree"] = match.group(3)
    match = re.search(r"GAPBS .* allocator mapped .*", text)
    if match:
        result["allocator_line"] = match.group(0)
    return result


def workload_command(args, benchmark, extra_args):
    gapbs_bin = args.gapbs_dir / benchmark
    parts = [
        str(gapbs_bin),
        "-g",
        str(args.scale),
        "-k",
        str(args.degree),
        "-n",
        str(args.trials),
        "-r",
        str(args.root),
    ]
    if args.gapbs_args:
        parts.extend(shlex.split(args.gapbs_args))
    if extra_args:
        parts.extend(shlex.split(extra_args))
    return shlex.join(parts)


def host_cores_string(host_count, cores_per_host):
    return ",".join(str(cores_per_host) for _ in range(host_count))


def metadata_reserved_bytes(region_bytes, num_vmids):
    bytes_per_page = align_up(max(1, num_vmids), 64)
    exact = math.ceil(region_bytes / PAGE_SIZE) * bytes_per_page
    return align_up(exact + 64 * MIB, MIB)


def estimate_cxl_region_bytes(benchmark, scale, degree):
    nodes = 1 << scale
    generated_edges = nodes * degree
    directed_slots = 2 * generated_edges
    dest_size = 8 if benchmark == "sssp" else 4

    # During non-in-place build, GAPBS holds the initial CSR and the squished
    # CSR concurrently. CXL allocations cover CSR index/neighborhood arrays.
    neighbor_bytes = 2 * directed_slots * dest_size
    index_bytes = 2 * (nodes + 1) * 8
    estimate = int((neighbor_bytes + index_bytes) * 1.35)
    return align_up(max(4 * GIB, estimate), GIB)


def estimate_private_bytes(
    variant, benchmark, scale, degree, cxl_region_bytes
):
    nodes = 1 << scale
    generated_edges = nodes * degree
    edge_size = 16 if benchmark == "sssp" else 8
    edge_list = generated_edges * edge_size
    temp = nodes * 96
    kernel_extra = nodes * (96 if benchmark in ("bc", "sssp", "pr") else 48)
    graph_private = (
        0 if variant in ("cxl", "dsmtee") else int(cxl_region_bytes / 1.35)
    )
    estimate = int((edge_list + temp + kernel_extra + graph_private) * 1.45)
    minimum = 8 * GIB if variant in ("cxl", "dsmtee") else 16 * GIB
    return align_up(max(minimum, estimate), GIB)


def estimate_rss_bytes(variant, benchmark, scale, degree, cxl_region_bytes):
    private = estimate_private_bytes(
        variant, benchmark, scale, degree, cxl_region_bytes
    )
    cxl_touched = 0 if variant == "native" else int(cxl_region_bytes / 1.35)
    return align_up(int((private + cxl_touched) * 1.10), GIB)


def resolve_size(raw, default_bytes):
    if raw == "auto":
        return default_bytes
    return parse_size_to_bytes(raw)


def job_label(args, benchmark, variant, host_count):
    return (
        f"{args.label_prefix}_{benchmark}_{variant}_"
        f"h{host_count}_c{args.cores_per_host}_s{args.scale}_d{args.degree}"
    )


def write_env_file(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_job(args, benchmark, variant, host_count):
    total_threads = host_count * args.cores_per_host
    host_cores = host_cores_string(host_count, args.cores_per_host)
    label = job_label(args, benchmark, variant, host_count)
    out_dir = args.result_root / label
    m5out = out_dir / "m5out"
    program_out = out_dir / "program.out"
    program_err = out_dir / "program.err"
    env_file = out_dir / "workload.env"
    gem5_log = out_dir / "gem5.log"
    command_file = out_dir / "command.sh"
    summary_json = out_dir / "summary.json"

    cxl_region_default = estimate_cxl_region_bytes(
        benchmark, args.scale, args.degree
    )
    cxl_region_bytes = resolve_size(args.cxl_region_size, cxl_region_default)
    num_vmids = args.dsm_tee_num_vmids or total_threads
    metadata_default = metadata_reserved_bytes(cxl_region_bytes, num_vmids)
    metadata_bytes = resolve_size(args.dsm_tee_metadata_size, metadata_default)
    cxl_mem_default = align_up(
        cxl_region_bytes + metadata_bytes + args.cxl_mem_margin_bytes, GIB
    )
    cxl_mem_bytes = resolve_size(args.cxl_mem_size, cxl_mem_default)

    private_default = estimate_private_bytes(
        variant, benchmark, args.scale, args.degree, cxl_region_bytes
    )
    if variant == "native":
        host_mem_bytes = resolve_size(
            args.native_host_mem_size, private_default
        )
        variant_extra_args = args.native_gapbs_args
    else:
        host_mem_bytes = resolve_size(args.cxl_host_mem_size, private_default)
        variant_extra_args = (
            args.cxl_gapbs_args if variant == "cxl" else args.dsmtee_gapbs_args
        )
    estimated_rss = (
        parse_size_to_bytes(args.mem_per_job)
        if args.mem_per_job != "auto"
        else estimate_rss_bytes(
            variant, benchmark, args.scale, args.degree, cxl_region_bytes
        )
    )

    workload_cmd = workload_command(args, benchmark, variant_extra_args)
    env_lines = [
        f"OMP_NUM_THREADS={total_threads}",
        "MALLOC_ARENA_MAX=1",
    ]
    if args.fast_forward_to_roi:
        env_lines.append("GAPBS_M5_ROI=1")

    cmd = [
        str(args.gem5_bin),
        f"--outdir={m5out}",
        str(args.config),
        "--mode=cross-host-threaded",
        f"--host-cores={host_cores}",
        f"--cpu={args.cpu}",
        f"--cmd={workload_cmd}",
        f"--cwd={args.gapbs_dir}",
        f"--env-file={env_file}",
        f"--stdout={program_out}",
        f"--stderr={program_err}",
        f"--mem={args.mem}",
        f"--mem-size={format_gem5_size(host_mem_bytes)}",
        f"--host-mem-size={format_gem5_size(host_mem_bytes)}",
        f"--sys-clock={args.sys_clock}",
        f"--cpu-clock={args.cpu_clock}",
    ]
    if args.fast_forward_to_roi:
        cmd.append("--fast-forward-to-roi")
        if args.roi_maxinsts:
            cmd.append(f"--roi-maxinsts={args.roi_maxinsts}")
        if args.roi_continue_after_workend:
            cmd.append("--roi-continue-after-workend")

    if variant == "native":
        cmd.append("--cxl-mem-size=0B")
    else:
        cxl_path = (
            "/dev/gem5_dsm_tee" if variant == "dsmtee" else "/dev/gem5_cxl_mem"
        )
        env_lines.extend(
            [
                "GAPBS_CXL_GRAPH=1",
                f"GAPBS_CXL_PATH={cxl_path}",
                f"GAPBS_CXL_SIZE={format_env_size(cxl_region_bytes)}",
                f"GAPBS_CXL_STRICT={int(args.strict_allocator)}",
                f"GAPBS_CXL_VERBOSE={int(args.allocator_verbose)}",
            ]
        )
        cmd.extend(
            [
                f"--cxl-mem-size={format_gem5_size(cxl_mem_bytes)}",
                f"--cxl-mem-type={args.cxl_mem_type}",
                f"--cxl-latency={args.cxl_latency}",
                f"--cxl-bandwidth={args.cxl_bandwidth}",
                f"--cxl-link-delay={args.cxl_link_delay}",
            ]
        )
        if args.cxl_link_read_req_delay:
            cmd.append(
                f"--cxl-link-read-req-delay={args.cxl_link_read_req_delay}"
            )
        if args.cxl_link_read_resp_delay:
            cmd.append(
                f"--cxl-link-read-resp-delay={args.cxl_link_read_resp_delay}"
            )
        if args.cxl_link_write_req_delay:
            cmd.append(
                f"--cxl-link-write-req-delay={args.cxl_link_write_req_delay}"
            )
        if args.cxl_link_write_resp_delay:
            cmd.append(
                f"--cxl-link-write-resp-delay={args.cxl_link_write_resp_delay}"
            )

    if variant == "dsmtee":
        env_lines.extend(
            [
                "GAPBS_DSMTEE_GRAPH=1",
                "GAPBS_DSMTEE_PATH=/dev/gem5_dsm_tee",
                f"GAPBS_DSMTEE_SIZE={format_env_size(cxl_region_bytes)}",
                f"GAPBS_DSMTEE_STRICT={int(args.strict_allocator)}",
                f"GAPBS_DSMTEE_VERBOSE={int(args.allocator_verbose)}",
            ]
        )
        cmd.extend(
            [
                "--enable-dsm-tee",
                "--dsm-tee-data-path",
                f"--dsm-tee-num-vmids={num_vmids}",
                f"--dsm-tee-metadata-size={format_gem5_size(metadata_bytes)}",
                f"--dsm-tee-perm-cache-entries={args.dsm_tee_perm_cache_entries}",
                f"--dsm-tee-perm-check-cycles={args.dsm_tee_perm_check_cycles}",
                f"--dsm-tee-perm-cache-access-cycles={args.dsm_tee_perm_cache_access_cycles}",
                f"--dsm-tee-perm-cache-hit-latency={args.dsm_tee_perm_cache_hit_latency}",
                f"--dsm-tee-perm-cache-miss-latency={args.dsm_tee_perm_cache_miss_latency}",
                f"--dsm-tee-ide-req-delay={args.dsm_tee_ide_req_delay}",
                f"--dsm-tee-ide-resp-delay={args.dsm_tee_ide_resp_delay}",
                f"--dsm-tee-ide-req-cycles={args.dsm_tee_ide_req_cycles}",
                f"--dsm-tee-ide-resp-cycles={args.dsm_tee_ide_resp_cycles}",
                f"--dsm-tee-encrypt-read-delay={args.dsm_tee_encrypt_read_delay}",
                f"--dsm-tee-encrypt-write-delay={args.dsm_tee_encrypt_write_delay}",
            ]
        )
        if args.dsm_tee_no_metadata_packets:
            cmd.append("--dsm-tee-no-metadata-packets")
        if args.dsm_tee_warn_only:
            cmd.append("--dsm-tee-warn-only")

    return {
        "benchmark": benchmark,
        "variant": variant,
        "host_count": host_count,
        "cores_per_host": args.cores_per_host,
        "threads": total_threads,
        "host_cores": host_cores,
        "cpu_clock": args.cpu_clock,
        "fast_forward_to_roi": args.fast_forward_to_roi,
        "roi_maxinsts": args.roi_maxinsts,
        "label": label,
        "out_dir": out_dir,
        "m5out": m5out,
        "program_out": program_out,
        "program_err": program_err,
        "env_file": env_file,
        "gem5_log": gem5_log,
        "command_file": command_file,
        "summary_json": summary_json,
        "cmd": cmd,
        "env_lines": env_lines,
        "workload_cmd": workload_cmd,
        "host_mem_bytes": host_mem_bytes,
        "cxl_region_bytes": 0 if variant == "native" else cxl_region_bytes,
        "cxl_mem_bytes": 0 if variant == "native" else cxl_mem_bytes,
        "metadata_bytes": metadata_bytes if variant == "dsmtee" else 0,
        "estimated_rss_bytes": estimated_rss,
    }


def prepare_job(job, clean=False):
    out_dir = job["out_dir"]
    if clean and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_env_file(job["env_file"], job["env_lines"])
    command = shlex.join(str(item) for item in job["cmd"])
    job["command_file"].write_text(command + "\n", encoding="utf-8")


def summarize_job(job, status, returncode=None, wall_seconds=None):
    stats_path = job["m5out"] / "stats.txt"
    stats = read_stats(stats_path)
    program = parse_program_output(job["program_out"])
    cpu_cycles = read_cpu_cycles(stats)
    summary = {
        "status": status,
        "returncode": returncode,
        "wall_seconds": wall_seconds,
        "benchmark": job["benchmark"],
        "variant": job["variant"],
        "host_count": job["host_count"],
        "cores_per_host": job["cores_per_host"],
        "threads": job["threads"],
        "host_cores": job["host_cores"],
        "fast_forward_to_roi": job["fast_forward_to_roi"],
        "roi_maxinsts": job["roi_maxinsts"],
        "label": job["label"],
        "out_dir": str(job["out_dir"]),
        "workload_cmd": job["workload_cmd"],
        "host_mem_bytes": job["host_mem_bytes"],
        "cxl_region_bytes": job["cxl_region_bytes"],
        "cxl_mem_bytes": job["cxl_mem_bytes"],
        "metadata_bytes": job["metadata_bytes"],
        "estimated_rss_bytes": job["estimated_rss_bytes"],
        "sim_ticks": stat(stats, "simTicks", ""),
        "sim_cycles": sim_cycles(stats, job["cpu_clock"]) if stats else "",
        "max_cpu_cycles": max(cpu_cycles.values()) if cpu_cycles else "",
        "cxl_mem_reads": stat_any(
            stats,
            (
                "system.cxl_mem_ctrl.numReads::total",
                "system.cxl_mem_ctrl.readReqs",
            ),
            "",
        ),
        "cxl_mem_writes": stat_any(
            stats,
            (
                "system.cxl_mem_ctrl.numWrites::total",
                "system.cxl_mem_ctrl.writeReqs",
            ),
            "",
        ),
        "cxl_mem_bytes_read": stat_any(
            stats,
            (
                "system.cxl_mem_ctrl.bytesRead::total",
                "system.cxl_mem_ctrl.bytesReadSys",
            ),
            "",
        ),
        "cxl_mem_bytes_written": stat_any(
            stats,
            (
                "system.cxl_mem_ctrl.bytesWritten::total",
                "system.cxl_mem_ctrl.bytesWrittenSys",
            ),
            "",
        ),
        "permission_checks": stat(
            stats, "system.dsm_tee_ctrl.permissionChecks", ""
        ),
        "permission_cache_hits": stat(
            stats, "system.dsm_tee_ctrl.permissionCacheHits", ""
        ),
        "permission_cache_misses": stat(
            stats, "system.dsm_tee_ctrl.permissionCacheMisses", ""
        ),
        "metadata_reads": stat(stats, "system.dsm_tee_ctrl.metadataReads", ""),
        "metadata_read_bytes": stat(
            stats, "system.dsm_tee_ctrl.metadataReadBytes", ""
        ),
        "metadata_reserved": stat(
            stats, "system.dsm_tee_mem_driver.metadataReservedBytes", ""
        ),
        "metadata_allocated": stat(
            stats, "system.dsm_tee_mem_driver.metadataAllocatedBytes", ""
        ),
        **program,
    }
    job["summary_json"].write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with (job["out_dir"] / "summary.csv").open(
        "w", newline="", encoding="utf-8"
    ) as fh:
        writer = csv.writer(fh)
        writer.writerow(["metric", "value"])
        for key, value in summary.items():
            writer.writerow([key, value])
    return summary


def load_existing_summary(job):
    if not job["summary_json"].exists():
        return None
    try:
        return json.loads(job["summary_json"].read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_aggregate(args, jobs):
    rows = []
    for job in jobs:
        summary = load_existing_summary(job)
        if summary is None:
            summary = {
                "status": "pending",
                "benchmark": job["benchmark"],
                "variant": job["variant"],
                "host_count": job["host_count"],
                "threads": job["threads"],
                "fast_forward_to_roi": job["fast_forward_to_roi"],
                "roi_maxinsts": job["roi_maxinsts"],
                "label": job["label"],
                "out_dir": str(job["out_dir"]),
            }
        rows.append(summary)

    fieldnames = [
        "status",
        "returncode",
        "wall_seconds",
        "benchmark",
        "variant",
        "host_count",
        "cores_per_host",
        "threads",
        "fast_forward_to_roi",
        "roi_maxinsts",
        "sim_cycles",
        "max_cpu_cycles",
        "cxl_mem_reads",
        "cxl_mem_bytes_read",
        "permission_checks",
        "permission_cache_misses",
        "metadata_reads",
        "metadata_read_bytes",
        "graph_nodes",
        "graph_edges",
        "graph_degree",
        "generate_time",
        "build_time",
        "trial_time",
        "average_time",
        "host_mem_bytes",
        "cxl_region_bytes",
        "cxl_mem_bytes",
        "metadata_bytes",
        "estimated_rss_bytes",
        "label",
        "out_dir",
    ]
    agg_csv = args.result_root / f"{args.label_prefix}_aggregate_summary.csv"
    with agg_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=fieldnames, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)

    if args.no_plot:
        return agg_csv, None

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        print(
            f"warning: matplotlib unavailable, skip aggregate plot: {exc}",
            file=sys.stderr,
        )
        return agg_csv, None

    completed = [
        row
        for row in rows
        if row.get("status") == "success" and row.get("sim_cycles") != ""
    ]
    if not completed:
        return agg_csv, None

    plot_path = args.result_root / f"{args.label_prefix}_aggregate_cycles.png"
    groups = sorted(
        {(row["benchmark"], int(row["host_count"])) for row in completed}
    )
    variants = list(args.variants)
    xlabels = [
        bench.replace("\\n", "\n").replace("/n", "\n") + f"\nh{hosts}"
        for bench, hosts in groups
    ]
    x = list(range(len(groups)))
    width = 0.8 / max(1, len(variants))

    fig, ax = plt.subplots(figsize=(max(12, len(groups) * 0.6), 5))
    for vidx, variant in enumerate(variants):
        values = []
        by_key = {
            (row["benchmark"], int(row["host_count"])): row
            for row in completed
            if row["variant"] == variant
        }
        for key in groups:
            row = by_key.get(key)
            values.append(float(row["sim_cycles"]) if row else 0)
        offsets = [
            item + (vidx - (len(variants) - 1) / 2) * width for item in x
        ]
        ax.bar(offsets, values, width, label=variant)
    ax.set_xticks(x, xlabels)
    ax.set_ylabel("cycles")
    ax.set_title(f"GAPBS scale={args.scale}, degree={args.degree}")
    ax.ticklabel_format(axis="y", style="sci", scilimits=(0, 0))
    ax.legend()
    fig.tight_layout()
    fig.savefig(plot_path, dpi=180)
    plt.close(fig)
    return agg_csv, plot_path


def can_launch(job, running, args):
    if len(running) >= args.max_parallel:
        return False
    available = read_mem_available()
    if available is None:
        return True
    reserve = args.reserve_mem_bytes
    needed = job["estimated_rss_bytes"]
    if available - reserve >= needed:
        return True
    # Avoid deadlock: if nothing is running, launch the job even if the
    # estimate is larger than MemAvailable and let the OS/gem5 fail honestly.
    return len(running) == 0


def run_jobs(args, jobs):
    run_env = os.environ.copy()
    prepend = args.prepend_ld_library_path
    if prepend and Path(prepend).exists():
        old_ld = run_env.get("LD_LIBRARY_PATH", "")
        run_env["LD_LIBRARY_PATH"] = (
            prepend if not old_ld else f"{prepend}:{old_ld}"
        )

    pending = list(jobs)
    running = []
    failures = 0

    while pending or running:
        launched = False
        while pending and can_launch(pending[0], running, args):
            job = pending.pop(0)
            existing = load_existing_summary(job)
            if (
                args.resume
                and existing
                and existing.get("status") == "success"
            ):
                print(f"skip completed: {job['label']}", flush=True)
                launched = True
                continue

            prepare_job(job, clean=args.clean)
            log_fh = job["gem5_log"].open(
                "w", encoding="utf-8", errors="replace"
            )
            print(
                "launch "
                f"{job['label']} "
                f"rss_est={job['estimated_rss_bytes'] / GIB:.1f}GiB "
                f"out={job['out_dir']}",
                flush=True,
            )
            proc = subprocess.Popen(
                job["cmd"],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=run_env,
                text=True,
            )
            running.append(
                {
                    "job": job,
                    "proc": proc,
                    "log_fh": log_fh,
                    "start": time.time(),
                }
            )
            launched = True

        still_running = []
        for item in running:
            rc = item["proc"].poll()
            if rc is None:
                if args.timeout and time.time() - item["start"] > args.timeout:
                    item["proc"].kill()
                    rc = item["proc"].wait()
                else:
                    still_running.append(item)
                    continue
            item["log_fh"].close()
            wall = time.time() - item["start"]
            status = "success" if rc == 0 else "failed"
            summarize_job(item["job"], status, rc, wall)
            print(
                f"finish {item['job']['label']} status={status} "
                f"rc={rc} wall={wall:.1f}s",
                flush=True,
            )
            if rc != 0:
                failures += 1
                if args.stop_on_fail:
                    for other in still_running:
                        other["proc"].terminate()
                    pending.clear()
        running = still_running

        write_aggregate(args, jobs)
        if not launched:
            time.sleep(args.poll_interval)

    return 1 if failures else 0


def parse_args():
    gem5_dir, workspace_dir, gapbs_dir = repo_default_paths()
    parser = argparse.ArgumentParser(
        description="Run GAPBS native/CXL/DSM-TEE multi-host batch experiments."
    )
    parser.add_argument("--gem5-dir", type=Path, default=gem5_dir)
    parser.add_argument("--gapbs-dir", type=Path, default=gapbs_dir)
    parser.add_argument(
        "--gem5-bin", type=Path, default=gem5_dir / "build/X86/gem5.opt"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=gem5_dir / "configs/example/multihost_se.py",
    )
    parser.add_argument(
        "--result-root", type=Path, default=workspace_dir / "results"
    )
    parser.add_argument("--label-prefix", default="gapbs_s24_d20_multivariant")
    parser.add_argument("--benchmarks", default="bfs,pr,sssp,cc,bc")
    parser.add_argument("--variants", default="native,cxl,dsmtee")
    parser.add_argument("--host-counts", default="1,2,4,8")
    parser.add_argument("--cores-per-host", type=int, default=2)
    parser.add_argument("--scale", type=int, default=24)
    parser.add_argument(
        "--degree",
        type=int,
        default=20,
        help="Default is 20. Use --degree 2 if the intended graph degree is 2.",
    )
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--root", type=int, default=0)
    parser.add_argument("--gapbs-args", default="")
    parser.add_argument("--native-gapbs-args", default="")
    parser.add_argument("--cxl-gapbs-args", default="")
    parser.add_argument("--dsmtee-gapbs-args", default="")
    parser.add_argument("--cpu", choices=["timing", "o3"], default="timing")
    parser.add_argument(
        "--fast-forward-to-roi",
        action="store_true",
        help=(
            "Run graph generation/build with AtomicSimpleCPU and switch to "
            "--cpu at GAPBS m5_work_begin. The workload env will include "
            "GAPBS_M5_ROI=1."
        ),
    )
    parser.add_argument(
        "--roi-continue-after-workend",
        action="store_true",
        help="Continue the workload after dumping ROI stats instead of exiting at m5_work_end.",
    )
    parser.add_argument(
        "--roi-maxinsts",
        type=int,
        default=0,
        help=(
            "When used with --fast-forward-to-roi, stop after any ROI CPU "
            "thread commits this many instructions. 0 disables the limit."
        ),
    )
    parser.add_argument(
        "--mem",
        choices=["simple", "ddr3", "ddr5-4400", "ddr5-6400"],
        default="ddr5-6400",
    )
    parser.add_argument("--sys-clock", default="4GHz")
    parser.add_argument("--cpu-clock", default="4GHz")
    parser.add_argument(
        "--native-host-mem-size",
        default="auto",
        help="Per-host private memory for native/no-CXL runs; auto estimates from graph size.",
    )
    parser.add_argument(
        "--cxl-host-mem-size",
        default="auto",
        help="Per-host private memory for CXL/DSM-TEE runs; auto estimates construction temporaries.",
    )
    parser.add_argument(
        "--cxl-region-size",
        default="auto",
        help="Shared graph mmap size. auto estimates per benchmark; accepts gem5-style sizes.",
    )
    parser.add_argument(
        "--cxl-mem-size",
        default="auto",
        help="Total shared CXL range. auto = cxl_region + metadata + margin.",
    )
    parser.add_argument("--cxl-mem-margin", default="1GiB")
    parser.add_argument(
        "--cxl-mem-type",
        choices=["simple", "ddr5-4400", "ddr5-6400"],
        default="ddr5-6400",
    )
    parser.add_argument("--cxl-latency", default=PAPER_CXL_DDR5_READ_LATENCY)
    parser.add_argument("--cxl-bandwidth", default=PAPER_CXL_BANDWIDTH)
    parser.add_argument(
        "--cxl-link-delay", default=PAPER_CXL_LINK_ONE_WAY_DELAY
    )
    parser.add_argument("--cxl-link-read-req-delay")
    parser.add_argument("--cxl-link-read-resp-delay")
    parser.add_argument("--cxl-link-write-req-delay")
    parser.add_argument("--cxl-link-write-resp-delay")
    parser.add_argument("--dsm-tee-num-vmids", type=int)
    parser.add_argument("--dsm-tee-metadata-size", default="auto")
    parser.add_argument("--dsm-tee-perm-cache-entries", type=int, default=256)
    parser.add_argument("--dsm-tee-perm-check-cycles", type=int, default=8)
    parser.add_argument(
        "--dsm-tee-perm-cache-access-cycles", type=int, default=30
    )
    parser.add_argument("--dsm-tee-perm-cache-hit-latency", default="0ns")
    parser.add_argument("--dsm-tee-perm-cache-miss-latency", default="0ns")
    parser.add_argument("--dsm-tee-ide-req-delay", default="0ns")
    parser.add_argument("--dsm-tee-ide-resp-delay", default="0ns")
    parser.add_argument("--dsm-tee-ide-req-cycles", type=int, default=1)
    parser.add_argument("--dsm-tee-ide-resp-cycles", type=int, default=1)
    parser.add_argument("--dsm-tee-encrypt-read-delay", default="10ns")
    parser.add_argument("--dsm-tee-encrypt-write-delay", default="10ns")
    parser.add_argument("--dsm-tee-no-metadata-packets", action="store_true")
    parser.add_argument("--dsm-tee-warn-only", action="store_true")
    parser.add_argument(
        "--allocator-verbose", action="store_true", default=True
    )
    parser.add_argument(
        "--quiet-allocator", action="store_false", dest="allocator_verbose"
    )
    parser.add_argument(
        "--strict-allocator", action="store_true", default=True
    )
    parser.add_argument(
        "--no-strict-allocator", action="store_false", dest="strict_allocator"
    )
    parser.add_argument(
        "--max-parallel",
        default="auto",
        help="Maximum concurrent gem5 processes. auto uses min(4, nproc/4).",
    )
    parser.add_argument(
        "--mem-per-job",
        default="auto",
        help="Override RSS estimate used by scheduler, e.g. 20GiB.",
    )
    parser.add_argument("--reserve-mem", default="8GiB")
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--resume", action="store_true", default=True)
    parser.add_argument("--no-resume", action="store_false", dest="resume")
    parser.add_argument("--stop-on-fail", action="store_true")
    parser.add_argument("--no-plot", action="store_true")
    parser.add_argument(
        "--prepend-ld-library-path",
        default="/home/yyang460/miniconda3/lib",
        help="Directory prepended to LD_LIBRARY_PATH before launching gem5.",
    )
    args = parser.parse_args()

    args.benchmarks = parse_csv(args.benchmarks)
    args.variants = parse_csv(args.variants)
    args.host_counts = parse_csv(args.host_counts, int)
    valid_variants = {"native", "cxl", "dsmtee"}
    invalid = [
        variant for variant in args.variants if variant not in valid_variants
    ]
    if invalid:
        raise ValueError(f"invalid variants: {invalid}")
    if args.cores_per_host <= 0:
        raise ValueError("--cores-per-host must be positive")
    if any(hosts <= 0 for hosts in args.host_counts):
        raise ValueError("--host-counts must be positive")
    if args.roi_maxinsts < 0:
        raise ValueError("--roi-maxinsts must be >= 0")
    if args.roi_maxinsts and not args.fast_forward_to_roi:
        raise ValueError("--roi-maxinsts requires --fast-forward-to-roi")

    args.result_root.mkdir(parents=True, exist_ok=True)
    args.cxl_mem_margin_bytes = parse_size_to_bytes(args.cxl_mem_margin)
    args.reserve_mem_bytes = parse_size_to_bytes(args.reserve_mem)
    if args.max_parallel == "auto":
        args.max_parallel = max(1, min(4, (os.cpu_count() or 4) // 4))
    else:
        args.max_parallel = int(args.max_parallel)
        if args.max_parallel <= 0:
            raise ValueError("--max-parallel must be positive")

    if not args.gem5_bin.exists():
        raise FileNotFoundError(f"missing gem5 binary: {args.gem5_bin}")
    if not args.config.exists():
        raise FileNotFoundError(f"missing gem5 config: {args.config}")
    for benchmark in args.benchmarks:
        binary = args.gapbs_dir / benchmark
        if not binary.exists():
            raise FileNotFoundError(
                f"missing GAPBS binary for {benchmark}: {binary}"
            )

    return args


def main():
    args = parse_args()
    jobs = [
        build_job(args, benchmark, variant, host_count)
        for benchmark in args.benchmarks
        for host_count in args.host_counts
        for variant in args.variants
    ]

    for job in jobs:
        prepare_job(job, clean=args.clean and args.dry_run)

    plan_path = args.result_root / f"{args.label_prefix}_plan.csv"
    with plan_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "benchmark",
                "variant",
                "host_count",
                "threads",
                "fast_forward_to_roi",
                "roi_maxinsts",
                "host_mem",
                "cxl_region",
                "cxl_mem",
                "metadata",
                "estimated_rss",
                "out_dir",
                "command",
            ]
        )
        for job in jobs:
            writer.writerow(
                [
                    job["benchmark"],
                    job["variant"],
                    job["host_count"],
                    job["threads"],
                    job["fast_forward_to_roi"],
                    job["roi_maxinsts"],
                    format_gem5_size(job["host_mem_bytes"]),
                    format_gem5_size(job["cxl_region_bytes"]),
                    format_gem5_size(job["cxl_mem_bytes"]),
                    format_gem5_size(job["metadata_bytes"]),
                    format_gem5_size(job["estimated_rss_bytes"]),
                    job["out_dir"],
                    shlex.join(str(item) for item in job["cmd"]),
                ]
            )

    print(f"jobs: {len(jobs)}")
    print(f"plan: {plan_path}")
    print(
        f"max_parallel: {args.max_parallel}, reserve_mem={format_gem5_size(args.reserve_mem_bytes)}"
    )
    if args.dry_run:
        write_aggregate(args, jobs)
        return 0

    rc = run_jobs(args, jobs)
    agg_csv, agg_png = write_aggregate(args, jobs)
    print(f"aggregate: {agg_csv}")
    if agg_png:
        print(f"plot: {agg_png}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
