# Copyright 2017 Paul Balanca. All Rights Reserved.
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
# ==============================================================================
"""TF Extended: additional metrics.
"""
import tensorflow as tf

from tensorflow.contrib.framework.python.ops import variables as contrib_variables
from tensorflow.contrib.metrics.python.ops import set_ops
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.framework import sparse_tensor
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import check_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import nn
from tensorflow.python.ops import state_ops
from tensorflow.python.ops import variable_scope
from tensorflow.python.ops import variables

import tf_extended as tfe

# =========================================================================== #
# TensorFlow utils
# =========================================================================== #
def _create_local(name, shape, collections=None, validate_shape=True,
                  dtype=dtypes.float32):
    """Creates a new local variable.
    Args:
        name: The name of the new or existing variable.
        shape: Shape of the new or existing variable.
        collections: A list of collection names to which the Variable will be added.
        validate_shape: Whether to validate the shape of the variable.
        dtype: Data type of the variables.
    Returns:
        The created variable.
    """
    # Make sure local variables are added to tf.GraphKeys.LOCAL_VARIABLES
    collections = list(collections or [])
    collections += [ops.GraphKeys.LOCAL_VARIABLES]
    return variables.Variable(
            initial_value=array_ops.zeros(shape, dtype=dtype),
            name=name,
            trainable=False,
            collections=collections,
            validate_shape=validate_shape)


def _safe_div(numerator, denominator, name):
    """Divides two values, returning 0 if the denominator is <= 0.
    Args:
      numerator: A real `Tensor`.
      denominator: A real `Tensor`, with dtype matching `numerator`.
      name: Name for the returned op.
    Returns:
      0 if `denominator` <= 0, else `numerator` / `denominator`
    """
    return tf.where(
        math_ops.greater(denominator, 0),
        math_ops.divide(numerator, denominator),
        tf.zeros_like(numerator),
        name=name)


def _broadcast_weights(weights, values):
    """Broadcast `weights` to the same shape as `values`.
    This returns a version of `weights` following the same broadcast rules as
    `mul(weights, values)`. When computing a weighted average, use this function
    to broadcast `weights` before summing them; e.g.,
    `reduce_sum(w * v) / reduce_sum(_broadcast_weights(w, v))`.
    Args:
      weights: `Tensor` whose shape is broadcastable to `values`.
      values: `Tensor` of any shape.
    Returns:
      `weights` broadcast to `values` shape.
    """
    weights_shape = weights.get_shape()
    values_shape = values.get_shape()
    if(weights_shape.is_fully_defined() and
       values_shape.is_fully_defined() and
       weights_shape.is_compatible_with(values_shape)):
        return weights
    return math_ops.mul(
        weights, array_ops.ones_like(values), name='broadcast_weights')


# =========================================================================== #
# TF Extended metrics
# =========================================================================== #
def _precision_recall(n_gbboxes, scores, tp, fp, scope=None):
    """Compute precision and recall from scores, true positives and false
    positives booleans arrays
    """
    # Sort by score.
    with tf.name_scope(scope, 'prec_rec'):
        scores, idxes = tf.nn.top_k(scores, k=tf.size(scores), sorted=True)
        tp = tf.gather(tp, idxes)
        fp = tf.gather(fp, idxes)
        # Computer recall and precision.
        tp = tf.cumsum(tp, axis=0)
        fp = tf.cumsum(fp, axis=0)
        recall = _safe_div(tp, tf.cast(n_gbboxes, tp.dtype), 'recall')
        precision = _safe_div(tp, tp + fp, 'precision')
        return tf.tuple([precision, recall])


def streaming_precision_recall_arrays(n_gbboxes, rclasses, rscores,
                                      tp_tensor, fp_tensor,
                                      remove_zero_labels=True,
                                      metrics_collections=None,
                                      updates_collections=None,
                                      name=None):
    """Streaming computation of precision / recall arrays. This metrics
    keeps tracks of boolean True positives and False positives arrays.
    """
    with variable_scope.variable_scope(name, 'stream_precision_recall',
                                       [n_gbboxes, rclasses, tp_tensor, fp_tensor]):
        n_gbboxes = math_ops.to_int64(n_gbboxes)
        rclasses = math_ops.to_int64(rclasses)
        rscores = math_ops.to_float(rscores)
        tp_tensor = math_ops.to_float(tp_tensor)
        fp_tensor = math_ops.to_float(fp_tensor)
        # Reshape TP and FP tensors and clean away 0 classes.
        rclasses = tf.reshape(rclasses, [-1])
        rscores = tf.reshape(rscores, [-1])
        tp_tensor = tf.reshape(tp_tensor, [-1])
        fp_tensor = tf.reshape(tp_tensor, [-1])
        if remove_zero_labels:
            mask = tf.greater(rclasses, 0)
            rclasses = tf.boolean_mask(rclasses, mask)
            rscores = tf.boolean_mask(rscores, mask)
            tp_tensor = tf.boolean_mask(tp_tensor, mask)
            fp_tensor = tf.boolean_mask(fp_tensor, mask)

        # Local variables accumlating information over batches.
        v_nobjects = _create_local('v_tp', shape=[], dtype=tf.int64)
        v_scores = _create_local('v_scores', shape=[0, ])
        v_tp = _create_local('v_tp', shape=[0, ])
        v_fp = _create_local('v_fp', shape=[0, ])

        # Update operations.
        nobjects_op = state_ops.assign_add(v_nobjects, tf.reduce_sum(n_gbboxes))
        scores_op = state_ops.assign(v_scores,
                                     tf.concat([v_scores, rscores], axis=0),
                                     validate_shape=False)
        tp_op = state_ops.assign(v_tp, tf.concat([v_tp, tp_tensor], axis=0),
                                 validate_shape=False)
        fp_op = state_ops.assign(v_fp, tf.concat([v_fp, fp_tensor], axis=0),
                                 validate_shape=False)
        fp_op = tf.Print(fp_op, [tf.shape(fp_op)], 'test')

        # Precision and recall computations.
        r = _precision_recall(v_nobjects, v_scores, v_tp, v_fp, 'value')
        with ops.control_dependencies([nobjects_op, scores_op, tp_op, fp_op]):
            update_op = _precision_recall(nobjects_op, scores_op, tp_op, fp_op,
                                          'update_op')

        if metrics_collections:
            ops.add_to_collections(metrics_collections, r)
        if updates_collections:
            ops.add_to_collections(updates_collections, update_op)
        return r, update_op


def average_precision(precision, recall):
    """Compute a average precision from precision and recall Tensors.
    Implementation inspired by the Pascal 2012 devkit.
    """
    # Add bounds values to precision and recall.
    precision = tf.concat([[0.], precision, [0.]], axis=0)
    recall = tf.concat([[0.], recall, [1.]], axis=0)
    # Ensures precision is increasing in reverse order.
    precision = tfe.cummax(precision, reverse=True)

    # Riemann sum for estimating the integral.
    mean_pre = (precision[1:] + precision[:-1]) / 2.
    diff_rec = recall[1:] - recall[:-1]
    ap = tf.reduce_sum(mean_pre * diff_rec)
    return ap


def precision_recall_values(xvals, precision, recall, name=None):
    """Compute values on the precision/recall curve.

    Args:
      x: Python list of floats;
      precision: 1D Tensor decreasing.
      recall: 1D Tensor increasing.
    Return:
      list of precision values.
    """
    with ops.name_scope(name, "precision_recall_values",
                        [precision, recall]) as name:
        prec_values = []
        dtype = precision.dtype
        for x in xvals:
            mask = recall <= x
            prec_values.append(tf.reduce_min(precision * tf.cast(mask, dtype)))
        return prec_values

