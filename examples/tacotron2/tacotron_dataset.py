# -*- coding: utf-8 -*-
# Copyright 2020 Minh Nguyen (@dathudeptrai)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tacotron Related Dataset modules."""

import logging
import os
import random
import itertools
import numpy as np

import tensorflow as tf

from tensorflow_tts.datasets.abstract_dataset import AbstractDataset

from tensorflow_tts.utils import find_files

from tensorflow_tts.processor.ljspeech import symbols


def guided_attention(char_len, mel_len, g=0.2):
    """Guided attention. Refer to page 3 on the paper."""
    ga = np.zeros((char_len, mel_len), dtype=np.float32)
    for n_pos in range(char_len):
        for t_pos in range(mel_len):
            ga[n_pos, t_pos] = 1 - np.exp(-(t_pos / float(mel_len) - n_pos / float(char_len)) ** 2 / (2 * g * g))
    return ga


class CharactorMelDataset(AbstractDataset):
    """Tensorflow Charactor Mel dataset."""

    def __init__(self,
                 root_dir,
                 charactor_query="*-ids.npy",
                 mel_query="*-norm-feats.npy",
                 charactor_load_fn=np.load,
                 mel_load_fn=np.load,
                 mel_length_threshold=None,
                 return_utt_id=False,
                 reduction_factor=1,
                 mel_pad_value=0.0,
                 char_pad_value=0,
                 ga_pad_value=-1.0,
                 g=0.2,
                 ):
        """Initialize dataset.

        Args:
            root_dir (str): Root directory including dumped files.
            charactor_query (str): Query to find charactor files in root_dir.
            mel_query (str): Query to find feature files in root_dir.
            charactor_load_fn (func): Function to load charactor file.
            mel_load_fn (func): Function to load feature file.
            mel_length_threshold (int): Threshold to remove short feature files.
            return_utt_id (bool): Whether to return the utterance id with arrays.
            reduction_factor (int): Reduction factor on Tacotron-2 paper.
            mel_pad_value (float): Padding value for mel-spectrogram.
            char_pad_value (int): Padding value for charactor.
            ga_pad_value (float): Padding value for guided attention.
            g (float): G value for guided attention.

        """
        # find all of charactor and mel files.
        charactor_files = sorted(find_files(root_dir, charactor_query))
        mel_files = sorted(find_files(root_dir, mel_query))
        mel_lengths = [mel_load_fn(f).shape[0] for f in mel_files]
        char_lengths = [charactor_load_fn(f).shape[0] for f in charactor_files]

        # filter by threshold
        if mel_length_threshold is not None:
            idxs = [idx for idx in range(len(mel_files)) if mel_lengths[idx] > mel_length_threshold]
            if len(mel_files) != len(idxs):
                logging.warning(f"Some files are filtered by mel length threshold "
                                f"({len(mel_files)} -> {len(idxs)}).")
            mel_files = [mel_files[idx] for idx in idxs]
            charactor_files = [charactor_files[idx] for idx in idxs]
            mel_lengths = [mel_lengths[idx] for idx in idxs]
            char_lengths = [char_lengths[idx] for idx in idxs]

            # bucket sequence length trick, sort based-on mel-length.
            idx_sort = np.argsort(mel_lengths)

            # sort
            mel_files = np.array(mel_files)[idx_sort]
            charactor_files = np.array(charactor_files)[idx_sort]
            mel_lengths = np.array(mel_lengths)[idx_sort]
            char_lengths = np.array(char_lengths)[idx_sort]

            # group
            idx_lengths = [[idx, length] for idx, length in zip(np.arange(len(mel_lengths)), mel_lengths)]
            groups = [list(g) for _, g in itertools.groupby(idx_lengths, lambda a: a[1])]

            # group shuffle
            random.shuffle(groups)

            # get idxs affter group shuffle
            idxs = []
            for group in groups:
                for idx, _ in group:
                    idxs.append(idx)

            # re-arange dataset
            mel_files = np.array(mel_files)[idxs]
            charactor_files = np.array(charactor_files)[idxs]
            mel_lengths = np.array(mel_lengths)[idxs]
            char_lengths = np.array(char_lengths)[idxs]

        # assert the number of files
        assert len(mel_files) != 0, f"Not found any mels files in ${root_dir}."
        assert len(mel_files) == len(charactor_files) == len(mel_lengths), \
            f"Number of charactor, mel and duration files are different \
                ({len(mel_files)} vs {len(charactor_files)} vs {len(mel_lengths)})."

        if ".npy" in charactor_query:
            utt_ids = [os.path.basename(f).replace("-ids.npy", "") for f in charactor_files]

        # set global params
        self.utt_ids = utt_ids
        self.mel_files = mel_files
        self.charactor_files = charactor_files
        self.mel_load_fn = mel_load_fn
        self.charactor_load_fn = charactor_load_fn
        self.return_utt_id = return_utt_id
        self.mel_lengths = mel_lengths
        self.char_lengths = char_lengths
        self.reduction_factor = reduction_factor
        self.mel_pad_value = mel_pad_value
        self.char_pad_value = char_pad_value
        self.ga_pad_value = ga_pad_value
        self.g = g

    def get_args(self):
        return [self.utt_ids]

    def generator(self, utt_ids):
        for i, utt_id in enumerate(utt_ids):
            mel_file = self.mel_files[i]
            charactor_file = self.charactor_files[i]
            mel = self.mel_load_fn(mel_file)
            charactor = self.charactor_load_fn(charactor_file)
            mel_length = self.mel_lengths[i]
            char_length = self.char_lengths[i]

            # add eos token for charactor since charactor is original token.
            charactor = np.concatenate([charactor, [len(symbols) - 1]], -1)
            char_length += 1

            # padding mel to make its length is multiple of reduction factor.
            remainder = mel_length % self.reduction_factor
            if remainder != 0:
                new_mel_length = mel_length + self.reduction_factor - remainder
                mel = np.pad(mel, [[0, new_mel_length - mel_length], [0, 0]], constant_values=self.mel_pad_value)
                mel_length = new_mel_length

            # create guided attention (default).
            g_attention = guided_attention(char_length, mel_length // self.reduction_factor, self.g)

            if self.return_utt_id:
                items = utt_id, charactor, char_length, mel, mel_length, g_attention
            else:
                items = charactor, char_length, mel, mel_length, g_attention
            yield items

    def create(self,
               allow_cache=False,
               batch_size=1,
               is_shuffle=False,
               map_fn=None,
               reshuffle_each_iteration=True
               ):
        """Create tf.dataset function."""
        output_types = self.get_output_dtypes()
        datasets = tf.data.Dataset.from_generator(
            self.generator,
            output_types=output_types,
            args=(self.get_args())
        )

        if allow_cache:
            datasets = datasets.cache()

        if is_shuffle:
            datasets = datasets.shuffle(
                self.get_len_dataset(), reshuffle_each_iteration=reshuffle_each_iteration)

        # define padding value for each element
        padding_values = (self.char_pad_value, 0, self.mel_pad_value, 0, self.ga_pad_value)

        # define padded shapes.
        padded_shapes = ([None], [], [None, 80], [], [None, None])

        if self.return_utt_id:
            padding_values = ("", *padding_values)
            padded_shapes = ([], *padded_shapes)

        datasets = datasets.padded_batch(batch_size,
                                         padded_shapes=padded_shapes,
                                         padding_values=padding_values
                                         )
        datasets = datasets.prefetch(tf.data.experimental.AUTOTUNE)
        return datasets

    def get_output_dtypes(self):
        output_types = (tf.int32, tf.int32, tf.float32, tf.int32, tf.float32)
        if self.return_utt_id:
            output_types = (tf.dtypes.string, *output_types)
        return output_types

    def get_len_dataset(self):
        return len(self.utt_ids)

    def __name__(self):
        return "CharactorMelDataset"
