"""Conservative Modal Sandbox cost estimates, in USD per second.

Rates are configurable estimates copied from Modal's public pricing page. The
provider invoice remains authoritative and may include items this app cannot see.
"""

from dataclasses import dataclass

SANDBOX_CPU_PER_CORE_SECOND = 0.00003942
SANDBOX_RAM_PER_GIB_SECOND = 0.00000667


@dataclass(frozen=True)
class GPU:
    key: str
    name: str
    vram_gib: int
    usd_per_second: float
    note: str = ""


GPUS: tuple[GPU, ...] = (
    GPU("T4", "NVIDIA T4", 16, 0.000164),
    GPU("L4", "NVIDIA L4", 24, 0.000222),
    GPU("A10", "NVIDIA A10", 24, 0.000306),
    GPU("L40S", "NVIDIA L40S", 48, 0.000542),
    GPU("A100-40GB", "NVIDIA A100 40 GB", 40, 0.000583),
    GPU("A100-80GB", "NVIDIA A100 80 GB", 80, 0.000694),
    GPU("RTX-PRO-6000", "NVIDIA RTX PRO 6000", 96, 0.000842),
    GPU("H100", "NVIDIA H100", 80, 0.001097, "Modal may route H100 to H200."),
    GPU("H200", "NVIDIA H200", 141, 0.001261),
    GPU("B200", "NVIDIA B200", 180, 0.001736),
    GPU("B300", "NVIDIA B300", 288, 0.001972, "Requires CUDA 13.1+ compatible libraries."),
)
GPU_BY_KEY = {gpu.key: gpu for gpu in GPUS}

CPU_CHOICES = (2, 4, 8, 16)
RAM_CHOICES_GIB = (8, 16, 32, 64, 128)


def hourly_rate(gpu_key: str, cpus: int, memory_gib: int) -> float:
    gpu = GPU_BY_KEY[gpu_key]
    per_second = (
        gpu.usd_per_second
        + cpus * SANDBOX_CPU_PER_CORE_SECOND
        + memory_gib * SANDBOX_RAM_PER_GIB_SECOND
    )
    return per_second * 3600


def estimate_cost(gpu_key: str, cpus: int, memory_gib: int, seconds: float) -> float:
    return hourly_rate(gpu_key, cpus, memory_gib) * max(0.0, seconds) / 3600
