# coding: utf-8
# Copyright (C) 2021, [Breezedeus](https://github.com/breezedeus).
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

from __future__ import absolute_import, division, print_function
import os
import logging
import time
from collections import Counter
import json
import glob
from operator import itemgetter
from pathlib import Path

import click
from torchvision import transforms as T
import torch

from cnocr.consts import MODEL_VERSION, ENCODER_CONFIGS, DECODER_CONFIGS
from cnocr.utils import (
    set_logger,
    load_model_params,
    check_model_name,
    save_img,
    read_img,
)
from cnocr.data_utils.aug import (
    NormalizeAug,
    RandomPaddingAug,
    RandomStretchAug,
    RandomCrop,
)
from cnocr.dataset import OcrDataModule
from cnocr.trainer import PlTrainer, resave_model
from cnocr import CnOcr, gen_model

_CONTEXT_SETTINGS = {"help_option_names": ['-h', '--help']}
logger = set_logger(log_level=logging.INFO)

DEFAULT_MODEL_NAME = 'densenet_lite_136-fc'
LEGAL_MODEL_NAMES = {
    enc_name + '-' + dec_name
    for enc_name in ENCODER_CONFIGS.keys()
    for dec_name in DECODER_CONFIGS.keys()
}


@click.group(context_settings=_CONTEXT_SETTINGS)
def cli():
    pass


@cli.command('train')
@click.option(
    '-m',
    '--model-name',
    type=str,
    default=DEFAULT_MODEL_NAME,
    help='模型名称。默认值为 `%s`' % DEFAULT_MODEL_NAME,
)
@click.option(
    '-i',
    '--index-dir',
    type=str,
    required=True,
    help='索引文件所在的文件夹，会读取文件夹中的 train.tsv 和 dev.tsv 文件',
)
@click.option(
    '--train-config-fp',
    type=str,
    required=True,
    help='训练使用的json配置文件，参考 `docs/examples/train_config.json`',
)
@click.option(
    '-r',
    '--resume-from-checkpoint',
    type=str,
    default=None,
    help='恢复此前中断的训练状态，继续训练。所以文件中应该包含训练状态。默认为 `None`',
)
@click.option(
    '-p',
    '--pretrained-model-fp',
    type=str,
    default=None,
    help='导入的训练好的模型，作为模型初始值。'
    '优先级低于"--resume-from-checkpoint"，当传入"--resume-from-checkpoint"时，此传入失效。默认为 `None`',
)
def train(
    model_name, index_dir, train_config_fp, resume_from_checkpoint, pretrained_model_fp
):
    """训练模型"""
    check_model_name(model_name)
    train_transform = T.Compose(
        [
            RandomStretchAug(min_ratio=0.5, max_ratio=1.5),
            # RandomCrop((8, 10)),
            T.RandomInvert(p=0.2),
            T.RandomApply([T.RandomRotation(degrees=1)], p=0.4),
            # T.RandomAutocontrast(p=0.05),
            # T.RandomPosterize(bits=4, p=0.3),
            # T.RandomAdjustSharpness(sharpness_factor=0.5, p=0.3),
            # T.RandomEqualize(p=0.3),
            # T.RandomApply([T.GaussianBlur(kernel_size=3)], p=0.5),
            NormalizeAug(),
            # RandomPaddingAug(p=0.5, max_pad_len=72),
        ]
    )
    val_transform = NormalizeAug()

    train_config = json.load(open(train_config_fp))

    data_mod = OcrDataModule(
        index_dir=index_dir,
        vocab_fp=train_config['vocab_fp'],
        img_folder=train_config['img_folder'],
        train_transforms=train_transform,
        val_transforms=val_transform,
        batch_size=train_config['batch_size'],
        num_workers=train_config['num_workers'],
        pin_memory=train_config['pin_memory'],
    )

    # train_ds = data_mod.train
    # for i in range(min(100, len(train_ds))):
    #     visualize_example(train_transform(train_ds[i][0]), 'debugs/train-1-%d' % i)
    #     visualize_example(train_transform(train_ds[i][0]), 'debugs/train-2-%d' % i)
    #     visualize_example(train_transform(train_ds[i][0]), 'debugs/train-3-%d' % i)
    # val_ds = data_mod.val
    # for i in range(min(10, len(val_ds))):
    #     visualize_example(val_transform(val_ds[i][0]), 'debugs/val-1-%d' % i)
    #     visualize_example(val_transform(val_ds[i][0]), 'debugs/val-2-%d' % i)
    #     visualize_example(val_transform(val_ds[i][0]), 'debugs/val-2-%d' % i)
    # return

    trainer = PlTrainer(
        train_config, ckpt_fn=['cnocr', 'v%s' % MODEL_VERSION, model_name]
    )
    model = gen_model(model_name, data_mod.vocab)
    logger.info(model)

    if pretrained_model_fp is not None:
        load_model_params(model, pretrained_model_fp)

    trainer.fit(
        model, datamodule=data_mod, resume_from_checkpoint=resume_from_checkpoint
    )


def visualize_example(example, fp_prefix):
    if not os.path.exists(os.path.dirname(fp_prefix)):
        os.makedirs(os.path.dirname(fp_prefix))
    image = example
    save_img(image, '%s-image.jpg' % fp_prefix)


@cli.command('predict')
@click.option(
    '-m',
    '--model-name',
    type=str,
    default=DEFAULT_MODEL_NAME,
    help='模型名称。默认值为 %s' % DEFAULT_MODEL_NAME,
)
@click.option(
    '-b',
    '--model-backend',
    type=click.Choice(['pytorch', 'onnx']),
    default='onnx',
    help='模型类型。默认值为 `onnx`',
)
@click.option(
    '-p',
    '--pretrained-model-fp',
    type=str,
    default=None,
    help='使用训练好的模型。默认为 `None`，表示使用系统自带的预训练模型',
)
@click.option(
    "-c",
    "--context",
    help="使用cpu还是 `gpu` 运行代码，也可指定为特定gpu，如`cuda:0`。默认为 `cpu`",
    type=str,
    default='cpu',
)
@click.option("-i", "--img-file-or-dir", required=True, help="输入图片的文件路径或者指定的文件夹")
@click.option(
    "-s",
    "--single-line",
    is_flag=True,
    help="是否输入图片只包含单行文字。对包含单行文字的图片，不做按行切分；否则会先对图片按行分割后再进行识别",
)
def predict(
    model_name,
    model_backend,
    pretrained_model_fp,
    context,
    img_file_or_dir,
    single_line,
):
    """模型预测"""
    ocr = CnOcr(
        model_name=model_name,
        model_backend=model_backend,
        model_fp=pretrained_model_fp,
        context=context,
    )
    ocr_func = ocr.ocr_for_single_line if single_line else ocr.ocr
    fp_list = []
    if os.path.isfile(img_file_or_dir):
        fp_list.append(img_file_or_dir)
    elif os.path.isdir(img_file_or_dir):
        fn_list = glob.glob1(img_file_or_dir, '*g')
        fp_list = [os.path.join(img_file_or_dir, fn) for fn in fn_list]

    for fp in fp_list:
        start_time = time.time()
        logger.info('\n' + '=' * 10 + fp + '=' * 10)
        res = ocr_func(fp)
        logger.info('time cost: %f' % (time.time() - start_time))
        logger.info(res)
        if single_line:
            res = [res]
        for line_res in res:
            preds, prob = line_res
            logger.info('\npred: %s, with probability %f' % (''.join(preds), prob))


@cli.command('evaluate')
@click.option(
    '-m',
    '--model-name',
    type=str,
    default=DEFAULT_MODEL_NAME,
    help='模型名称。默认值为 %s' % DEFAULT_MODEL_NAME,
)
@click.option(
    '-b',
    '--model-backend',
    type=click.Choice(['pytorch', 'onnx']),
    default='onnx',
    help='模型类型。默认值为 `onnx`',
)
@click.option(
    '-p',
    '--pretrained-model-fp',
    type=str,
    default=None,
    help='使用训练好的模型。默认为 `None`，表示使用系统自带的预训练模型',
)
@click.option(
    "-c",
    "--context",
    help="使用cpu还是 `gpu` 运行代码，也可指定为特定gpu，如`cuda:0`。默认为 `cpu`",
    type=str,
    default='cpu',
)
@click.option(
    "-i",
    "--eval-index-fp",
    type=str,
    help='待评估文件所在的索引文件，格式与训练时训练集索引文件相同，每行格式为 `<图片路径>\t<以空格分割的labels>`',
    default='test.txt',
)
@click.option("--img-folder", required=True, help="图片所在文件夹，相对于索引文件中记录的图片位置")
@click.option("--batch-size", type=int, help="batch size. 默认值：128", default=128)
@click.option(
    '-o',
    '--output-dir',
    type=str,
    default='eval_results',
    help='存放评估结果的文件夹。默认值：`eval_results`',
)
@click.option(
    "-v", "--verbose", is_flag=True, help="whether to print details to screen",
)
def evaluate(
    model_name,
    model_backend,
    pretrained_model_fp,
    context,
    eval_index_fp,
    img_folder,
    batch_size,
    output_dir,
    verbose,
):
    """评估模型效果"""
    try:
        import Levenshtein
    except Exception as e:
        logger.error(e)
        logger.error(
            'try to install the package by using `pip install python-Levenshtein`'
        )
        return
    ocr = CnOcr(
        model_name=model_name,
        model_backend=model_backend,
        model_fp=pretrained_model_fp,
        context=context,
    )

    fn_labels_list = read_input_file(eval_index_fp)

    miss_cnt, redundant_cnt = Counter(), Counter()
    total_time_cost = 0.0
    bad_cnt = 0
    badcases = []

    start_idx = 0
    while start_idx < len(fn_labels_list):
        logger.info('start_idx: %d', start_idx)
        batch = fn_labels_list[start_idx : start_idx + batch_size]
        img_fps = [os.path.join(img_folder, fn) for fn, _ in batch]
        reals = [labels for _, labels in batch]

        imgs = [read_img(img) for img in img_fps]
        start_time = time.time()
        outs = ocr.ocr_for_single_lines(imgs, batch_size=1)
        total_time_cost += time.time() - start_time

        preds = [out[0] for out in outs]
        for bad_info in compare_preds_to_reals(preds, reals, img_fps):
            if verbose:
                logger.info('\t'.join(bad_info))
            distance = Levenshtein.distance(bad_info[1], bad_info[2])
            bad_info.insert(0, distance)
            badcases.append(bad_info)
            miss_cnt.update(list(bad_info[-2]))
            redundant_cnt.update(list(bad_info[-1]))
            bad_cnt += 1

        start_idx += batch_size

    badcases.sort(key=itemgetter(0), reverse=True)

    output_dir = Path(output_dir)
    if not output_dir.exists():
        os.makedirs(output_dir)
    with open(output_dir / 'badcases.txt', 'w') as f:
        f.write(
            '\t'.join(
                [
                    'distance',
                    'image_fp',
                    'real_words',
                    'pred_words',
                    'miss_words',
                    'redundant_words',
                ]
            )
            + '\n'
        )
        for bad_info in badcases:
            f.write('\t'.join(map(str, bad_info)) + '\n')
    with open(output_dir / 'miss_words_stat.txt', 'w') as f:
        for word, num in miss_cnt.most_common():
            f.write('\t'.join([word, str(num)]) + '\n')
    with open(output_dir / 'redundant_words_stat.txt', 'w') as f:
        for word, num in redundant_cnt.most_common():
            f.write('\t'.join([word, str(num)]) + '\n')

    logger.info(
        "number of total cases: %d, number of bad cases: %d, acc: %.4f, time cost per image: %f"
        % (
            len(fn_labels_list),
            bad_cnt,
            1.0 - bad_cnt / len(fn_labels_list),
            total_time_cost / len(fn_labels_list),
        )
    )


def read_input_file(in_fp):
    fn_labels_list = []
    with open(in_fp) as f:
        for line in f:
            fields = line.strip().split('\t')
            labels = fields[1].split(' ')
            labels = [l if l != '<space>' else ' ' for l in labels]
            fn_labels_list.append((fields[0], labels))
    return fn_labels_list


def compare_preds_to_reals(batch_preds, batch_reals, batch_img_fns):
    for preds, reals, img_fn in zip(batch_preds, batch_reals, batch_img_fns):
        if preds == reals:
            continue
        preds_set, reals_set = set(preds), set(reals)

        miss_words = reals_set.difference(preds_set)
        redundant_words = preds_set.difference(reals_set)
        yield [
            img_fn,
            ''.join(reals),
            ''.join(preds),
            ''.join(miss_words),
            ''.join(redundant_words),
        ]


@cli.command('resave')
@click.option('-i', '--input-model-fp', type=str, required=True, help='输入的模型文件路径')
@click.option('-o', '--output-model-fp', type=str, required=True, help='输出的模型文件路径')
def resave_model_file(
    input_model_fp, output_model_fp,
):
    """训练好的模型会存储训练状态，使用此命令去掉预测时无关的数据，降低模型大小"""
    resave_model(input_model_fp, output_model_fp, map_location='cpu')


def export_to_onnx(model_name, output_model_fp, input_model_fp=None):
    import onnx

    ocr = CnOcr(model_name, model_fp=input_model_fp)
    model = ocr._model

    x = torch.randn(1, 1, 32, 280)
    input_lengths = torch.tensor([280])

    model.postprocessor = None  # 这个无法ONNX化
    symbolic_names = {0: 'batch_size', 3: 'width'}
    with torch.no_grad():
        model.eval()
        torch.onnx.export(
            model,
            args=(x, input_lengths),
            f=output_model_fp,
            export_params=True,
            # opset_version=10,
            do_constant_folding=True,
            input_names=['x', 'input_lengths'],
            output_names=['logits', 'output_lengths'],
            dynamic_axes={
                'x': symbolic_names,  # variable length axes
                'input_lengths': {0: 'batch_size'},
                'logits': {0: 'batch_size'},
            },
        )

    onnx_model = onnx.load(output_model_fp)
    onnx.checker.check_model(onnx_model)
    logger.info('model is exported to %s' % output_model_fp)


@cli.command('export-onnx')
@click.option(
    '-m',
    '--model-name',
    type=str,
    default=DEFAULT_MODEL_NAME,
    help='模型名称。默认值为 `%s`' % DEFAULT_MODEL_NAME,
)
@click.option(
    '-i',
    '--input-model-fp',
    type=str,
    default=None,
    help='输入的模型文件路径。 默认为 `None`，表示使用系统自带的预训练模型',
)
@click.option(
    '-o', '--output-model-fp', type=str, required=True, help='输出的模型文件路径（.onnx）'
)
def export_onnx_model(
    model_name, input_model_fp, output_model_fp,
):
    """把训练好的模型导出为 ONNX 格式。
    当前无法导出 `*-gru` 模型， 具体说明见：https://discuss.pytorch.org/t/exporting-gru-rnn-to-onnx/27244 ，
    后续版本会修复此问题。
    """
    export_to_onnx(model_name, output_model_fp, input_model_fp)


if __name__ == "__main__":
    cli()
