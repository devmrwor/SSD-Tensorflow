# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Generic evaluation script that evaluates a SSD model
on a given dataset."""
import math
import six
import time

import tensorflow as tf
import tf_extended as tfe

from datasets import dataset_factory
from nets import nets_factory
from nets import ssd_common
from preprocessing import preprocessing_factory

import tf_utils

slim = tf.contrib.slim

# =========================================================================== #
# Some default EVAL parameters
# =========================================================================== #
# List of recalls values at which precision is evaluated.
LIST_RECALLS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.85, 0.87, 0.88, 0.89,
                0.90, 0.91, 0.92, 0.93, 0.94, 0.95, 0.96, 0.97, 0.98, 0.99]

# =========================================================================== #
# Evaluation flags.
# =========================================================================== #
tf.app.flags.DEFINE_integer(
    'num_classes', 21, 'Number of classes to use in the dataset.')
tf.app.flags.DEFINE_integer(
    'batch_size', 100, 'The number of samples in each batch.')
tf.app.flags.DEFINE_integer(
    'max_num_batches', None,
    'Max number of batches to evaluate by default use all.')
tf.app.flags.DEFINE_string(
    'master', '', 'The address of the TensorFlow master to use.')
tf.app.flags.DEFINE_string(
    'checkpoint_path', '/tmp/tfmodel/',
    'The directory where the model was written to or an absolute path to a '
    'checkpoint file.')
tf.app.flags.DEFINE_string(
    'eval_dir', '/tmp/tfmodel/', 'Directory where the results are saved to.')
tf.app.flags.DEFINE_integer(
    'num_preprocessing_threads', 4,
    'The number of threads used to create the batches.')
tf.app.flags.DEFINE_string(
    'dataset_name', 'imagenet', 'The name of the dataset to load.')
tf.app.flags.DEFINE_string(
    'dataset_split_name', 'test', 'The name of the train/test split.')
tf.app.flags.DEFINE_string(
    'dataset_dir', None, 'The directory where the dataset files are stored.')
tf.app.flags.DEFINE_integer(
    'labels_offset', 0,
    'An offset for the labels in the dataset. This flag is primarily used to '
    'evaluate the VGG and ResNet architectures which do not use a background '
    'class for the ImageNet dataset.')
tf.app.flags.DEFINE_string(
    'model_name', 'inception_v3', 'The name of the architecture to evaluate.')
tf.app.flags.DEFINE_string(
    'preprocessing_name', None, 'The name of the preprocessing to use. If left '
    'as `None`, then the model_name flag is used.')
tf.app.flags.DEFINE_float(
    'moving_average_decay', None,
    'The decay to use for the moving average.'
    'If left as None, then moving averages are not used.')
tf.app.flags.DEFINE_integer(
    'eval_image_size', None, 'Eval image size')

FLAGS = tf.app.flags.FLAGS


def main(_):
    if not FLAGS.dataset_dir:
        raise ValueError('You must supply the dataset directory with --dataset_dir')

    tf.logging.set_verbosity(tf.logging.INFO)
    with tf.Graph().as_default():
        tf_global_step = slim.get_or_create_global_step()

        # =================================================================== #
        # Dataset + SSD model + Pre-processing
        # =================================================================== #
        dataset = dataset_factory.get_dataset(
            FLAGS.dataset_name, FLAGS.dataset_split_name, FLAGS.dataset_dir)

        # Get the SSD network and its anchors.
        ssd_class = nets_factory.get_network(FLAGS.model_name)
        ssd_params = ssd_class.default_params._replace(num_classes=FLAGS.num_classes)
        ssd_net = ssd_class(ssd_params)

        # Evaluation shape and associated anchors.
        # eval_image_size
        ssd_shape = ssd_net.params.img_shape
        ssd_anchors = ssd_net.anchors(ssd_shape)

        # Select the preprocessing function.
        preprocessing_name = FLAGS.preprocessing_name or FLAGS.model_name
        image_preprocessing_fn = preprocessing_factory.get_preprocessing(
            preprocessing_name, is_training=False)

        # =================================================================== #
        # Create a dataset provider and batches.
        # =================================================================== #
        with tf.device('/cpu:0'):
            with tf.name_scope(FLAGS.dataset_name + '_data_provider'):
                provider = slim.dataset_data_provider.DatasetDataProvider(
                    dataset,
                    common_queue_capacity=2 * FLAGS.batch_size,
                    common_queue_min=FLAGS.batch_size,
                    shuffle=False)
            # Get for SSD network: image, labels, bboxes.
            [image, shape, glabels, gbboxes] = provider.get(['image', 'shape',
                                                             'object/label',
                                                             'object/bbox'])
            # Pre-processing image, labels and bboxes.
            image, glabels, gbboxes, gbbox_img = \
                image_preprocessing_fn(image, glabels, gbboxes, ssd_shape)
            # Encode groundtruth labels and bboxes.
            gclasses, glocalisations, gscores = \
                ssd_net.bboxes_encode(glabels, gbboxes, ssd_anchors)
            batch_shape = [1] * 4 + [len(ssd_anchors)] * 3

            # Evaluation batch.
            r = tf.train.batch(
                tf_utils.reshape_list([image, glabels, gbboxes, gbbox_img,
                                       gclasses, glocalisations, gscores]),
                batch_size=FLAGS.batch_size,
                num_threads=FLAGS.num_preprocessing_threads,
                capacity=5 * FLAGS.batch_size,
                dynamic_pad=True)
            (b_image, b_glabels, b_gbboxes, b_gbbox_img, b_gclasses,
             b_glocalisations, b_gscores) = tf_utils.reshape_list(r, batch_shape)

        # =================================================================== #
        # SSD Network + Ouputs decoding.
        # =================================================================== #
        dict_metrics = {}
        arg_scope = ssd_net.arg_scope()
        with slim.arg_scope(arg_scope):
            predictions, localisations, logits, end_points = \
                ssd_net.net(b_image, is_training=False)
        # Add losses functions.
        ssd_net.losses(logits, localisations,
                       b_gclasses, b_glocalisations, b_gscores)

        # Performing post-processing on CPU: loop-intensive, usually more efficient.
        with tf.device('/cpu:0'):
            # Detected objects from SSD output.
            localisations = ssd_net.bboxes_decode(localisations, ssd_anchors)
            rclasses, rscores, rbboxes = \
                ssd_net.detected_bboxes(predictions, localisations,
                                        select_threshold=None,
                                        nms_threshold=0.4,
                                        clipping_bbox=b_gbbox_img,
                                        top_k=400)

            # Compute TP and FP statistics.
            n_gbboxes, tp_tensor, fp_tensor = \
                tfe.bboxes_matching_batch(rclasses, rscores, rbboxes,
                                          b_glabels, b_gbboxes,
                                          matching_threshold=0.5)

        # Variables to restore: moving avg. or normal weights.
        if FLAGS.moving_average_decay:
            variable_averages = tf.train.ExponentialMovingAverage(
                FLAGS.moving_average_decay, tf_global_step)
            variables_to_restore = variable_averages.variables_to_restore(
                slim.get_model_variables())
            variables_to_restore[tf_global_step.op.name] = tf_global_step
        else:
            variables_to_restore = slim.get_variables_to_restore()

        # =================================================================== #
        # Evaluation metrics.
        # =================================================================== #
        with tf.device('/cpu:0'):
            dict_metrics = {}
            # First add all losses.
            for loss in tf.get_collection(tf.GraphKeys.LOSSES):
                dict_metrics[loss.op.name] = slim.metrics.streaming_mean(loss)
            # Extra losses as well.
            for loss in tf.get_collection('EXTRA_LOSSES'):
                dict_metrics[loss.op.name] = slim.metrics.streaming_mean(loss)

            # Add metrics to summaries and Print on screen.
            for name, metric in dict_metrics.items():
                # summary_name = 'eval/%s' % name
                summary_name = name
                op = tf.summary.scalar(summary_name, metric[0], collections=[])
                op = tf.Print(op, [metric[0]], summary_name)
                tf.add_to_collection(tf.GraphKeys.SUMMARIES, op)

            # Precision / recall arrays metrics.
            dict_metrics['precision_recall'] = \
                tfe.streaming_precision_recall_arrays(n_gbboxes, rclasses, rscores,
                                                      tp_tensor, fp_tensor)
        # Add to summaries precision/recall values.
        metric_val = dict_metrics['precision_recall'][0]
        l_precisions = tfe.precision_recall_values(LIST_RECALLS,
                                                   metric_val[0],
                                                   metric_val[1])
        for i, v in enumerate(l_precisions):
            summary_name = 'eval/precision_at_recall_%.2f' % LIST_RECALLS[i]
            op = tf.summary.scalar(summary_name, v, collections=[])
            op = tf.Print(op, [v], summary_name)
            tf.add_to_collection(tf.GraphKeys.SUMMARIES, op)
        # Compute Average Precision as well.
        ap = tfe.average_precision(metric_val[0], metric_val[1])
        summary_name = 'eval/average_precision'
        op = tf.summary.scalar(summary_name, ap, collections=[])
        op = tf.Print(op, [ap], summary_name)
        tf.add_to_collection(tf.GraphKeys.SUMMARIES, op)

        # Split into values and updates ops.
        names_to_values, names_to_updates = slim.metrics.aggregate_metric_map(dict_metrics)

        # =================================================================== #
        # Evaluation loop.
        # =================================================================== #
        gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=1.)
        config = tf.ConfigProto(log_device_placement=False,
                                gpu_options=gpu_options)
        # Number of batches...
        if FLAGS.max_num_batches:
            num_batches = FLAGS.max_num_batches
        else:
            num_batches = math.ceil(dataset.num_samples / float(FLAGS.batch_size))

        if tf.gfile.IsDirectory(FLAGS.checkpoint_path):
            checkpoint_path = tf.train.latest_checkpoint(FLAGS.checkpoint_path)
        else:
            checkpoint_path = FLAGS.checkpoint_path

        start = time.clock()
        tf.logging.info('Evaluating %s' % checkpoint_path)
        slim.evaluation.evaluate_once(
            master=FLAGS.master,
            checkpoint_path=checkpoint_path,
            logdir=FLAGS.eval_dir,
            num_evals=num_batches,
            eval_op=list(names_to_updates.values()),
            variables_to_restore=variables_to_restore,
            session_config=config)
        # Log time spent.
        elapsed = time.clock()
        elapsed = elapsed - start
        print('Time spent per BATCH: %.3f seconds.' % (elapsed / num_batches))


if __name__ == '__main__':
    tf.app.run()
