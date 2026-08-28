import argparse
import os
import shlex
import shutil

import m5
from m5.objects import (
    X86O3CPU,
    AddrRange,
    Cache,
    CxlMemoryDriver,
    DDR3_1600_8x8,
    DDR5_4400_4x8,
    DDR5_6400_4x8,
    DsmTeeController,
    DsmTeeMemoryDriver,
    L2XBar,
    MemCtrl,
    Process,
    Root,
    SEWorkload,
    SimpleMemDelay,
    SimpleMemory,
    SrcClockDomain,
    System,
    SystemXBar,
    VoltageDomain,
    X86AtomicSimpleCPU,
    X86TimingSimpleCPU,
)
from m5.simulate import (
    memInvalidate,
    memWriteback,
)
from m5.util.convert import toMemorySize


class L1Cache(Cache):
    assoc = 8
    tag_latency = 1
    data_latency = 1
    response_latency = 1
    mshrs = 16
    tgts_per_mshr = 20


class L1ICache(L1Cache):
    is_read_only = True
    writeback_clean = True


class L1DCache(L1Cache):
    pass


class WalkerCache(Cache):
    assoc = 2
    tag_latency = 2
    data_latency = 2
    response_latency = 2
    mshrs = 10
    tgts_per_mshr = 12
    size = "1KiB"


class LLCache(Cache):
    assoc = 16
    tag_latency = 20
    data_latency = 20
    response_latency = 20
    mshrs = 32
    tgts_per_mshr = 12
    write_buffers = 16

    def connect_cpu_side_bus(self, bus):
        self.cpu_side = bus.mem_side_ports

    def connect_mem_side_bus(self, bus):
        self.mem_side = bus.cpu_side_ports


class FastSimpleMemory(SimpleMemory):
    latency = "1ns"


PAPER_CXL_DDR5_READ_LATENCY = "42ns"
PAPER_CXL_LINK_ONE_WAY_DELAY = "35ns"
PAPER_CXL_BANDWIDTH = "25.6GB/s"
DEFAULT_PROGRESS_INTERVAL_INSTS = 100_000_000
PROGRESS_EXIT_CAUSE = "gem5 progress instruction interval"


CPU_TYPES = {
    "atomic": X86AtomicSimpleCPU,
    "timing": X86TimingSimpleCPU,
    "o3": X86O3CPU,
}


def make_paper_ddr5_4400():
    return DDR5_4400_4x8(
        tRCD="14ns",
        tCL="14ns",
        tRP="14ns",
        tRAS="32ns",
        tWR="30ns",
    )


def make_paper_ddr5_6400():
    return DDR5_6400_4x8(
        tRCD="14ns",
        tCL="14ns",
        tRP="14ns",
        tRAS="32ns",
        tWR="30ns",
    )


PAPER_DDR5_FACTORIES = {
    "ddr5-4400": make_paper_ddr5_4400,
    "ddr5-6400": make_paper_ddr5_6400,
}


class InstructionProgress:
    def __init__(self, interval_insts, core_to_host):
        self.interval_insts = interval_insts
        self.core_to_host = core_to_host
        self.active_cpus = []
        self.phase = "main"
        self.next_targets = {}
        self.start_counts = {}
        self.heartbeat_count = 0

    def enabled(self):
        return self.interval_insts > 0

    def _thread_count(self, cpu):
        return int(getattr(cpu, "numThreads", 1))

    def _thread_key(self, cpu, tid):
        return (id(cpu), tid)

    def _cpu_label(self, cpu, cpu_index, tid):
        try:
            core_id = int(cpu.cpu_id)
        except (AttributeError, TypeError, ValueError):
            core_id = cpu_index
        host_id = (
            self.core_to_host[core_id]
            if 0 <= core_id < len(self.core_to_host)
            else "?"
        )
        return f"h{host_id}c{core_id}t{tid}"

    def arm(self, cpus, phase):
        if not self.enabled():
            return
        self.active_cpus = list(cpus)
        self.phase = phase
        self.next_targets = {}
        self.start_counts = {}
        for cpu_index, cpu in enumerate(self.active_cpus):
            for tid in range(self._thread_count(cpu)):
                current = int(cpu.getCurrentInstCount(tid))
                key = self._thread_key(cpu, tid)
                self.start_counts[key] = current
                self.next_targets[key] = current + self.interval_insts
                cpu.scheduleInstStop(
                    tid, self.interval_insts, PROGRESS_EXIT_CAUSE
                )
        print(
            "[gem5-progress] armed "
            f"phase={self.phase} interval_insts={self.interval_insts} "
            f"cpus={len(self.active_cpus)}",
            flush=True,
        )

    def handle(self):
        if not self.enabled():
            return
        self.heartbeat_count += 1
        phase_committed = 0
        reached = []

        for cpu_index, cpu in enumerate(self.active_cpus):
            for tid in range(self._thread_count(cpu)):
                current = int(cpu.getCurrentInstCount(tid))
                key = self._thread_key(cpu, tid)
                start = self.start_counts.get(key, current)
                phase_committed += max(0, current - start)
                target = self.next_targets.get(key)
                if target is None or current < target:
                    continue

                intervals = ((current - target) // self.interval_insts) + 1
                reached_target = target + (intervals - 1) * self.interval_insts
                next_target = target + intervals * self.interval_insts
                self.next_targets[key] = next_target
                delta = max(1, next_target - current)
                cpu.scheduleInstStop(tid, delta, PROGRESS_EXIT_CAUSE)
                label = self._cpu_label(cpu, cpu_index, tid)
                reached.append(
                    f"{label}:phase_insts={current - start},"
                    f"target={reached_target - start}"
                )

        if reached:
            reached_text = ";".join(reached)
        else:
            reached_text = "stale_progress_event"
        print(
            "[gem5-progress] "
            f"event={self.heartbeat_count} phase={self.phase} "
            f"tick={m5.curTick()} "
            f"phase_committed_insts={phase_committed} "
            f"reached={reached_text}",
            flush=True,
        )


def parse_csv_list(raw, name, cast=str):
    parts = [item.strip() for item in raw.split(",") if item.strip()]
    if not parts:
        raise ValueError(f"{name} must not be empty")
    try:
        return [cast(item) for item in parts]
    except ValueError as exc:
        raise ValueError(f"Invalid value in {name}: {raw}") from exc


def expand_list(values, target_len, name):
    if len(values) == 1:
        return values * target_len
    if len(values) != target_len:
        raise ValueError(
            f"{name} expects either 1 value or {target_len} values, got {len(values)}"
        )
    return values


def parse_semicolon_list(raw):
    if raw is None:
        return []
    return [item.strip() for item in raw.split(";")]


def expand_semicolon_field(raw, target_len, name, default=None):
    values = parse_semicolon_list(raw) if raw is not None else []
    if not values:
        return [default] * target_len
    if len(values) == 1:
        return values * target_len
    if len(values) != target_len:
        raise ValueError(
            f"{name} expects either 1 item or {target_len} items, got {len(values)}"
        )
    return values


def parse_commands(raw, target_count, arg_name):
    command_specs = [item.strip() for item in raw.split(";") if item.strip()]
    if not command_specs:
        raise ValueError(f"{arg_name} must specify at least one command")
    commands = [shlex.split(spec) for spec in command_specs]
    if any(not cmd for cmd in commands):
        raise ValueError(f"{arg_name} contains an empty command")
    if len(commands) == 1:
        return [list(commands[0]) for _ in range(target_count)]
    if len(commands) != target_count:
        raise ValueError(
            f"{arg_name} expects either 1 command or {target_count} commands, got {len(commands)}"
        )
    return commands


def resolve_executable(executable, cwd):
    if os.path.isabs(executable):
        candidate = executable
    else:
        local_candidate = os.path.join(cwd, executable)
        if os.path.isfile(local_candidate):
            candidate = local_candidate
        else:
            found = shutil.which(executable)
            if found is None:
                raise FileNotFoundError(
                    f"Unable to resolve executable '{executable}' from cwd '{cwd}'"
                )
            candidate = found
    if not os.path.isfile(candidate):
        raise FileNotFoundError(f"Executable not found: {candidate}")
    return os.path.abspath(candidate)


def make_process(
    host_id,
    command,
    cwd,
    env_file,
    stdin_path,
    stdout_path,
    stderr_path,
):
    process = Process(pid=100 + host_id)
    process.cwd = cwd

    executable = resolve_executable(command[0], cwd)
    process.executable = executable
    process.cmd = [executable] + command[1:]

    if env_file:
        env_path = os.path.abspath(env_file)
        with open(env_path, encoding="utf-8") as fh:
            process.env = [line.rstrip("\n") for line in fh if line.strip()]

    if stdin_path:
        process.input = os.path.abspath(stdin_path)
    if stdout_path:
        process.output = os.path.abspath(stdout_path)
    if stderr_path:
        process.errout = os.path.abspath(stderr_path)

    return process


parser = argparse.ArgumentParser(
    description=(
        "Run a pseudo multi-host x86 SE simulation. Each host owns one or more "
        "cores and a private LLC, while all hosts still live inside one SE system. "
        "Each host runs one SE process, preserving the original gem5 SE "
        "multithreaded process semantics within that host."
    )
)
parser.add_argument(
    "--host-cores",
    required=True,
    help="Comma-separated core counts per host, e.g. 1,2,4",
)
parser.add_argument(
    "--cmd",
    required=True,
    help=(
        "Semicolon-separated commands. Provide either 1 command to replicate "
        "across all hosts or one command per host."
    ),
)
parser.add_argument(
    "--mode",
    choices=["per-host-process", "cross-host-threaded"],
    default="per-host-process",
    help=(
        "per-host-process runs one SE process per host. cross-host-threaded "
        "runs one SE process whose threads may execute on cores from all hosts."
    ),
)
parser.add_argument(
    "--cpu",
    choices=CPU_TYPES,
    default="o3",
    help="CPU model. Use atomic only for functional smoke tests or fast setup.",
)
parser.add_argument(
    "--fast-forward-to-roi",
    action="store_true",
    help=(
        "Start with X86AtomicSimpleCPU and switch to --cpu at the first "
        "m5_work_begin. Requires the workload to emit workbegin/workend "
        "annotations, e.g. GAPBS_M5_ROI=1."
    ),
)
parser.add_argument(
    "--roi-exit-after-workend",
    action="store_true",
    default=True,
    help="Exit simulation after dumping stats at the first m5_work_end.",
)
parser.add_argument(
    "--roi-continue-after-workend",
    action="store_false",
    dest="roi_exit_after_workend",
    help="After dumping ROI stats, continue executing the workload.",
)
parser.add_argument(
    "--roi-maxinsts",
    type=int,
    default=0,
    help=(
        "After m5_work_begin, simulate at most this many committed "
        "instructions on any ROI CPU thread. 0 disables the ROI instruction "
        "limit. Requires --fast-forward-to-roi."
    ),
)
parser.add_argument(
    "--progress-interval-insts",
    type=int,
    default=DEFAULT_PROGRESS_INTERVAL_INSTS,
    help=(
        "Print a progress line every N committed instructions per CPU "
        "thread and continue simulation automatically. 0 disables progress "
        f"printing. Default: {DEFAULT_PROGRESS_INTERVAL_INSTS}."
    ),
)
parser.add_argument(
    "--progress-scope",
    choices=["roi", "all"],
    default="roi",
    help=(
        "Scope for instruction progress printing. roi prints only after "
        "m5_work_begin when --fast-forward-to-roi is used. all also prints "
        "during fast-forward or the whole non-ROI run."
    ),
)
parser.add_argument(
    "--mem",
    choices=["simple", "ddr3", "ddr5-4400", "ddr5-6400"],
    default="ddr5-6400",
    help=(
        "Host private memory model. ddr5-4400 and ddr5-6400 use "
        "tRCD/tCL/tRP/tRAS/tWR=14/14/14/32/30ns."
    ),
)
parser.add_argument("--mem-size", default="4GiB")
parser.add_argument(
    "--host-mem-size",
    help=(
        "Comma-separated per-host physical memory sizes or a single size for "
        "all hosts. Defaults to using --mem-size for every host."
    ),
)
parser.add_argument("--sys-clock", default="4GHz")
parser.add_argument("--cpu-clock", default="4GHz")
parser.add_argument(
    "--cwd", help="Semicolon-separated working directories, one per host."
)
parser.add_argument(
    "--stdin", help="Semicolon-separated stdin paths, one per host."
)
parser.add_argument(
    "--stdout", help="Semicolon-separated stdout paths, one per host."
)
parser.add_argument(
    "--stderr", help="Semicolon-separated stderr paths, one per host."
)
parser.add_argument(
    "--env-file", help="Semicolon-separated env-file paths, one per host."
)
parser.add_argument("--l1i-size", default="32KiB")
parser.add_argument("--l1d-size", default="32KiB")
parser.add_argument("--walker-size", default="1KiB")
parser.add_argument(
    "--llc-size",
    default="2MiB",
    help="Comma-separated per-host LLC sizes or a single size for all hosts.",
)
parser.add_argument(
    "--cxl-mem-size",
    default="0B",
    help=(
        "Size of one shared CXL memory range appended after all host private "
        "memory. Set to 0B to disable /dev/gem5_cxl_mem."
    ),
)
parser.add_argument(
    "--cxl-latency",
    default=PAPER_CXL_DDR5_READ_LATENCY,
    help=(
        "SimpleMemory access latency for shared CXL memory and default "
        "DSM-TEE metadata-read latency. The paper DDR5 row-miss read "
        "timing is tRP+tRCD+tCL=42ns."
    ),
)
parser.add_argument(
    "--cxl-bandwidth",
    default=PAPER_CXL_BANDWIDTH,
    help=(
        "SimpleMemory bandwidth for the shared CXL memory range. Ignored "
        "when --cxl-mem-type uses a DDR5 MemCtrl model."
    ),
)
parser.add_argument(
    "--cxl-mem-type",
    choices=["simple", "ddr5-4400", "ddr5-6400"],
    default="ddr5-6400",
    help=(
        "Shared CXL memory model. ddr5-4400 and ddr5-6400 use "
        "tRCD/tCL/tRP/tRAS/tWR=14/14/14/32/30ns."
    ),
)
parser.add_argument(
    "--cxl-link-delay",
    default=PAPER_CXL_LINK_ONE_WAY_DELAY,
    help=(
        "Default per-direction request/response delay inserted between the "
        "coherent membus and shared CXL memory. The paper uses 70ns CXL "
        "round-trip latency, modeled by default as 35ns request + 35ns "
        "response."
    ),
)
parser.add_argument("--cxl-link-read-req-delay")
parser.add_argument("--cxl-link-read-resp-delay")
parser.add_argument("--cxl-link-write-req-delay")
parser.add_argument("--cxl-link-write-resp-delay")
parser.add_argument(
    "--enable-dsm-tee",
    action="store_true",
    help=(
        "Expose /dev/gem5_dsm_tee for SE DSM-TEE region creation and "
        "fixed CXL physical mappings. This does not insert a controller into "
        "the CXL data path."
    ),
)
parser.add_argument(
    "--dsm-tee-num-vmids",
    type=int,
    help=(
        "Number of DSM-TEE VM identities. Defaults to total cores, with "
        "VMID=cpu_id for the core/thread VM model."
    ),
)
parser.add_argument(
    "--dsm-tee-no-auto-grant-all",
    action="store_false",
    dest="dsm_tee_auto_grant_all",
    default=True,
    help=(
        "Do not automatically grant RW access to every VMID on "
        "mmap-created regions."
    ),
)
parser.add_argument(
    "--dsm-tee-no-auto-create-on-mmap",
    action="store_false",
    dest="dsm_tee_auto_create_on_mmap",
    default=True,
    help="Require explicit DSM-TEE create ioctl before mmap.",
)
parser.add_argument(
    "--dsm-tee-data-path",
    action="store_true",
    help=(
        "Insert the DSM-TEE controller into the CXL data path. Baseline "
        "mode leaves the CXL path unchanged unless this flag is set."
    ),
)
parser.add_argument(
    "--dsm-tee-metadata-size",
    default="4MiB",
    help=(
        "CXL memory reserved for DSM-TEE permission table and reverse page "
        "table metadata."
    ),
)
parser.add_argument(
    "--dsm-tee-perm-cache-entries",
    type=int,
    default=256,
    help="DSM-TEE data-path permission cache entries; 0 disables the cache.",
)
parser.add_argument(
    "--dsm-tee-perm-cache-hit-latency",
    default="0ns",
    help="Additional DSM-TEE permission cache hit latency.",
)
parser.add_argument(
    "--dsm-tee-perm-check-cycles",
    type=int,
    default=8,
    help="Base DSM-TEE permission check latency in controller cycles.",
)
parser.add_argument(
    "--dsm-tee-perm-cache-access-cycles",
    type=int,
    default=30,
    help="DSM-TEE permission cache access latency in controller cycles.",
)
parser.add_argument(
    "--dsm-tee-perm-cache-miss-latency",
    default="0ns",
    help="Additional DSM-TEE permission miss latency after metadata CXL read.",
)
parser.add_argument(
    "--dsm-tee-metadata-read-latency",
    help=(
        "Latency to read DSM-TEE permission metadata from CXL memory on a "
        "permission cache miss when metadata packets are disabled. Defaults "
        "to --cxl-latency."
    ),
)
parser.add_argument(
    "--dsm-tee-no-metadata-packets",
    action="store_true",
    help=(
        "Use the legacy fixed metadata-read latency instead of issuing real "
        "metadata read packets on permission cache misses."
    ),
)
parser.add_argument(
    "--dsm-tee-ide-req-delay",
    default="0ns",
    help="Additional DSM-TEE IDE/MAC absolute delay on CXL requests.",
)
parser.add_argument(
    "--dsm-tee-ide-resp-delay",
    default="0ns",
    help="Additional DSM-TEE IDE/MAC absolute delay on CXL responses.",
)
parser.add_argument(
    "--dsm-tee-ide-req-cycles",
    type=int,
    default=1,
    help="DSM-TEE IDE/MAC processing cycles on CXL requests.",
)
parser.add_argument(
    "--dsm-tee-ide-resp-cycles",
    type=int,
    default=1,
    help="DSM-TEE IDE/MAC processing cycles on CXL responses.",
)
parser.add_argument(
    "--dsm-tee-encrypt-read-delay",
    default="10ns",
    help="DSM-TEE decrypt/authenticate delay for CXL read responses.",
)
parser.add_argument(
    "--dsm-tee-encrypt-write-delay",
    default="10ns",
    help="DSM-TEE encrypt/authenticate delay for CXL write requests.",
)
parser.add_argument(
    "--dsm-tee-warn-only",
    action="store_true",
    help=(
        "Warn instead of panicking on DSM-TEE data-path permission "
        "violations."
    ),
)
parser.add_argument(
    "--dsm-tee-protect-unregistered-cxl",
    action="store_true",
    help=(
        "Treat CXL addresses that are not registered DSM-TEE regions as "
        "data-path violations instead of bypassing them."
    ),
)

args = parser.parse_args()

if args.roi_maxinsts < 0:
    raise ValueError("--roi-maxinsts must be >= 0")
if args.roi_maxinsts and not args.fast_forward_to_roi:
    raise ValueError("--roi-maxinsts requires --fast-forward-to-roi")
if args.progress_interval_insts < 0:
    raise ValueError("--progress-interval-insts must be >= 0")

host_core_counts = parse_csv_list(args.host_cores, "--host-cores", int)
if any(count <= 0 for count in host_core_counts):
    raise ValueError("--host-cores values must be positive")

num_hosts = len(host_core_counts)
total_cores = sum(host_core_counts)
num_processes = 1 if args.mode == "cross-host-threaded" else num_hosts
commands = parse_commands(args.cmd, num_processes, "--cmd")

cwd_list = expand_semicolon_field(
    args.cwd, num_processes, "--cwd", os.getcwd()
)
stdin_list = expand_semicolon_field(args.stdin, num_processes, "--stdin")
stdout_list = expand_semicolon_field(args.stdout, num_processes, "--stdout")
stderr_list = expand_semicolon_field(args.stderr, num_processes, "--stderr")
env_list = expand_semicolon_field(args.env_file, num_processes, "--env-file")
llc_sizes = expand_list(
    parse_csv_list(args.llc_size, "--llc-size"), num_hosts, "--llc-size"
)
host_mem_sizes = expand_list(
    parse_csv_list(
        args.host_mem_size if args.host_mem_size else args.mem_size,
        "--host-mem-size" if args.host_mem_size else "--mem-size",
    ),
    num_hosts,
    "--host-mem-size" if args.host_mem_size else "--mem-size",
)
host_mem_sizes = [toMemorySize(size) for size in host_mem_sizes]
cxl_mem_size = toMemorySize(args.cxl_mem_size)
dsm_tee_metadata_size = toMemorySize(args.dsm_tee_metadata_size)
dsm_tee_metadata_read_latency = (
    args.dsm_tee_metadata_read_latency
    if args.dsm_tee_metadata_read_latency
    else args.cxl_latency
)
cxl_link_read_req_delay = (
    args.cxl_link_read_req_delay
    if args.cxl_link_read_req_delay
    else args.cxl_link_delay
)
cxl_link_read_resp_delay = (
    args.cxl_link_read_resp_delay
    if args.cxl_link_read_resp_delay
    else args.cxl_link_delay
)
cxl_link_write_req_delay = (
    args.cxl_link_write_req_delay
    if args.cxl_link_write_req_delay
    else args.cxl_link_delay
)
cxl_link_write_resp_delay = (
    args.cxl_link_write_resp_delay
    if args.cxl_link_write_resp_delay
    else args.cxl_link_delay
)
dsm_tee_num_vmids = (
    args.dsm_tee_num_vmids if args.dsm_tee_num_vmids else total_cores
)
if dsm_tee_num_vmids <= 0:
    raise ValueError("--dsm-tee-num-vmids must be positive")
if args.enable_dsm_tee and cxl_mem_size <= 0:
    raise ValueError("--enable-dsm-tee requires --cxl-mem-size > 0")
if args.enable_dsm_tee and dsm_tee_metadata_size >= cxl_mem_size:
    raise ValueError(
        "--dsm-tee-metadata-size must be smaller than --cxl-mem-size"
    )
if args.dsm_tee_data_path and not args.enable_dsm_tee:
    raise ValueError("--dsm-tee-data-path requires --enable-dsm-tee")
if args.dsm_tee_perm_cache_entries < 0:
    raise ValueError("--dsm-tee-perm-cache-entries must be non-negative")
if args.dsm_tee_perm_check_cycles < 0:
    raise ValueError("--dsm-tee-perm-check-cycles must be non-negative")
if args.dsm_tee_perm_cache_access_cycles < 0:
    raise ValueError("--dsm-tee-perm-cache-access-cycles must be non-negative")
if args.dsm_tee_ide_req_cycles < 0:
    raise ValueError("--dsm-tee-ide-req-cycles must be non-negative")
if args.dsm_tee_ide_resp_cycles < 0:
    raise ValueError("--dsm-tee-ide-resp-cycles must be non-negative")

for cwd in cwd_list:
    if cwd and not os.path.isdir(cwd):
        raise FileNotFoundError(f"Working directory not found: {cwd}")

processes = []
for process_id in range(num_processes):
    processes.append(
        make_process(
            host_id=process_id,
            command=commands[process_id],
            cwd=os.path.abspath(cwd_list[process_id]),
            env_file=env_list[process_id],
            stdin_path=stdin_list[process_id],
            stdout_path=stdout_list[process_id],
            stderr_path=stderr_list[process_id],
        )
    )

if args.mode == "cross-host-threaded":
    processes[0].crossHostThreads = True

detailed_cpu_cls = CPU_TYPES[args.cpu]
startup_cpu_cls = (
    X86AtomicSimpleCPU if args.fast_forward_to_roi else detailed_cpu_cls
)
using_atomic_cpu = args.fast_forward_to_roi or args.cpu == "atomic"

core_to_host = []
for host_id, count in enumerate(host_core_counts):
    core_to_host.extend([host_id] * count)

cpus = [
    startup_cpu_cls(cpu_id=core_id, socket_id=core_to_host[core_id])
    for core_id in range(total_cores)
]

system = System(
    cpu=cpus,
    mem_mode="atomic" if using_atomic_cpu else "timing",
    mem_ranges=[],
)
system.exit_on_work_items = args.fast_forward_to_roi
system.workload = SEWorkload.init_compatible(processes[0].executable)
system.clk_domain = SrcClockDomain()
system.clk_domain.clock = args.sys_clock
system.clk_domain.voltage_domain = VoltageDomain()
system.cpu_clk_domain = SrcClockDomain()
system.cpu_clk_domain.clock = args.cpu_clock
system.cpu_clk_domain.voltage_domain = VoltageDomain()
system.membus = SystemXBar()
system.system_port = system.membus.cpu_side_ports

for cpu in system.cpu:
    cpu.clk_domain = system.cpu_clk_domain

switch_cpu_list = []
if args.fast_forward_to_roi:
    system.switch_cpus = [
        detailed_cpu_cls(
            switched_out=True,
            cpu_id=core_id,
            socket_id=core_to_host[core_id],
        )
        for core_id in range(total_cores)
    ]
    for cpu in system.switch_cpus:
        cpu.clk_domain = system.cpu_clk_domain
    switch_cpu_list = [
        (system.cpu[core_id], system.switch_cpus[core_id])
        for core_id in range(total_cores)
    ]

host_mem_ranges = []
host_mem_base = 0
for host_mem_size in host_mem_sizes:
    host_range = AddrRange(host_mem_base, size=host_mem_size)
    host_mem_ranges.append(host_range)
    system.mem_ranges.append(host_range)
    host_mem_base += host_mem_size

cxl_range = None
cxl_pool_id = None
if cxl_mem_size > 0:
    cxl_pool_id = len(host_mem_ranges)
    cxl_range = AddrRange(host_mem_base, size=cxl_mem_size)
    system.mem_ranges.append(cxl_range)
    host_mem_base += cxl_mem_size

    system.cxl_mem_driver = CxlMemoryDriver(
        filename="gem5_cxl_mem",
        memory_pool_id=cxl_pool_id,
    )
    for process in processes:
        process.drivers = list(process.drivers) + [system.cxl_mem_driver]

    if args.enable_dsm_tee:
        system.dsm_tee_mem_driver = DsmTeeMemoryDriver(
            filename="gem5_dsm_tee",
            memory_pool_id=cxl_pool_id,
            num_vmids=dsm_tee_num_vmids,
            auto_grant_all=args.dsm_tee_auto_grant_all,
            auto_create_on_mmap=args.dsm_tee_auto_create_on_mmap,
            metadata_reserved_size=args.dsm_tee_metadata_size,
        )
        for process in processes:
            process.drivers = list(process.drivers) + [
                system.dsm_tee_mem_driver
            ]

core_index = 0
for host_id, core_count in enumerate(host_core_counts):
    host_bus = L2XBar()
    host_llc = LLCache(size=llc_sizes[host_id])
    host_llc.connect_cpu_side_bus(host_bus)
    host_llc.connect_mem_side_bus(system.membus)

    setattr(system, f"host{host_id}_l2bus", host_bus)
    setattr(system, f"host{host_id}_llc", host_llc)

    for _ in range(core_count):
        workload = (
            processes[0]
            if args.mode == "cross-host-threaded"
            else processes[host_id]
        )
        cpu = system.cpu[core_index]
        cpu.workload = workload

        cpu.addPrivateSplitL1Caches(
            L1ICache(size=args.l1i_size),
            L1DCache(size=args.l1d_size),
            WalkerCache(size=args.walker_size),
            WalkerCache(size=args.walker_size),
        )
        cpu.createInterruptController()
        cpu.connectAllPorts(
            host_bus.cpu_side_ports,
            system.membus.cpu_side_ports,
            system.membus.mem_side_ports,
        )
        cpu.createThreads()

        if args.fast_forward_to_roi:
            timing_cpu = system.switch_cpus[core_index]
            timing_cpu.workload = workload
            timing_cpu.isa = cpu.isa
            timing_cpu.createThreads()
        core_index += 1

for host_id, host_range in enumerate(host_mem_ranges):
    if args.mem == "simple":
        mem_ctrl = FastSimpleMemory()
        mem_ctrl.range = host_range
        mem_ctrl.port = system.membus.mem_side_ports
    elif args.mem == "ddr3":
        mem_ctrl = MemCtrl()
        mem_ctrl.dram = DDR3_1600_8x8()
        mem_ctrl.dram.range = host_range
        mem_ctrl.port = system.membus.mem_side_ports
    else:
        mem_ctrl = MemCtrl()
        mem_ctrl.dram = PAPER_DDR5_FACTORIES[args.mem]()
        mem_ctrl.dram.range = host_range
        mem_ctrl.port = system.membus.mem_side_ports

    setattr(system, f"host{host_id}_mem_ctrl", mem_ctrl)

if cxl_range is not None:
    if args.cxl_mem_type == "simple":
        system.cxl_mem_ctrl = FastSimpleMemory(
            latency=args.cxl_latency,
            bandwidth=args.cxl_bandwidth,
        )
        system.cxl_mem_ctrl.range = cxl_range
    else:
        system.cxl_mem_ctrl = MemCtrl()
        system.cxl_mem_ctrl.dram = PAPER_DDR5_FACTORIES[args.cxl_mem_type]()
        system.cxl_mem_ctrl.dram.range = cxl_range

    system.cxl_link = SimpleMemDelay(
        read_req=cxl_link_read_req_delay,
        read_resp=cxl_link_read_resp_delay,
        write_req=cxl_link_write_req_delay,
        write_resp=cxl_link_write_resp_delay,
    )
    if args.dsm_tee_data_path:
        system.dsm_tee_ctrl = DsmTeeController(
            driver=system.dsm_tee_mem_driver,
            perm_cache_entries=args.dsm_tee_perm_cache_entries,
            perm_check_cycles=args.dsm_tee_perm_check_cycles,
            perm_cache_access_cycles=args.dsm_tee_perm_cache_access_cycles,
            perm_cache_hit_latency=args.dsm_tee_perm_cache_hit_latency,
            perm_cache_miss_latency=args.dsm_tee_perm_cache_miss_latency,
            metadata_read_latency=dsm_tee_metadata_read_latency,
            metadata_read_packets=not args.dsm_tee_no_metadata_packets,
            ide_req_delay=args.dsm_tee_ide_req_delay,
            ide_resp_delay=args.dsm_tee_ide_resp_delay,
            ide_req_cycles=args.dsm_tee_ide_req_cycles,
            ide_resp_cycles=args.dsm_tee_ide_resp_cycles,
            encrypt_read_delay=args.dsm_tee_encrypt_read_delay,
            encrypt_write_delay=args.dsm_tee_encrypt_write_delay,
            deny_on_violation=not args.dsm_tee_warn_only,
            bypass_non_dsm=not args.dsm_tee_protect_unregistered_cxl,
        )
        system.cxl_link.cpu_side_port = system.membus.mem_side_ports
        system.cxl_link.mem_side_port = system.dsm_tee_ctrl.cpu_side_port
        system.dsm_tee_ctrl.mem_side_port = system.cxl_mem_ctrl.port
    else:
        system.cxl_link.cpu_side_port = system.membus.mem_side_ports
        system.cxl_link.mem_side_port = system.cxl_mem_ctrl.port

root = Root(full_system=False, system=system)
m5.instantiate()

print("Pseudo multi-host SE topology")
print(f"  mode: {args.mode}")
print(
    "  cpu: "
    f"startup={'atomic' if using_atomic_cpu else args.cpu}, "
    f"roi={args.cpu}, fast_forward_to_roi={args.fast_forward_to_roi}, "
    f"roi_maxinsts={args.roi_maxinsts}, "
    f"progress_interval_insts={args.progress_interval_insts}, "
    f"progress_scope={args.progress_scope}"
)
print(f"  hosts: {num_hosts}")
print(f"  total cores: {total_cores}")
for host_id, core_count in enumerate(host_core_counts):
    host_core_ids = [
        core_id
        for core_id, mapped_host in enumerate(core_to_host)
        if mapped_host == host_id
    ]
    print(
        f"  host{host_id}: cores={host_core_ids}, llc={llc_sizes[host_id]}, "
        f"mem={host_mem_sizes[host_id]}, range={host_mem_ranges[host_id]}, "
        f"workload="
        f"{processes[0].cmd if args.mode == 'cross-host-threaded' else processes[host_id].cmd}"
    )
if cxl_range is not None:
    print(
        f"  cxl: pool={cxl_pool_id}, mem={cxl_mem_size}, range={cxl_range}, "
        f"mem_type={args.cxl_mem_type}, simple_latency={args.cxl_latency}, "
        f"simple_bandwidth={args.cxl_bandwidth}, "
        f"link_read={cxl_link_read_req_delay}+{cxl_link_read_resp_delay}, "
        f"link_write={cxl_link_write_req_delay}+{cxl_link_write_resp_delay}, "
        "device=/dev/gem5_cxl_mem"
    )
    if args.enable_dsm_tee:
        print(
            "  dsm-tee: device=/dev/gem5_dsm_tee, "
            f"vmid_policy=core, num_vmids={dsm_tee_num_vmids}, "
            f"auto_grant_all={args.dsm_tee_auto_grant_all}, "
            f"metadata_size={dsm_tee_metadata_size}, "
            f"data_path={'enabled' if args.dsm_tee_data_path else 'unchanged'}"
        )
        if args.dsm_tee_data_path:
            print(
                "    data_path_model: "
                "controller_location=device_side_after_cxl_link, "
                f"perm_cache_entries={args.dsm_tee_perm_cache_entries}, "
                f"perm_check={args.dsm_tee_perm_check_cycles}cy, "
                "perm_cache_access="
                f"{args.dsm_tee_perm_cache_access_cycles}cy, "
                f"perm_hit_extra={args.dsm_tee_perm_cache_hit_latency}, "
                f"metadata_read={dsm_tee_metadata_read_latency}, "
                f"metadata_packets={not args.dsm_tee_no_metadata_packets}, "
                f"perm_miss_extra={args.dsm_tee_perm_cache_miss_latency}, "
                f"ide_req={args.dsm_tee_ide_req_cycles}cy+"
                f"{args.dsm_tee_ide_req_delay}, "
                f"ide_resp={args.dsm_tee_ide_resp_cycles}cy+"
                f"{args.dsm_tee_ide_resp_delay}, "
                f"decrypt_read={args.dsm_tee_encrypt_read_delay}, "
                f"encrypt_write={args.dsm_tee_encrypt_write_delay}, "
                f"deny_on_violation={not args.dsm_tee_warn_only}, "
                f"bypass_non_dsm={not args.dsm_tee_protect_unregistered_cxl}"
            )
        for core_id, host_id in enumerate(core_to_host):
            print(f"    cpu{core_id}: host{host_id}, vmid={core_id}")
else:
    print("  cxl: disabled")

progress = InstructionProgress(args.progress_interval_insts, core_to_host)

if args.fast_forward_to_roi:
    print("Fast-forward to ROI: waiting for m5_work_begin")
    if args.progress_scope == "all":
        progress.arm(system.cpu, "fast-forward")
    switched_to_roi = False
    roi_maxinsts_scheduled = False
    exit_event = None

    while True:
        exit_event = m5.simulate()
        cause = exit_event.getCause()
        if cause == PROGRESS_EXIT_CAUSE:
            progress.handle()
            continue
        print(f"Simulation event @ tick {m5.curTick()}: {cause}")

        if cause == "workbegin":
            if not switched_to_roi:
                m5.switchCpus(system, switch_cpu_list)
                switched_to_roi = True
                print(f"Switched to ROI CPUs @ tick {m5.curTick()}")
            memWriteback(root)
            memInvalidate(root)
            print("ROI caches writeback+invalidate")
            m5.stats.reset()
            print("ROI stats reset")
            progress.arm(system.switch_cpus, "roi")
            if args.roi_maxinsts and not roi_maxinsts_scheduled:
                for cpu in system.switch_cpus:
                    cpu.scheduleInstStopAnyThread(args.roi_maxinsts)
                roi_maxinsts_scheduled = True
                print(
                    "ROI max instruction limit scheduled: "
                    f"{args.roi_maxinsts} committed instructions per CPU "
                    "thread, any thread exits"
                )
        elif cause == "workend":
            m5.stats.dump()
            print("ROI stats dumped")
            if args.roi_exit_after_workend:
                break
        elif cause == "a thread reached the max instruction count":
            m5.stats.dump()
            print("ROI maxinst stats dumped")
            break
        else:
            if not switched_to_roi:
                print("Warning: workload exited before emitting m5_work_begin")
            break
else:
    if args.progress_scope == "all":
        progress.arm(system.cpu, "main")
    while True:
        exit_event = m5.simulate()
        cause = exit_event.getCause()
        if cause == PROGRESS_EXIT_CAUSE:
            progress.handle()
            continue
        print(f"Simulation event @ tick {m5.curTick()}: {cause}")
        break

print(f"Exit tick: {m5.curTick()}")
print(f"Exit cause: {exit_event.getCause()}")
