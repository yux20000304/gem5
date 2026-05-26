# Copyright (c) 2026 The Regents of The University of Michigan
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met: redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer;
# redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution;
# neither the name of the copyright holders nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

from m5.objects.MemDelay import MemDelay
from m5.params import *
from m5.proxy import *


class DsmTeeController(MemDelay):
    type = "DsmTeeController"
    cxx_header = "mem/dsm_tee_controller.hh"
    cxx_class = "gem5::DsmTeeController"

    system = Param.System(Parent.any, "System owning requestor IDs")
    driver = Param.DsmTeeMemoryDriver(
        "DSM-TEE memory driver providing region and permission metadata"
    )

    perm_cache_entries = Param.Unsigned(
        256, "Number of DSM-TEE page permission cache entries; 0 disables it"
    )
    perm_check_cycles = Param.Cycles(
        8, "Base DSM-TEE permission check latency in controller cycles"
    )
    perm_cache_access_cycles = Param.Cycles(
        30, "DSM-TEE permission cache access latency in controller cycles"
    )
    perm_cache_hit_latency = Param.Latency(
        "0ns", "Additional latency for a DSM-TEE permission cache hit"
    )
    perm_cache_miss_latency = Param.Latency(
        "0ns", "Additional latency after a DSM-TEE metadata miss read"
    )
    metadata_read_latency = Param.Latency(
        "150ns", "Latency to read DSM-TEE permission metadata from CXL memory"
    )
    metadata_read_packets = Param.Bool(
        True,
        "Issue real metadata read packets on permission cache misses. "
        "Disable to use metadata_read_latency as a fixed timing-only delay.",
    )

    ide_req_delay = Param.Latency(
        "0ns", "Additional DSM-TEE IDE/MAC delay on CXL requests"
    )
    ide_resp_delay = Param.Latency(
        "0ns", "Additional DSM-TEE IDE/MAC delay on CXL responses"
    )
    ide_req_cycles = Param.Cycles(
        1, "DSM-TEE IDE/MAC processing cycles on CXL requests"
    )
    ide_resp_cycles = Param.Cycles(
        1, "DSM-TEE IDE/MAC processing cycles on CXL responses"
    )
    encrypt_read_delay = Param.Latency(
        "10ns", "DSM-TEE decrypt/authenticate delay for read responses"
    )
    encrypt_write_delay = Param.Latency(
        "10ns", "DSM-TEE encrypt/authenticate delay for write requests"
    )

    deny_on_violation = Param.Bool(
        True, "Panic on DSM-TEE data-path permission violations"
    )
    bypass_non_dsm = Param.Bool(
        True, "Bypass CXL addresses that were not allocated as DSM-TEE regions"
    )
