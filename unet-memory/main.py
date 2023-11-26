"""U-Net Memory Benchmark"""
import platform
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

import click
import torch
from torch import Tensor, nn
import torch.nn.functional as F
from torch.optim import SGD

import torchgpipe
from torchgpipe.balance import balance_by_time
from torchgpipe.balance import balance_by_size
from torchgpipe import GPipe
from unet import unet

start_time = time.time()

Stuffs = Tuple[nn.Module, int, int, List[torch.device]]  # (model, B, C, devices)
Experiment = Callable[[List[int]], Stuffs]


class Experiments:

    @staticmethod
    def baseline(devices: List[int]) -> Stuffs:
        B, C = 5, 70
        balance = [241]

        model: nn.Module = unet(depth=5, num_convs=B, base_channels=C,
                                input_channels=3, output_channels=1)
        model = cast(nn.Sequential, model)
        model = GPipe(model, balance, devices=devices, chunks=32)

        return model, B, C, list(model.devices)

    @staticmethod
    def pipeline1(devices: List[int]) -> Stuffs:
        B, C = 5, 70

        # Crear el modelo U-Net
        model: nn.Module = unet(depth=5, num_convs=B, base_channels=C,
                                input_channels=3, output_channels=1)
        model = cast(nn.Sequential, model)

        # Crear un tensor de muestra para el balanceo de tiempo
        partitions = len(devices)
        sample = torch.rand(128, 3, 224, 224, device=devices[0])  # Usar el primer dispositivo
        balance = balance_by_time(partitions, model, sample)

        # Crear el modelo GPipe con el balanceo de tiempo
        model = GPipe(model, balance, devices=devices, chunks=32)

        return model, B, C, list(model.devices)

    @staticmethod
    def pipeline2(devices: List[int]) -> Stuffs:
        B, C = 5, 70

        # Crear el modelo U-Net
        model: nn.Module = unet(depth=5, num_convs=B, base_channels=C,
                                input_channels=3, output_channels=1)
        model = cast(nn.Sequential, model)

        # Crear un tensor de muestra para el balanceo de tiempo
        partitions = len(devices)
        sample = torch.rand(32, 3, 192, 192, device=devices[0])  # Usar el primer dispositivo
        balance = balance_by_time(partitions, model, sample)

        # Crear el modelo GPipe con el balanceo de tiempo
        model = GPipe(model, balance, devices=devices, chunks=32)

        return model, B, C, list(model.devices)

    @staticmethod
    def pipeline4(devices: List[int]) -> Stuffs:
        B, C = 5, 70

        # Crear el modelo U-Net
        model: nn.Module = unet(depth=5, num_convs=B, base_channels=C,
                                input_channels=3, output_channels=1)
        model = cast(nn.Sequential, model)

        # Crear un tensor de muestra para el balanceo por tamaño
        sample = torch.empty(32, 3, 192, 192, device=devices[0])  # Same size as the mini-batch to train
        balance = balance_by_size(torch.cuda.device_count(), model, sample, chunks=8, param_scale=4.0)

        # Crear el modelo GPipe con el balanceo por tamaño
        model = GPipe(model, balance, devices=devices, chunks=32)

        return model, B, C, list(model.devices)

    @staticmethod
    def pipeline8(devices: List[int]) -> Stuffs:
        B, C = 5, 70

        # Crear el modelo U-Net
        model: nn.Module = unet(depth=5, num_convs=B, base_channels=C,
                                input_channels=3, output_channels=1)
        model = cast(nn.Sequential, model)

        # Crear un tensor de muestra para el balanceo por tamaño
        sample_size = torch.empty(32, 3, 192, 192, device=devices[0])  # Same size as the mini-batch to train
        balance_size = balance_by_size(torch.cuda.device_count(), model, sample_size, chunks=8, param_scale=4.0)

        # Crear un tensor de muestra para el balanceo por tiempo
        sample_time = torch.empty(32, 3, 192, 192, device=devices[0])  # Adapted size for time-based balancing
        balance_time = balance_by_time(torch.cuda.device_count(), model, sample_time)

        # Combinar los dos balances utilizando un promedio ponderado (puedes ajustar los pesos)
        alpha = 0.5  # Puedes ajustar este valor según sea necesario
        balance = [(1 - alpha) * s + alpha * t for s, t in zip(balance_size, balance_time)]

        # Crear el modelo GPipe con el balanceo combinado
        model = GPipe(model, balance, devices=devices, chunks=32)

        return model, B, C, list(model.devices)


EXPERIMENTS: Dict[str, Experiment] = {
    'baseline': Experiments.baseline,
    'pipeline-1': Experiments.pipeline1,
    'pipeline-2': Experiments.pipeline2,
    'pipeline-4': Experiments.pipeline4,
    'pipeline-8': Experiments.pipeline8,
}


def hr() -> None:
    """Prints a horizontal line."""
    width, _ = click.get_terminal_size()
    click.echo('-' * width)


def parse_devices(ctx: Any, param: Any, value: Optional[str]) -> List[int]:
    if value is None:
        return list(range(torch.cuda.device_count()))
    return [int(x) for x in value.split(',')]


@click.command()
@click.pass_context
@click.argument(
    'experiment',
    type=click.Choice(sorted(EXPERIMENTS.keys())),
)
@click.option(
    '--devices', '-d',
    metavar='0,1,2,3',
    callback=parse_devices,
    help='Device IDs to use (default: all CUDA devices)',
)
def cli(ctx: click.Context,
        experiment: str,
        devices: List[int],
        ) -> None:
    """U-Net (B, C) Memory Benchmark"""
    f = EXPERIMENTS[experiment]
    try:
        model, B, C, _devices = f(devices)
    except ValueError as exc:
        # Examples:
        #   ValueError: too few devices to hold given partitions (devices: 1, paritions: 2)
        ctx.fail(str(exc))

    optimizer = SGD(model.parameters(), lr=0.1)

    in_device = _devices[0]
    out_device = _devices[-1]
    torch.cuda.set_device(in_device)

    input = torch.rand(32, 3, 192, 192, device=in_device)
    target = torch.rand(32, 1, 192, 192, device=out_device)

    # HEADER ======================================================================================

    title = f'{experiment}, U-Net ({B}, {C})'
    click.echo(title)

    if isinstance(model, GPipe):
        click.echo(f'balance: {model.balance}')

    click.echo('torchgpipe: %s, python: %s, torch: %s, cudnn: %s, cuda: %s, gpu: %s' % (
        torchgpipe.__version__,
        platform.python_version(),
        torch.__version__,
        torch.backends.cudnn.version(),
        torch.version.cuda,
        torch.cuda.get_device_name(in_device)))

    hr()

    # PARAMETERS ==================================================================================

    param_count = sum(p.storage().size() for p in model.parameters())
    param_size = sum(p.storage().size() * p.storage().element_size() for p in model.parameters())
    param_scale = 2  # param + grad

    click.echo(f'# of Model Parameters: {param_count:,}')
    click.echo(f'Total Model Parameter Memory: {param_size*param_scale:,} Bytes')

    # ACTIVATIONS =================================================================================

    try:
        torch.cuda.empty_cache()
        for d in _devices:
            torch.cuda.reset_max_memory_cached(d)

        for _ in range(2):
            output = model(input)
            output = cast(Tensor, output)
            loss = F.binary_cross_entropy_with_logits(output, target)
            loss.backward()
            optimizer.step()

        max_memory = 0
        for d in _devices:
            torch.cuda.synchronize(d)
            max_memory += torch.cuda.max_memory_cached(d)

        latent_size = max_memory - param_size*param_scale
        click.echo(f'Peak Activation Memory: {latent_size:,} Bytes')
        click.echo(f'Total Memory: {max_memory:,} Bytes')
        end_time = time.time()
        elapsed_time = end_time - start_time
        click.echo(f'Tiempo tomado: {elapsed_time:,}')

    # MAX MEMORY PER DEVICE =======================================================================

    finally:
        hr()

        for d in _devices:
            memory_usage = torch.cuda.memory_cached(d)
            click.echo(f'{d!s}: {memory_usage:,} Bytes')


if __name__ == '__main__':
    cli()
