import json
import os
import random
import string
import uuid
import shutil
import pickle

from typing import Optional, Union
from pprint import pprint

import configargparse

import sys
from contextlib import contextmanager, redirect_stderr, redirect_stdout

from bayeso_benchmarks.two_dim_branin import Branin as BraninFunction

import matplotlib.pyplot as plt

from forward import ForwardModel

@contextmanager
def suppress_output():
    """
        A context manager that redirects stdout and stderr to devnull
        https://stackoverflow.com/a/52442331
    """
    with open(os.devnull, 'w') as fnull:
        with redirect_stderr(fnull) as err, redirect_stdout(fnull) as out:
            yield (err, out)


import numpy as np
import pandas as pd
import pytorch_lightning as pl
import pickle as pkl

import torch
from torch.utils.data import Dataset, DataLoader

from nets import DiffusionTest, DiffusionScore
from util import TASKNAME2TASK, configure_gpu, set_seed, get_weights

args_filename = "args.json"
checkpoint_dir = "checkpoints"
wandb_project = "sde-flow"

class Branin:

    def __init__(self, path="design_baselines/diff_branin/dataset/branin_gaussian_5k.p"):
        data = pickle.load(open(path, "rb"))
        self.x = data[0].astype(np.float32)
        self.y = data[1].astype(np.float32)

        self.mean_x = self.x.mean(axis=0)
        self.std_x = self.x.std(axis=0)

        self.mean_y = self.y.mean(axis=0)
        self.std_y = self.y.std(axis=0)

        self.is_x_normalized = False
        self.is_y_normalized = False

        self.is_discrete = False
        self.obj_func = BraninFunction()


    def map_normalize_x(self):
        self.x = (self.x - self.mean_x) / self.std_x
        self.is_x_normalized = True

    def map_normalize_y(self):
        self.y = (self.y - self.mean_y) / self.std_y
        self.is_y_normalized = True

    def predict(self, x):

        if self.is_x_normalized:
            x = x * self.std_x + self.mean_x

        x[:, 0] = np.clip(x[:, 0], self.obj_func.bounds[0, 0], self.obj_func.bounds[0, 1])
        x[:, 1] = np.clip(x[:, 1], self.obj_func.bounds[1, 0], self.obj_func.bounds[1, 1])

        return 2*(-self.obj_func.output(x))+2

    def denormalize_y(self, y):
        return y * self.std_y + self.mean_y

class RvSDataset(Dataset):

    def __init__(self, task, x, y, w=None, device=None, mode='train'):
        self.task = task
        self.device = device
        self.mode = mode
        self.x = x
        self.y = y
        self.w = w

    def __len__(self):
        return self.x.shape[0]

    def __getitem__(self, idx):
        x = torch.tensor(self.x[idx])
        y = torch.tensor(self.y[idx])
        if self.w is not None:
            w = torch.tensor(self.w[idx])
        else:
            w = None
        '''
        if self.device is not None:
            x = x.to(self.device)
            y = y.to(self.device)
            if w is not None:
                w = w.to(self.device)
        '''
        if w is None:
            return x, y
        else:
            return x, y, w


def split_dataset(task, val_frac=None, device=None):
    # print("Args: ", normalise_x, normalise_y)
    length = task.y.shape[0]
    shuffle_idx = np.arange(length)
    shuffle_idx = np.random.shuffle(shuffle_idx)

    if task.is_discrete:
        task.map_to_logits()
        x = task.x[shuffle_idx]
        x = x.reshape(x.shape[1:])
        x = x.reshape(x.shape[0], -1)
    else:
        x = task.x[shuffle_idx]

    y = task.y[shuffle_idx]
    if not task.is_discrete:
        x = x.reshape(-1, task.x.shape[-1])

    print("shapesss", x.shape, y.shape)
    y = y.reshape(-1, 1)
    print("shapesss", x.shape, y.shape)

    # w = get_weights(y, base_temp=0.03 * length)
    w = get_weights(y, base_temp=0.1)
    # w = None

    # TODO: Modify
    # full_ds = DKittyMorphologyDataset()
    # y = (y - full_ds.y.min()) / (full_ds.y.max() - full_ds.y.min())

    # print(w)
    # print(w.shape)

    if val_frac is None:
        val_frac = 0

    val_length = int(length * val_frac)
    train_length = length - val_length

    train_dataset = RvSDataset(
        task,
        x[:train_length],
        y[:train_length],
        # None,
        w[:train_length],
        device,
        mode='train')
    val_dataset = RvSDataset(
        task,
        x[train_length:],
        y[train_length:],
        # None,
        w[train_length:],
        device,
        mode='val')

    # print("Dataset shapes:", x.shape, y.shape, w.shape)

    return train_dataset, val_dataset


class RvSDataModule(pl.LightningDataModule):

    def __init__(self, task, batch_size, num_workers, val_frac, device, normalise_x, normalise_y):
        super().__init__()

        self.task = task
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.val_frac = val_frac
        self.device = device
        self.train_dataset = None
        self.val_dataset = None
        self.normalise_x = normalise_x
        self.normalise_y = normalise_y


    def setup(self, stage=None):
        self.train_dataset, self.val_dataset = split_dataset(
            self.task, self.val_frac, self.device)

    def train_dataloader(self):
        train_loader = DataLoader(self.train_dataset,
                                  num_workers=self.num_workers,
                                  batch_size=self.batch_size)
        return train_loader

    def val_dataloader(self):
        val_loader = DataLoader(self.val_dataset,
                                num_workers=self.num_workers,
                                batch_size=self.batch_size)
        return val_loader


def log_args(
    args: configargparse.Namespace,
    wandb_logger: pl.loggers.wandb.WandbLogger,
) -> None:
    """Log arguments to a file in the wandb directory."""
    wandb_logger.log_hyperparams(args)

    args.wandb_entity = wandb_logger.experiment.entity
    args.wandb_project = wandb_logger.experiment.project
    args.wandb_run_id = wandb_logger.experiment.id
    args.wandb_path = wandb_logger.experiment.path

    out_directory = wandb_logger.experiment.dir
    pprint(f"out_directory: {out_directory}")
    args_file = os.path.join(out_directory, args_filename)
    with open(args_file, "w") as f:
        try:
            json.dump(args.__dict__, f)
        except AttributeError:
            json.dump(args, f)

def run_training_forward(
    taskname: str,
    seed: int,
    wandb_logger: pl.loggers.wandb.WandbLogger,
    args,
    device=None,
):
    epochs = args.epochs
    max_steps = args.max_steps
    train_time = args.train_time
    hidden_size = args.hidden_size
    depth = args.depth
    learning_rate = args.learning_rate
    auto_tune_lr = args.auto_tune_lr
    dropout_p = args.dropout_p
    checkpoint_every_n_epochs = args.checkpoint_every_n_epochs
    checkpoint_every_n_steps = args.checkpoint_every_n_steps
    checkpoint_time_interval = args.checkpoint_time_interval
    batch_size = args.batch_size
    val_frac = args.val_frac
    use_gpu = args.use_gpu
    device = device
    num_workers = args.num_workers
    vtype = args.vtype
    T0 = args.T0
    normalise_x = args.normalise_x
    normalise_y = args.normalise_y
    debias = args.debias
    score_matching = args.score_matching

    set_seed(seed)
    if taskname == "branin":
        task = Branin(path=args.path)
    elif taskname != 'tf-bind-10':
        task = design_bench.make(TASKNAME2TASK[taskname])
    else:
        task = design_bench.make(TASKNAME2TASK[taskname],
                                 dataset_kwargs={"max_samples": 10000})

    if normalise_x:
        task.map_normalize_x()
    if normalise_y:
        task.map_normalize_y()

    if task.is_discrete:
        task.map_to_logits()

    model = ForwardModel(taskname=taskname,
                          task=task,
                          learning_rate=learning_rate,
                          hidden_size=hidden_size,
                          vtype=vtype,
                          beta_min=args.beta_min,
                          beta_max=args.beta_max,
                          simple_clip=args.simple_clip,
                          T0=T0,
                          debias=debias,
                          dropout_p=dropout_p)

    monitor = "val_loss" if val_frac > 0 else "train_loss"
    checkpoint_dirpath = os.path.join(wandb_logger.experiment.dir,
                                      checkpoint_dir)
    checkpoint_filename = f"{taskname}_{seed}-" + "-{epoch:03d}-{" + f"{monitor}" + ":.4e}"
    periodic_checkpoint_callback = pl.callbacks.ModelCheckpoint(
        dirpath=checkpoint_dirpath,
        filename=checkpoint_filename,
        save_last=False,
        save_top_k=-1,
        every_n_epochs=checkpoint_every_n_epochs,
        every_n_train_steps=checkpoint_every_n_steps,
        train_time_interval=pd.Timedelta(checkpoint_time_interval).
        to_pytimedelta() if checkpoint_time_interval is not None else None,
    )
    val_checkpoint_callback = pl.callbacks.ModelCheckpoint(
        dirpath=checkpoint_dirpath,
        monitor=monitor,
        filename=checkpoint_filename,
        save_last=True,  # save latest model
        save_top_k=1,  # save top model based on monitored loss
    )
    trainer = pl.Trainer(
        gpus=int(use_gpu),
        auto_lr_find=auto_tune_lr,
        max_epochs=epochs,
        max_steps=max_steps,
        max_time=train_time,
        logger=wandb_logger,
        progress_bar_refresh_rate=20,
        callbacks=[periodic_checkpoint_callback, val_checkpoint_callback],
        track_grad_norm=2,  # logs the 2-norm of gradients
        limit_val_batches=1.0 if val_frac > 0 else 0,
        limit_test_batches=0,
    )

    # train_dataset, val_dataset = split_dataset(task=task,
    #                                            val_frac=val_frac,
    #                                            device=device)
    # train_data_module = DataLoader(train_dataset)  #, num_workers=num_workers)
    # val_data_module = DataLoader(val_dataset)  #, num_workers=num_workers)

    # trainer.fit(model, train_data_module, val_data_module)
    data_module = RvSDataModule(task=task,
                                val_frac=val_frac,
                                device=device,
                                batch_size=batch_size,
                                normalise_x=normalise_x,
                                normalise_y=normalise_y,
                                num_workers=num_workers,)
    trainer.fit(model, data_module)





def run_training(
    taskname: str,
    seed: int,
    wandb_logger: pl.loggers.wandb.WandbLogger,
    args,
    device=None,
):
    epochs = args.epochs
    max_steps = args.max_steps
    train_time = args.train_time
    hidden_size = args.hidden_size
    depth = args.depth
    learning_rate = args.learning_rate
    auto_tune_lr = args.auto_tune_lr
    dropout_p = args.dropout_p
    checkpoint_every_n_epochs = args.checkpoint_every_n_epochs
    checkpoint_every_n_steps = args.checkpoint_every_n_steps
    checkpoint_time_interval = args.checkpoint_time_interval
    batch_size = args.batch_size
    val_frac = args.val_frac
    use_gpu = args.use_gpu
    device = device
    num_workers = args.num_workers
    vtype = args.vtype
    T0 = args.T0
    normalise_x = args.normalise_x
    normalise_y = args.normalise_y
    debias = args.debias
    score_matching = args.score_matching

    set_seed(seed)
    if taskname == "branin":
        task = Branin(path=args.path)
    elif taskname != 'tf-bind-10':
        task = design_bench.make(TASKNAME2TASK[taskname])
    else:
        task = design_bench.make(TASKNAME2TASK[taskname], dataset_kwargs={"max_samples": 10000})

    if normalise_x:
        task.map_normalize_x()
    if normalise_y:
        task.map_normalize_y()

    if task.is_discrete:
        task.map_to_logits()

    if not score_matching:
        model = DiffusionTest(taskname=taskname,
                              task=task,
                              learning_rate=learning_rate,
                              hidden_size=hidden_size,
                              vtype=vtype,
                              beta_min=args.beta_min,
                              beta_max=args.beta_max,
                              simple_clip=args.simple_clip,
                              T0=T0,
                              debias=debias,
                              dropout_p=dropout_p)
    else:
        print("Score matching loss")
        model = DiffusionScore(taskname=taskname,
                               task=task,
                               learning_rate=learning_rate,
                               hidden_size=hidden_size,
                               vtype=vtype,
                               beta_min=args.beta_min,
                               beta_max=args.beta_max,
                               simple_clip=args.simple_clip,
                               T0=T0,
                               debias=debias,
                               dropout_p=dropout_p)

    # monitor = "val_loss" if val_frac > 0 else "train_loss"
    monitor = "elbo_estimator" if val_frac > 0 else "train_loss"
    checkpoint_dirpath = os.path.join(wandb_logger.experiment.dir,
                                      checkpoint_dir)
    checkpoint_filename = f"{taskname}_{seed}-" + "-{epoch:03d}-{" + f"{monitor}" + ":.4e}"
    periodic_checkpoint_callback = pl.callbacks.ModelCheckpoint(
        dirpath=checkpoint_dirpath,
        filename=checkpoint_filename,
        save_last=False,
        save_top_k=-1,
        every_n_epochs=checkpoint_every_n_epochs,
        every_n_train_steps=checkpoint_every_n_steps,
        train_time_interval=pd.Timedelta(checkpoint_time_interval).
        to_pytimedelta() if checkpoint_time_interval is not None else None,
    )
    val_checkpoint_callback = pl.callbacks.ModelCheckpoint(
        dirpath=checkpoint_dirpath,
        monitor=monitor,
        filename=checkpoint_filename,
        save_last=True,  # save latest model
        save_top_k=1,  # save top model based on monitored loss
    )
    trainer = pl.Trainer(
        gpus=int(use_gpu),
        auto_lr_find=auto_tune_lr,
        max_epochs=epochs,
        max_steps=max_steps,
        max_time=train_time,
        logger=wandb_logger,
        progress_bar_refresh_rate=20,
        callbacks=[periodic_checkpoint_callback, val_checkpoint_callback],
        track_grad_norm=2,  # logs the 2-norm of gradients
        limit_val_batches=1.0 if val_frac > 0 else 0,
        limit_test_batches=0,
    )

    # train_dataset, val_dataset = split_dataset(task=task,
    #                                            val_frac=val_frac,
    #                                            device=device)
    # train_data_module = DataLoader(train_dataset)  #, num_workers=num_workers)
    # val_data_module = DataLoader(val_dataset)  #, num_workers=num_workers)

    # trainer.fit(model, train_data_module, val_data_module)
    data_module = RvSDataModule(task=task,
                                val_frac=val_frac,
                                device=device,
                                batch_size=batch_size,
                                num_workers=num_workers,
                                normalise_x=normalise_x,
                                normalise_y=normalise_y)
    trainer.fit(model, data_module)


@torch.no_grad()
def run_evaluate(
    taskname,
    seed,
    hidden_size,
    learning_rate,
    checkpoint_path,
    args,
    wandb_logger=None,
    device=None,
    normalise_x=False,
    normalise_y=False,
):
    set_seed(seed)
    if taskname == "branin":
        task = Branin(args.path)
    else:
        task = design_bench.make(TASKNAME2TASK[taskname])

    if normalise_x:
        task.map_normalize_x()
    if normalise_y:
        task.map_normalize_y()

    if task.is_discrete:
        task.map_to_logits()

    if not args.score_matching:
        model = DiffusionTest.load_from_checkpoint(
            checkpoint_path=checkpoint_path,
            taskname=taskname,
            task=task,
            learning_rate=args.learning_rate,
            hidden_size=args.hidden_size,
            vtype=args.vtype,
            beta_min=args.beta_min,
            beta_max=args.beta_max,
            T0=args.T0,
            dropout_p=args.dropout_p)
    else:
        print("Score matching loss")
        model = DiffusionScore.load_from_checkpoint(
            checkpoint_path=checkpoint_path,
            taskname=taskname,
            task=task,
            learning_rate=args.learning_rate,
            hidden_size=args.hidden_size,
            vtype=args.vtype,
            beta_min=args.beta_min,
            beta_max=args.beta_max,
            T0=args.T0,
            dropout_p=args.dropout_p)

    model = model.to(device)
    model.eval()

    def euler_maruyama_sampler(sde,
                               x_0,
                               ya,
                               num_steps,
                               lmbd=0.,
                               keep_all_samples=True):
        """
        Euler Maruyama method with a step size delta
        """
        # init
        device = sde.gen_sde.T.device
        batch_size = x_0.size(0)
        ndim = x_0.dim() - 1
        T_ = sde.gen_sde.T.cpu().item()
        delta = T_ / num_steps
        ts = torch.linspace(0, 1, num_steps + 1) * T_

        # sample
        xs = []
        x_t = x_0.detach().clone().to(device)
        t = torch.zeros(batch_size, *([1] * ndim), device=device)
        num_grid = 5
        x_plot = np.meshgrid(np.linspace(-5, 10, num_grid), np.linspace(0, 15, num_grid))
        x_plot_0 = np.meshgrid(np.linspace(-5, 10, num_grid), np.linspace(0, 15, num_grid))
        x_plot = np.concatenate((x_plot[0].reshape(-1,1), x_plot[1].reshape(-1,1)), axis=1)
        x_plot = (x_plot - np.asarray([2.5, 7.5]).reshape(1,-1)) / 4.33
        y_plot = np.zeros((num_grid * num_grid, 1))
        t2 = torch.zeros((num_grid*num_grid, *([1] * ndim)), device=device)

        x_plot = torch.tensor(x_plot, dtype=torch.float32, device=device)
        y_plot = torch.tensor(y_plot, dtype=torch.float32, device=device)
        with torch.no_grad():
            for i in range(num_steps):
                t.fill_(ts[i].item())
                t2.fill_(ts[i].item())
                mu = sde.gen_sde.mu(t, x_t, ya, lmbd=lmbd, gamma=args.gamma)
                g = sde.gen_sde.base_sde.g(1-t2, y_plot)
                a = sde.gen_sde.a(x_plot, 1-t2.squeeze(), y_plot)

                score = a / g
                # print(score)
                score = score.cpu().numpy()
                plt.quiver(x_plot_0[0], x_plot_0[1], score[:,0].reshape(num_grid, num_grid), score[:,1].reshape(num_grid, num_grid))
                plt.savefig(f'score_plots/score_{ts[i]}.png')
                plt.clf()

                sigma = sde.gen_sde.sigma(t, x_t, lmbd=lmbd)
                x_t = x_t + delta * mu + delta**0.5 * sigma * torch.randn_like(
                    x_t
                )  # one step update of Euler Maruyama method with a step size delta
                if keep_all_samples or i == num_steps - 1:
                    xs.append(x_t.cpu())
                else:
                    pass
        return xs

    @torch.no_grad()
    def _get_trained_model():
        checkpoint_path = f"experiments/{taskname}/forward_model/123/wandb/latest-run/files/checkpoints/last.ckpt"
        model = ForwardModel.load_from_checkpoint(
            checkpoint_path=checkpoint_path,
            taskname=taskname,
            task=task,)

        return model

    num_steps = args.num_steps
    num_samples = 512
    # num_samples = 10

    # lmbds = [0., 1.]
    lmbds = [args.lamda]

    # save to file
    expt_save_path = f"./experiments/{args.task}/{args.name}/{args.seed}"
    assert os.path.exists(expt_save_path)

    alias = uuid.uuid4()
    run_specific_str = f"{num_samples}_{num_steps}_{args.condition}_{args.gamma}_{args.beta_min}_{args.beta_max}_{alias}"
    save_results_dir = os.path.join(
        expt_save_path, f"wandb/latest-run/files/results/{run_specific_str}/")
    if not os.path.exists(save_results_dir):
        os.makedirs(save_results_dir)

    assert os.path.exists(save_results_dir)

    symlink_dir = os.path.join(expt_save_path,
                               f"wandb/latest-run/files/results/latest-run")

    if os.path.exists(symlink_dir):
        os.unlink(symlink_dir)
    os.symlink(run_specific_str, symlink_dir)

    # sample and plot
    designs = []
    results = []
    for lmbd in lmbds:
        if not task.is_discrete:
            x_0 = torch.randn(num_samples, task.x.shape[-1],
                              device=device)  # init from prior
        else:
            x_0 = torch.randn(num_samples, task.x.shape[-1] * task.x.shape[-2],
                              device=device)  # init from prior

        y_ = torch.ones(num_samples).to(device) * args.condition
        xs = euler_maruyama_sampler(model,
                                    x_0,
                                    y_,
                                    num_steps,
                                    lmbd=lmbd,
                                    keep_all_samples=False)  # sample
                                    # keep_all_samples=True)  # sample

        pred_model = _get_trained_model()
        preds = []
        for qqq in xs:
            print(qqq.shape)
            if not qqq.isnan().any():
                designs.append(qqq.cpu().numpy())
                if not task.is_discrete:
                    ys = task.predict(qqq.cpu().numpy())
                else:
                    qqq = qqq.view(qqq.size(0), -1, task.x.shape[-1])
                    ys = task.predict(qqq.cpu().numpy())

                pred_ys = pred_model.mlp(qqq)
                preds.append(pred_ys.cpu().numpy())

                print("GT ys: {}".format(ys.max()))
                print("Pred ys: {}".format(pred_ys.max()))
                if normalise_y:
                    print("normalise")
                    ys = task.denormalize_y(ys)
                    print(ys.max())
                else:
                    print("none")
                    print(ys.max())
                results.append(ys)
            else:
                print("fuck")

    designs = np.concatenate(designs, axis=0)
    results = np.concatenate(results, axis=0)

    print(designs.shape)
    print(results.shape)

    with open(os.path.join(save_results_dir, 'designs.pkl'), 'wb') as f:
        pkl.dump(designs, f)

    with open(os.path.join(save_results_dir, 'results.pkl'), 'wb') as f:
        pkl.dump(results, f)

    shutil.copy(args.configs, save_results_dir)


if __name__ == "__main__":
    parser = configargparse.ArgumentParser()
    # configuration
    parser.add_argument(
        "--configs",
        default=None,
        required=False,
        is_config_file=True,
        help="path(s) to configuration file(s)",
    )
    parser.add_argument('--mode',
                        choices=['train', 'eval'],
                        default='train',
                        required=True)
    parser.add_argument('--task',
                        choices=list(TASKNAME2TASK.keys()) + ["branin"],
                        required=True)
    # reproducibility
    parser.add_argument(
        "--seed",
        default=None,
        type=int,
        help=
        "sets the random seed; if this is not specified, it is chosen randomly",
    )
    parser.add_argument("--condition", default=0.0, type=float)
    parser.add_argument("--lamda", default=0.0, type=float)
    # experiment tracking
    parser.add_argument("--name", type=str, help="Experiment name")
    parser.add_argument("--score_matching", action='store_true', default=False)
    # training
    train_time_group = parser.add_mutually_exclusive_group(required=True)
    train_time_group.add_argument(
        "--epochs",
        default=None,
        type=int,
        help="the number of training epochs.",
    )
    train_time_group.add_argument(
        "--max_steps",
        default=None,
        type=int,
        help=
        "the number of training gradient steps per bootstrap iteration. ignored "
        "if --train_time is set",
    )
    train_time_group.add_argument(
        "--train_time",
        default=None,
        type=str,
        help="how long to train, specified as a DD:HH:MM:SS str",
    )
    parser.add_argument("--num_workers",
                        default=1,
                        type=int,
                        help="Number of workers")
    checkpoint_frequency_group = parser.add_mutually_exclusive_group(
        required=True)
    checkpoint_frequency_group.add_argument(
        "--checkpoint_every_n_epochs",
        default=None,
        type=int,
        help="the period of training epochs for saving checkpoints",
    )
    checkpoint_frequency_group.add_argument(
        "--checkpoint_every_n_steps",
        default=None,
        type=int,
        help="the period of training gradient steps for saving checkpoints",
    )
    checkpoint_frequency_group.add_argument(
        "--checkpoint_time_interval",
        default=None,
        type=str,
        help="how long between saving checkpoints, specified as a HH:MM:SS str",
    )
    parser.add_argument(
        "--val_frac",
        type=float,
        required=True,
        help="fraction of data to use for validation",
    )
    parser.add_argument(
        "--use_gpu",
        action="store_true",
        default=False,
        help="place networks and data on the GPU",
    )
    parser.add_argument('--simple_clip', action="store_true", default=False)
    parser.add_argument("--which_gpu",
                        default=0,
                        type=int,
                        help="which GPU to use")
    parser.add_argument(
        "--normalise_x",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--normalise_y",
        action="store_true",
        default=False,
    )

    # i/o
    parser.add_argument('--dataset',
                        type=str,
                        choices=['mnist', 'cifar'],
                        default='mnist')
    parser.add_argument('--dataroot', type=str, default='~/.datasets')
    parser.add_argument('--saveroot', type=str, default='~/.saved')
    parser.add_argument('--expname', type=str, default='default')
    parser.add_argument('--num_steps',
                        type=int,
                        default=1000,
                        help='number of integration steps for sampling')

    # optimization
    parser.add_argument('--T0',
                        type=float,
                        default=1.0,
                        help='integration time')
    parser.add_argument('--vtype',
                        type=str,
                        choices=['rademacher', 'gaussian'],
                        default='rademacher',
                        help='random vector for the Hutchinson trace estimator')
    parser.add_argument('--batch_size', type=int, default=64)
    parser.add_argument('--test_batch_size', type=int, default=256)
    parser.add_argument('--num_iterations', type=int, default=10000)
    parser.add_argument('--gamma', type=float, default=1.)

    # model
    parser.add_argument(
        '--real',
        type=eval,
        choices=[True, False],
        default=True,
        help=
        'transforming the data from [0,1] to the real space using the logit function'
    )
    parser.add_argument(
        '--debias',
        action="store_true",
        default=False,
        help=
        'using non-uniform sampling to debias the denoising score matching loss'
    )

    # TODO: remove
    parser.add_argument(
        "--learning_rate",
        type=float,
        required=False,
        help="learning rate for each gradient step",
    )
    parser.add_argument(
        "--auto_tune_lr",
        action="store_true",
        default=False,
        help=
        "have PyTorch Lightning try to automatically find the best learning rate",
    )
    parser.add_argument(
        "--hidden_size",
        type=int,
        required=False,
        help="size of hidden layers in policy network",
    )
    parser.add_argument(
        "--depth",
        type=int,
        required=False,
        help="number of hidden layers in policy network",
    )
    parser.add_argument(
        "--dropout_p",
        type=float,
        required=False,
        help="dropout probability",
        default=0,
    )
    parser.add_argument(
        "--beta_min",
        type=float,
        required=False,
        default=0.1,
    )
    parser.add_argument("--path", type=str, required=False, default="fuck")
    parser.add_argument(
        "--beta_max",
        type=float,
        required=False,
        default=20.0,
    )
    args = parser.parse_args()

    wandb_project = "score-matching " if args.score_matching else "sde-flow"

    args.seed = np.random.randint(2**31 - 1) if args.seed is None else args.seed
    set_seed(args.seed + 1)
    device = configure_gpu(args.use_gpu, args.which_gpu)

    expt_save_path = f"./experiments/{args.task}/{args.name}/{args.seed}"

    if args.mode == 'train':
        if not os.path.exists(expt_save_path):
            os.makedirs(expt_save_path)
        wandb_logger = pl.loggers.wandb.WandbLogger(
            project=wandb_project,
            name=f"{args.name}_{args.seed}",
            save_dir=expt_save_path)
        log_args(args, wandb_logger)
        # run_training(
        run_training_forward(
            taskname=args.task,
            seed=args.seed,
            wandb_logger=wandb_logger,
            args=args,
            device=device,
        )
    elif args.mode == 'eval':
        checkpoint_path = os.path.join(
            expt_save_path, "wandb/latest-run/files/checkpoints/last.ckpt")
        # checkpoint_path = os.path.join(
        #     expt_save_path, f"wandb/latest-run/files/checkpoints/val.ckpt")
        run_evaluate(taskname=args.task,
                     seed=args.seed,
                     hidden_size=args.hidden_size,
                     args=args,
                     learning_rate=args.learning_rate,
                     checkpoint_path=checkpoint_path,
                     device=device,
                     normalise_x=args.normalise_x,
                     normalise_y=args.normalise_y)
    else:
        raise NotImplementedError
