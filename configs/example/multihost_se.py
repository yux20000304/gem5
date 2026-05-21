import argparse
import os
import shlex
import shutil

import m5
from m5.objects import (
    X86O3CPU,
    AddrRange,
    Cache,
    DDR3_1600_8x8,
    L2XBar,
    MemCtrl,
    Process,
    Root,
    SEWorkload,
    SimpleMemory,
    SrcClockDomain,
    System,
    SystemXBar,
    VoltageDomain,
    X86TimingSimpleCPU,
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


CPU_TYPES = {
    "timing": X86TimingSimpleCPU,
    "o3": X86O3CPU,
}


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
    default="timing",
    help="CPU model. Atomic is intentionally unsupported because this script models LLCs.",
)
parser.add_argument("--mem", choices=["simple", "ddr3"], default="simple")
parser.add_argument("--mem-size", default="4GiB")
parser.add_argument(
    "--host-mem-size",
    help=(
        "Comma-separated per-host physical memory sizes or a single size for "
        "all hosts. Defaults to using --mem-size for every host."
    ),
)
parser.add_argument("--sys-clock", default="3GHz")
parser.add_argument("--cpu-clock", default="3GHz")
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

args = parser.parse_args()

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

cpu_cls = CPU_TYPES[args.cpu]

core_to_host = []
for host_id, count in enumerate(host_core_counts):
    core_to_host.extend([host_id] * count)

cpus = [
    cpu_cls(cpu_id=core_id, socket_id=core_to_host[core_id])
    for core_id in range(total_cores)
]

system = System(
    cpu=cpus,
    mem_mode="timing",
    mem_ranges=[],
)
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

host_mem_ranges = []
host_mem_base = 0
for host_mem_size in host_mem_sizes:
    host_range = AddrRange(host_mem_base, size=host_mem_size)
    host_mem_ranges.append(host_range)
    system.mem_ranges.append(host_range)
    host_mem_base += host_mem_size

core_index = 0
for host_id, core_count in enumerate(host_core_counts):
    host_bus = L2XBar()
    host_llc = LLCache(size=llc_sizes[host_id])
    host_llc.connect_cpu_side_bus(host_bus)
    host_llc.connect_mem_side_bus(system.membus)

    setattr(system, f"host{host_id}_l2bus", host_bus)
    setattr(system, f"host{host_id}_llc", host_llc)

    for _ in range(core_count):
        cpu = system.cpu[core_index]
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
        cpu.workload = (
            processes[0]
            if args.mode == "cross-host-threaded"
            else processes[host_id]
        )
        cpu.createThreads()
        core_index += 1

for host_id, host_range in enumerate(host_mem_ranges):
    if args.mem == "simple":
        mem_ctrl = FastSimpleMemory()
        mem_ctrl.range = host_range
        mem_ctrl.port = system.membus.mem_side_ports
    else:
        mem_ctrl = MemCtrl()
        mem_ctrl.dram = DDR3_1600_8x8()
        mem_ctrl.dram.range = host_range
        mem_ctrl.port = system.membus.mem_side_ports

    setattr(system, f"host{host_id}_mem_ctrl", mem_ctrl)

root = Root(full_system=False, system=system)
m5.instantiate()

print("Pseudo multi-host SE topology")
print(f"  mode: {args.mode}")
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

exit_event = m5.simulate()
print(f"Exit tick: {m5.curTick()}")
print(f"Exit cause: {exit_event.getCause()}")
