#!/usr/bin/env python3
#
#    This file is part of Leela Zero.
#    Copyright (C) 2017-2018 Gian-Carlo Pascutto
#
#    Leela Zero is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Leela Zero is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with Leela Zero.  If not, see <http://www.gnu.org/licenses/>.

import numpy as np
import os
import os.path
import tensorflow as tf
import time
import bisect
import operator
from functools import reduce

from .net import Net, ApplyPolicyMap, ApplySqueezeExcitation
from ..utils import printWithDate
from ..file_handling.data_file_reader import move_time_splits
import maia_rl.proto.net_pb2 as pb


class TFProcess:
    def __init__(self, cfg, model_name, data_name, train_handler, val_handler, tb_logs_dir = 'runs', models_dir = 'models'):
        tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.ERROR)
        self.cfg = cfg
        self.data_name = data_name
        self.model_name = model_name
        self.tb_logs_dir = tb_logs_dir
        self.models_dir = models_dir
        self.train_handler = train_handler
        self.val_handler = val_handler
        self.board_stack_size = cfg['model'].get('board_stack_size', 8)
        self.net = Net()
        self.root_dir = os.path.join(self.models_dir,  self.data_name)

         # Network structure
        self.RESIDUAL_FILTERS = self.cfg['model']['filters']
        self.RESIDUAL_BLOCKS = self.cfg['model']['residual_blocks']
        self.SE_ratio = self.cfg['model']['se_ratio']
        self.policy_channels = self.cfg['model'].get('policy_channels', 32)
        precision = self.cfg['training'].get('precision', 'single')
        loss_scale = self.cfg['training'].get('loss_scale', 128)
        self.virtual_batch_size = self.cfg['model'].get(
            'virtual_batch_size', None)

        if precision == 'single':
            self.model_dtype = tf.float32
        elif precision == 'half':
            self.model_dtype = tf.float16
        else:
            raise ValueError("Unknown precision: {}".format(precision))

        # Scale the loss to prevent gradient underflow
        self.loss_scale = 1 if self.model_dtype == tf.float32 else loss_scale
        value_head = self.cfg['model'].get('value', 'wdl')
        moves_left_head = self.cfg['model'].get('moves_left', 'v1')
        input_mode = self.cfg['model'].get('input_type', 'classic')

        self.POLICY_HEAD = None
        self.VALUE_HEAD = None
        self.MOVES_LEFT_HEAD = None
        self.INPUT_MODE = None

        self.POLICY_HEAD = pb.NetworkFormat.POLICY_CONVOLUTION
        self.net.set_policyformat(self.POLICY_HEAD)

        if value_head == "classical":
            self.VALUE_HEAD = pb.NetworkFormat.VALUE_CLASSICAL
            self.wdl = False
        elif value_head == "wdl":
            self.VALUE_HEAD = pb.NetworkFormat.VALUE_WDL
            self.wdl = True
        else:
            raise ValueError(
                "Unknown value head format: {}".format(value_head))

        self.net.set_valueformat(self.VALUE_HEAD)

        if moves_left_head == "none":
            self.MOVES_LEFT_HEAD = pb.NetworkFormat.MOVES_LEFT_NONE
            self.moves_left = False
        elif moves_left_head == "v1":
            self.MOVES_LEFT_HEAD = pb.NetworkFormat.MOVES_LEFT_V1
            self.moves_left = True
        else:
            raise ValueError(
                "Unknown moves left head format: {}".format(moves_left_head))

        self.net.set_movesleftformat(self.MOVES_LEFT_HEAD)

        if input_mode == "classic":
            self.INPUT_MODE = pb.NetworkFormat.INPUT_CLASSICAL_112_PLANE
        else:
            raise ValueError(
                "Unknown input mode format: {}".format(input_mode))

        self.net.set_input(self.INPUT_MODE)

        self.swa_enabled = self.cfg['training'].get('swa', False)

        # Limit momentum of SWA exponential average to 1 - 1/(swa_max_n + 1)
        self.swa_max_n = self.cfg['training'].get('swa_max_n', 0)

        self.renorm_enabled = self.cfg['training'].get('renorm', False)
        self.renorm_max_r = self.cfg['training'].get('renorm_max_r', 1)
        self.renorm_max_d = self.cfg['training'].get('renorm_max_d', 0)
        self.renorm_momentum = self.cfg['training'].get(
            'renorm_momentum', 0.99)

        self.strategy = None
        if self.model_dtype == tf.float16:
            tf.keras.mixed_precision.experimental.set_policy('mixed_float16')

        self.global_step = tf.Variable(0,
                                       name='global_step',
                                       trainable=False,
                                       dtype=tf.int64)

        self.tb_writer = tf.summary.create_file_writer(os.path.join(
                self.tb_logs_dir,
                self.data_name,
                'train-set',
        ))

    def init_v2(self, train_dataset, test_dataset, starting_model = None):
        self.train_dataset = train_dataset
        self.train_iter = iter(self.train_dataset)
        self.test_dataset = test_dataset
        self.test_iter = iter(self.test_dataset)
        self.init_net_v2(starting_model = starting_model)

    def restore_ckpt(self, ckpt_path):
        printWithDate("loading weights from {}".format(ckpt_path))
        self.checkpoint_restore.restore(ckpt_path)

    def init_net_v2(self, starting_model = None):
        self.l2reg = tf.keras.regularizers.l2(l=0.5 * (0.0001))
        input_var = tf.keras.Input(shape=(112, 8, 8))
        #x_planes = tf.keras.layers.Reshape([112, 8, 8])(input_var)
        policy, value, moves_left = self.construct_net_v2(input_var)
        if self.moves_left:
            outputs = [policy, value, moves_left]
        else:
            outputs = [policy, value]
        self.model = tf.keras.Model(inputs=input_var, outputs=outputs)

        if starting_model is not None:
            if not os.path.isfile(os.path.join(starting_model, 'checkpoint')):
                raise FileNotFoundError(f"{os.path.join(starting_model, 'checkpoint')} not found, this isn't a checkpoint directory")
            self.checkpoint_restore = tf.train.Checkpoint(model=self.model)
            self.restore_ckpt(tf.train.latest_checkpoint(starting_model))


        # swa_count initialized reguardless to make checkpoint code simpler.
        self.swa_count = tf.Variable(0., name='swa_count', trainable=False)
        self.swa_weights = None
        if self.swa_enabled:
            # Count of networks accumulated into SWA
            self.swa_weights = [
                tf.Variable(w, trainable=False) for w in self.model.weights
            ]

        self.active_lr = 0.01
        self.optimizer = tf.keras.optimizers.SGD(
            learning_rate=lambda: self.active_lr, momentum=0.9, nesterov=True)
        self.orig_optimizer = self.optimizer
        if self.loss_scale != 1:
            self.optimizer = tf.keras.mixed_precision.experimental.LossScaleOptimizer(
                self.optimizer, self.loss_scale)

        def correct_policy(target, output):
            output = tf.cast(output, tf.float32)
            # Calculate loss on policy head
            if self.cfg['training'].get('mask_legal_moves'):
                # extract mask for legal moves from target policy
                move_is_legal = tf.greater_equal(target, 0)
                # replace logits of illegal moves with large negative value (so that it doesn't affect policy of legal moves) without gradient
                illegal_filler = tf.zeros_like(output) - 1.0e10
                output = tf.where(move_is_legal, output, illegal_filler)
            # y_ still has -1 on illegal moves, flush them to 0
            target = tf.nn.relu(target)
            return target, output

        def policy_loss(target, output):
            target, output = correct_policy(target, output)
            policy_cross_entropy = tf.nn.softmax_cross_entropy_with_logits(
                labels=tf.stop_gradient(target), logits=output)
            return tf.reduce_mean(input_tensor=policy_cross_entropy)

        self.policy_loss_fn = policy_loss

        def policy_accuracy(target, output):
            target, output = correct_policy(target, output)
            return tf.reduce_mean(
                tf.cast(
                    tf.equal(tf.argmax(input=target, axis=1),
                             tf.argmax(input=output, axis=1)), tf.float32))

        self.policy_accuracy_fn = policy_accuracy

        self.policy_accuracy_fn = policy_accuracy

        def moves_left_mean_error_fn(target, output):
            output = tf.cast(output, tf.float32)
            return tf.reduce_mean(tf.abs(target - output))

        self.moves_left_mean_error = moves_left_mean_error_fn

        def policy_entropy(target, output):
            target, output = correct_policy(target, output)
            softmaxed = tf.nn.softmax(output)
            return tf.math.negative(
                tf.reduce_mean(
                    tf.reduce_sum(tf.math.xlogy(softmaxed, softmaxed),
                                  axis=1)))

        self.policy_entropy_fn = policy_entropy

        def policy_uniform_loss(target, output):
            uniform = tf.where(tf.greater_equal(target, 0),
                               tf.ones_like(target), tf.zeros_like(target))
            balanced_uniform = uniform / tf.reduce_sum(
                uniform, axis=1, keepdims=True)
            target, output = correct_policy(target, output)
            policy_cross_entropy = \
                tf.nn.softmax_cross_entropy_with_logits(labels=tf.stop_gradient(balanced_uniform),
                                                        logits=output)
            return tf.reduce_mean(input_tensor=policy_cross_entropy)

        self.policy_uniform_loss_fn = policy_uniform_loss

        q_ratio = self.cfg['training'].get('q_ratio', 0)
        assert 0 <= q_ratio <= 1

        # Linear conversion to scalar to compute MSE with, for comparison to old values
        wdl = tf.expand_dims(tf.constant([1.0, 0.0, -1.0]), 1)

        self.qMix = lambda z, q: q * q_ratio + z * (1 - q_ratio)
        # Loss on value head
        if self.wdl:

            def value_loss(target, output):
                output = tf.cast(output, tf.float32)
                value_cross_entropy = tf.nn.softmax_cross_entropy_with_logits(
                    labels=tf.stop_gradient(target), logits=output)
                return tf.reduce_mean(input_tensor=value_cross_entropy)

            self.value_loss_fn = value_loss

            def mse_loss(target, output):
                output = tf.cast(output, tf.float32)
                scalar_z_conv = tf.matmul(tf.nn.softmax(output), wdl)
                scalar_target = tf.matmul(target, wdl)
                return tf.reduce_mean(input_tensor=tf.math.squared_difference(
                    scalar_target, scalar_z_conv))

            self.mse_loss_fn = mse_loss
        else:

            def value_loss(target, output):
                return tf.constant(0)

            self.value_loss_fn = value_loss

            def mse_loss(target, output):
                output = tf.cast(output, tf.float32)
                scalar_target = tf.matmul(target, wdl)
                return tf.reduce_mean(input_tensor=tf.math.squared_difference(
                    scalar_target, output))

            self.mse_loss_fn = mse_loss

        if self.moves_left:

            def moves_left_loss(target, output):
                # Scale the loss to similar range as other losses.
                scale = 20.0
                target = target / scale
                output = tf.cast(output, tf.float32) / scale
                if self.strategy is not None:
                    huber = tf.keras.losses.Huber(
                        10.0 / scale, reduction=tf.keras.losses.Reduction.NONE)
                else:
                    huber = tf.keras.losses.Huber(10.0 / scale)
                return tf.reduce_mean(huber(target, output))
        else:
            moves_left_loss = None

        self.moves_left_loss_fn = moves_left_loss

        pol_loss_w = self.cfg['training']['policy_loss_weight']
        val_loss_w = self.cfg['training']['value_loss_weight']

        if self.moves_left:
            moves_loss_w = self.cfg['training']['moves_left_loss_weight']
        else:
            moves_loss_w = tf.constant(0.0, dtype=tf.float32)

        def _lossMix(policy, value, moves_left):
            return pol_loss_w * policy + val_loss_w * value + moves_loss_w * moves_left

        self.lossMix = _lossMix

        def accuracy(target, output):
            output = tf.cast(output, tf.float32)
            return tf.reduce_mean(
                tf.cast(
                    tf.equal(tf.argmax(input=target, axis=1),
                             tf.argmax(input=output, axis=1)), tf.float32))

        self.accuracy_fn = accuracy

        self.avg_policy_loss = []
        self.avg_value_loss = []
        self.avg_moves_left_loss = []
        self.avg_mse_loss = []
        self.avg_reg_term = []
        self.time_start = None
        self.last_steps = None
        # Set adaptive learning rate during training
        self.cfg['training']['lr_boundaries'].sort()
        self.warmup_steps = self.cfg['training'].get('warmup_steps', 0)
        self.lr = self.cfg['training']['lr_values'][0]
        self.test_writer = tf.summary.create_file_writer(
            os.path.join(os.getcwd(),
                         "leelalogs/{}-test".format(self.model_name)))
        self.train_writer = tf.summary.create_file_writer(
            os.path.join(os.getcwd(),
                         "leelalogs/{}-train".format(self.model_name)))
        if vars(self).get('validation_dataset', None) is not None:
            self.validation_writer = tf.summary.create_file_writer(
                os.path.join(
                    os.getcwd(),
                    "leelalogs/{}-validation".format(self.model_name)))
        if self.swa_enabled:
            self.swa_writer = tf.summary.create_file_writer(
                os.path.join(os.getcwd(),
                             "leelalogs/{}-swa-test".format(self.model_name)))
            self.swa_validation_writer = tf.summary.create_file_writer(
                os.path.join(
                    os.getcwd(),
                    "leelalogs/{}-swa-validation".format(self.model_name)))
        self.checkpoint = tf.train.Checkpoint(optimizer=self.orig_optimizer,
                                              model=self.model,
                                              global_step=self.global_step,
                                              swa_count=self.swa_count)
        self.checkpoint.listed = self.swa_weights
        self.manager = tf.train.CheckpointManager(
            self.checkpoint,
            directory=self.root_dir,
            max_to_keep=50,
            keep_checkpoint_every_n_hours=24,
            checkpoint_name=self.model_name)

    def replace_weights_v2(self, proto_filename, ignore_errors=False):
        self.net.parse_proto(proto_filename)

        filters, blocks = self.net.filters(), self.net.blocks()
        if not ignore_errors:
            if self.RESIDUAL_FILTERS != filters:
                raise ValueError("Number of filters doesn't match the network")
            if self.RESIDUAL_BLOCKS != blocks:
                raise ValueError("Number of blocks doesn't match the network")
            if self.VALUE_HEAD != self.net.pb.format.network_format.value:
                raise ValueError("Value head type doesn't match the network")

        # List all tensor names we need weights for.
        names = []
        for weight in self.model.weights:
            names.append(weight.name)

        new_weights = self.net.get_weights_v2(names)
        for weight in self.model.weights:
            if 'renorm' in weight.name:
                # Renorm variables are not populated.
                continue

            try:
                new_weight = new_weights[weight.name]
            except KeyError:
                error_string = 'No values for tensor {} in protobuf'.format(
                    weight.name)
                if ignore_errors:
                    print(error_string)
                    continue
                else:
                    raise KeyError(error_string)

            if reduce(operator.mul, weight.shape.as_list(),
                      1) != len(new_weight):
                error_string = 'Tensor {} has wrong length. Tensorflow shape {}, size in protobuf {}'.format(
                    weight.name, weight.shape.as_list(), len(new_weight))
                if ignore_errors:
                    print(error_string)
                    continue
                else:
                    raise KeyError(error_string)

            if weight.shape.ndims == 4:
                # Rescale rule50 related weights as clients do not normalize the input.
                if weight.name == 'input/conv2d/kernel:0' and self.net.pb.format.network_format.input < pb.NetworkFormat.INPUT_112_WITH_CANONICALIZATION_HECTOPLIES:
                    num_inputs = 112
                    # 50 move rule is the 110th input, or 109 starting from 0.
                    rule50_input = 109
                    for i in range(len(new_weight)):
                        if (i % (num_inputs * 9)) // 9 == rule50_input:
                            new_weight[i] = new_weight[i] * 99

                # Convolution weights need a transpose
                #
                # TF (kYXInputOutput)
                # [filter_height, filter_width, in_channels, out_channels]
                #
                # Leela/cuDNN/Caffe (kOutputInputYX)
                # [output, input, filter_size, filter_size]
                s = weight.shape.as_list()
                shape = [s[i] for i in [3, 2, 0, 1]]
                new_weight = tf.constant(new_weight, shape=shape)
                weight.assign(tf.transpose(a=new_weight, perm=[2, 3, 1, 0]))
            elif weight.shape.ndims == 2:
                # Fully connected layers are [in, out] in TF
                #
                # [out, in] in Leela
                #
                s = weight.shape.as_list()
                shape = [s[i] for i in [1, 0]]
                new_weight = tf.constant(new_weight, shape=shape)
                weight.assign(tf.transpose(a=new_weight, perm=[1, 0]))
            else:
                # Biases, batchnorm etc
                new_weight = tf.constant(new_weight, shape=weight.shape)
                weight.assign(new_weight)
        # Replace the SWA weights as well, ensuring swa accumulation is reset.
        if self.swa_enabled:
            self.swa_count.assign(tf.constant(0.))
            self.update_swa_v2()
        # This should result in identical file to the starting one
        # self.save_leelaz_weights_v2('restored.pb.gz')

    def restore_v2(self):
        if self.manager.latest_checkpoint is not None:
            print("Restoring from {0}".format(self.manager.latest_checkpoint))
            self.checkpoint.restore(self.manager.latest_checkpoint)

    def process_loop_v2(self, batch_size, test_batches, batch_splits=1):
        if self.swa_enabled:
            # split half of test_batches between testing regular weights and SWA weights
            test_batches //= 2
        # Make sure that ghost batch norm can be applied
        if self.virtual_batch_size and batch_size % self.virtual_batch_size != 0:
            # Adjust required batch size for batch splitting.
            required_factor = self.virtual_batch_size * self.cfg[
                'training'].get('num_batch_splits', 1)
            raise ValueError(
                'batch_size must be a multiple of {}'.format(required_factor))

        # Get the initial steps value in case this is a resume from a step count
        # which is not a multiple of total_steps.
        steps = self.global_step.read_value()
        self.last_steps = steps
        self.time_start = time.time()
        self.profiling_start_step = None

        total_steps = self.cfg['training']['total_steps']
        for _ in range(steps % total_steps, total_steps):
            self.process_v2(
                            batch_size,
                            test_batches,
                            batch_splits=batch_splits,
                            )

    @tf.function()
    def read_weights(self):
        return [w.read_value() for w in self.model.weights]

    @tf.function()
    def process_inner_loop(self, x, y, z, q):
        with tf.GradientTape() as tape:
            outputs = self.model(x, training=True)
            policy = outputs[0]
            value = outputs[1]
            policy_loss = self.policy_loss_fn(y, policy)
            reg_term = sum(self.model.losses)
            if self.wdl:
                value_ce_loss = self.value_loss_fn(self.qMix(z, q), value)
                value_loss = value_ce_loss
            else:
                value_mse_loss = self.mse_loss_fn(self.qMix(z, q), value)
                value_loss = value_mse_loss
            moves_left_loss = tf.constant(0.)
            total_loss = self.lossMix(policy_loss, value_loss, moves_left_loss) + reg_term
            if self.loss_scale != 1:
                total_loss = self.optimizer.get_scaled_loss(total_loss)
        if self.wdl:
            mse_loss = self.mse_loss_fn(self.qMix(z, q), value)
        else:
            value_loss = self.value_loss_fn(self.qMix(z, q), value)
        return policy_loss, value_loss, mse_loss, moves_left_loss, reg_term, tape.gradient(
            total_loss, self.model.trainable_weights)

    def apply_grads(self, grads, effective_batch_splits):
        if self.loss_scale != 1:
            grads = self.optimizer.get_unscaled_gradients(grads)
        max_grad_norm = self.cfg['training'].get(
            'max_grad_norm', 10000.0) * effective_batch_splits
        grads, grad_norm = tf.clip_by_global_norm(grads, max_grad_norm)
        self.optimizer.apply_gradients(zip(grads,
                                           self.model.trainable_weights))
        return grad_norm

    @tf.function()
    def merge_grads(self, grads, new_grads):
        return [tf.math.add(a, b) for (a, b) in zip(grads, new_grads)]

    def train_step(self, steps, batch_size):
        if (steps +
                1) % self.cfg['training']['train_avg_report_steps'] == 0 or (
                    steps + 1) % self.cfg['training']['total_steps'] == 0:
            before_weights = self.read_weights()

        x, y, z, q = next(self.train_iter)

        policy_loss, value_loss, mse_loss, moves_left_loss, reg_term, grads = self.process_inner_loop( x, y, z, q)

        # Keep running averages
        # Google's paper scales MSE by 1/4 to a [0, 1] range, so do the same to
        # get comparable values.
        mse_loss /= 4.0
        self.avg_policy_loss.append(policy_loss)
        if self.wdl:
            self.avg_value_loss.append(value_loss)
        if self.moves_left:
            self.avg_moves_left_loss.append(moves_left_loss)
        self.avg_mse_loss.append(mse_loss)
        self.avg_reg_term.append(reg_term)

        # Gradients of batch splits are summed, not averaged like usual, so need to scale lr accordingly to correct for this.
        self.active_lr = self.lr # batch_splits = 1
        grad_norm = self.apply_grads(grads, 1)

        # Update steps.
        self.global_step.assign_add(1)
        steps = self.global_step.read_value()

        if steps % self.cfg['training']['train_avg_report_steps'] == 0 or steps % self.cfg['training']['total_steps'] == 0:
            pol_loss_w = self.cfg['training']['policy_loss_weight']
            val_loss_w = self.cfg['training']['value_loss_weight']
            time_end = time.time()
            speed = 0
            if self.time_start:
                elapsed = time_end - self.time_start
                steps_elapsed = steps - self.last_steps
                speed = batch_size * (tf.cast(steps_elapsed, tf.float32) / elapsed)
            avg_policy_loss = np.mean(self.avg_policy_loss or [0])
            avg_value_loss = np.mean(self.avg_value_loss or [0])
            avg_mse_loss = np.mean(self.avg_mse_loss or [0])
            avg_reg_term = np.mean(self.avg_reg_term or [0])

            num_games = len(self.train_handler.sample_ids)
            num_samples = self.train_handler.num_samples
            game_ratio = num_games / num_samples

            printWithDate("step {} games {} ({:.2f}), lr={:.4g} policy={:.4g} value={:.4g} mse={:.4g} reg={:.4g} total={:.4g} ({:.0f} pos/s)".format(
                    steps,
                    num_games,
                    game_ratio,
                    self.lr,
                    avg_policy_loss,
                    avg_value_loss,
                    avg_mse_loss,
                    avg_reg_term,
                    pol_loss_w * avg_policy_loss + val_loss_w * avg_value_loss + avg_reg_term,
                    speed,
                    ), flush = True)

            after_weights = self.read_weights()
            with self.tb_writer.as_default():
                tf.summary.scalar("5-Positions per Second", speed, step=steps)
                tf.summary.scalar("1-Policy Loss", avg_policy_loss, step=steps)
                tf.summary.scalar("3-Value Loss", avg_value_loss, step=steps)
                tf.summary.scalar("Reg term", avg_reg_term, step=steps)
                tf.summary.scalar("LR", self.lr, step=steps)
                tf.summary.scalar("Gradient norm", grad_norm, step=steps)
                tf.summary.scalar("2-MSE Loss", avg_mse_loss, step=steps)
                tf.summary.scalar("Info-Num Games", num_games, step=steps)
                tf.summary.scalar("Info-Ratio Novel Games", game_ratio, step=steps)
                tf.summary.scalar("Info-Total Samples", num_samples, step=steps)

                self.compute_update_ratio_v2(
                    before_weights, after_weights, steps)
            self.time_start = time_end
            self.last_steps = steps
            self.avg_policy_loss  = []
            self.avg_value_loss = []
            self.avg_mse_loss = []
            self.avg_reg_term = []
            self.avg_move_time_loss = []
        return steps
    #@profile
    def process_v2(self, batch_size, test_batches, batch_splits=1):
        if not self.time_start:
            self.time_start = time.time()

        # Get the initial steps value before we do a training step.
        steps = self.global_step.read_value()
        if not self.last_steps:
            self.last_steps = steps

        # Run test before first step to see delta since end of last run.
        if steps % self.cfg['training']['total_steps'] == 0:
            # Steps is given as one higher than current in order to avoid it
            # being equal to the value the end of a run is stored against.
            self.calculate_test_summaries_v2(test_batches, steps + 1)

        # Make sure that ghost batch norm can be applied
        if batch_size % 2 != 0:
            # Adjust required batch size for batch splitting.
            required_factor = 2 * self.cfg['training'].get('num_batch_splits', 1)
            raise ValueError('batch_size must be a multiple of {}'.format(required_factor))

        # Determine learning rate
        lr_values = self.cfg['training']['lr_values']
        lr_boundaries = self.cfg['training']['lr_boundaries']
        steps_total = steps % self.cfg['training']['total_steps']
        self.lr = lr_values[bisect.bisect_right(lr_boundaries, steps_total)]
        if self.warmup_steps > 0 and steps < self.warmup_steps:
            self.lr = self.lr * tf.cast(steps + 1, tf.float32) / self.warmup_steps

        # need to add 1 to steps because steps will be incremented after gradient update
        if (steps + 1) % self.cfg['training']['train_avg_report_steps'] == 0 or (steps + 1) % self.cfg['training']['total_steps'] == 0:
            before_weights = self.read_weights()

        with tf.profiler.experimental.Trace("Train", step_num=steps):
            steps = self.train_step(steps, batch_size)

        # Calculate test values every 'test_steps', but also ensure there is
        # one at the final step so the delta to the first step can be calculted.
        if steps % self.cfg['training']['test_steps'] == 0 or steps % self.cfg['training']['total_steps'] == 0:
            self.calculate_test_summaries_v2(test_batches, steps)

        # Save session and weights at end, and also optionally every 'checkpoint_steps'.
        if steps % self.cfg['training']['total_steps'] == 0 or (
                'checkpoint_steps' in self.cfg['training'] and steps % self.cfg['training']['checkpoint_steps'] == 0):
            self.manager.save()
            printWithDate(f"Model saved in file: {self.manager.latest_checkpoint}")
            evaled_steps = steps.numpy()
            leela_path = self.manager.latest_checkpoint + "-" + str(evaled_steps)
            swa_path = self.manager.latest_checkpoint + "-swa-" + str(evaled_steps)
            self.net.pb.training_params.training_steps = evaled_steps
            self.save_leelaz_weights_v2(leela_path)
            printWithDate("Weights saved in file: {}".format(leela_path))

    @tf.function()
    def calculate_test_summaries_inner_loop(self, x, y, z, q):
        outputs = self.model(x, training=False)
        policy = outputs[0]
        value = outputs[1]
        policy_loss = self.policy_loss_fn(y, policy)
        policy_accuracy = self.policy_accuracy_fn(y, policy)
        policy_entropy = self.policy_entropy_fn(y, policy)
        policy_ul = self.policy_uniform_loss_fn(y, policy)
        if self.wdl:
            value_loss = self.value_loss_fn(self.qMix(z, q), value)
            mse_loss = self.mse_loss_fn(self.qMix(z, q), value)
            value_accuracy = self.accuracy_fn(self.qMix(z, q), value)
        else:
            value_loss = self.value_loss_fn(self.qMix(z, q), value)
            mse_loss = self.mse_loss_fn(self.qMix(z, q), value)
            value_accuracy = tf.constant(0.)
        moves_left_loss = tf.constant(0.)
        moves_left_mean_error = tf.constant(0.)
        return policy_loss, value_loss, moves_left_loss, mse_loss, policy_accuracy, value_accuracy, moves_left_mean_error, policy_entropy, policy_ul

    def calculate_test_summaries_v2(self, test_batches, steps):
        sum_policy_accuracy = 0
        sum_value_accuracy = 0
        sum_moves_left = 0
        sum_moves_left_mean_error = 0
        sum_mse = 0
        sum_policy = 0
        sum_value = 0
        sum_policy_entropy = 0
        sum_policy_ul = 0
        for i in range(0, test_batches):
            x, y, z, q = next(self.test_iter)
            policy_loss, value_loss, moves_left_loss, mse_loss, policy_accuracy, value_accuracy, moves_left_mean_error, policy_entropy, policy_ul = self.calculate_test_summaries_inner_loop(x, y, z, q)
            sum_policy_accuracy += policy_accuracy
            sum_mse += mse_loss
            sum_policy += policy_loss
            if self.wdl:
                sum_value_accuracy += value_accuracy
                sum_value += value_loss
        sum_policy_accuracy /= test_batches
        sum_policy_accuracy *= 100
        sum_policy /= test_batches
        sum_value /= test_batches
        if self.wdl:
            sum_value_accuracy /= test_batches
            sum_value_accuracy *= 100
        # Additionally rescale to [0, 1] so divide by 4
        sum_mse /= (4.0 * test_batches)
        self.net.pb.training_params.learning_rate = self.lr
        self.net.pb.training_params.mse_loss = sum_mse
        self.net.pb.training_params.policy_loss = sum_policy
        # TODO store value and value accuracy in pb
        self.net.pb.training_params.accuracy = sum_policy_accuracy
        with self.tb_writer.as_default():
            tf.summary.scalar("1-Policy Loss Validate", sum_policy, step=steps)
            tf.summary.scalar("3-Value Loss Validate", sum_value, step=steps)
            tf.summary.scalar("2-MSE Loss Validate", sum_mse, step=steps)
            tf.summary.scalar("0-Policy Accuracy Validate", sum_policy_accuracy, step=steps)
            tf.summary.scalar("Policy Entropy", sum_policy_entropy, step=steps)
            tf.summary.scalar("Policy UL", sum_policy_ul, step=steps)

            if self.wdl:
                tf.summary.scalar("4-Value Accuracy Validate", sum_value_accuracy, step=steps)
            #for w in self.model.weights:
            #    tf.summary.histogram(w.name, w, buckets=1000, step=steps)
            #tf.summary.histogram("Indices", [min(i, 100) for i in self.train_handler.sample_indices], buckets=100, step=steps)
            self.train_handler.sample_indices = []
            self.val_handler.sample_indices = []
        self.tb_writer.flush()
        printWithDate("step {}, policy={:.4g} value={:.4g} policy accuracy={:.2f}% value accuracy={:.2f}% mse={:.4g}".\
            format(
                    steps,
                    sum_policy,
                    sum_value,
                    sum_policy_accuracy,
                    sum_value_accuracy,
                    sum_mse), flush = True)

    @tf.function()
    def compute_update_ratio_v2(self, before_weights, after_weights, steps):
        """Compute the ratio of gradient norm to weight norm.

        Adapted from https://github.com/tensorflow/minigo/blob/c923cd5b11f7d417c9541ad61414bf175a84dc31/dual_net.py#L567
        """
        deltas = [
            after - before
            for after, before in zip(after_weights, before_weights)
        ]
        delta_norms = [tf.math.reduce_euclidean_norm(d) for d in deltas]
        weight_norms = [
            tf.math.reduce_euclidean_norm(w) for w in before_weights
        ]
        ratios = [(tensor.name, tf.cond(w != 0., lambda: d / w, lambda: -1.))
                  for d, w, tensor in zip(delta_norms, weight_norms,
                                          self.model.weights)
                  if not 'moving' in tensor.name]
        for name, ratio in ratios:
            tf.summary.scalar('update_ratios/' + name, ratio, step=steps)
        # Filtering is hard, so just push infinities/NaNs to an unreasonably large value.
        ratios = [
            tf.cond(r > 0, lambda: tf.math.log(r) / 2.30258509299,
                    lambda: 200.) for (_, r) in ratios
        ]
        tf.summary.histogram('update_ratios_log10',
                             tf.stack(ratios),
                             buckets=1000,
                             step=steps)

    def update_swa_v2(self):
        num = self.swa_count.read_value()
        for (w, swa) in zip(self.model.weights, self.swa_weights):
            swa.assign(swa.read_value() * (num / (num + 1.)) + w.read_value() *
                       (1. / (num + 1.)))
        self.swa_count.assign(min(num + 1., self.swa_max_n))

    def save_swa_weights_v2(self, filename):
        backup = self.read_weights()
        for (swa, w) in zip(self.swa_weights, self.model.weights):
            w.assign(swa.read_value())
        self.save_leelaz_weights_v2(filename)
        for (old, w) in zip(backup, self.model.weights):
            w.assign(old)

    def save_leelaz_weights_v2(self, filename):
        numpy_weights = []
        for weight in self.model.weights:
            numpy_weights.append([weight.name, weight.numpy()])
        self.net.fill_net_v2(numpy_weights)
        self.net.save_proto(filename)

    def set_name(self, name, suffix):
        return None if not name else "{}/{}".format(name, suffix)

    def batch_norm_v2(self, input, name, scale=False):
        if self.renorm_enabled:
            clipping = {
                "rmin": 1.0 / self.renorm_max_r,
                "rmax": self.renorm_max_r,
                "dmax": self.renorm_max_d
            }
            return tf.keras.layers.BatchNormalization(
                epsilon=1e-5,
                axis=1,
                fused=False,
                center=True,
                scale=scale,
                renorm=True,
                renorm_clipping=clipping,
                renorm_momentum=self.renorm_momentum,
                name=name)(input)
        else:
            return tf.keras.layers.BatchNormalization(
                epsilon=1e-5,
                axis=1,
                center=True,
                scale=scale,
                virtual_batch_size=self.virtual_batch_size,
                name=name)(input)

    def squeeze_excitation_v2(self, inputs, channels, name):
        assert channels % self.SE_ratio == 0

        pooled = tf.keras.layers.GlobalAveragePooling2D(
            data_format='channels_first')(inputs)
        squeezed = tf.keras.layers.Activation('relu')(tf.keras.layers.Dense(
            channels // self.SE_ratio,
            kernel_initializer='glorot_normal',
            kernel_regularizer=self.l2reg,
            name=name + '/se/dense1')(pooled))
        excited = tf.keras.layers.Dense(2 * channels,
                                        kernel_initializer='glorot_normal',
                                        kernel_regularizer=self.l2reg,
                                        name=name + '/se/dense2')(squeezed)
        return ApplySqueezeExcitation()([inputs, excited])

    def conv_block_v2(self,
                      inputs,
                      filter_size,
                      output_channels,
                      name,
                      bn_scale=False):
        conv = tf.keras.layers.Conv2D(output_channels,
                                      filter_size,
                                      use_bias=False,
                                      padding='same',
                                      kernel_initializer='glorot_normal',
                                      kernel_regularizer=self.l2reg,
                                      data_format='channels_first',
                                      name=name + '/conv2d')(inputs)
        return tf.keras.layers.Activation('relu')(self.batch_norm_v2(
            conv, name=name + '/bn', scale=bn_scale))

    def residual_block_v2(self, inputs, channels, name):
        conv1 = tf.keras.layers.Conv2D(channels,
                                       3,
                                       use_bias=False,
                                       padding='same',
                                       kernel_initializer='glorot_normal',
                                       kernel_regularizer=self.l2reg,
                                       data_format='channels_first',
                                       name=name + '/1/conv2d')(inputs)
        out1 = tf.keras.layers.Activation('relu')(self.batch_norm_v2(
            conv1, name + '/1/bn', scale=False))
        conv2 = tf.keras.layers.Conv2D(channels,
                                       3,
                                       use_bias=False,
                                       padding='same',
                                       kernel_initializer='glorot_normal',
                                       kernel_regularizer=self.l2reg,
                                       data_format='channels_first',
                                       name=name + '/2/conv2d')(out1)
        out2 = self.squeeze_excitation_v2(self.batch_norm_v2(conv2,
                                                             name + '/2/bn',
                                                             scale=True),
                                          channels,
                                          name=name + '/se')
        return tf.keras.layers.Activation('relu')(tf.keras.layers.add(
            [inputs, out2]))

    def construct_net_v2(self, inputs):
        flow = self.conv_block_v2(inputs,
                                  filter_size=3,
                                  output_channels=self.RESIDUAL_FILTERS,
                                  name='input',
                                  bn_scale=True)
        for i in range(self.RESIDUAL_BLOCKS):
            flow = self.residual_block_v2(flow,
                                          self.RESIDUAL_FILTERS,
                                          name='residual_{}'.format(i + 1))

        conv_pol = self.conv_block_v2(
                flow,
                filter_size=3,
                output_channels=self.RESIDUAL_FILTERS,
                name='policy1')
        conv_pol2 = tf.keras.layers.Conv2D(
            80,
            3,
            use_bias=True,
            padding='same',
            kernel_initializer='glorot_normal',
            kernel_regularizer=self.l2reg,
            bias_regularizer=self.l2reg,
            data_format='channels_first',
            name='policy')(conv_pol)
        h_fc1 = ApplyPolicyMap()(conv_pol2)

        # Value head
        conv_val = self.conv_block_v2(flow,
                                      filter_size=1,
                                      output_channels=32,
                                      name='value')
        h_conv_val_flat = tf.keras.layers.Flatten()(conv_val)
        h_fc2 = tf.keras.layers.Dense(128,
                                      kernel_initializer='glorot_normal',
                                      kernel_regularizer=self.l2reg,
                                      activation='relu',
                                      name='value/dense1')(h_conv_val_flat)
        if self.wdl:
            h_fc3 = tf.keras.layers.Dense(3,
                                          kernel_initializer='glorot_normal',
                                          kernel_regularizer=self.l2reg,
                                          bias_regularizer=self.l2reg,
                                          name='value/dense2')(h_fc2)
        else:
            h_fc3 = tf.keras.layers.Dense(1,
                                          kernel_initializer='glorot_normal',
                                          kernel_regularizer=self.l2reg,
                                          activation='tanh',
                                          name='value/dense2')(h_fc2)

        # Moves left head
        if self.moves_left:
            conv_mov = self.conv_block_v2(flow,
                                          filter_size=1,
                                          output_channels=8,
                                          name='moves_left')
            h_conv_mov_flat = tf.keras.layers.Flatten()(conv_mov)
            h_fc4 = tf.keras.layers.Dense(
                128,
                kernel_initializer='glorot_normal',
                kernel_regularizer=self.l2reg,
                activation='relu',
                name='moves_left/dense1')(h_conv_mov_flat)

            h_fc5 = tf.keras.layers.Dense(1,
                                          kernel_initializer='glorot_normal',
                                          kernel_regularizer=self.l2reg,
                                          activation='relu',
                                          name='moves_left/dense2')(h_fc4)
        else:
            h_fc5 = None

        return h_fc1, h_fc3, h_fc5
