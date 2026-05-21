import argparse
import os

import m5
from m5.objects import (
    X86O3CPU,
    AddrRange,
    Cache,
    DDR3_1600_8x8,
    L2XBar,
    Process,
    Root,
    SEWorkload,
    SimpleMemory,
    SrcClockDomain,
    System,
    SystemXBar,
    VoltageDomain,
    X86AtomicSimpleCPU,
    X86TimingSimpleCPU,
)


class L1Cache(Cache):
    assoc = 8
    tag_latency = 1
    data_latency = 1
    response_latency = 1
    mshrs = 16
    tgts_per_mshr = 20

    def connect_bus(self, bus):
        self.mem_side = bus.cpu_side_ports


class L1ICache(L1Cache):
    size = "32KiB"

    def connect_cpu(self, cpu):
        self.cpu_side = cpu.icache_port


class L1DCache(L1Cache):
    size = "32KiB"

    def connect_cpu(self, cpu):
        self.cpu_side = cpu.dcache_port


class L2Cache(Cache):
    size = "512KiB"
    assoc = 16
    tag_latency = 10
    data_latency = 10
    response_latency = 1
    mshrs = 20
    tgts_per_mshr = 12

    def connect_cpu_side_bus(self, bus):
        self.cpu_side = bus.mem_side_ports

    def connect_mem_side_bus(self, bus):
        self.mem_side = bus.cpu_side_ports


class FastSimpleMemory(SimpleMemory):
    latency = "1ns"


CPU_TYPES = {
    "atomic": X86AtomicSimpleCPU,
    "timing": X86TimingSimpleCPU,
    "o3": X86O3CPU,
}

MEM_TYPES = {
    "simple": FastSimpleMemory,
    "ddr3": DDR3_1600_8x8,
}


def load_env(path):
    if not path:
        return []
    with open(path, encoding="utf-8") as fh:
        return [line.rstrip("\n") for line in fh if line.strip()]


parser = argparse.ArgumentParser(
    description="Run an x86 SPEC CPU2017 workload in gem5 SE mode."
)
parser.add_argument(
    "--binary", required=True, help="Path to benchmark binary."
)
parser.add_argument(
    "--args",
    nargs=argparse.REMAINDER,
    default=[],
    help="Arguments passed to the benchmark binary.",
)
parser.add_argument("--cpu", choices=CPU_TYPES, default="atomic")
parser.add_argument("--mem", choices=MEM_TYPES, default="simple")
parser.add_argument("--mem-size", default="2GiB")
parser.add_argument("--sys-clock", default="3GHz")
parser.add_argument("--cwd", default=os.getcwd())
parser.add_argument("--stdin")
parser.add_argument("--stdout")
parser.add_argument("--stderr")
parser.add_argument("--env-file")

args = parser.parse_args()

binary = os.path.abspath(args.binary)
cwd = os.path.abspath(args.cwd)

if not os.path.isfile(binary):
    raise FileNotFoundError(f"Binary not found: {binary}")
if not os.path.isdir(cwd):
    raise FileNotFoundError(f"Working directory not found: {cwd}")

system = System()
system.workload = SEWorkload.init_compatible(binary)
system.clk_domain = SrcClockDomain()
system.clk_domain.clock = args.sys_clock
system.clk_domain.voltage_domain = VoltageDomain()
system.mem_ranges = [AddrRange(args.mem_size)]
system.cpu = CPU_TYPES[args.cpu]()

if args.cpu == "atomic":
    system.mem_mode = "atomic"
    system.membus = SystemXBar()
    system.cpu.icache_port = system.membus.cpu_side_ports
    system.cpu.dcache_port = system.membus.cpu_side_ports
else:
    system.mem_mode = "timing"
    system.cpu.l1i = L1ICache()
    system.cpu.l1d = L1DCache()
    system.l1_to_l2 = L2XBar()
    system.l2cache = L2Cache()
    system.membus = SystemXBar()

    system.cpu.l1i.connect_cpu(system.cpu)
    system.cpu.l1i.connect_bus(system.l1_to_l2)
    system.cpu.l1d.connect_cpu(system.cpu)
    system.cpu.l1d.connect_bus(system.l1_to_l2)
    system.l2cache.connect_cpu_side_bus(system.l1_to_l2)
    system.l2cache.connect_mem_side_bus(system.membus)

system.cpu.createInterruptController()
system.cpu.interrupts[0].pio = system.membus.mem_side_ports
system.cpu.interrupts[0].int_requestor = system.membus.cpu_side_ports
system.cpu.interrupts[0].int_responder = system.membus.mem_side_ports

system.mem_ctrl = MEM_TYPES[args.mem]()
system.mem_ctrl.range = system.mem_ranges[0]
system.mem_ctrl.port = system.membus.mem_side_ports
system.system_port = system.membus.cpu_side_ports

process = Process()
process.executable = binary
process.cmd = [binary] + args.args
process.cwd = cwd
process.env = load_env(args.env_file)
if args.stdin:
    process.input = args.stdin
if args.stdout:
    process.output = args.stdout
if args.stderr:
    process.errout = args.stderr

system.cpu.workload = process
system.cpu.createThreads()

root = Root(full_system=False, system=system)
m5.instantiate()

print(f"Running: {' '.join(process.cmd)}")
print(f"Working directory: {process.cwd}")
exit_event = m5.simulate()
print(f"Exit tick: {m5.curTick()}")
print(f"Exit cause: {exit_event.getCause()}")
