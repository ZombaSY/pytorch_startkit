import copy
import os
import platform

import torch
import torchvision.transforms.functional as tf
import torch.nn.functional as F
import albumentations
import random
import numpy as np
import cv2
import pandas as pd

from PIL import Image
from torchvision import transforms
from torchvision.transforms import InterpolationMode
from torch.utils.data import Dataset, DataLoader
from models import utils
from multiprocessing import set_start_method


# function for non-utf-8 string

def is_image(src):
    ext = os.path.splitext(src)[1]
    return True if ext in ['.jpg', '.png', '.JPG', '.PNG'] else False


class Image2ImageLoader(Dataset):

    def __init__(self,
                 x_path,
                 y_path,
                 mode,
                 **kwargs):

        self.mode = mode
        self.args = kwargs['args']

        if hasattr(self.args, 'input_size'):
            h, w = self.args.input_size[0], self.args.input_size[1]
            self.size_1x = [int(h), int(w)]

        if hasattr(self.args, 'crop_size'):
            self.crop_factor = int(self.args.crop_size)

        if self.args.project_name == 'ADE':
            self.image_mean = [0.485, 0.456, 0.406]
            self.image_std = [0.229, 0.224, 0.225]
        else:
            self.image_mean = [0.512, 0.459, 0.353]
            self.image_std = [0.254, 0.226, 0.219]

        if platform.system() == 'Linux' and hasattr(self.args, 'offset'):
            self.offset = self.__offset(self.args.offset)

        x_img_name = os.listdir(x_path)
        y_img_name = os.listdir(y_path)
        x_img_name = filter(is_image, x_img_name)
        y_img_name = filter(is_image, y_img_name)

        self.x_img_path = []
        self.y_img_path = []

        x_img_name = sorted(x_img_name)
        y_img_name = sorted(y_img_name)

        img_paths = zip(x_img_name, y_img_name)
        for item in img_paths:
            self.x_img_path.append(x_path + os.sep + item[0])
            self.y_img_path.append(y_path + os.sep + item[1])

        assert len(self.x_img_path) == len(self.y_img_path), 'Images in directory must have same file indices!!'

        self.len = len(x_img_name)

        del x_img_name
        del y_img_name

    def transform(self, image, target):

        if hasattr(self.args, 'input_size'):
            image = tf.resize(image, self.size_1x, interpolation=InterpolationMode.BILINEAR)
            target = tf.resize(target, self.size_1x, interpolation=InterpolationMode.NEAREST)

        if hasattr(self, 'offset') and not self.mode == 'validation':
            image_np = np.array(image)
            image_np = self.offset(image_np)
            image = Image.fromarray(image_np.astype(np.uint8))

        if hasattr(self.args, 'crop_size') and not self.mode == 'validation':
            i, j, h, w = transforms.RandomCrop.get_params(image, output_size=(self.crop_factor, self.crop_factor))
            image = tf.crop(image, i, j, h, w)
            target = tf.crop(target, i, j, h, w)

        if (random.random() < 0.5) and not self.mode == 'validation' and self.args.transform_hflip:
            image = tf.hflip(image)
            target = tf.hflip(target)

        if (random.random() < 0.8) and not self.mode == 'validation' and self.args.transform_jitter:
            transform = transforms.ColorJitter(brightness=0.15, contrast=0.15, saturation=0.15, hue=0.02)
            image = transform(image)

        if (random.random() < 0.3) and not self.mode == 'validation' and self.args.transform_blur:
            transform = transforms.GaussianBlur(kernel_size=3)
            image = transform(image)

        # recommend to use at the end.
        # recommend to use in same shape both image and target
        if (random.random() < 0.3) and not self.mode == 'validation' and self.args.transform_perspective:
            start_p, end_p = transforms.RandomPerspective.get_params(image.width, image.height, distortion_scale=0.5)
            image = tf.perspective(image, start_p, end_p)
            target = tf.perspective(target, start_p, end_p)

        image_tensor = tf.to_tensor(image)
        target_tensor = torch.tensor(np.array(target))

        if self.args.input_space == 'GR':   # grey, red
            image_tensor_r = image_tensor[0].unsqueeze(0)
            image_tensor_grey = tf.to_tensor(tf.to_grayscale(image))

            image_tensor = torch.cat((image_tensor_r, image_tensor_grey), dim=0)

        # 'mean' and 'std' are acquired by cropped face from sense-time landmark
        if self.args.input_space == 'RGB':
            image_tensor = tf.normalize(image_tensor,
                                        mean=self.image_mean,
                                        std=self.image_std)

        if self.args.num_class == 2:  # for binary label with {0, 255} set
            target_tensor[target_tensor < 128] = 0
            target_tensor[target_tensor >= 128] = 1
        target_tensor = target_tensor.unsqueeze(0)    # expand 'grey channel' for loss function dependency

        if self.args.input_space == 'HSV':
            try:
                set_start_method('spawn')
                image_tensor = utils.ImageProcessing.rgb_to_hsv(image_tensor)
            except RuntimeError:
                pass

        return image_tensor, target_tensor

    def __offset(self, offset):
        if offset == 1:
            scaler = [10, 10, -10]
            return lambda x: np.clip(x + scaler, 0, 255)

        elif offset == 2:
            scaler = [10, 10, -10]
            return lambda x: np.clip(x - scaler, 0, 255)

    def __getitem__(self, index):
        x_path = self.x_img_path[index]
        y_path = self.y_img_path[index]

        img_x = Image.open(x_path).convert('RGB')
        img_y = Image.open(y_path).convert('L')

        img_x_tr, img_y_tr = self.transform(img_x, img_y)

        return (img_x_tr, x_path), (img_y_tr, y_path)

    def __len__(self):
        return self.len


class Image2VectorLoader(Dataset):

    def __init__(self,
                 csv_path,
                 mode,
                 **kwargs):

        self.mode = mode
        self.args = kwargs['args']

        if hasattr(self.args, 'input_size'):
            h, w = self.args.input_size[0], self.args.input_size[1]
            self.size_1x = [int(h), int(w)]

        if hasattr(self.args, 'crop_size'):
            self.crop_factor = int(self.args.crop_size)

        if self.args.project_name == 'ADE':
            self.image_mean = [0.485, 0.456, 0.406]
            self.image_std = [0.229, 0.224, 0.225]
        else:
            self.image_mean = [0.512, 0.459, 0.353]
            self.image_std = [0.254, 0.226, 0.219]

        self.data_root_path = os.path.split(csv_path)[0]
        self.df = pd.read_csv(csv_path)
        self.len = len(self.df['value_1'])

    def transform(self, image):

        if hasattr(self.args, 'input_size'):
            image = tf.resize(image, self.size_1x, interpolation=InterpolationMode.BILINEAR)

        if hasattr(self.args, 'crop_size') and not self.mode == 'validation':
            i, j, h, w = transforms.RandomCrop.get_params(image, output_size=(self.crop_factor, self.crop_factor))
            image = tf.crop(image, i, j, h, w)

        if (random.random() < 0.5) and not self.mode == 'validation' and self.args.transform_hflip:
            image = tf.hflip(image)

        if (random.random() < 0.8) and not self.mode == 'validation' and self.args.transform_jitter:
            transform = transforms.ColorJitter(brightness=0.15, contrast=0.15, saturation=0.15, hue=0.02)
            image = transform(image)

        if (random.random() < 0.3) and not self.mode == 'validation' and self.args.transform_blur:
            transform = transforms.GaussianBlur(kernel_size=3)
            image = transform(image)

        # recommend to use at the end.
        # recommend to use in same shape both image and target
        if (random.random() < 0.3) and not self.mode == 'validation' and self.args.transform_perspective:
            start_p, end_p = transforms.RandomPerspective.get_params(image.width, image.height, distortion_scale=0.5)
            image = tf.perspective(image, start_p, end_p)

        image_tensor = tf.to_tensor(image)

        if self.args.input_space == 'GR':   # grey, red
            image_tensor_r = image_tensor[0].unsqueeze(0)
            image_tensor_grey = tf.to_tensor(tf.to_grayscale(image))

            image_tensor = torch.cat((image_tensor_r, image_tensor_grey), dim=0)

        # 'mean' and 'std' are acquired by cropped face from sense-time landmark
        if self.args.input_space == 'RGB':
            image_tensor = tf.normalize(image_tensor,
                                        mean=self.image_mean,
                                        std=self.image_std)

        if self.args.input_space == 'HSV':
            try:
                set_start_method('spawn')
                image_tensor = utils.ImageProcessing.rgb_to_hsv(image_tensor)
            except RuntimeError:
                pass

        return image_tensor

    def __getitem__(self, index):
        x_path = os.path.join(*[self.data_root_path, self.df['sub_path'][index], self.df['image_file_name'][index]])

        img_x = Image.open(x_path).convert('RGB')
        img_x = self.transform(img_x)

        # example for csv column
        vec_y = torch.tensor([self.df['value_1'][index],
                              self.df['value_2'][index],
                              self.df['value_3'][index],
                              self.df['value_4'][index],
                              self.df['value_5'][index],
                              self.df['value_6'][index]])

        return (img_x, x_path), (vec_y, torch.tensor(0))

    def __len__(self):
        return self.len


class Image2ImageDataLoader:

    def __init__(self,
                 x_path,
                 y_path,
                 mode,
                 batch_size=4,
                 num_workers=0,
                 pin_memory=True,
                 **kwargs):

        self.image_loader = Image2ImageLoader(x_path,
                                              y_path,
                                              mode=mode,
                                              **kwargs)

        # use your own data loader
        self.Loader = DataLoader(self.image_loader,
                                 batch_size=batch_size,
                                 num_workers=num_workers,
                                 shuffle=(not mode == 'validation'),
                                 pin_memory=pin_memory)

    def __len__(self):
        return self.Loader.__len__()


class Image2VectorDataLoader:

    def __init__(self,
                 csv_path,
                 mode,
                 batch_size=4,
                 num_workers=0,
                 pin_memory=True,
                 **kwargs):

        self.image_loader = Image2VectorLoader(csv_path,
                                               mode=mode,
                                               **kwargs)

        # use your own data loader
        self.Loader = DataLoader(self.image_loader,
                                 batch_size=batch_size,
                                 num_workers=num_workers,
                                 shuffle=(not mode == 'validation'),
                                 pin_memory=pin_memory)

    def __len__(self):
        return self.Loader.__len__()