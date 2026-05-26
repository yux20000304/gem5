/*
 * Copyright (c) 2026 The Regents of The University of Michigan
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are
 * met: redistributions of source code must retain the above copyright
 * notice, this list of conditions and the following disclaimer;
 * redistributions in binary form must reproduce the above copyright
 * notice, this list of conditions and the following disclaimer in the
 * documentation and/or other materials provided with the distribution;
 * neither the name of the copyright holders nor the names of its
 * contributors may be used to endorse or promote products derived from
 * this software without specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 * "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
 * LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
 * A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
 * OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
 * SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
 * LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
 * DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
 * THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 * OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */

#include "sim/cxl_memory_driver.hh"

#include <cerrno>
#include <memory>

#include "base/intmath.hh"
#include "base/logging.hh"
#include "cpu/thread_context.hh"
#include "sim/fd_array.hh"
#include "sim/fd_entry.hh"
#include "sim/mem_state.hh"
#include "sim/process.hh"
#include "sim/se_workload.hh"

namespace gem5
{

CxlMemoryDriver::CxlMemoryDriver(const CxlMemoryDriverParams &p)
    : EmulatedDriver(p), memoryPoolId(p.memory_pool_id)
{
    fatal_if(memoryPoolId < 0,
             "%s requires a non-negative memory_pool_id", name());
}

int
CxlMemoryDriver::open(ThreadContext *tc, int mode, int flags)
{
    auto process = tc->getProcessPtr();
    auto device_fd_entry = std::make_shared<DeviceFDEntry>(this, filename);
    return process->fds->allocFD(device_fd_entry);
}

int
CxlMemoryDriver::ioctl(ThreadContext *tc, unsigned req, Addr buf)
{
    return -EINVAL;
}

Addr
CxlMemoryDriver::getOrCreateBacking(ThreadContext *tc, off_t offset,
                                    uint64_t length)
{
    const auto key = std::make_pair(offset, length);
    auto it = sharedBackings.find(key);
    if (it != sharedBackings.end())
        return it->second;

    auto process = tc->getProcessPtr();
    const Addr page_bytes = process->pTable->pageSize();
    const int npages = divCeil(length, page_bytes);
    Addr phys_base = process->seWorkload->allocPhysPages(npages, memoryPoolId);
    sharedBackings.emplace(key, phys_base);
    return phys_base;
}

Addr
CxlMemoryDriver::mmap(ThreadContext *tc, Addr start, uint64_t length,
                      int prot, int tgt_flags, int tgt_fd, off_t offset)
{
    auto process = tc->getProcessPtr();
    auto mem_state = process->memState;

    if (start && !mem_state->isUnmapped(start, length))
        start = 0;

    if (!start)
        start = mem_state->extendMmap(length);

    Addr phys_base = getOrCreateBacking(tc, offset, length);
    mem_state->mapRegion(start, length, "/dev/" + filename, -1, offset,
                         memoryPoolId, phys_base, false);

    return start;
}

} // namespace gem5
