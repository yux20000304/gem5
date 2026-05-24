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

#include "sim/dsm_tee_memory_driver.hh"

#include <sys/mman.h>

#include <cerrno>
#include <limits>
#include <memory>
#include <string>

#include "base/intmath.hh"
#include "base/logging.hh"
#include "base/str.hh"
#include "cpu/thread_context.hh"
#include "mem/se_translating_port_proxy.hh"
#include "sim/fd_array.hh"
#include "sim/fd_entry.hh"
#include "sim/mem_state.hh"
#include "sim/process.hh"
#include "sim/se_workload.hh"

namespace gem5
{

DsmTeeMemoryDriver::DsmTeeStats::DsmTeeStats(DsmTeeMemoryDriver &driver)
    : statistics::Group(&driver),
      ADD_STAT(regionsCreated, statistics::units::Count::get(),
               "DSM-TEE regions created"),
      ADD_STAT(regionsDestroyed, statistics::units::Count::get(),
               "DSM-TEE regions destroyed"),
      ADD_STAT(grantCalls, statistics::units::Count::get(),
               "DSM-TEE grant calls accepted"),
      ADD_STAT(revokeCalls, statistics::units::Count::get(),
               "DSM-TEE revoke calls accepted"),
      ADD_STAT(mmapCalls, statistics::units::Count::get(),
               "DSM-TEE mmap calls accepted"),
      ADD_STAT(deniedCalls, statistics::units::Count::get(),
               "DSM-TEE calls denied by control-plane permission checks"),
      ADD_STAT(bytesAllocated, statistics::units::Byte::get(),
               "Bytes allocated from the CXL pool for DSM-TEE regions"),
      ADD_STAT(metadataReservedBytes, statistics::units::Byte::get(),
               "CXL bytes reserved for DSM-TEE permission metadata"),
      ADD_STAT(metadataAllocatedBytes, statistics::units::Byte::get(),
               "CXL metadata bytes assigned to DSM-TEE data pages")
{
}

DsmTeeMemoryDriver::DsmTeeMemoryDriver(
        const DsmTeeMemoryDriverParams &p)
    : EmulatedDriver(p), stats(*this), memoryPoolId(p.memory_pool_id),
      numVmids_(p.num_vmids), autoGrantAll(p.auto_grant_all),
      autoCreateOnMmap(p.auto_create_on_mmap),
      metadataReservedSize(p.metadata_reserved_size), pageSizeBytes_(0),
      metadataPhysBase_(0), metadataReservedBytes_(0), metadataNextOffset_(0),
      metadataReserved_(false),
      permissionEpoch_(0), nextRid(1), autoRid(0)
{
    fatal_if(memoryPoolId < 0,
             "%s requires a non-negative memory_pool_id", name());
    fatal_if(numVmids_ == 0, "%s requires at least one VMID", name());
}

int
DsmTeeMemoryDriver::open(ThreadContext *tc, int mode, int flags)
{
    auto process = tc->getProcessPtr();
    auto device_fd_entry = std::make_shared<DeviceFDEntry>(this, filename);
    return process->fds->allocFD(device_fd_entry);
}

uint32_t
DsmTeeMemoryDriver::currentVmid(ThreadContext *tc) const
{
    const uint32_t vmid = tc->cpuId();
    fatal_if(vmid >= numVmids_,
             "%s maps cpu%u to VMID %u, but num_vmids is %u",
             name(), tc->cpuId(), vmid, numVmids_);
    return vmid;
}

uint8_t
DsmTeeMemoryDriver::protToPerm(int prot) const
{
    uint8_t perm = 0;
    if (prot & PROT_READ)
        perm |= PermRead;
    if (prot & PROT_WRITE)
        perm |= PermWrite;
    if (prot & PROT_EXEC)
        perm |= PermExec;
    return perm;
}

bool
DsmTeeMemoryDriver::hasPermission(const Region &region, uint32_t vmid,
                                  uint8_t perm) const
{
    if (vmid >= region.permissions.size())
        return false;
    return (region.permissions[vmid] & perm) == perm;
}

Addr
DsmTeeMemoryDriver::metadataBytesPerDataPage() const
{
    return roundUp(static_cast<Addr>(numVmids_), MetadataCacheLineBytes);
}

Addr
DsmTeeMemoryDriver::metadataBytesForRegion(int npages) const
{
    return metadataBytesPerDataPage() * static_cast<Addr>(npages);
}

void
DsmTeeMemoryDriver::initPageSize(ThreadContext *tc)
{
    auto process = tc->getProcessPtr();
    const Addr page_bytes = process->pTable->pageSize();
    if (pageSizeBytes_ == 0) {
        pageSizeBytes_ = page_bytes;
    } else {
        fatal_if(pageSizeBytes_ != page_bytes,
                 "%s cannot mix DSM-TEE page sizes: %llu != %llu",
                 name(), pageSizeBytes_, page_bytes);
    }
}

void
DsmTeeMemoryDriver::ensureMetadataReserved(ThreadContext *tc)
{
    initPageSize(tc);
    if (metadataReserved_ || metadataReservedSize == 0)
        return;

    auto process = tc->getProcessPtr();
    const Addr reserved_bytes = roundUp(metadataReservedSize, pageSizeBytes_);
    const uint64_t npages64 = reserved_bytes / pageSizeBytes_;
    fatal_if(npages64 > std::numeric_limits<int>::max(),
             "%s metadata reservation is too large: %llu pages",
             name(), npages64);

    metadataPhysBase_ = process->seWorkload->allocPhysPages(
            static_cast<int>(npages64), memoryPoolId);
    metadataReservedBytes_ = reserved_bytes;
    metadataReserved_ = true;
    stats.metadataReservedBytes += reserved_bytes;
}

DsmTeeMemoryDriver::Region &
DsmTeeMemoryDriver::createRegion(ThreadContext *tc, Addr size, bool grant_all)
{
    ensureMetadataReserved(tc);
    auto process = tc->getProcessPtr();
    const Addr page_bytes = pageSizeBytes_;
    const Addr region_size = roundUp(size, page_bytes);
    const uint64_t npages64 = region_size / page_bytes;
    fatal_if(npages64 > std::numeric_limits<int>::max(),
             "%s region is too large: %llu pages", name(), npages64);

    Region region;
    region.rid = nextRid++;
    region.size = region_size;
    region.npages = static_cast<int>(npages64);
    region.ownerVmid = currentVmid(tc);
    region.permissions.resize(numVmids_, 0);
    region.metadataBytes = metadataBytesForRegion(region.npages);
    fatal_if(metadataNextOffset_ + region.metadataBytes >
                     metadataReservedBytes_,
             "%s metadata reservation exhausted: need %llu bytes, have %llu "
             "bytes free. Increase --dsm-tee-metadata-size.",
             name(), region.metadataBytes,
             metadataReservedBytes_ - metadataNextOffset_);
    region.metadataBase = metadataPhysBase_ + metadataNextOffset_;
    metadataNextOffset_ += region.metadataBytes;
    region.permissions[region.ownerVmid] = PermRead | PermWrite | PermExec;
    if (grant_all) {
        for (auto &perm : region.permissions)
            perm |= PermRead | PermWrite;
    }

    region.physBase =
        process->seWorkload->allocPhysPages(region.npages, memoryPoolId);

    auto [it, inserted] = regions.emplace(region.rid, std::move(region));
    fatal_if(!inserted, "%s reused DSM-TEE RID %llu", name(), region.rid);

    for (int i = 0; i < it->second.npages; ++i)
        reversePageTable[it->second.physBase + i * page_bytes] =
            it->second.rid;

    stats.regionsCreated++;
    stats.bytesAllocated += it->second.size;
    stats.metadataAllocatedBytes += it->second.metadataBytes;
    permissionEpoch_++;
    return it->second;
}

DsmTeeMemoryDriver::Region *
DsmTeeMemoryDriver::findRegion(uint64_t rid)
{
    auto it = regions.find(rid);
    return it == regions.end() ? nullptr : &it->second;
}

int
DsmTeeMemoryDriver::destroyRegion(ThreadContext *tc, uint64_t rid)
{
    auto it = regions.find(rid);
    if (it == regions.end())
        return -ENOENT;

    Region &region = it->second;
    if (region.ownerVmid != currentVmid(tc)) {
        stats.deniedCalls++;
        return -EACCES;
    }

    auto process = tc->getProcessPtr();
    const Addr page_bytes = process->pTable->pageSize();
    for (int i = 0; i < region.npages; ++i) {
        reversePageTable.erase(region.physBase + i * page_bytes);
        process->seWorkload->deallocPhysPage(
                region.physBase + i * page_bytes, memoryPoolId);
    }

    regions.erase(it);
    if (autoRid == rid)
        autoRid = 0;

    stats.regionsDestroyed++;
    permissionEpoch_++;
    return 0;
}

uint64_t
DsmTeeMemoryDriver::regionIdForPaddr(Addr paddr) const
{
    if (pageSizeBytes_ == 0)
        return 0;

    const Addr page_base = paddr - (paddr % pageSizeBytes_);
    auto it = reversePageTable.find(page_base);
    return it == reversePageTable.end() ? 0 : it->second;
}

Addr
DsmTeeMemoryDriver::metadataPaddrFor(Addr paddr, uint32_t vmid) const
{
    if (pageSizeBytes_ == 0 || vmid >= numVmids_)
        return 0;

    const Addr page_base = paddr - (paddr % pageSizeBytes_);
    auto reverse_it = reversePageTable.find(page_base);
    if (reverse_it == reversePageTable.end())
        return 0;

    auto region_it = regions.find(reverse_it->second);
    if (region_it == regions.end())
        return 0;

    const Region &region = region_it->second;
    const Addr page_index = (page_base - region.physBase) / pageSizeBytes_;
    const Addr metadata_line =
        (static_cast<Addr>(vmid) / MetadataCacheLineBytes) *
        MetadataCacheLineBytes;
    return region.metadataBase +
           page_index * metadataBytesPerDataPage() + metadata_line;
}

bool
DsmTeeMemoryDriver::hasAccess(Addr paddr, uint32_t vmid, uint8_t perm) const
{
    const uint64_t rid = regionIdForPaddr(paddr);
    if (rid == 0)
        return false;

    auto it = regions.find(rid);
    return it != regions.end() && hasPermission(it->second, vmid, perm);
}

void
DsmTeeMemoryDriver::readTarget(ThreadContext *tc, Addr addr, void *buf,
                               size_t size) const
{
    SETranslatingPortProxy proxy(tc);
    proxy.readBlob(addr, static_cast<uint8_t *>(buf), size);
}

void
DsmTeeMemoryDriver::writeTarget(ThreadContext *tc, Addr addr,
                                const void *buf, size_t size) const
{
    SETranslatingPortProxy proxy(tc);
    proxy.writeBlob(addr, static_cast<const uint8_t *>(buf), size);
}

int
DsmTeeMemoryDriver::ioctl(ThreadContext *tc, unsigned req, Addr buf)
{
    if (buf == 0)
        return -EFAULT;

    switch (req) {
      case IoctlCreate: {
        CreateArgs args;
        readTarget(tc, buf, &args, sizeof(args));
        if (args.size == 0)
            return -EINVAL;

        Region &region = createRegion(tc, args.size, false);
        args.rid = region.rid;
        writeTarget(tc, buf, &args, sizeof(args));
        return 0;
      }

      case IoctlGrant: {
        GrantArgs args;
        readTarget(tc, buf, &args, sizeof(args));
        Region *region = findRegion(args.rid);
        if (!region)
            return -ENOENT;
        if (region->ownerVmid != currentVmid(tc)) {
            stats.deniedCalls++;
            return -EACCES;
        }
        if (args.vmid >= numVmids_)
            return -EINVAL;
        region->permissions[args.vmid] = args.perm;
        stats.grantCalls++;
        permissionEpoch_++;
        return 0;
      }

      case IoctlRevoke: {
        GrantArgs args;
        readTarget(tc, buf, &args, sizeof(args));
        Region *region = findRegion(args.rid);
        if (!region)
            return -ENOENT;
        if (region->ownerVmid != currentVmid(tc)) {
            stats.deniedCalls++;
            return -EACCES;
        }
        if (args.vmid >= numVmids_)
            return -EINVAL;
        region->permissions[args.vmid] = 0;
        stats.revokeCalls++;
        permissionEpoch_++;
        return 0;
      }

      case IoctlDestroy: {
        DestroyArgs args;
        readTarget(tc, buf, &args, sizeof(args));
        return destroyRegion(tc, args.rid);
      }

      case IoctlQuery: {
        QueryArgs args;
        readTarget(tc, buf, &args, sizeof(args));
        Region *region = findRegion(args.rid);
        if (!region)
            return -ENOENT;
        args.size = region->size;
        args.phys_base = region->physBase;
        args.owner_vmid = region->ownerVmid;
        args.flags = 0;
        writeTarget(tc, buf, &args, sizeof(args));
        return 0;
      }

      default:
        return -EINVAL;
    }
}

Addr
DsmTeeMemoryDriver::mmap(ThreadContext *tc, Addr start, uint64_t length,
                         int prot, int tgt_flags, int tgt_fd, off_t offset)
{
    auto process = tc->getProcessPtr();
    auto mem_state = process->memState;
    const Addr page_bytes = process->pTable->pageSize();

    if (start && !mem_state->isUnmapped(start, length))
        start = 0;

    if (!start)
        start = mem_state->extendMmap(length);

    uint64_t rid = 0;
    if (offset != 0) {
        rid = offset / page_bytes;
    } else {
        if (!autoCreateOnMmap)
            return -EINVAL;
        if (autoRid == 0) {
            Region &region = createRegion(tc, length, autoGrantAll);
            autoRid = region.rid;
        }
        rid = autoRid;
    }

    Region *region = findRegion(rid);
    if (!region)
        return -ENOENT;
    if (length > region->size)
        return -ENOMEM;

    const uint8_t requested_perm = protToPerm(prot);
    if (!hasPermission(*region, currentVmid(tc), requested_perm)) {
        stats.deniedCalls++;
        return -EACCES;
    }

    mem_state->mapRegion(start, length, csprintf("/dev/%s:rid=%llu",
                         filename.c_str(), rid), -1, 0, memoryPoolId,
                         region->physBase, false, rid);

    stats.mmapCalls++;
    return start;
}

} // namespace gem5
