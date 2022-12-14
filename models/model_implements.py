import torch
import torch.nn as nn


from models.backbones import Resnet
from models.backbones import Unet_part
from models.backbones.Swin import SwinTransformer
from models.blocks.Blocks import Upsample
from models.heads.UPerHead import M_UPerHead

from collections import OrderedDict


def initialize_weights(layer, activation='relu'):

    for module in layer.modules():
        module_name = module.__class__.__name__

        if activation in ('relu', 'leaky_relu'):
            layer_init_func = nn.init.kaiming_uniform_
        elif activation == 'tanh':
            layer_init_func = nn.init.xavier_uniform_
        else:
            raise Exception('Please specify your activation function name')

        if hasattr(module, 'weight'):
            if module_name.find('Conv2') != -1:
                layer_init_func(module.weight)
            elif module_name.find('BatchNorm') != -1:
                nn.init.normal_(module.weight.data, 1.0, 0.02)
                nn.init.constant_(module.bias.data, 0.0)
            elif module_name.find('Linear') != -1:
                layer_init_func(module.weight)
                if module.bias is not None:
                    module.bias.data.fill_(0.1)
            else:
                # print('Cannot initialize the layer :', module_name)
                pass
        else:
            pass


class Unet(nn.Module):
    def __init__(self, n_channels=3, n_classes=2, bilinear=True):
        super(Unet, self).__init__()
        self.n_channels = n_channels
        self.n_classes = n_classes
        self.bilinear = bilinear

        self.inc = Unet_part.DoubleConv(n_channels, 64)
        self.down1 = Unet_part.Down(64, 128)
        self.down2 = Unet_part.Down(128, 256)
        self.down3 = Unet_part.Down(256, 512)
        factor = 2 if bilinear else 1
        self.down4 = Unet_part.Down(512, 1024 // factor)
        self.up1 = Unet_part.Up(1024, 512 // factor, bilinear)
        self.up2 = Unet_part.Up(512, 256 // factor, bilinear)
        self.up3 = Unet_part.Up(256, 128 // factor, bilinear)
        self.up4 = Unet_part.Up(128, 64, bilinear)
        self.outc = Unet_part.OutConv(64, n_classes)

    def forward(self, x):
        x1 = self.inc(x)
        x2 = self.down1(x1)
        x3 = self.down2(x2)
        x4 = self.down3(x3)
        x5 = self.down4(x4)

        x = self.up1(x5, x4)
        x = self.up2(x, x3)
        x = self.up3(x, x2)
        x = self.up4(x, x1)
        logits = self.outc(x)

        return logits


class Swin(nn.Module):
    def __init__(self, num_classes=2, in_channel=3):
        super(Swin, self).__init__()

        self.swin_transformer = SwinTransformer(in_chans=in_channel,
                                                embed_dim=96,
                                                depths=[2, 2, 6, 2],
                                                num_heads=[3, 6, 12, 24],
                                                window_size=7,
                                                mlp_ratio=4.,
                                                qkv_bias=True,
                                                qk_scale=None,
                                                drop_rate=0.,
                                                attn_drop_rate=0.,
                                                drop_path_rate=0.3,
                                                ape=False,
                                                patch_norm=True,
                                                out_indices=(0, 1, 2, 3),
                                                use_checkpoint=False)

        self.uper_head = M_UPerHead(in_channels=[96, 192, 384, 768],
                                    in_index=[0, 1, 2, 3],
                                    pool_scales=(1, 2, 3, 6),
                                    channels=512,
                                    dropout_ratio=0.1,
                                    num_classes=num_classes,
                                    align_corners=False,)

    def load_pretrained(self, dst):
        pretrained_states = torch.load(dst)
        pretrained_states_backbone = OrderedDict()
        for item in pretrained_states.keys():
            if 'swin_transformer' in item:
                key = item.replace('module.', '')   # strip wrapper class
                key = key.replace('swin_transformer.', '')  # strip "swin_transformer" class
                pretrained_states_backbone[key] = pretrained_states[item]

        self.swin_transformer.load_state_dict(pretrained_states_backbone)

    def load_pretrained_imagenet(self, dst):
        pretrained_states = torch.load(dst)['model']
        pretrained_states_backbone = OrderedDict()

        for item in pretrained_states.keys():
            if 'head.weight' == item or 'head.bias' == item or 'norm.weight' == item or 'norm.bias' == item or 'layers.0.blocks.1.attn_mask' == item or 'layers.1.blocks.1.attn_mask' == item or 'layers.2.blocks.1.attn_mask' == item or 'layers.2.blocks.3.attn_mask' == item or 'layers.2.blocks.5.attn_mask' == item:
                continue
            pretrained_states_backbone[item] = pretrained_states[item]

        self.swin_transformer.remove_fpn_norm_layers()  # temporally remove fpn norm layers that not included on public-release model
        self.swin_transformer.load_state_dict(pretrained_states_backbone)
        self.swin_transformer.add_fpn_norm_layers()

    def forward(self, x):
        x_size = x.shape[2:]

        feat = self.swin_transformer(x)     # list of feature pyramid
        feat = self.uper_head(feat)
        feat = Upsample(feat, x_size)

        return feat


class ResNet18_multihead(nn.Module):
    def __init__(self, num_classes=6, sub_classes=4):
        super(ResNet18_multihead, self).__init__()

        self.num_classes = num_classes
        self.sub_classes = sub_classes

        self.resnet = Resnet.ResNet18()
        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        self.classifiers = nn.ModuleList([nn.Sequential(*[
            nn.Linear(512 * 4, 512),
            nn.ReLU(),
            nn.Dropout(),
            nn.Linear(512, sub_classes)
        ]) for _ in range(num_classes)])

    def forward(self, x):
        x = x.contiguous()
        s1_feature, s2_feature, s3_feature, final_feature = self.resnet(x)

        final_feature = self.avgpool(final_feature)
        final_feature = torch.flatten(final_feature, 1)
        outputs = [self.classifiers[i](final_feature) for i in range(self.num_classes)]

        output = torch.cat([output.unsqueeze(1) for output in outputs], dim=1)
        output = output.view([-1, self.sub_classes, self.num_classes])  # reshape for CE loss

        return output


class DeepLabV3_Res50(nn.Module):
    def __init__(self, num_classes=2):
        super(DeepLabV3_Res50, self).__init__()

        self.model_0 = nn.Sequential(*[
            Resnet.ResNet50(),
            ASPP.ASPP(num_classes=num_classes, in_channel=2048)
        ])
        self.model_1 = nn.Sequential(*[
            Resnet.ResNet50(),
            ASPP.ASPP(num_classes=num_classes, in_channel=2048)
        ])
        self.model_2 = nn.Sequential(*[
            Resnet.ResNet50(),
            ASPP.ASPP(num_classes=num_classes, in_channel=2048)
        ])
        self.model_3 = nn.Sequential(*[
            Resnet.ResNet50(),
            ASPP.ASPP(num_classes=num_classes, in_channel=2048)
        ])
        self.model_4 = nn.Sequential(*[
            Resnet.ResNet50(),
            ASPP.ASPP(num_classes=num_classes, in_channel=2048)
        ])

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def forward(self, x, prefix):
        x = x.contiguous()
        _, _, h, w = x.shape

        output = []
        for idx, item in enumerate(prefix):
            if item == 0:
                _, _, _, tmp = self.model_0[0](x[idx])
                output.append(self.model_0[1](tmp))
            elif item == 1:
                _, _, _, tmp = self.model_1[0](x[idx])
                output.append(self.model_1[1](tmp))
            elif item == 2:
                _, _, _, tmp = self.model_2[0](x[idx])
                output.append(self.model_2[1](tmp))
            elif item == 3:
                _, _, _, tmp = self.model_3[0](x[idx])
                output.append(self.model_3[1](tmp))
            elif item == 4:
                _, _, _, tmp = self.model_4[0](x[idx])
                output.append(self.model_4[1](tmp))

        output = torch.cat([item for item in output], dim=0)

        output = F.interpolate(x, size=[h, w], mode='bilinear', align_corners=False)

        return output
