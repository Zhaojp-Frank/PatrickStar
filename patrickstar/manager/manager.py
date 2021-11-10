# BSD 3-Clause License
#
# Copyright (C) 2021 THL A29 Limited, a Tencent company.  All rights reserved.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
#  * Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#
#  * Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
#  * Neither the name of the psutil authors nor the names of its contributors
#    may be used to endorse or promote products derived from this software without
#    specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
# ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


import time
import torch

from patrickstar.core.const import TrainingStage
from patrickstar.profiler import profiler
from patrickstar.utils import (
    get_memory_info,
    get_sys_memory_used,
    get_world_size,
    SingletonMeta,
    logger,
    max_mem_usage_period,
)
from .metronome import Metronome


class PatrickStarManager(metaclass=SingletonMeta):
    r"""Storing information about devices to help payload move."""

    def __init__(self, local_rank: int = 0, config=None):
        self.local_rank = local_rank
        self.gpu_chunk_available_mem = 0
        self.cpu_chunk_available_mem = 0

        self.gpu_chunk_used_mem = 0
        self.cpu_chunk_used_mem = 0

        self.copy_stream = torch.cuda.Stream()

        if config is not None:
            self._overall_gpu_mem_ratio = config.overall_gpu_mem_ratio
            self._overall_cpu_mem_ratio = config.overall_cpu_mem_ratio
            self._margin_use_ratio = config.margin_use_ratio
            self.warmup_gpu_chunk_mem_ratio = config.warmup_gpu_chunk_mem_ratio
            self.use_fake_dist = config.use_fake_dist
            self.always_warmup = config.always_warmup
        else:
            self._overall_gpu_mem_ratio = 0.8
            self._overall_cpu_mem_ratio = 0.8
            self._margin_use_ratio = 0.8
            self.warmup_gpu_chunk_mem_ratio = 0.2
            self.use_fake_dist = False
            self.always_warmup = False

        mem_info = get_memory_info()
        if self.use_fake_dist:
            # Fake distribtued mode: all processes share the same GPU.
            self._overall_gpu_mem = (
                torch.cuda.get_device_properties(0).total_memory
                * self._overall_gpu_mem_ratio
                / get_world_size()
            )
            self._overall_cpu_mem = (
                mem_info.total * self._overall_cpu_mem_ratio / get_world_size()
            )
        else:
            self._overall_gpu_mem = (
                torch.cuda.get_device_properties(self.local_rank).total_memory
                * self._overall_gpu_mem_ratio
            )
            self._overall_cpu_mem = (
                mem_info.total * self._overall_cpu_mem_ratio / get_world_size()
            )

        logger.info(
            f"Init Manager over all gpu mem {self._overall_gpu_mem / 1e6} MB, "
            f"cpu mem {self._overall_cpu_mem / 1e6} MB"
        )
        self.cpu_used_list = []
        self.cpu_chunk_used_list = []
        # non-chunk memory
        self.cpu_sys_used_list = []

        self.gpu_used_list = []
        self.gpu_chunk_used_list = []
        self.gpu_sys_used_list = []

        self._start_training = False
        self._training_stage = TrainingStage.UNSTART
        # The number of gpu chunks for adam.
        # Calculated by substracting the peak memory of fp16 params
        # from peak system memory.
        self._margin_chunk_num_for_gpu_adam = 0
        self._default_chunk_size = 0

    def set_training_stage(self, training_stage: TrainingStage):
        if profiler.started():
            profiler.stage_convert_time.append((time.time(), training_stage))
        self._training_stage = training_stage
        logger.info(f"Enter {self._training_stage}")

    def start_train(self, metronome: Metronome, param_fp16_chunk_size, chunk_size):
        metronome.set_warmup(True)
        self._start_training = True
        self._param_fp16_chunk_size = param_fp16_chunk_size
        self._default_chunk_size = chunk_size
        logger.info("Start to train.")

    def update_margin_mem(self):
        r"""Update the number of GPU free chunks for optimizer."""
        max_gpu_sys_used = max(self.gpu_sys_used_list)
        margin_mem_size = (
            self._overall_gpu_mem - max_gpu_sys_used - self._param_fp16_chunk_size
        )
        # 12 = 4 + 4 + 4 fp32 + m + v
        self._margin_chunk_num_for_gpu_adam = (
            (margin_mem_size) / (self._default_chunk_size * 12) * self._margin_use_ratio
        )

        logger.info("--------------- GPU INFO AFTER BWD ----------------")
        logger.info(
            f"Max GPU System Mem (non-chunk) Used {max(self.gpu_sys_used_list) / 1e6} MB"
        )
        logger.info(f"Param FP16 Chunk Size {self._param_fp16_chunk_size / 1e6} MB")
        logger.info(
            f"Margin Mem Size {margin_mem_size / 1e6} MB, "
            f"available chunk num for Optimizer States {self._margin_chunk_num_for_gpu_adam}"
        )
        logger.info(f"OVERALL GPU MEM {self._overall_gpu_mem}")

    def reset_memory_stats(self, metronome):
        """
        Reset statistics collected from memory tracing.
        It is used in case of gradient overflow during warmup and
        the memory stats is incomplete.
        """
        # As the reset happens right before forward, if the manager
        # is still doing warmup, it means the previous run didn't
        # cover the full procedure (forward -> backward -> optimizer).
        # Therefore clean the stats collected.
        if metronome.is_warmup():
            self.cpu_used_list = []
            self.cpu_chunk_used_list = []
            self.cpu_sys_used_list = []

            self.gpu_used_list = []
            self.gpu_chunk_used_list = []
            self.gpu_sys_used_list = []
        logger.info("Reset Memory Statistics")

    def get_margin_chunk_num_for_gpu_adam(self):
        return self._margin_chunk_num_for_gpu_adam

    def tiktac(self, client):
        """Record the memory usage of the moment and increase moment counter."""
        if torch.distributed.is_initialized():
            rank = client.local_rank
        else:
            rank = 0
        gpu_device = torch.device(f"cuda:{rank}")
        cpu_device = torch.device("cpu:0")
        gpu_used = get_sys_memory_used(gpu_device)

        if profiler.started():
            timestamp = time.time()
            cur_mom = client.metronome.moment()
            profiler.gpu_memory_used.append((cur_mom, timestamp, gpu_used))
            profiler.gpu_chunk_memory_used.append(
                (cur_mom, timestamp, self.gpu_chunk_used_mem)
            )
            # TODO(jiaruifang) the value of cpu_used does not
            # take into consideration of pinned mem.
            cpu_used = get_sys_memory_used(cpu_device)
            profiler.cpu_memory_used.append((cur_mom, timestamp, cpu_used))
            profiler.cpu_chunk_memory_used.append(
                (cur_mom, timestamp, self.cpu_chunk_used_mem)
            )

        if client.metronome.is_warmup():
            # max_mem_perid involves temp buff used inside an operator.
            max_mem_period = max_mem_usage_period()
            gpu_used = max(max_mem_period, gpu_used)
            self.gpu_used_list.append(gpu_used)
            self.gpu_chunk_used_list.append(self.gpu_chunk_used_mem)
            self.gpu_sys_used_list.append((gpu_used - self.gpu_chunk_used_mem))

            cpu_used = get_sys_memory_used(cpu_device)
            self.cpu_used_list.append(cpu_used)
            self.cpu_chunk_used_list.append(self.cpu_chunk_used_mem)
            self.cpu_sys_used_list.append((cpu_used - self.cpu_chunk_used_mem))

            # For non-warmup iter, we update the mem of index cur_mom,
            # and for warmup iter, we append the gpu mem to the end of the list.
            # Make sure the length of the list is 1 more than `cur_mom`.
            cur_mom = client.metronome.moment()
            assert (
                len(self.gpu_sys_used_list) - 1 == cur_mom
            ), f"{len(self.gpu_sys_used_list) - 1} vs {cur_mom}"
        else:
            # If the available chunk memory is smaller than current chunk memory,
            # we need to move chunks from compute device to make room.
            next_mom = client.metronome.next_moment()
            cur_mom = client.metronome.moment()
            gpu_next_mom_ava_chunk_mem = (
                self._overall_gpu_mem - self.gpu_sys_used_list[next_mom]
            )
            gpu_cur_mom_used_chunk_mem = client.chunk_list.get_chunk_memory_used(
                gpu_device
            )
            if gpu_next_mom_ava_chunk_mem < gpu_cur_mom_used_chunk_mem:
                offload_size = gpu_cur_mom_used_chunk_mem - gpu_next_mom_ava_chunk_mem
                # NOTE() Here will lead to GPU <-> CPU memory movement.
                client.chunk_list.make_room(offload_size, gpu_device)

            # The async memory monitor maybe time-consuming.
            # We only run it during warmup.
            # Calibrate the GPU sys used memory.
            # self.gpu_sys_used_list[cur_mom] = gpu_used - self.gpu_chunk_used_mem

        client.metronome.tiktac()

    def add(self, device_type: str, size_in_bytes: int):
        if device_type == "cpu":
            self.cpu_chunk_used_mem += size_in_bytes
        elif device_type == "cuda":
            self.gpu_chunk_used_mem += size_in_bytes
        else:
            raise f"device type {device_type} is not supported"

    def delete(self, device_type, size_in_bytes):
        if device_type == "cpu":
            self.cpu_chunk_used_mem -= size_in_bytes
        elif device_type == "cuda":
            self.gpu_chunk_used_mem -= size_in_bytes
        else:
            raise f"device type {device_type} is not supported"

    def free_chunk_mem(self, metronome, device_type):
        size = self.available_chunk_mem(metronome, device_type) - self.used_chunk_mem(
            device_type
        )
        logger.debug(
            f"free_chunk_mem on {device_type} {size / 1e6} MB on mement {metronome.moment()}"
        )
        return size

    def used_chunk_mem(self, device_type):
        if device_type == "cpu":
            return self.cpu_chunk_used_mem
        elif device_type == "cuda":
            return self.gpu_chunk_used_mem
        else:
            raise RuntimeError(f"used_chunk_mem {device_type}")

    def available_chunk_mem(self, metronome, device_type):
        r"""The amount of memory that can be used for chunks.

        This includes the memory that has been allocated for chunks
        and the free memory.

            available_chunk_mem = free_chunk_mem + used_chunk_mem

        In warmup, the available chunk mem is part of GPU mem and all
        CPU mem.
        After warmup, it is the minimal value of available mem of the
        current moment and next moment.
        """
        if device_type == "cpu":
            if metronome.is_warmup() or not self._start_training:
                # TODO(jiaruifang) using a guessed number -- 1/3 of the GPU
                # mem is used for chunk.
                return self._overall_cpu_mem
            else:
                return self._overall_cpu_mem
        elif device_type == "cuda":
            if self.always_warmup or metronome.is_warmup() or not self._start_training:
                if self._training_stage == TrainingStage.ADAM:
                    # There is no activation during Adam stage, so we can use all the GPU
                    # mem for chunks. Need 2 * default_chunk_size for buffer, save 6 here for now.
                    ava_mem = self._overall_gpu_mem - 4 * self._default_chunk_size * 4
                    logger.debug(f"GPU available_chunk_mem is {ava_mem / 1e6} MB")
                    return ava_mem
                else:
                    # TODO(jiaruifang) using a guessed number -- 1/3 of the GPU
                    # mem is used for chunk.
                    return self._overall_gpu_mem * self.warmup_gpu_chunk_mem_ratio
            else:
                world_size = get_world_size()
                if self._training_stage == TrainingStage.ADAM:
                    return self._overall_gpu_mem - 4 * self._default_chunk_size * 4
                elif self._training_stage == TrainingStage.FWD:
                    next_mom = metronome.next_moment()
                    cur_mom = metronome.moment()
                    next_mom_ava_mem = (
                        self._overall_gpu_mem - self.gpu_sys_used_list[next_mom]
                    )
                    cur_mom_ava_mem = (
                        self._overall_gpu_mem - self.gpu_sys_used_list[cur_mom]
                    )
                    return (
                        min(next_mom_ava_mem, cur_mom_ava_mem)
                        - world_size * 2 * self._default_chunk_size
                    )
                elif self._training_stage == TrainingStage.BWD:
                    next_mom = metronome.next_moment()
                    cur_mom = metronome.moment()
                    next_mom_ava_mem = (
                        self._overall_gpu_mem - self.gpu_sys_used_list[next_mom]
                    )
                    cur_mom_ava_mem = (
                        self._overall_gpu_mem - self.gpu_sys_used_list[cur_mom]
                    )
                    return (
                        min(next_mom_ava_mem, cur_mom_ava_mem)
                        - world_size * 2 * self._default_chunk_size
                    )
