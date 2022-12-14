import torch
import time
import os
import wandb
import numpy as np
import copy

from models import dataloader as dataloader_hub
from models import lr_scheduler
from models import model_implements
from models import losses as loss_hub
from models import metrics

from datetime import datetime


class Trainer_cls:
    def __init__(self, args, now=None):
        self.start_time = time.time()
        self.args = args

        # Check cuda available and assign to device
        use_cuda = self.args.cuda and torch.cuda.is_available()
        self.device = torch.device('cuda' if use_cuda else 'cpu')

        # 'init' means that this variable must be initialized.
        # 'set' means that this variable is available of being set, not must.
        self.loader_train = self.__init_data_loader(self.args.train_csv_path,
                                                    self.args.batch_size,
                                                    mode='train')
        self.loader_val = self.__init_data_loader(self.args.val_csv_path,
                                                  batch_size=1,
                                                  mode='validation')

        self.model = self.__init_model(self.args.model_name)
        self.optimizer = self._init_optimizer(self.model, self.args.lr)
        self.scheduler = self._set_scheduler(self.optimizer, self.args.scheduler, self.loader_train, self.args.batch_size)

        if self.args.model_path != '':
            if 'imagenet' in self.args.model_path.lower():
                self.model.module.load_pretrained_imagenet(self.args.model_path)
                print('Model loaded successfully!!! (ImageNet)')
            else:
                self.model.module.load_pretrained(self.args.model_path)    # TODO: define "load_pretrained" abstract method to all models
                print('Model loaded successfully!!! (Custom)')
            self.model.to(self.device)

        self.criterion = self._init_criterion(self.args.criterion)

        if self.args.wandb:
            if self.args.mode == 'train':
                wandb.watch(self.model)

        now_time = now if now is not None else datetime.now().strftime("%Y%m%d %H%M%S")
        self.saved_model_directory = self.args.saved_model_directory + '/' + now_time
        self.num_batches_train = int(len(self.loader_train))
        self.num_batches_val = int(len(self.loader_val))

        self.metric_train = metrics.StreamSegMetrics_classification(self.args.num_class)
        self.metric_val = metrics.StreamSegMetrics_classification(self.args.num_class)
        self.metric_best = copy.deepcopy(self.metric_train.metric_dict)
        self.model_post_path_dict = {}

        self.__validate_interval = 1 if (self.loader_train.__len__() // self.args.train_fold) == 0 else self.loader_train.__len__() // self.args.train_fold

        # self.amp_scaler = torch.cuda.amp.GradScaler()

    def _train(self, epoch):
        self.model.train()
        batch_losses = []
        print('Start Train')
        for batch_idx, (x_in, target) in enumerate(self.loader_train):
            # if (x_in[0].shape[0] / torch.cuda.device_count()) <= torch.cuda.device_count():   # if has 1 batch per GPU
            #     break   # avoid BN issue
            x_in, _ = x_in
            target, _ = target

            x_in = x_in.to(self.device)
            target = target.long().to(self.device)  # (shape: (batch_size, img_h, img_w))

            output = self.model(x_in)
            loss = self.criterion(output, target)

            if not torch.isfinite(loss):
                raise Exception('Loss is NAN. End training.')

            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()
            if self.scheduler is not None:
                self.scheduler.step()

            batch_losses.append(loss.item())

            output_argmax = torch.argmax(output, dim=1).cpu()
            self.metric_train.update(target.cpu().detach().numpy(), output_argmax.numpy())

            if hasattr(self.args, 'train_fold'):
                if batch_idx != 0 and (batch_idx % self.__validate_interval) == 0:
                    self._validate(epoch)

            if (batch_idx != 0) and (batch_idx % (self.args.log_interval // self.args.batch_size) == 0):
                loss_mean = np.mean(batch_losses)
                print('{} epoch / Train Loss {} : {}, lr {}'.format(epoch,
                                                                    self.args.criterion,
                                                                    loss_mean,
                                                                    self.optimizer.param_groups[0]['lr']))

            torch.cuda.empty_cache()

        loss_mean = np.mean(batch_losses)
        metrics = self.metric_train.get_results()
        mean_kappa_score = metrics['Mean Kappa Score']
        kappa_scores = metrics['Class Kappa Score']
        mean_acc_score = metrics['Mean Accuracy']
        acc_scores = metrics['Class Accuracy']

        print(f'{epoch} epoch / Train {self.args.criterion} : {loss_mean}, '
              f'lr {self.optimizer.param_groups[0]["lr"]}')

        print(f'{epoch} epoch / Train Mean Kappa Score : {mean_kappa_score} \n'
              f'Mean Accuracy : {mean_acc_score}')

        for i in range(self.args.num_class):
            print(f'\t \t Train Class Kappa Score {i} : {kappa_scores[i]}')
            print(f'\t \t Train Class Accuracy {i} : {acc_scores[i]}')

        if self.args.wandb:
            wandb.log({f'Train {self.args.criterion}': loss_mean,
                       f'Train Mean Kappa Score': mean_kappa_score,
                       f'Train Mean Accuracy': mean_acc_score})

        self.metric_train.reset()

    def _validate(self, epoch):
        self.model.eval()

        for batch_idx, (x_in, target) in enumerate(self.loader_val):
            with torch.no_grad():
                x_in, _ = x_in
                target, _ = target

                x_in = x_in.to(self.device)
                target = target.long().to(self.device)  # (shape: (batch_size, img_h, img_w))

                output = self.model(x_in)
                output_argmax = torch.argmax(output, dim=1).cpu()

                self.metric_val.update(target.cpu().detach().numpy(), output_argmax.numpy())

        metrics = self.metric_val.get_results()
        mean_kappa_score = metrics['Mean Kappa Score']
        kappa_scores = metrics['Class Kappa Score']
        mean_acc_score = metrics['Mean Accuracy']
        acc_scores = metrics['Class Accuracy']

        print(f'{epoch} epoch / Val Mean Kappa Score : {mean_kappa_score} \n'
              f'Mean Accuracy : {mean_acc_score}')
        for i in range(self.args.num_class):
            print(f'\t \t Val Class Kappa Score {i} : {kappa_scores[i]}')
            print(f'\t \t Val Class Accuracy {i} : {acc_scores[i]}')

        if self.args.wandb:
            wandb.log({'Val Mean Kappa Score': mean_kappa_score,
                       'Val Mean Accuracy': mean_acc_score})

        model_metrics = {'Mean Kappa Score': mean_kappa_score,
                         'Mean Accuracy': mean_acc_score}

        for key in model_metrics.keys():
            if model_metrics[key] > self.metric_best[key]:
                self.metric_best[key] = model_metrics[key]
                self.save_model(self.args.model_name, epoch, model_metrics[key], best_flag=True, metric_name=key)

        self.metric_val.reset()

    def start_train(self):
        for epoch in range(1, self.args.epoch + 1):
            self._train(epoch)
            self._validate(epoch)

            print('### {} / {} epoch ended###'.format(epoch, self.args.epoch))

    def save_model(self, model_name, epoch, metric=None, best_flag=False, metric_name='metric'):
        file_path = self.saved_model_directory + '/'

        file_format = file_path + model_name + '_Epoch_' + str(epoch) + '_' + metric_name + '_' + str(metric) + '.pt'

        if not os.path.exists(file_path):
            os.mkdir(file_path)

        if best_flag:
            if metric_name in self.model_post_path_dict.keys():
                os.remove(self.model_post_path_dict[metric_name])
            self.model_post_path_dict[metric_name] = file_format

        torch.save(self.model.state_dict(), file_format)

        print(file_format + '\t model saved!!')

    def __init_data_loader(self,
                           x_path,
                           batch_size,
                           mode):

        if self.args.dataloader == 'Image2Vector':
            loader = dataloader_hub.Image2VectorDataLoader(csv_path=x_path,
                                                           batch_size=batch_size,
                                                           num_workers=self.args.worker,
                                                           pin_memory=self.args.pin_memory,
                                                           mode=mode,
                                                           args=self.args)

        return loader.Loader

    def __init_model(self, model_name):
        if model_name == 'ResNet18_multihead':
            model = model_implements.ResNet18_multihead(num_classes=self.args.num_class).to(self.device)

        else:
            raise Exception('No model named', model_name)

        return torch.nn.DataParallel(model)

    def _init_criterion(self, criterion_name):
        if criterion_name == 'CE':
            criterion = loss_hub.CrossEntropy().to(self.device)
        elif criterion_name == 'HausdorffDT':
            criterion = loss_hub.HausdorffDTLoss().to(self.device)
        elif criterion_name == 'KLDivergence':
            criterion = loss_hub.KLDivergence().to(self.device)
        elif criterion_name == 'JSDivergence':
            criterion = loss_hub.JSDivergence().to(self.device)
        elif criterion_name == 'MSE':
            criterion = loss_hub.MSELoss().to(self.device)
        elif criterion_name == 'MSE_SSL':
            criterion = loss_hub.MSELoss_SSL().to(self.device)
        elif criterion_name == 'BCE':
            criterion = loss_hub.BCELoss().to(self.device)
        elif criterion_name == 'Dice':
            criterion = loss_hub.DiceLoss().to(self.device)
        elif criterion_name == 'DiceBCE':
            criterion = loss_hub.DiceBCELoss().to(self.device)
        elif criterion_name == 'FocalBCE':
            criterion = loss_hub.FocalBCELoss().to(self.device)
        elif criterion_name == 'Tversky':
            criterion = loss_hub.TverskyLoss().to(self.device)
        elif criterion_name == 'FocalTversky':
            criterion = loss_hub.FocalTverskyLoss().to(self.device)
        elif criterion_name == 'KLDivergenceLogit':
            criterion = loss_hub.KLDivergenceLogit().to(self.device)
        elif criterion_name == 'JSDivergenceLogit':
            criterion = loss_hub.JSDivergenceLogit().to(self.device)
        elif criterion_name == 'JSDivergenceLogitBatch':
            criterion = loss_hub.JSDivergenceLogitBatch().to(self.device)
        else:
            raise Exception('No criterion named', criterion_name)

        return criterion

    def _init_optimizer(self, model, lr):
        optimizer = None

        optimizer = torch.optim.AdamW(filter(lambda p: p.requires_grad, model.parameters()),
                                      lr=lr, betas=(0.9, 0.999), eps=1e-8, weight_decay=0.01, amsgrad=False)

        return optimizer

    def _set_scheduler(self, optimizer, scheduler_name, data_loader, batch_size):
        scheduler = None
        step_per_epoch = data_loader.__len__() // batch_size

        if hasattr(self.args, 'scheduler'):
            if scheduler_name == 'WarmupCosine':
                scheduler = lr_scheduler.WarmupCosineSchedule(optimizer=optimizer,
                                                              warmup_steps=step_per_epoch,
                                                              t_total=data_loader.__len__(),
                                                              cycles=10,
                                                              last_epoch=-1)
            elif scheduler_name == 'CosineAnnealingLR':
                scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=100, eta_min=0)
            elif scheduler_name == 'ConstantLRSchedule':
                scheduler = lr_scheduler.ConstantLRSchedule(optimizer, last_epoch=-1)
            elif scheduler_name == 'WarmupConstantSchedule':
                scheduler = lr_scheduler.WarmupConstantSchedule(optimizer, warmup_steps=step_per_epoch * 100)
            else:
                raise Exception('No scheduler named', scheduler_name)
        else:
            pass

        return scheduler
