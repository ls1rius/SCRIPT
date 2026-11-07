// Process-wide fixed CUDA arena. No device malloc/free after installation.
// Tensor frees stay in the arena; event dependencies protect cross-stream reuse.
#include <torch/csrc/cuda/CUDAPluggableAllocator.h>
#include <c10/cuda/CUDAException.h>
#include <cuda_runtime_api.h>
#include <map>
#include <set>
#include <vector>
#include <memory>
#include <mutex>
#include <algorithm>
#include <string>

namespace {
using Base = torch::cuda::CUDAPluggableAllocator::CUDAPluggableAllocator;
using Stats = c10::CachingDeviceAllocator::DeviceStats;
struct Event {
  cudaEvent_t event;
  explicit Event(cudaStream_t stream) {
    C10_CUDA_CHECK(cudaEventCreateWithFlags(&event, cudaEventDisableTiming));
    C10_CUDA_CHECK(cudaEventRecord(event, stream));
  }
  ~Event() { cudaEventDestroy(event); }
};
struct FreeBlock { size_t bytes; std::vector<std::shared_ptr<Event>> events; };
struct LiveBlock { size_t bytes; std::set<cudaStream_t> streams; };
struct Arena {
  std::mutex mutex;
  void* memory = nullptr;
  size_t capacity = 0, limit = 0, used = 0, total = 0;
  int device = 0;
  std::map<size_t, FreeBlock> free;
  std::map<void*, LiveBlock> live;
  Stats stats;
  void check_device(int d) { TORCH_CHECK(d == device, "Physical CUDA pool is single-device"); }
  void stat_bytes(int64_t n) {
    for (auto* arr : {&stats.allocated_bytes, &stats.active_bytes, &stats.requested_bytes}) {
      if (n >= 0) (*arr)[0].increase(n); else (*arr)[0].decrease(-n);
    }
  }
  void* alloc(size_t bytes, int d, cudaStream_t stream) {
    std::lock_guard<std::mutex> lock(mutex);
    check_device(d);
    C10_CUDA_CHECK(cudaSetDevice(device));
    TORCH_CHECK(!([](cudaStream_t s) { cudaStreamCaptureStatus status;
      C10_CUDA_CHECK(cudaStreamIsCapturing(s, &status));
      return status != cudaStreamCaptureStatusNone; })(stream),
      "Physical CUDA pool does not support CUDA graph capture");
    if (!bytes) return nullptr;
    TORCH_CHECK(bytes <= SIZE_MAX - 511, "Allocation size overflow");
    const size_t size = (bytes + 511) & ~size_t(511);
    auto best = free.end();
    if (size <= limit && used <= limit - size) {
      for (auto it = free.begin(); it != free.end(); ++it)
        if (it->second.bytes >= size && (best == free.end() || it->second.bytes < best->second.bytes)) best = it;
    }
    if (best == free.end()) {
      ++stats.num_ooms;
      TORCH_CHECK_WITH(OutOfMemoryError, false,
        "Physical CUDA pool exhausted: requested=", size, " live=", used,
        " limit=", limit, " reserved=", capacity,
        "; no fallback device allocation (capacity or fragmentation)");
    }
    size_t offset = best->first;
    FreeBlock block = std::move(best->second);
    free.erase(best);
    for (auto& e : block.events) C10_CUDA_CHECK(cudaStreamWaitEvent(stream, e->event, 0));
    if (block.bytes > size) free.emplace(offset + size, FreeBlock{block.bytes-size, std::move(block.events)});
    void* ptr = static_cast<char*>(memory) + offset;
    live.emplace(ptr, LiveBlock{size, {stream}});
    used += size;
    stat_bytes(size);
    stats.allocation[0].increase(1);
    stats.active[0].increase(1);
    return ptr;
  }
  void release(void* ptr, size_t, int d, cudaStream_t) {
    if (!ptr) return;
    std::lock_guard<std::mutex> lock(mutex);
    check_device(d);
    C10_CUDA_CHECK(cudaSetDevice(device));
    auto it = live.find(ptr);
    TORCH_CHECK(it != live.end(), "Unknown physical pool pointer");
    FreeBlock block{it->second.bytes, {}};
    for (auto stream : it->second.streams) block.events.push_back(std::make_shared<Event>(stream));
    const size_t bytes = block.bytes;
    size_t offset = static_cast<char*>(ptr) - static_cast<char*>(memory);
    live.erase(it);
    // Coalesce spatial neighbours, preserving every outstanding stream dependency.
    auto next = free.lower_bound(offset);
    if (next != free.end() && offset + block.bytes == next->first) {
      block.bytes += next->second.bytes;
      block.events.insert(block.events.end(), next->second.events.begin(), next->second.events.end());
      free.erase(next);
    }
    next = free.lower_bound(offset);
    if (next != free.begin()) {
      auto prev = std::prev(next);
      if (prev->first + prev->second.bytes == offset) {
        offset = prev->first;
        block.bytes += prev->second.bytes;
        block.events.insert(block.events.end(), prev->second.events.begin(), prev->second.events.end());
        free.erase(prev);
      }
    }
    // Completed events can be discarded without device synchronization.
    block.events.erase(std::remove_if(block.events.begin(), block.events.end(), [](auto& event) {
      auto status = cudaEventQuery(event->event);
      if (status == cudaErrorNotReady) return false;
      C10_CUDA_CHECK(status);
      return true;
    }), block.events.end());
    free.emplace(offset, std::move(block));
    used -= bytes;
    stat_bytes(-static_cast<int64_t>(bytes));
    stats.allocation[0].decrease(1);
    stats.active[0].decrease(1);
  }
};
Arena arena; // cudaFree is deliberately left to process teardown.
struct Allocator final : Base {
  Allocator() : Base(
    [](size_t n, int d, cudaStream_t s) { return arena.alloc(n,d,s); },
    [](void* p, size_t n, int d, cudaStream_t s) { arena.release(p,n,d,s); }) {
    set_memory_fraction_fn([](double fraction, int d) {
      std::lock_guard<std::mutex> lock(arena.mutex);
      arena.check_device(d);
      size_t limit = static_cast<size_t>(fraction * arena.total + 0.5);
      TORCH_CHECK(limit <= arena.capacity, "Stage limit exceeds physical CUDA pool");
      TORCH_CHECK(limit >= arena.used, "Stage limit smaller than live tensors");
      arena.limit = limit;
    });
    set_record_stream_fn([](void* p, cudaStream_t s) {
      if (!p) return;
      std::lock_guard<std::mutex> lock(arena.mutex);
      arena.live.at(p).streams.insert(s);
    });
    set_base_alloc_fn([](void*, size_t* size) { if (size) *size = arena.capacity; return arena.memory; });
  }
  Stats getDeviceStats(c10::DeviceIndex d) override {
    std::lock_guard<std::mutex> lock(arena.mutex); arena.check_device(d); return arena.stats;
  }
  void resetPeakStats(c10::DeviceIndex d) override {
    std::lock_guard<std::mutex> lock(arena.mutex); arena.check_device(d);
    for (auto* arr : {&arena.stats.allocated_bytes, &arena.stats.active_bytes,
        &arena.stats.requested_bytes, &arena.stats.reserved_bytes,
        &arena.stats.allocation, &arena.stats.active, &arena.stats.segment})
      for (auto& stat : *arr) stat.reset_peak();
  }
  void resetAccumulatedStats(c10::DeviceIndex d) override {
    std::lock_guard<std::mutex> lock(arena.mutex); arena.check_device(d);
    for (auto* arr : {&arena.stats.allocated_bytes, &arena.stats.active_bytes,
        &arena.stats.requested_bytes, &arena.stats.reserved_bytes,
        &arena.stats.allocation, &arena.stats.active, &arena.stats.segment})
      for (auto& stat : *arr) stat.reset_accumulated();
    arena.stats.num_ooms = 0;
  }
  double getMemoryFraction(c10::DeviceIndex d) override {
    std::lock_guard<std::mutex> lock(arena.mutex); arena.check_device(d);
    return double(arena.limit) / arena.total;
  }
  void beginAllocateToPool(c10::DeviceIndex, c10::cuda::MempoolId_t,
                          std::function<bool(cudaStream_t)>) override {
    TORCH_CHECK(false, "Physical CUDA pool does not support CUDA graph capture");
  }
  std::string name() override { return "videoagent_physical_pool"; }
};
std::string error;
}
extern "C" const char* videoagent_install_pool(size_t capacity, int device) {
  try {
    TORCH_CHECK(!arena.memory, "Physical CUDA pool already installed");
    TORCH_CHECK(!c10::cuda::CUDACachingAllocator::get()->initialized(),
                "Install physical CUDA pool before any PyTorch CUDA initialization");
    C10_CUDA_CHECK(cudaSetDevice(device));
    cudaDeviceProp prop;
    C10_CUDA_CHECK(cudaGetDeviceProperties(&prop, device));
    TORCH_CHECK(capacity > 0 && capacity < prop.totalGlobalMem, "Invalid physical CUDA pool capacity");
    void* memory;
    C10_CUDA_CHECK(cudaMalloc(&memory, capacity));
    arena.memory = memory; arena.capacity = arena.limit = capacity;
    arena.device = device; arena.total = prop.totalGlobalMem;
    arena.free.emplace(0, FreeBlock{capacity, {}});
    arena.stats.reserved_bytes[0].increase(capacity);
    arena.stats.segment[0].increase(1);
    arena.stats.num_device_alloc = 1;
    auto allocator = std::make_shared<Allocator>();
    int count; C10_CUDA_CHECK(cudaGetDeviceCount(&count));
    allocator->init(count);
    torch::cuda::CUDAPluggableAllocator::changeCurrentAllocator(allocator);
    return nullptr;
  } catch (const std::exception& exc) { error = exc.what(); return error.c_str(); }
}
