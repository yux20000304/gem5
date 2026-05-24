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

#ifndef __SIM_DSM_TEE_MEMORY_DRIVER_HH__
#define __SIM_DSM_TEE_MEMORY_DRIVER_HH__

#include <sys/types.h>

#include <cstdint>
#include <unordered_map>
#include <vector>

#include "base/statistics.hh"
#include "base/types.hh"
#include "params/DsmTeeMemoryDriver.hh"
#include "sim/emul_driver.hh"

namespace gem5
{

class DsmTeeMemoryDriver : public EmulatedDriver
{
  public:
    enum : unsigned
    {
        IoctlCreate = 0xD500,
        IoctlGrant = 0xD501,
        IoctlRevoke = 0xD502,
        IoctlDestroy = 0xD503,
        IoctlQuery = 0xD504,
    };

    enum Permission : uint8_t
    {
        PermRead = 1 << 0,
        PermWrite = 1 << 1,
        PermExec = 1 << 2,
    };

    struct CreateArgs
    {
        uint64_t size;
        uint64_t rid;
        uint64_t flags;
    };

    struct GrantArgs
    {
        uint64_t rid;
        uint32_t vmid;
        uint32_t perm;
    };

    struct DestroyArgs
    {
        uint64_t rid;
    };

    struct QueryArgs
    {
        uint64_t rid;
        uint64_t size;
        uint64_t phys_base;
        uint32_t owner_vmid;
        uint32_t flags;
    };

    DsmTeeMemoryDriver(const DsmTeeMemoryDriverParams &p);

    static constexpr Addr MetadataCacheLineBytes = 64;

    int open(ThreadContext *tc, int mode, int flags) override;
    int ioctl(ThreadContext *tc, unsigned req, Addr buf) override;
    Addr mmap(ThreadContext *tc, Addr start, uint64_t length, int prot,
              int tgt_flags, int tgt_fd, off_t offset) override;

    uint32_t numVmids() const { return numVmids_; }
    Addr pageSizeBytes() const { return pageSizeBytes_; }
    uint64_t permissionEpoch() const { return permissionEpoch_; }
    Addr metadataPhysBase() const { return metadataPhysBase_; }
    Addr metadataReservedBytes() const { return metadataReservedBytes_; }
    uint64_t regionIdForPaddr(Addr paddr) const;
    Addr metadataPaddrFor(Addr paddr, uint32_t vmid) const;
    bool hasAccess(Addr paddr, uint32_t vmid, uint8_t perm) const;

  private:
    struct Region
    {
        uint64_t rid = 0;
        Addr size = 0;
        Addr physBase = 0;
        Addr metadataBase = 0;
        Addr metadataBytes = 0;
        int npages = 0;
        uint32_t ownerVmid = 0;
        std::vector<uint8_t> permissions;
    };

    struct DsmTeeStats : public statistics::Group
    {
        DsmTeeStats(DsmTeeMemoryDriver &driver);

        statistics::Scalar regionsCreated;
        statistics::Scalar regionsDestroyed;
        statistics::Scalar grantCalls;
        statistics::Scalar revokeCalls;
        statistics::Scalar mmapCalls;
        statistics::Scalar deniedCalls;
        statistics::Scalar bytesAllocated;
        statistics::Scalar metadataReservedBytes;
        statistics::Scalar metadataAllocatedBytes;
    } stats;

    const int memoryPoolId;
    const uint32_t numVmids_;
    const bool autoGrantAll;
    const bool autoCreateOnMmap;
    const Addr metadataReservedSize;
    Addr pageSizeBytes_;
    Addr metadataPhysBase_;
    Addr metadataReservedBytes_;
    Addr metadataNextOffset_;
    bool metadataReserved_;
    uint64_t permissionEpoch_;
    uint64_t nextRid;
    uint64_t autoRid;
    std::unordered_map<uint64_t, Region> regions;
    std::unordered_map<Addr, uint64_t> reversePageTable;

    uint32_t currentVmid(ThreadContext *tc) const;
    uint8_t protToPerm(int prot) const;
    bool hasPermission(const Region &region, uint32_t vmid,
                       uint8_t perm) const;
    Addr metadataBytesPerDataPage() const;
    Addr metadataBytesForRegion(int npages) const;
    void initPageSize(ThreadContext *tc);
    void ensureMetadataReserved(ThreadContext *tc);
    Region &createRegion(ThreadContext *tc, Addr size, bool grant_all);
    Region *findRegion(uint64_t rid);
    int destroyRegion(ThreadContext *tc, uint64_t rid);
    void readTarget(ThreadContext *tc, Addr addr, void *buf,
                    size_t size) const;
    void writeTarget(ThreadContext *tc, Addr addr, const void *buf,
                     size_t size) const;
};

} // namespace gem5

#endif // __SIM_DSM_TEE_MEMORY_DRIVER_HH__
