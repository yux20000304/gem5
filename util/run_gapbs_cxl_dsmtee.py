#!/usr/bin/env python3
#
# Run GAPBS on the pseudo multi-host SE CXL setup and summarize baseline vs
# DSM-TEE cycle costs.

import argparse
import csv
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

STAT_RE = re.compile(
    r"^(?P<name>\S+)\s+(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
)

PAPER_CXL_DDR5_READ_LATENCY = "42ns"
PAPER_CXL_LINK_ONE_WAY_DELAY = "35ns"
PAPER_CXL_BANDWIDTH = "25.6GB/s"


def repo_default_paths():
    gem5_dir = Path(__file__).resolve().parents[1]
    workspace_dir = gem5_dir.parent
    return gem5_dir, workspace_dir, workspace_dir / "gapbs"


def parse_size_to_bytes(raw):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*", raw)
    if not match:
        raise ValueError(f"Invalid size: {raw}")
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
    }
    if unit not in units:
        raise ValueError(f"Unsupported size unit in {raw}")
    return int(value * units[unit])


def parse_duration_seconds(raw):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]+)\s*", raw)
    if not match:
        raise ValueError(f"Invalid duration: {raw}")
    value = float(match.group(1))
    unit = match.group(2).lower()
    units = {
        "ps": 1e-12,
        "ns": 1e-9,
        "us": 1e-6,
        "ms": 1e-3,
        "s": 1.0,
    }
    if unit not in units:
        raise ValueError(f"Unsupported duration unit in {raw}")
    return value * units[unit]


def parse_frequency_hz(raw):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]+)\s*", raw)
    if not match:
        raise ValueError(f"Invalid frequency: {raw}")
    value = float(match.group(1))
    unit = match.group(2).lower()
    units = {
        "hz": 1,
        "khz": 1e3,
        "mhz": 1e6,
        "ghz": 1e9,
        "thz": 1e12,
    }
    if unit not in units:
        raise ValueError(f"Unsupported frequency unit in {raw}")
    return value * units[unit]


def duration_to_cycles(duration, cpu_clock):
    return parse_duration_seconds(duration) * parse_frequency_hz(cpu_clock)


def csv_join(paths):
    return ";".join(str(path) for path in paths)


def split_host_cores(raw):
    values = [int(item.strip()) for item in raw.split(",") if item.strip()]
    if not values or any(value <= 0 for value in values):
        raise ValueError("--host-cores must contain positive integers")
    return values


def read_stats(path):
    stats = {}
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            match = STAT_RE.match(line)
            if not match:
                continue
            value_raw = match.group("value")
            value = float(value_raw)
            if value.is_integer():
                value = int(value)
            stats[match.group("name")] = value
    return stats


def stat(stats, name, default=0):
    return stats.get(name, default)


def stat_any(stats, names, default=0):
    for name in names:
        if name in stats:
            return stats[name]
    return default


def read_cpu_cycles(stats):
    cpus = {}
    for name, value in stats.items():
        match = re.fullmatch(r"system\.cpu(\d+)\.numCycles", name)
        if match:
            cpus[int(match.group(1))] = int(value)
    return cpus


def sim_cycles(stats, cpu_clock):
    sim_ticks = stat(stats, "simTicks", 0)
    sim_freq = stat(stats, "simFreq", 1_000_000_000_000)
    clock_ticks = stat(stats, "system.cpu_clk_domain.clock", None)
    if clock_ticks is None:
        clock_ticks = stat(stats, "system.clk_domain.clock", None)
    if clock_ticks:
        return sim_ticks / clock_ticks
    return sim_ticks / (sim_freq / parse_frequency_hz(cpu_clock))


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


def write_env_file(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def command_string(args, gapbs_bin):
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
    return shlex.join(parts)


def variant_prefix(name, cpu):
    return f"{name}_{cpu}"


def make_variant_paths(out_dir, name, cpu, processes):
    prefix = variant_prefix(name, cpu)
    m5out = out_dir / f"m5out_{prefix}"
    if processes == 1:
        stdout = [out_dir / f"{prefix}.program.out"]
        stderr = [out_dir / f"{prefix}.program.err"]
    else:
        stdout = [
            out_dir / f"{prefix}.process{idx}.program.out"
            for idx in range(processes)
        ]
        stderr = [
            out_dir / f"{prefix}.process{idx}.program.err"
            for idx in range(processes)
        ]
    return {
        "prefix": prefix,
        "m5out": m5out,
        "stdout": stdout,
        "stderr": stderr,
        "stats": m5out / "stats.txt",
    }


def make_gem5_cmd(args, variant, env_file, workload_cmd, extra_args):
    stdout_arg = csv_join(variant["stdout"])
    stderr_arg = csv_join(variant["stderr"])
    cmd = [
        str(args.gem5_bin),
        f"--outdir={variant['m5out']}",
        str(args.config),
        f"--mode={args.mode}",
        f"--host-cores={args.host_cores}",
        f"--cpu={args.cpu}",
        f"--cmd={workload_cmd}",
        f"--cwd={args.gapbs_dir}",
        f"--env-file={env_file}",
        f"--stdout={stdout_arg}",
        f"--stderr={stderr_arg}",
        f"--mem={args.mem}",
        f"--mem-size={args.mem_size}",
        f"--sys-clock={args.sys_clock}",
        f"--cpu-clock={args.cpu_clock}",
        f"--cxl-mem-size={args.cxl_mem_size}",
        f"--cxl-mem-type={args.cxl_mem_type}",
        f"--cxl-latency={args.cxl_latency}",
        f"--cxl-bandwidth={args.cxl_bandwidth}",
        f"--cxl-link-delay={args.cxl_link_delay}",
    ]
    if args.cxl_link_read_req_delay:
        cmd.append(f"--cxl-link-read-req-delay={args.cxl_link_read_req_delay}")
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
    if args.host_mem_size:
        cmd.append(f"--host-mem-size={args.host_mem_size}")
    cmd.extend(extra_args)
    return cmd


def build_variant_commands(args, out_dir, workload_cmd, process_count):
    selected = []
    if args.only in ("both", "baseline"):
        selected.append("baseline")
    if args.only in ("both", "dsm-tee"):
        selected.append("dsm_tee")

    variants = {}
    if "baseline" in selected:
        baseline_env = out_dir / "baseline.env"
        write_env_file(
            baseline_env,
            [
                "GAPBS_CXL_GRAPH=1",
                f"GAPBS_CXL_SIZE={args.cxl_region_size}",
                "GAPBS_CXL_STRICT=1",
                f"GAPBS_CXL_VERBOSE={int(args.allocator_verbose)}",
                f"OMP_NUM_THREADS={args.threads}",
            ],
        )
        baseline = make_variant_paths(
            out_dir, "baseline", args.cpu, process_count
        )
        baseline["cmd"] = make_gem5_cmd(
            args, baseline, baseline_env, workload_cmd, []
        )
        baseline["program_out"] = baseline["stdout"][0]
        variants["baseline"] = baseline

    if "dsm_tee" in selected:
        dsm_env = out_dir / "dsm_tee.env"
        write_env_file(
            dsm_env,
            [
                "GAPBS_CXL_GRAPH=1",
                "GAPBS_CXL_PATH=/dev/gem5_dsm_tee",
                f"GAPBS_CXL_SIZE={args.cxl_region_size}",
                "GAPBS_CXL_STRICT=1",
                f"GAPBS_CXL_VERBOSE={int(args.allocator_verbose)}",
                "GAPBS_DSMTEE_GRAPH=1",
                "GAPBS_DSMTEE_PATH=/dev/gem5_dsm_tee",
                f"GAPBS_DSMTEE_SIZE={args.cxl_region_size}",
                "GAPBS_DSMTEE_STRICT=1",
                f"GAPBS_DSMTEE_VERBOSE={int(args.allocator_verbose)}",
                f"OMP_NUM_THREADS={args.threads}",
            ],
        )
        extra = [
            "--enable-dsm-tee",
            "--dsm-tee-data-path",
            f"--dsm-tee-metadata-size={args.dsm_tee_metadata_size}",
            f"--dsm-tee-perm-cache-entries={args.dsm_tee_perm_cache_entries}",
            f"--dsm-tee-perm-check-cycles={args.dsm_tee_perm_check_cycles}",
            (
                "--dsm-tee-perm-cache-access-cycles="
                f"{args.dsm_tee_perm_cache_access_cycles}"
            ),
            f"--dsm-tee-perm-cache-hit-latency={args.dsm_tee_perm_cache_hit_latency}",
            (
                "--dsm-tee-perm-cache-miss-latency="
                f"{args.dsm_tee_perm_cache_miss_latency}"
            ),
            f"--dsm-tee-ide-req-delay={args.dsm_tee_ide_req_delay}",
            f"--dsm-tee-ide-resp-delay={args.dsm_tee_ide_resp_delay}",
            f"--dsm-tee-ide-req-cycles={args.dsm_tee_ide_req_cycles}",
            f"--dsm-tee-ide-resp-cycles={args.dsm_tee_ide_resp_cycles}",
            f"--dsm-tee-encrypt-read-delay={args.dsm_tee_encrypt_read_delay}",
            f"--dsm-tee-encrypt-write-delay={args.dsm_tee_encrypt_write_delay}",
        ]
        if args.dsm_tee_metadata_read_latency:
            extra.append(
                "--dsm-tee-metadata-read-latency="
                f"{args.dsm_tee_metadata_read_latency}"
            )
        if args.dsm_tee_no_metadata_packets:
            extra.append("--dsm-tee-no-metadata-packets")
        if args.dsm_tee_warn_only:
            extra.append("--dsm-tee-warn-only")
        if args.dsm_tee_protect_unregistered_cxl:
            extra.append("--dsm-tee-protect-unregistered-cxl")
        if args.dsm_tee_no_auto_grant_all:
            extra.append("--dsm-tee-no-auto-grant-all")
        if args.dsm_tee_no_auto_create_on_mmap:
            extra.append("--dsm-tee-no-auto-create-on-mmap")
        if args.dsm_tee_num_vmids:
            extra.append(f"--dsm-tee-num-vmids={args.dsm_tee_num_vmids}")
        dsm = make_variant_paths(out_dir, "dsm_tee", args.cpu, process_count)
        dsm["cmd"] = make_gem5_cmd(args, dsm, dsm_env, workload_cmd, extra)
        dsm["program_out"] = dsm["stdout"][0]
        variants["dsm_tee"] = dsm

    return variants


def clean_variant(variant):
    if variant["m5out"].exists():
        shutil.rmtree(variant["m5out"])
    for path in variant["stdout"] + variant["stderr"]:
        if path.exists():
            path.unlink()


def run_command(cmd, env, timeout):
    print("+ " + shlex.join(str(item) for item in cmd), flush=True)
    subprocess.run(cmd, check=True, env=env, timeout=timeout)


def load_variant_result(args, variant):
    if not variant["stats"].exists():
        raise FileNotFoundError(f"Missing stats file: {variant['stats']}")
    stats = read_stats(variant["stats"])
    program = parse_program_output(variant["program_out"])
    cpu_cycles = read_cpu_cycles(stats)
    return {
        "stats": stats,
        "program": program,
        "cpu_cycles": cpu_cycles,
        "sim_cycles": sim_cycles(stats, args.cpu_clock),
        "max_cpu_cycles": max(cpu_cycles.values()) if cpu_cycles else "",
    }


def fnum(value):
    if value == "":
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def row(rows, metric, baseline="", dsm_tee="", unit=""):
    rows.append([metric, fnum(baseline), fnum(dsm_tee), unit])


def cycles_from_ticks(stats, ticks, args):
    sim_freq = stat(stats, "simFreq", 1_000_000_000_000)
    seconds = ticks / sim_freq
    return seconds * parse_frequency_hz(args.cpu_clock)


def dsm_overhead_rows(args, dsm_result):
    stats = dsm_result["stats"]
    permission_checks = stat(stats, "system.dsm_tee_ctrl.permissionChecks")
    permission_hits = stat(stats, "system.dsm_tee_ctrl.permissionCacheHits")
    permission_misses = stat(
        stats, "system.dsm_tee_ctrl.permissionCacheMisses"
    )
    metadata_reads = stat(stats, "system.dsm_tee_ctrl.metadataReads")
    dsm_requests = stat(stats, "system.dsm_tee_ctrl.dsmRequests")
    read_responses = stat(stats, "system.dsm_tee_ctrl.readResponses")
    write_responses = stat(stats, "system.dsm_tee_ctrl.writeResponses")
    responses = read_responses + write_responses

    read_encrypt_cycles = duration_to_cycles(
        args.dsm_tee_encrypt_read_delay, args.cpu_clock
    )
    write_encrypt_cycles = duration_to_cycles(
        args.dsm_tee_encrypt_write_delay, args.cpu_clock
    )
    metadata_latency = (
        args.dsm_tee_metadata_read_latency
        if args.dsm_tee_metadata_read_latency
        else args.cxl_latency
    )
    metadata_cycles = (
        duration_to_cycles(metadata_latency, args.cpu_clock)
        if args.dsm_tee_no_metadata_packets
        else 0
    )
    metadata_formula = (
        f"metadataReads * {metadata_latency} * {args.cpu_clock}"
        if args.dsm_tee_no_metadata_packets
        else "metadataReads issue real 64B CXL MemCtrl reads"
    )
    perm_hit_cycles = duration_to_cycles(
        args.dsm_tee_perm_cache_hit_latency, args.cpu_clock
    )
    perm_miss_cycles = duration_to_cycles(
        args.dsm_tee_perm_cache_miss_latency, args.cpu_clock
    )
    ide_req_delay_cycles = duration_to_cycles(
        args.dsm_tee_ide_req_delay, args.cpu_clock
    )
    ide_resp_delay_cycles = duration_to_cycles(
        args.dsm_tee_ide_resp_delay, args.cpu_clock
    )

    components = [
        (
            "permission_check",
            permission_checks * args.dsm_tee_perm_check_cycles,
            permission_checks,
            args.dsm_tee_perm_check_cycles,
            f"permissionChecks * {args.dsm_tee_perm_check_cycles}cy",
        ),
        (
            "permission_cache_access",
            permission_checks * args.dsm_tee_perm_cache_access_cycles,
            permission_checks,
            args.dsm_tee_perm_cache_access_cycles,
            (
                "permissionChecks * "
                f"{args.dsm_tee_perm_cache_access_cycles}cy"
            ),
        ),
        (
            "permission_cache_hit_extra",
            permission_hits * perm_hit_cycles,
            permission_hits,
            perm_hit_cycles,
            (
                "permissionCacheHits * "
                f"{args.dsm_tee_perm_cache_hit_latency} * {args.cpu_clock}"
            ),
        ),
        (
            "metadata_read_on_cache_miss",
            metadata_reads * metadata_cycles,
            metadata_reads,
            metadata_cycles,
            metadata_formula,
        ),
        (
            "permission_cache_miss_extra",
            permission_misses * perm_miss_cycles,
            permission_misses,
            perm_miss_cycles,
            (
                "permissionCacheMisses * "
                f"{args.dsm_tee_perm_cache_miss_latency} * {args.cpu_clock}"
            ),
        ),
        (
            "ide_request_processing",
            dsm_requests * args.dsm_tee_ide_req_cycles,
            dsm_requests,
            args.dsm_tee_ide_req_cycles,
            f"dsmRequests * {args.dsm_tee_ide_req_cycles}cy",
        ),
        (
            "ide_request_absolute_delay",
            dsm_requests * ide_req_delay_cycles,
            dsm_requests,
            ide_req_delay_cycles,
            f"dsmRequests * {args.dsm_tee_ide_req_delay} * {args.cpu_clock}",
        ),
        (
            "ide_response_processing",
            responses * args.dsm_tee_ide_resp_cycles,
            responses,
            args.dsm_tee_ide_resp_cycles,
            (
                "(readResponses + writeResponses) * "
                f"{args.dsm_tee_ide_resp_cycles}cy"
            ),
        ),
        (
            "ide_response_absolute_delay",
            responses * ide_resp_delay_cycles,
            responses,
            ide_resp_delay_cycles,
            (
                "(readResponses + writeResponses) * "
                f"{args.dsm_tee_ide_resp_delay} * {args.cpu_clock}"
            ),
        ),
        (
            "read_decrypt_authenticate",
            read_responses * read_encrypt_cycles,
            read_responses,
            read_encrypt_cycles,
            (
                "readResponses * "
                f"{args.dsm_tee_encrypt_read_delay} * {args.cpu_clock}"
            ),
        ),
        (
            "write_encrypt_authenticate",
            write_responses * write_encrypt_cycles,
            write_responses,
            write_encrypt_cycles,
            (
                "writeResponses * "
                f"{args.dsm_tee_encrypt_write_delay} * {args.cpu_clock}"
            ),
        ),
    ]
    return components


def write_outputs(args, out_dir, workload_cmd, results):
    summary_path = out_dir / "summary_cycles.csv"
    overhead_path = out_dir / "dsm_tee_overhead_cycles.csv"
    cpu_path = out_dir / "per_cpu_cycles.csv"

    baseline = results.get("baseline")
    dsm = results.get("dsm_tee")

    rows = []
    row(rows, "workload", workload_cmd, workload_cmd)
    row(rows, "cpu_model", args.cpu, args.cpu)
    row(rows, "cpu_clock", args.cpu_clock, args.cpu_clock)
    row(rows, "host_cores", args.host_cores, args.host_cores)
    row(rows, "omp_threads", args.threads, args.threads)
    row(rows, "host_mem_type", args.mem, args.mem)
    row(rows, "cxl_mem_size", args.cxl_mem_size, args.cxl_mem_size)
    row(rows, "cxl_region_size", args.cxl_region_size, args.cxl_region_size)
    row(rows, "cxl_mem_type", args.cxl_mem_type, args.cxl_mem_type)
    row(rows, "cxl_latency", args.cxl_latency, args.cxl_latency)
    row(rows, "cxl_link_delay", args.cxl_link_delay, args.cxl_link_delay)
    row(
        rows,
        "dsm_tee_metadata_packets",
        "false" if baseline else "",
        str(not args.dsm_tee_no_metadata_packets).lower() if dsm else "",
    )
    row(
        rows,
        "cxl_link_read_delay",
        f"{args.cxl_link_read_req_delay or args.cxl_link_delay}+"
        f"{args.cxl_link_read_resp_delay or args.cxl_link_delay}",
        f"{args.cxl_link_read_req_delay or args.cxl_link_delay}+"
        f"{args.cxl_link_read_resp_delay or args.cxl_link_delay}",
    )
    row(
        rows,
        "cxl_link_write_delay",
        f"{args.cxl_link_write_req_delay or args.cxl_link_delay}+"
        f"{args.cxl_link_write_resp_delay or args.cxl_link_delay}",
        f"{args.cxl_link_write_req_delay or args.cxl_link_delay}+"
        f"{args.cxl_link_write_resp_delay or args.cxl_link_delay}",
    )

    def bstat(name):
        return stat(baseline["stats"], name) if baseline else ""

    def dstat(name):
        return stat(dsm["stats"], name) if dsm else ""

    def bcxl(names):
        return stat_any(baseline["stats"], names) if baseline else ""

    def dcxl(names):
        return stat_any(dsm["stats"], names) if dsm else ""

    row(rows, "sim_ticks", bstat("simTicks"), dstat("simTicks"), "tick")
    row(
        rows,
        "sim_cycles",
        baseline["sim_cycles"] if baseline else "",
        dsm["sim_cycles"] if dsm else "",
        "cycle",
    )
    row(
        rows,
        "max_cpu_cycles",
        baseline["max_cpu_cycles"] if baseline else "",
        dsm["max_cpu_cycles"] if dsm else "",
        "cycle",
    )
    observed = ""
    if baseline and dsm:
        observed = dsm["sim_cycles"] - baseline["sim_cycles"]
    row(rows, "observed_dsm_minus_baseline", "", observed, "cycle")

    modeled_sum = ""
    if dsm:
        components = dsm_overhead_rows(args, dsm)
        modeled_sum = sum(component[1] for component in components)
    else:
        components = []
    row(rows, "modeled_dsm_tee_extra_sum", "", modeled_sum, "cycle")

    if dsm:
        req_delay_cycles = cycles_from_ticks(
            dsm["stats"],
            stat(dsm["stats"], "system.dsm_tee_ctrl.totalReqDelay"),
            args,
        )
        resp_delay_cycles = cycles_from_ticks(
            dsm["stats"],
            stat(dsm["stats"], "system.dsm_tee_ctrl.totalRespDelay"),
            args,
        )
    else:
        req_delay_cycles = ""
        resp_delay_cycles = ""
    row(rows, "modeled_dsm_tee_request_delay", "", req_delay_cycles, "cycle")
    row(rows, "modeled_dsm_tee_response_delay", "", resp_delay_cycles, "cycle")

    row(
        rows,
        "cxl_mem_reads",
        bcxl(
            (
                "system.cxl_mem_ctrl.numReads::total",
                "system.cxl_mem_ctrl.readReqs",
            )
        ),
        dcxl(
            (
                "system.cxl_mem_ctrl.numReads::total",
                "system.cxl_mem_ctrl.readReqs",
            )
        ),
        "request",
    )
    row(
        rows,
        "cxl_mem_writes",
        bcxl(
            (
                "system.cxl_mem_ctrl.numWrites::total",
                "system.cxl_mem_ctrl.writeReqs",
            )
        ),
        dcxl(
            (
                "system.cxl_mem_ctrl.numWrites::total",
                "system.cxl_mem_ctrl.writeReqs",
            )
        ),
        "request",
    )
    row(
        rows,
        "cxl_mem_bytes_read",
        bcxl(
            (
                "system.cxl_mem_ctrl.bytesRead::total",
                "system.cxl_mem_ctrl.bytesReadSys",
            )
        ),
        dcxl(
            (
                "system.cxl_mem_ctrl.bytesRead::total",
                "system.cxl_mem_ctrl.bytesReadSys",
            )
        ),
        "byte",
    )
    row(
        rows,
        "cxl_mem_bytes_written",
        bcxl(
            (
                "system.cxl_mem_ctrl.bytesWritten::total",
                "system.cxl_mem_ctrl.bytesWrittenSys",
            )
        ),
        dcxl(
            (
                "system.cxl_mem_ctrl.bytesWritten::total",
                "system.cxl_mem_ctrl.bytesWrittenSys",
            )
        ),
        "byte",
    )
    row(
        rows,
        "permission_checks",
        0 if baseline else "",
        dstat("system.dsm_tee_ctrl.permissionChecks"),
        "count",
    )
    row(
        rows,
        "permission_cache_hits",
        0 if baseline else "",
        dstat("system.dsm_tee_ctrl.permissionCacheHits"),
        "count",
    )
    row(
        rows,
        "permission_cache_misses",
        0 if baseline else "",
        dstat("system.dsm_tee_ctrl.permissionCacheMisses"),
        "count",
    )
    row(
        rows,
        "permission_denied",
        0 if baseline else "",
        dstat("system.dsm_tee_ctrl.permissionDenied"),
        "count",
    )
    row(
        rows,
        "metadata_reads",
        0 if baseline else "",
        dstat("system.dsm_tee_ctrl.metadataReads"),
        "count",
    )
    row(
        rows,
        "metadata_read_bytes",
        0 if baseline else "",
        dstat("system.dsm_tee_ctrl.metadataReadBytes"),
        "byte",
    )
    row(
        rows,
        "metadata_reserved",
        0 if baseline else "",
        dstat("system.dsm_tee_mem_driver.metadataReservedBytes"),
        "byte",
    )
    row(
        rows,
        "metadata_allocated",
        0 if baseline else "",
        dstat("system.dsm_tee_mem_driver.metadataAllocatedBytes"),
        "byte",
    )
    row(
        rows,
        "dsm_regions_created",
        0 if baseline else "",
        dstat("system.dsm_tee_mem_driver.regionsCreated"),
        "count",
    )

    for metric, key in (
        ("gapbs_generate_time", "generate_time"),
        ("gapbs_build_time", "build_time"),
        ("gapbs_trial_time", "trial_time"),
        ("gapbs_average_time", "average_time"),
        ("gapbs_graph_nodes", "graph_nodes"),
        ("gapbs_graph_edges", "graph_edges"),
        ("gapbs_graph_degree", "graph_degree"),
        ("gapbs_allocator_line", "allocator_line"),
    ):
        row(
            rows,
            metric,
            baseline["program"].get(key, "") if baseline else "",
            dsm["program"].get(key, "") if dsm else "",
            "s" if key.endswith("_time") else "",
        )

    with summary_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["metric", "baseline", "dsm_tee", "unit"])
        writer.writerows(rows)

    if dsm:
        with overhead_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                ["component", "cycles", "count", "cycles_per_event", "formula"]
            )
            for component in components:
                writer.writerow(
                    [
                        component[0],
                        fnum(component[1]),
                        component[2],
                        fnum(component[3]),
                        component[4],
                    ]
                )
            writer.writerow(
                [
                    "modeled_dsm_tee_extra_sum",
                    fnum(modeled_sum),
                    "",
                    "",
                    "sum of rows above",
                ]
            )
            writer.writerow(
                [
                    "observed_total_runtime_overhead",
                    fnum(observed),
                    "",
                    "",
                    "dsm simCycles - baseline simCycles",
                ]
            )

    cpu_ids = set()
    if baseline:
        cpu_ids.update(baseline["cpu_cycles"].keys())
    if dsm:
        cpu_ids.update(dsm["cpu_cycles"].keys())
    with cpu_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["cpu_id", "baseline_cycles", "dsm_tee_cycles", "delta_cycles"]
        )
        for cpu_id in sorted(cpu_ids):
            bval = baseline["cpu_cycles"].get(cpu_id, "") if baseline else ""
            dval = dsm["cpu_cycles"].get(cpu_id, "") if dsm else ""
            delta = dval - bval if bval != "" and dval != "" else ""
            writer.writerow([cpu_id, bval, dval, delta])

    plot_path = None
    if not args.no_plot:
        plot_path = write_plot(args, out_dir, baseline, dsm, components)
    return summary_path, overhead_path if dsm else None, cpu_path, plot_path


def write_plot(args, out_dir, baseline, dsm, components):
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        print(
            f"warning: matplotlib unavailable, skip plot: {exc}",
            file=sys.stderr,
        )
        return None

    plot_path = out_dir / f"{args.label}_cycles.png"
    if baseline and dsm:
        fig, axes = plt.subplots(1, 2, figsize=(12, 4))
    else:
        fig, axes = plt.subplots(1, 1, figsize=(7, 4))
        axes = [axes]

    labels = []
    values = []
    if baseline:
        labels.append("baseline")
        values.append(baseline["sim_cycles"])
    if dsm:
        labels.append("dsm-tee")
        values.append(dsm["sim_cycles"])
    axes[0].bar(labels, values, color=["#4C78A8", "#F58518"][: len(labels)])
    axes[0].set_ylabel("cycles")
    axes[0].set_title(f"GAPBS BFS scale={args.scale}, degree={args.degree}")
    axes[0].ticklabel_format(axis="y", style="sci", scilimits=(0, 0))

    if baseline and dsm:
        nonzero = [
            (name, cycles) for name, cycles, _, _, _ in components if cycles
        ]
        if nonzero:
            names, comp_cycles = zip(*nonzero)
            axes[1].barh(names, comp_cycles, color="#54A24B")
            axes[1].set_xlabel("cycles")
            axes[1].set_title("DSM-TEE modeled overhead")
            axes[1].ticklabel_format(axis="x", style="sci", scilimits=(0, 0))
        else:
            axes[1].text(0.5, 0.5, "no DSM-TEE overhead stats", ha="center")
            axes[1].set_axis_off()

    fig.tight_layout()
    fig.savefig(plot_path, dpi=180)
    plt.close(fig)
    return plot_path


def parse_args():
    gem5_dir, workspace_dir, gapbs_dir = repo_default_paths()
    parser = argparse.ArgumentParser(
        description=(
            "Run GAPBS BFS on gem5 multi-host SE CXL shared memory and compare "
            "native CXL baseline against DSM-TEE data-path mode."
        )
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
    parser.add_argument("--benchmark", default="bfs")
    parser.add_argument("--scale", type=int, default=10)
    parser.add_argument("--degree", type=int, default=10)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--root", type=int, default=0)
    parser.add_argument(
        "--gapbs-args",
        default="",
        help="Extra command-line arguments appended to the GAPBS binary.",
    )
    parser.add_argument("--host-cores", default="2,2")
    parser.add_argument("--threads", type=int)
    parser.add_argument(
        "--mode",
        choices=["cross-host-threaded", "per-host-process"],
        default="cross-host-threaded",
    )
    parser.add_argument("--cpu", choices=["timing", "o3"], default="timing")
    parser.add_argument(
        "--mem",
        choices=["simple", "ddr3", "ddr5-4400", "ddr5-6400"],
        default="ddr5-6400",
    )
    parser.add_argument("--mem-size", default="4GiB")
    parser.add_argument("--host-mem-size")
    parser.add_argument("--sys-clock", default="4GHz")
    parser.add_argument("--cpu-clock", default="4GHz")
    parser.add_argument("--cxl-mem-size", default="512MiB")
    parser.add_argument("--cxl-region-size", default="256M")
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
    parser.add_argument("--dsm-tee-metadata-size", default="8MiB")
    parser.add_argument("--dsm-tee-num-vmids", type=int)
    parser.add_argument("--dsm-tee-perm-cache-entries", type=int, default=256)
    parser.add_argument("--dsm-tee-perm-check-cycles", type=int, default=8)
    parser.add_argument(
        "--dsm-tee-perm-cache-access-cycles", type=int, default=30
    )
    parser.add_argument("--dsm-tee-perm-cache-hit-latency", default="0ns")
    parser.add_argument("--dsm-tee-perm-cache-miss-latency", default="0ns")
    parser.add_argument("--dsm-tee-metadata-read-latency")
    parser.add_argument(
        "--dsm-tee-no-metadata-packets",
        action="store_true",
        help=(
            "Use legacy fixed metadata-read latency instead of real 64B "
            "metadata read packets on permission-cache misses."
        ),
    )
    parser.add_argument("--dsm-tee-ide-req-delay", default="0ns")
    parser.add_argument("--dsm-tee-ide-resp-delay", default="0ns")
    parser.add_argument("--dsm-tee-ide-req-cycles", type=int, default=1)
    parser.add_argument("--dsm-tee-ide-resp-cycles", type=int, default=1)
    parser.add_argument("--dsm-tee-encrypt-read-delay", default="10ns")
    parser.add_argument("--dsm-tee-encrypt-write-delay", default="10ns")
    parser.add_argument("--dsm-tee-warn-only", action="store_true")
    parser.add_argument(
        "--dsm-tee-protect-unregistered-cxl", action="store_true"
    )
    parser.add_argument("--dsm-tee-no-auto-grant-all", action="store_true")
    parser.add_argument(
        "--dsm-tee-no-auto-create-on-mmap", action="store_true"
    )
    parser.add_argument(
        "--only", choices=["both", "baseline", "dsm-tee"], default="both"
    )
    parser.add_argument("--skip-run", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--no-plot", action="store_true")
    parser.add_argument(
        "--allocator-verbose", action="store_true", default=True
    )
    parser.add_argument(
        "--quiet-allocator",
        action="store_false",
        dest="allocator_verbose",
    )
    parser.add_argument(
        "--result-root", type=Path, default=workspace_dir / "results"
    )
    parser.add_argument("--label")
    parser.add_argument(
        "--prepend-ld-library-path",
        default="/home/yyang460/miniconda3/lib",
        help="Directory prepended to LD_LIBRARY_PATH before launching gem5.",
    )
    args = parser.parse_args()

    host_cores = split_host_cores(args.host_cores)
    if args.threads is None:
        args.threads = sum(host_cores)
    if args.threads <= 0:
        raise ValueError("--threads must be positive")
    if args.label is None:
        args.label = f"gapbs_scale{args.scale}_degree{args.degree}"
    args.out_dir = args.result_root / args.label
    args.gapbs_bin = args.gapbs_dir / args.benchmark
    args.host_count = len(host_cores)
    args.process_count = (
        1 if args.mode == "cross-host-threaded" else args.host_count
    )

    # Validate common unit strings early so failures happen before a long run.
    parse_frequency_hz(args.cpu_clock)
    parse_duration_seconds(args.cxl_latency)
    parse_duration_seconds(args.cxl_link_delay)
    for delay in (
        args.cxl_link_read_req_delay,
        args.cxl_link_read_resp_delay,
        args.cxl_link_write_req_delay,
        args.cxl_link_write_resp_delay,
    ):
        if delay:
            parse_duration_seconds(delay)
    parse_duration_seconds(args.dsm_tee_perm_cache_hit_latency)
    parse_duration_seconds(args.dsm_tee_perm_cache_miss_latency)
    parse_duration_seconds(args.dsm_tee_ide_req_delay)
    parse_duration_seconds(args.dsm_tee_ide_resp_delay)
    parse_duration_seconds(args.dsm_tee_encrypt_read_delay)
    parse_duration_seconds(args.dsm_tee_encrypt_write_delay)
    if args.dsm_tee_metadata_read_latency:
        parse_duration_seconds(args.dsm_tee_metadata_read_latency)
    parse_size_to_bytes(args.cxl_mem_size)
    parse_size_to_bytes(args.cxl_region_size)
    parse_size_to_bytes(args.dsm_tee_metadata_size)

    if args.only == "dsm-tee":
        args.only = "dsm-tee"
    return args


def main():
    args = parse_args()

    if not args.skip_run:
        if not args.gem5_bin.exists():
            raise FileNotFoundError(f"Missing gem5 binary: {args.gem5_bin}")
        if not args.config.exists():
            raise FileNotFoundError(f"Missing gem5 config: {args.config}")
        if not args.gapbs_bin.exists():
            raise FileNotFoundError(f"Missing GAPBS binary: {args.gapbs_bin}")

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    workload_cmd = command_string(args, args.gapbs_bin)
    variants = build_variant_commands(
        args, out_dir, workload_cmd, args.process_count
    )

    run_env = os.environ.copy()
    prepend = args.prepend_ld_library_path
    if prepend and Path(prepend).exists():
        old_ld = run_env.get("LD_LIBRARY_PATH", "")
        run_env["LD_LIBRARY_PATH"] = (
            prepend if not old_ld else f"{prepend}:{old_ld}"
        )

    if args.clean:
        for variant in variants.values():
            clean_variant(variant)

    if args.dry_run:
        print(f"output directory: {out_dir}")
        print(f"workload: {workload_cmd}")
        for name, variant in variants.items():
            print(f"\n[{name}]")
            print(shlex.join(str(item) for item in variant["cmd"]))
        return 0

    if not args.skip_run:
        for name, variant in variants.items():
            print(f"\n== Running {name} ==", flush=True)
            run_command(variant["cmd"], run_env, args.timeout)

    results = {}
    for name, variant in variants.items():
        results[name] = load_variant_result(args, variant)

    summary, overhead, cpu_csv, plot = write_outputs(
        args, out_dir, workload_cmd, results
    )
    print(f"\nsummary: {summary}")
    if overhead:
        print(f"overhead: {overhead}")
    print(f"per-cpu cycles: {cpu_csv}")
    if plot:
        print(f"plot: {plot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
