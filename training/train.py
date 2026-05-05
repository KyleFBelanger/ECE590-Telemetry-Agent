import os
import time
import torch
import torch.distributed as dist
import torch.nn as nn
from torch.nn.parallel import DistributedDataParallel as DDP
from torchvision import datasets, transforms
from torch.utils.data import DataLoader, DistributedSampler

def setup():
    dist.init_process_group(
        backend="gloo",
        init_method=f"tcp://{os.environ['MASTER_ADDR']}:{os.environ['MASTER_PORT']}",
        world_size=int(os.environ['WORLD_SIZE']),
        rank=int(os.environ['RANK'])
    )

def cleanup():
    dist.destroy_process_group()

class SimpleNet(nn.Module):
    def __init__(self):
        super().__init__()
        self.net = nn.Sequential(
            nn.Flatten(),
            nn.Linear(28*28, 128),
            nn.ReLU(),
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Linear(64, 10)
        )

    def forward(self, x):
        return self.net(x)

def train():
    rank = int(os.environ['RANK'])
    world_size = int(os.environ['WORLD_SIZE'])

    setup()

    if rank == 0:
        print(f"[Master] Cluster ready — {world_size} nodes connected")

    transform = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize((0.1307,), (0.3081,))
    ])

    dataset = datasets.MNIST(
        '/workspace/data',
        train=True,
        download=True,
        transform=transform
    )

    sampler = DistributedSampler(dataset, num_replicas=world_size, rank=rank)
    loader  = DataLoader(dataset, batch_size=64, sampler=sampler)

    model = DDP(SimpleNet())
    optimizer = torch.optim.SGD(model.parameters(), lr=0.01)
    criterion = nn.CrossEntropyLoss()

    for epoch in range(3):
        sampler.set_epoch(epoch)
        epoch_start = time.time()
        total_loss = 0

        for batch_idx, (data, target) in enumerate(loader):
            optimizer.zero_grad()
            output = model(data)
            loss = criterion(output, target)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()

        epoch_time = time.time() - epoch_start

        if rank == 0:
            avg_loss = total_loss / len(loader)
            print(f"Epoch {epoch+1} | Loss: {avg_loss:.4f} | Time: {epoch_time:.2f}s")

            with open('/workspace/results/training_log.txt', 'a') as f:
                f.write(f"Epoch {epoch+1} | Loss: {avg_loss:.4f} | Time: {epoch_time:.2f}s\n")

    cleanup()
    if rank == 0:
        print("Training complete!")

if __name__ == "__main__":
    train()
