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

#include "mem/dsm_tee_controller.hh"

#include <cctype>
#include <functional>
#include <memory>

#include "base/logging.hh"
#include "mem/packet.hh"
#include "mem/request.hh"
#include "sim/dsm_tee_memory_driver.hh"
#include "sim/system.hh"

namespace gem5
{

DsmTeeController::DsmTeeControllerStats::DsmTeeControllerStats(
        DsmTeeController &ctrl)
    : statistics::Group(&ctrl),
      ADD_STAT(dsmRequests, statistics::units::Count::get(),
               "CXL requests targeting DSM-TEE regions"),
      ADD_STAT(nonDsmRequests, statistics::units::Count::get(),
               "CXL requests bypassed because they do not target "
               "DSM-TEE regions"),
      ADD_STAT(permissionChecks, statistics::units::Count::get(),
               "DSM-TEE data-path permission checks"),
      ADD_STAT(permissionCacheHits, statistics::units::Count::get(),
               "DSM-TEE permission cache hits"),
      ADD_STAT(permissionCacheMisses, statistics::units::Count::get(),
               "DSM-TEE permission cache misses"),
      ADD_STAT(permissionCacheInvalidations, statistics::units::Count::get(),
               "DSM-TEE permission cache invalidations after control-plane "
               "updates"),
      ADD_STAT(permissionDenied, statistics::units::Count::get(),
               "DSM-TEE data-path permission violations"),
      ADD_STAT(tlbMissPermissionChecks, statistics::units::Count::get(),
               "DSM-TEE permission checks caused by TLB misses targeting "
               "protected regions"),
      ADD_STAT(tlbMissMetadataReads, statistics::units::Count::get(),
               "DSM-TEE permission metadata reads caused by TLB-miss "
               "permission cache misses"),
      ADD_STAT(tlbMissMetadataReadBytes, statistics::units::Byte::get(),
               "DSM-TEE permission metadata bytes read for TLB-miss checks"),
      ADD_STAT(metadataReads, statistics::units::Count::get(),
               "DSM-TEE permission metadata reads from CXL memory"),
      ADD_STAT(metadataReadBytes, statistics::units::Byte::get(),
               "DSM-TEE permission metadata bytes read from CXL memory"),
      ADD_STAT(unmappedRequestors, statistics::units::Count::get(),
               "DSM-TEE region requests whose requestor could not be mapped "
               "to a VMID"),
      ADD_STAT(writebackBypass, statistics::units::Count::get(),
               "DSM-TEE region writebacks bypassing permission checks"),
      ADD_STAT(readPermitted, statistics::units::Count::get(),
               "DSM-TEE read permissions granted in the data path"),
      ADD_STAT(writePermitted, statistics::units::Count::get(),
               "DSM-TEE write permissions granted in the data path"),
      ADD_STAT(execPermitted, statistics::units::Count::get(),
               "DSM-TEE execute permissions granted in the data path"),
      ADD_STAT(readResponses, statistics::units::Count::get(),
               "DSM-TEE read responses with response-side protection "
               "overhead"),
      ADD_STAT(writeResponses, statistics::units::Count::get(),
               "DSM-TEE write responses with response-side protection "
               "overhead"),
      ADD_STAT(bytesRead, statistics::units::Byte::get(),
               "Bytes read from DSM-TEE regions"),
      ADD_STAT(bytesWritten, statistics::units::Byte::get(),
               "Bytes written to DSM-TEE regions"),
      ADD_STAT(tlbMissPermissionCheckDelay, statistics::units::Tick::get(),
               "Total DSM-TEE local delay from TLB-miss permission checks"),
      ADD_STAT(totalReqDelay, statistics::units::Tick::get(),
               "Total request-side DSM-TEE delay"),
      ADD_STAT(totalRespDelay, statistics::units::Tick::get(),
               "Total response-side DSM-TEE delay")
{
}

DsmTeeController::DsmTeeController(const DsmTeeControllerParams &p)
    : MemDelay(p), stats(*this), driver(p.driver), system(p.system),
      permCacheEntries(p.perm_cache_entries),
      permCheckCycles(p.perm_check_cycles),
      permCacheAccessCycles(p.perm_cache_access_cycles),
      permCacheHitLatency(p.perm_cache_hit_latency),
      permCacheMissLatency(p.perm_cache_miss_latency),
      metadataReadLatency(p.metadata_read_latency),
      metadataReadPackets(p.metadata_read_packets),
      ideReqCycles(p.ide_req_cycles), ideRespCycles(p.ide_resp_cycles),
      ideReqDelay(p.ide_req_delay), ideRespDelay(p.ide_resp_delay),
      encryptReadDelay(p.encrypt_read_delay),
      encryptWriteDelay(p.encrypt_write_delay),
      denyOnViolation(p.deny_on_violation),
      bypassNonDsm(p.bypass_non_dsm),
      cachedPermissionEpoch(p.driver ? p.driver->permissionEpoch() : 0)
{
    fatal_if(!driver, "%s requires a DSM-TEE memory driver", name());
    fatal_if(!system, "%s requires a system", name());
}

bool
DsmTeeController::PermissionCacheKey::operator==(
        const PermissionCacheKey &other) const
{
    return pageBase == other.pageBase && vmid == other.vmid &&
           perm == other.perm;
}

std::size_t
DsmTeeController::PermissionCacheKeyHash::operator()(
        const PermissionCacheKey &key) const
{
    std::size_t hash = std::hash<Addr>{}(key.pageBase);
    hash ^= std::hash<uint32_t>{}(key.vmid) + 0x9e3779b9 + (hash << 6) +
            (hash >> 2);
    hash ^= std::hash<uint8_t>{}(key.perm) + 0x9e3779b9 + (hash << 6) +
            (hash >> 2);
    return hash;
}

uint8_t
DsmTeeController::permissionForRequest(PacketPtr pkt) const
{
    if (!pkt->isRequest())
        return 0;

    uint8_t perm = 0;
    if (pkt->isRead()) {
        if (pkt->req && pkt->req->isInstFetch()) {
            perm |= DsmTeeMemoryDriver::PermExec;
        } else {
            perm |= DsmTeeMemoryDriver::PermRead;
        }
    }

    if (pkt->isWrite() || pkt->needsWritable())
        perm |= DsmTeeMemoryDriver::PermWrite;

    return perm;
}

bool
DsmTeeController::parseCpuVmid(const std::string &requestor_name,
                               uint32_t &vmid) const
{
    bool saw_cpu_token = false;

    for (std::size_t pos = requestor_name.find("cpu");
         pos != std::string::npos;
         pos = requestor_name.find("cpu", pos + 3)) {
        saw_cpu_token = true;
        std::size_t digit = pos + 3;
        if (digit < requestor_name.size() &&
            requestor_name[digit] == 's') {
            digit++;
        }
        if (digit >= requestor_name.size() ||
            !std::isdigit(static_cast<unsigned char>(requestor_name[digit]))) {
            continue;
        }

        uint64_t value = 0;
        while (digit < requestor_name.size() &&
               std::isdigit(static_cast<unsigned char>(
                       requestor_name[digit]))) {
            value = value * 10 + (requestor_name[digit] - '0');
            digit++;
        }

        fatal_if(value >= driver->numVmids(),
                 "%s maps requestor %s to VMID %llu, but num_vmids is %u",
                 name(), requestor_name, value, driver->numVmids());
        vmid = static_cast<uint32_t>(value);
        return true;
    }

    if (saw_cpu_token && driver->numVmids() == 1) {
        vmid = 0;
        return true;
    }

    return false;
}

bool
DsmTeeController::requestorToVmid(PacketPtr pkt, uint32_t &vmid) const
{
    const RequestorID requestor_id = pkt->requestorId();
    if (requestor_id >= system->maxRequestors())
        return false;

    return parseCpuVmid(system->getRequestorName(requestor_id), vmid);
}

Addr
DsmTeeController::permissionPageBase(Addr paddr) const
{
    const Addr page_size = driver->pageSizeBytes();
    if (page_size == 0)
        return paddr;
    return paddr - (paddr % page_size);
}

void
DsmTeeController::refreshPermissionCacheEpoch()
{
    const uint64_t epoch = driver->permissionEpoch();
    if (epoch == cachedPermissionEpoch)
        return;

    permissionCache.clear();
    permissionCacheLru.clear();
    cachedPermissionEpoch = epoch;
    stats.permissionCacheInvalidations++;
}

bool
DsmTeeController::hasCachedPermission(Addr paddr, uint32_t vmid, uint8_t perm)
{
    refreshPermissionCacheEpoch();

    if (permCacheEntries == 0)
        return false;

    const PermissionCacheKey key{permissionPageBase(paddr), vmid, perm};
    auto it = permissionCache.find(key);
    if (it == permissionCache.end())
        return false;

    permissionCacheLru.splice(permissionCacheLru.begin(), permissionCacheLru,
                              it->second.lruIt);
    it->second.lruIt = permissionCacheLru.begin();
    return true;
}

void
DsmTeeController::insertPermissionCache(Addr paddr, uint32_t vmid,
                                        uint8_t perm)
{
    if (permCacheEntries == 0)
        return;

    const PermissionCacheKey key{permissionPageBase(paddr), vmid, perm};
    if (permissionCache.find(key) != permissionCache.end())
        return;

    permissionCacheLru.push_front(key);
    permissionCache.emplace(
            key, PermissionCacheEntry{permissionCacheLru.begin()});

    while (permissionCache.size() > permCacheEntries) {
        const PermissionCacheKey &evicted = permissionCacheLru.back();
        permissionCache.erase(evicted);
        permissionCacheLru.pop_back();
    }
}

void
DsmTeeController::handlePermissionDenied(PacketPtr pkt, uint32_t vmid,
                                         uint8_t perm) const
{
    if (denyOnViolation) {
        panic("%s denied %s at %#x for VMID %u perm %#x\n",
              name(), pkt->cmdString(), pkt->getAddr(), vmid, perm);
    }

    warn("%s would deny %s at %#x for VMID %u perm %#x\n",
         name(), pkt->cmdString(), pkt->getAddr(), vmid, perm);
}

void
DsmTeeController::accountPermissionGranted(uint8_t perm)
{
    if (perm & DsmTeeMemoryDriver::PermRead)
        stats.readPermitted++;
    if (perm & DsmTeeMemoryDriver::PermWrite)
        stats.writePermitted++;
    if (perm & DsmTeeMemoryDriver::PermExec)
        stats.execPermitted++;
}

Tick
DsmTeeController::accessOverhead(PacketPtr pkt) const
{
    Tick delay = ideReqDelay;
    delay += cyclesToTicks(ideReqCycles);
    if (pkt->isWrite())
        delay += encryptWriteDelay;
    return delay;
}

DsmTeeController::PermissionLookup
DsmTeeController::checkPermission(PacketPtr pkt, Addr paddr,
                                  uint32_t vmid, uint8_t perm,
                                  bool resolve_metadata_miss,
                                  MetadataReadKind kind)
{
    PermissionLookup result;
    result.paddr = paddr;
    result.vmid = vmid;
    result.perm = perm;
    result.metadataKind = kind;

    const bool is_tlb_miss_check = kind == MetadataReadKind::TlbMiss;

    stats.permissionChecks++;
    if (is_tlb_miss_check)
        stats.tlbMissPermissionChecks++;

    const Tick local_check_delay =
        cyclesToTicks(permCheckCycles) +
        cyclesToTicks(permCacheAccessCycles);
    result.delay += local_check_delay;
    if (is_tlb_miss_check)
        stats.tlbMissPermissionCheckDelay += local_check_delay;

    bool permitted = false;
    if (hasCachedPermission(paddr, vmid, perm)) {
        stats.permissionCacheHits++;
        result.delay += permCacheHitLatency;
        permitted = true;
    } else {
        stats.permissionCacheMisses++;
        const Addr metadata_paddr = driver->metadataPaddrFor(paddr, vmid);
        fatal_if(metadata_paddr == 0,
                 "%s could not locate DSM-TEE metadata for %#x VMID %u",
                 name(), paddr, vmid);
        stats.metadataReads++;
        stats.metadataReadBytes += DsmTeeMemoryDriver::MetadataCacheLineBytes;
        if (is_tlb_miss_check) {
            stats.tlbMissMetadataReads++;
            stats.tlbMissMetadataReadBytes +=
                DsmTeeMemoryDriver::MetadataCacheLineBytes;
        }
        result.metadataMiss = true;
        result.metadataPaddr = metadata_paddr;
        if (resolve_metadata_miss) {
            result.delay += metadataReadLatency;
            result.delay += permCacheMissLatency;
            permitted = driver->hasAccess(paddr, vmid, perm);
            if (permitted)
                insertPermissionCache(paddr, vmid, perm);
        } else {
            result.permitted = true;
            return result;
        }
    }

    result.permitted = permitted;
    if (!permitted) {
        stats.permissionDenied++;
        handlePermissionDenied(pkt, vmid, perm);
    } else {
        accountPermissionGranted(perm);
    }

    return result;
}

DsmTeeController::PermissionLookup
DsmTeeController::permissionLookup(PacketPtr pkt,
                                   bool resolve_metadata_miss,
                                   bool include_tlb_miss)
{
    PermissionLookup result;

    const uint8_t perm = permissionForRequest(pkt);
    if (perm == 0)
        return result;

    const Addr paddr = pkt->getAddr();
    result.paddr = paddr;
    result.perm = perm;

    const uint64_t rid = driver->regionIdForPaddr(paddr);
    if (rid == 0) {
        stats.nonDsmRequests++;
        if (bypassNonDsm)
            return result;
    } else {
        stats.dsmRequests++;
    }

    result.delay = accessOverhead(pkt);
    if (pkt->isRead()) {
        stats.bytesRead += pkt->getSize();
    }
    if (pkt->isWrite()) {
        stats.bytesWritten += pkt->getSize();
    }

    uint32_t vmid = 0;
    if (!requestorToVmid(pkt, vmid)) {
        stats.unmappedRequestors++;
        if (pkt->isWriteback())
            stats.writebackBypass++;
        return result;
    }
    result.vmid = vmid;

    if (rid != 0 && include_tlb_miss && pkt->req &&
        pkt->req->isTlbMiss()) {
        const PermissionLookup tlb_lookup = checkPermission(
            pkt, paddr, vmid, perm, resolve_metadata_miss,
            MetadataReadKind::TlbMiss);
        result.delay += tlb_lookup.delay;
        if (tlb_lookup.metadataMiss) {
            result.metadataMiss = true;
            result.metadataPaddr = tlb_lookup.metadataPaddr;
            result.metadataKind = MetadataReadKind::TlbMiss;
            result.permitted = true;
            return result;
        }
    }

    const PermissionLookup data_lookup = checkPermission(
        pkt, paddr, vmid, perm, resolve_metadata_miss,
        MetadataReadKind::DataPath);
    result.delay += data_lookup.delay;
    if (data_lookup.metadataMiss) {
        result.metadataMiss = true;
        result.metadataPaddr = data_lookup.metadataPaddr;
        result.metadataKind = MetadataReadKind::DataPath;
        result.permitted = true;
        return result;
    }

    result.permitted = data_lookup.permitted;
    return result;
}

Tick
DsmTeeController::delayReq(PacketPtr pkt)
{
    const PermissionLookup lookup = permissionLookup(pkt, true);
    stats.totalReqDelay += lookup.delay;
    return lookup.delay;
}

PacketPtr
DsmTeeController::makeMetadataReadPacket(
        PacketPtr data_pkt, const PermissionLookup &lookup) const
{
    Request::Flags flags;
    if (data_pkt->isSecure())
        flags.set(Request::SECURE);

    auto req = std::make_shared<Request>(
            lookup.metadataPaddr,
            DsmTeeMemoryDriver::MetadataCacheLineBytes,
            flags,
            data_pkt->requestorId());
    PacketPtr metadata_pkt = Packet::createRead(req);
    metadata_pkt->allocate();
    metadata_pkt->pushSenderState(new MetadataReadSenderState(
            data_pkt, lookup.paddr, lookup.vmid, lookup.perm,
            lookup.metadataKind));
    return metadata_pkt;
}

bool
DsmTeeController::recvTimingReq(PacketPtr pkt, Tick receive_delay)
{
    if (!metadataReadPackets)
        return MemDelay::recvTimingReq(pkt, receive_delay);

    const PermissionLookup lookup = permissionLookup(pkt, false);
    stats.totalReqDelay += lookup.delay;

    if (!lookup.metadataMiss) {
        requestPort.schedTimingReq(pkt, curTick() + receive_delay +
                                   lookup.delay);
        return true;
    }

    PacketPtr metadata_pkt = makeMetadataReadPacket(pkt, lookup);
    requestPort.schedTimingReq(metadata_pkt, curTick() + receive_delay +
                               lookup.delay);
    return true;
}

void
DsmTeeController::finishMetadataRead(PacketPtr metadata_pkt,
                                     Tick receive_delay,
                                     MetadataReadSenderState *state)
{
    PacketPtr blocked_pkt = state->blockedPkt;
    const Addr paddr = state->dataPaddr;
    const uint32_t vmid = state->vmid;
    const uint8_t perm = state->perm;
    const MetadataReadKind kind = state->kind;

    bool permitted = driver->hasAccess(paddr, vmid, perm);
    if (permitted) {
        insertPermissionCache(paddr, vmid, perm);
    } else {
        stats.permissionDenied++;
        handlePermissionDenied(blocked_pkt, vmid, perm);
    }

    if (permitted) {
        accountPermissionGranted(perm);
    }

    stats.totalReqDelay += permCacheMissLatency;

    delete metadata_pkt->popSenderState();
    delete metadata_pkt;

    if (kind == MetadataReadKind::TlbMiss) {
        const PermissionLookup data_lookup = checkPermission(
            blocked_pkt, paddr, vmid, perm, false,
            MetadataReadKind::DataPath);
        const Tick delay = permCacheMissLatency + data_lookup.delay;
        stats.totalReqDelay += data_lookup.delay;

        if (data_lookup.metadataMiss) {
            PacketPtr data_metadata_pkt =
                makeMetadataReadPacket(blocked_pkt, data_lookup);
            requestPort.schedTimingReq(
                data_metadata_pkt, curTick() + receive_delay + delay);
            return;
        }

        requestPort.schedTimingReq(blocked_pkt, curTick() + receive_delay +
                                   delay);
        return;
    }

    requestPort.schedTimingReq(blocked_pkt, curTick() + receive_delay +
                               permCacheMissLatency);
}

bool
DsmTeeController::recvTimingResp(PacketPtr pkt, Tick receive_delay)
{
    auto *metadata_state =
        dynamic_cast<MetadataReadSenderState *>(pkt->senderState);
    if (metadata_state) {
        finishMetadataRead(pkt, receive_delay, metadata_state);
        return true;
    }

    return MemDelay::recvTimingResp(pkt, receive_delay);
}

Tick
DsmTeeController::delayResp(PacketPtr pkt)
{
    if (!pkt->isResponse())
        return 0;
    if (!pkt->isRead() && !pkt->isWrite())
        return 0;
    if (driver->regionIdForPaddr(pkt->getAddr()) == 0)
        return 0;

    Tick delay = ideRespDelay + cyclesToTicks(ideRespCycles);
    if (pkt->isRead()) {
        delay += encryptReadDelay;
        stats.readResponses++;
    }
    if (pkt->isWrite())
        stats.writeResponses++;

    stats.totalRespDelay += delay;
    return delay;
}

} // namespace gem5
