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

#ifndef __MEM_DSM_TEE_CONTROLLER_HH__
#define __MEM_DSM_TEE_CONTROLLER_HH__

#include <cstdint>
#include <list>
#include <string>
#include <unordered_map>

#include "base/statistics.hh"
#include "base/types.hh"
#include "mem/mem_delay.hh"
#include "params/DsmTeeController.hh"

namespace gem5
{

class DsmTeeMemoryDriver;
class System;

class DsmTeeController : public MemDelay
{
  public:
    DsmTeeController(const DsmTeeControllerParams &p);

  protected:
    Tick delayReq(PacketPtr pkt) override;
    Tick delayResp(PacketPtr pkt) override;

  private:
    struct PermissionCacheKey
    {
        Addr pageBase = 0;
        uint32_t vmid = 0;
        uint8_t perm = 0;

        bool operator==(const PermissionCacheKey &other) const;
    };

    struct PermissionCacheKeyHash
    {
        std::size_t operator()(const PermissionCacheKey &key) const;
    };

    struct PermissionCacheEntry
    {
        std::list<PermissionCacheKey>::iterator lruIt;
    };

    struct DsmTeeControllerStats : public statistics::Group
    {
        DsmTeeControllerStats(DsmTeeController &ctrl);

        statistics::Scalar dsmRequests;
        statistics::Scalar nonDsmRequests;
        statistics::Scalar permissionChecks;
        statistics::Scalar permissionCacheHits;
        statistics::Scalar permissionCacheMisses;
        statistics::Scalar permissionCacheInvalidations;
        statistics::Scalar permissionDenied;
        statistics::Scalar metadataReads;
        statistics::Scalar metadataReadBytes;
        statistics::Scalar unmappedRequestors;
        statistics::Scalar writebackBypass;
        statistics::Scalar readPermitted;
        statistics::Scalar writePermitted;
        statistics::Scalar execPermitted;
        statistics::Scalar readResponses;
        statistics::Scalar writeResponses;
        statistics::Scalar bytesRead;
        statistics::Scalar bytesWritten;
        statistics::Scalar totalReqDelay;
        statistics::Scalar totalRespDelay;
    } stats;

    DsmTeeMemoryDriver *const driver;
    System *const system;
    const unsigned permCacheEntries;
    const Cycles permCheckCycles;
    const Cycles permCacheAccessCycles;
    const Tick permCacheHitLatency;
    const Tick permCacheMissLatency;
    const Tick metadataReadLatency;
    const Cycles ideReqCycles;
    const Cycles ideRespCycles;
    const Tick ideReqDelay;
    const Tick ideRespDelay;
    const Tick encryptReadDelay;
    const Tick encryptWriteDelay;
    const bool denyOnViolation;
    const bool bypassNonDsm;

    uint64_t cachedPermissionEpoch;
    std::list<PermissionCacheKey> permissionCacheLru;
    std::unordered_map<PermissionCacheKey, PermissionCacheEntry,
                       PermissionCacheKeyHash> permissionCache;

    uint8_t permissionForRequest(PacketPtr pkt) const;
    bool requestorToVmid(PacketPtr pkt, uint32_t &vmid) const;
    bool parseCpuVmid(const std::string &requestor_name,
                      uint32_t &vmid) const;
    bool hasCachedPermission(Addr paddr, uint32_t vmid, uint8_t perm);
    Addr permissionPageBase(Addr paddr) const;
    void refreshPermissionCacheEpoch();
    void insertPermissionCache(Addr paddr, uint32_t vmid, uint8_t perm);
    void handlePermissionDenied(PacketPtr pkt, uint32_t vmid,
                                uint8_t perm) const;
    Tick accessOverhead(PacketPtr pkt) const;
};

} // namespace gem5

#endif // __MEM_DSM_TEE_CONTROLLER_HH__
