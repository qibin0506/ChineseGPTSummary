import os

import torch
from torch import nn
from torch.utils.data import Dataset, DataLoader

from torch.utils.data.distributed import DistributedSampler
from torch.nn.parallel import DistributedDataParallel as DDP
import torch.distributed as dist

class DDPHelper:
    def __init__(self):
        self.global_rank = int(os.environ.get('RANK', -1))
        self.local_rank = int(os.environ.get('LOCAL_RANK', -1))

        self.ddp = self.global_rank != -1

        if self.ddp:
            self.device = f'cuda:{self.local_rank}'
            dist.init_process_group(backend='nccl')
            torch.cuda.set_device(self.device)

            print(f'global_rank:{self.global_rank},local_rank:{self.local_rank}, world_size:{dist.get_world_size()}')
        else:
            device = "cpu"
            if torch.cuda.is_available():
                device = "cuda"
            elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                device = "mps"

            self.device = device

        self.device_type = 'cuda' if self.device.startswith('cuda') else 'cpu'

    def process_model(self, model: nn.Module, ckpt_path: str) -> nn.Module:
        model.to(self.device)

        if self.ddp:
            ddp_init_path = f'{ckpt_path}_ddp_init.pth'
            if not os.path.exists(ckpt_path):
                if self.is_main_process():
                    ckpt = {'model': model.state_dict()}
                    torch.save(ckpt, ddp_init_path)

                dist.barrier()
                ckpt = torch.load(ddp_init_path, map_location=self.device)
                model.load_state_dict(ckpt['model'])
                print(f'load init ckpt for {self.device}')
            else:
                ckpt = torch.load(ckpt_path, map_location=self.device)
                model.load_state_dict(ckpt['model'])

            # self.model = DDP(module=model, broadcast_buffers=False, find_unused_parameters=True)
            self.model = DDP(module=model, device_ids=[self.local_rank], output_device=self.local_rank)
            self.raw_model = self.model.module
        else:
            self.model = model
            self.raw_model = model

            if os.path.exists(ckpt_path):
                ckpt = torch.load(ckpt_path, map_location=self.device)
                model.load_state_dict(ckpt['model'])

        return self.model

    def create_dataloader(self, dataset: Dataset, batch_size, collate_fn=None) -> DataLoader:
        if self.ddp:
            self.sampler = DistributedSampler(dataset=dataset, shuffle=True, drop_last=True)

            return DataLoader(
                dataset=dataset,
                batch_size=batch_size,
                pin_memory=True,
                sampler=self.sampler,
                collate_fn=collate_fn,
                num_workers=4
            )

        return DataLoader(
            dataset=dataset,
            batch_size=batch_size,
            shuffle=True,
            drop_last=True,
            collate_fn=collate_fn,
            num_workers=4
        )

    def on_epoch(self, epoch: int):
        if self.ddp:
            self.sampler.set_epoch(epoch)

    def end_epoch(self, epoch: int):
        if self.ddp:
            torch.cuda.synchronize(device=self.device)

    def reduce_loss(self, avg_loss: torch.Tensor, loss: torch.Tensor, batch) -> torch.Tensor:
        if self.ddp:
            world_size = dist.get_world_size()
            if world_size < 2:
                return loss.detach()

            torch.distributed.all_reduce(loss)
            # 整个训练过程的滑动损失均值=在历史平均损失的基础上，加上最新损失再求平均
            avg_loss = (avg_loss * batch + loss.detach()) / (batch + 1)
            return avg_loss

        return loss.detach()

    def is_main_process(self) -> bool:
        if self.ddp:
            return self.global_rank == 0

        return True

    def destroy(self):
        if self.ddp:
            dist.destroy_process_group()

    def world_size(self):
        if self.ddp:
            return dist.get_world_size()
        return 1


