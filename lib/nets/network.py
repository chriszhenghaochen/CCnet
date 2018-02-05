# --------------------------------------------------------
# Tensorflow Faster R-CNN
# Licensed under The MIT License [see LICENSE for details]
# Written by Xinlei Chen
# --------------------------------------------------------
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import tensorflow.contrib.slim as slim
from tensorflow.contrib.slim import losses
from tensorflow.contrib.slim import arg_scope

import numpy as np

from layer_utils.snippets import generate_anchors_pre
from layer_utils.proposal_layer import proposal_layer
from layer_utils.proposal_top_layer import proposal_top_layer
from layer_utils.anchor_target_layer import anchor_target_layer
from layer_utils.proposal_target_layer import proposal_target_layer

from model.config import cfg
from layer_utils.focal_loss import SigmoidFocalClassificationLoss as fl

#repeat
repeat = cfg.TRAIN.REPEAT

rpn_batch3 = cfg.TRAIN.RPN_BATCH3
rpn_batch2 = cfg.TRAIN.RPN_BATCH2
rpn_batch1 = cfg.TRAIN.RPN_BATCH1
rpn_batch = cfg.TRAIN.RPN_BATCH

batch3 = cfg.TRAIN.BATCH3
batch2 = cfg.TRAIN.BATCH2
batch1 = cfg.TRAIN.BATCH1
batch = cfg.TRAIN.BATCH

    
class Network(object):
  def __init__(self, batch_size=1):
    self._feat_stride = [16, ]
    self._feat_compress = [1. / 16., ]
    self._batch_size = batch_size
    self._predictions = {}
    self._losses = {}
    self._anchor_targets = {}
    self._proposal_targets = {}
    self._layers = {}
    self._act_summaries = []
    self._score_summaries = {}
    self._train_summaries = []
    self._event_summaries = {}
    self._variables_to_fix = {}
    self.fl = fl(alpha=0.5)

    #debug
    self.debug = {}

  #Focal Loss
  def focal_loss(self, prediction_tensor, target_tensor, weights=None, alpha=0.25, gamma=2):
      """Compute focal loss for predictions.
                Multi-labels Focal loss formula:
                    FL = -alpha * (z-p)^gamma * log(p) -(1-alpha) * p^gamma * log(1-p)
                         ,which alpha = 0.25, gamma = 2, p = sigmoid(x), z = target_tensor.
            Args:
             prediction_tensor: A float tensor of shape [batch_size, num_anchors,
                num_classes] representing the predicted logits for each class
             target_tensor: A float tensor of shape [batch_size, num_anchors,
                num_classes] representing one-hot encoded classification targets
             weights: A float tensor of shape [batch_size, num_anchors]
             alpha: A scalar tensor for focal loss alpha hyper-parameter
             gamma: A scalar tensor for focal loss gamma hyper-parameter
            Returns:
                loss: A (scalar) tensor representing the value of the loss function
      """
      sigmoid_p = tf.nn.sigmoid(prediction_tensor)
      zeros = array_ops.zeros_like(sigmoid_p, dtype=sigmoid_p.dtype)
      pos_p_sub = array_ops.where(target_tensor >= sigmoid_p, target_tensor - sigmoid_p, zeros)
      neg_p_sub = array_ops.where(target_tensor > zeros, zeros, sigmoid_p)
      per_entry_cross_ent = - alpha * (pos_p_sub ** gamma) * tf.log(tf.clip_by_value(sigmoid_p, 1e-8, 1.0)) \
                              - (1 - alpha) * (neg_p_sub ** gamma) * tf.log(tf.clip_by_value(1.0 - sigmoid_p, 1e-8, 1.0))
      return tf.reduce_mean(per_entry_cross_ent)  

  #resize feature map
  def _resize_map(self, input_map, size_map, name):
    up_h = tf.shape(size_map)[1]
    up_w = tf.shape(size_map)[2]

    return tf.image.resize_bilinear(input_map, [up_h, up_w], name=name)



  def _add_image_summary(self, image, boxes):
    # add back mean
    image += cfg.PIXEL_MEANS
    # bgr to rgb (opencv uses bgr)
    channels = tf.unstack (image, axis=-1)
    image    = tf.stack ([channels[2], channels[1], channels[0]], axis=-1)
    # dims for normalization
    width  = tf.to_float(tf.shape(image)[2])
    height = tf.to_float(tf.shape(image)[1])
    # from [x1, y1, x2, y2, cls] to normalized [y1, x1, y1, x1]
    cols = tf.unstack(boxes, axis=1)
    boxes = tf.stack([cols[1] / height,
                      cols[0] / width,
                      cols[3] / height,
                      cols[2] / width], axis=1)
    # add batch dimension (assume batch_size==1)
    assert image.get_shape()[0] == 1
    boxes = tf.expand_dims(boxes, dim=0)
    image = tf.image.draw_bounding_boxes(image, boxes)
    
    return tf.summary.image('ground_truth', image)

  def _add_act_summary(self, tensor):
    tf.summary.histogram('ACT/' + tensor.op.name + '/activations', tensor)
    tf.summary.scalar('ACT/' + tensor.op.name + '/zero_fraction',
                      tf.nn.zero_fraction(tensor))

  def _add_score_summary(self, key, tensor):
    tf.summary.histogram('SCORE/' + tensor.op.name + '/' + key + '/scores', tensor)

  def _add_train_summary(self, var):
    tf.summary.histogram('TRAIN/' + var.op.name, var)

  def _reshape_layer(self, bottom, num_dim, name):
    input_shape = tf.shape(bottom)
    with tf.variable_scope(name) as scope:
      # change the channel to the caffe format
      to_caffe = tf.transpose(bottom, [0, 3, 1, 2])
      # then force it to have channel 2
      reshaped = tf.reshape(to_caffe,
                            tf.concat(axis=0, values=[[self._batch_size], [num_dim, -1], [input_shape[2]]]))
      # then swap the channel back
      to_tf = tf.transpose(reshaped, [0, 2, 3, 1])
      return to_tf

  # add 2 scores from rpn
  def _score_add_up(self, score1, score2, factor1, factor2, name):
    score = tf.add(x = score1*factor1, y = score2*factor2, name = name)
    return score



  def _softmax_layer(self, bottom, name):
    if name == 'rpn_cls_prob_reshape' or name == 'rpn1_cls_prob_reshape' or name == 'rpn2s_cls_prob_reshape' or name == 'rpn3_cls_prob_reshape':
      input_shape = tf.shape(bottom)
      bottom_reshaped = tf.reshape(bottom, [-1, input_shape[-1]])
      reshaped_score = tf.nn.softmax(bottom_reshaped, name=name)
      return tf.reshape(reshaped_score, input_shape)
    return tf.nn.softmax(bottom, name=name)

  def _proposal_top_layer(self, rpn_cls_prob, rpn_bbox_pred, name, reject1, reject2):
    with tf.variable_scope(name) as scope:
      rois, rpn_scores = tf.py_func(proposal_top_layer,
                                    [rpn_cls_prob, rpn_bbox_pred, self._im_info,
                                     self._feat_stride, self._anchors, self._num_anchors, reject1, reject2],
                                    [tf.float32, tf.float32], name="proposal_top")
      rois.set_shape([cfg.TEST.RPN_TOP_N, 5])
      rpn_scores.set_shape([cfg.TEST.RPN_TOP_N, 1])

    return rois, rpn_scores

  def _proposal_layer(self, rpn_cls_prob, rpn_bbox_pred, name, reject1, reject2):
    with tf.variable_scope(name) as scope:
      rois, rpn_scores = tf.py_func(proposal_layer,
                                    [rpn_cls_prob, rpn_bbox_pred, self._im_info, self._mode,
                                     self._feat_stride, self._anchors, self._num_anchors, reject1, reject2],
                                    [tf.float32, tf.float32], name="proposal")
      rois.set_shape([None, 5])
      rpn_scores.set_shape([None, 1])

    return rois, rpn_scores

  # Only use it if you have roi_pooling op written in tf.image

  def _roi_pool_layer(self, bootom, rois, name):
    with tf.variable_scope(name) as scope:
      return tf.image.roi_pooling(bootom, rois,
                                  pooled_height=cfg.POOLING_SIZE,
                                  pooled_width=cfg.POOLING_SIZE,
                                  spatial_scale=1. / 16.)[0]

  def _crop_pool_layer(self, bottom, rois, name):
    with tf.variable_scope(name) as scope:
      batch_ids = tf.squeeze(tf.slice(rois, [0, 0], [-1, 1], name="batch_id"), [1])

      self._predictions['batch_ids'] = batch_ids

      # Get the normalized coordinates of bboxes
      bottom_shape = tf.shape(bottom)
      height = (tf.to_float(bottom_shape[1]) - 1.) * np.float32(self._feat_stride[0])
      width = (tf.to_float(bottom_shape[2]) - 1.) * np.float32(self._feat_stride[0])
      x1 = tf.slice(rois, [0, 1], [-1, 1], name="x1") / width
      y1 = tf.slice(rois, [0, 2], [-1, 1], name="y1") / height
      x2 = tf.slice(rois, [0, 3], [-1, 1], name="x2") / width
      y2 = tf.slice(rois, [0, 4], [-1, 1], name="y2") / height
      # Won't be backpropagated to rois anyway, but to save time
      bboxes = tf.stop_gradient(tf.concat([y1, x1, y2, x2], axis=1))
      pre_pool_size = cfg.POOLING_SIZE * 2
      crops = tf.image.crop_and_resize(bottom, bboxes, tf.to_int32(batch_ids), [pre_pool_size, pre_pool_size], name=name + "crops")

    return slim.max_pool2d(crops, [2, 2], padding='SAME')

  def _dropout_layer(self, bottom, name, ratio=0.5):
    return tf.nn.dropout(bottom, ratio, name=name)

  def _anchor_target_layer(self, rpn_cls_score, name, reject_inds_1, reject_inds_2, batch, OHEM):
    with tf.variable_scope(name) as scope:
      rpn_labels, rpn_bbox_targets, rpn_bbox_inside_weights, rpn_bbox_outside_weights = tf.py_func(
        anchor_target_layer,
        [rpn_cls_score, self._gt_boxes, self._im_info, self._feat_stride, self._anchors, self._num_anchors, reject_inds_1, reject_inds_2, batch, OHEM],
        [tf.float32, tf.float32, tf.float32, tf.float32])

      rpn_labels.set_shape([1, 1, None, None])
      rpn_bbox_targets.set_shape([1, None, None, self._num_anchors * 4])
      rpn_bbox_inside_weights.set_shape([1, None, None, self._num_anchors * 4])
      rpn_bbox_outside_weights.set_shape([1, None, None, self._num_anchors * 4])

      rpn_labels = tf.to_int32(rpn_labels, name="to_int32")

      self._anchor_targets[name + '_rpn_labels'] = rpn_labels
      self._anchor_targets[name + '_rpn_bbox_targets'] = rpn_bbox_targets
      self._anchor_targets[name + '_rpn_bbox_inside_weights'] = rpn_bbox_inside_weights
      self._anchor_targets[name + '_rpn_bbox_outside_weights'] = rpn_bbox_outside_weights

      self._score_summaries.update(self._anchor_targets)

    return rpn_labels

  def _proposal_target_layer(self, rois, roi_scores, name, batch):
    with tf.variable_scope(name) as scope:
      rois, roi_scores, labels, bbox_targets, bbox_inside_weights, bbox_outside_weights, keep_inds = tf.py_func(
        proposal_target_layer,
        [rois, roi_scores, self._gt_boxes, self._num_classes, batch],
        [tf.float32, tf.float32, tf.float32, tf.float32, tf.float32, tf.float32, tf.int64],
        name="proposal_target")

      rois.set_shape([batch, 5])
      roi_scores.set_shape([batch])
      labels.set_shape([batch, 1])
      bbox_targets.set_shape([batch, self._num_classes * 4])
      bbox_inside_weights.set_shape([batch, self._num_classes * 4])
      bbox_outside_weights.set_shape([batch, self._num_classes * 4])

      keep_inds = tf.cast(tf.reshape(keep_inds, [-1]), tf.int32)

      self._proposal_targets[name + '_rois'] = rois
      self._proposal_targets[name + '_labels'] = tf.to_int32(labels, name="to_int32")
      self._proposal_targets[name + '_bbox_targets'] = bbox_targets
      self._proposal_targets[name + '_bbox_inside_weights'] = bbox_inside_weights
      self._proposal_targets[name + '_bbox_outside_weights'] = bbox_outside_weights

      self._score_summaries.update(self._proposal_targets)

      return rois, roi_scores, keep_inds

  def _anchor_component(self):
    with tf.variable_scope('ANCHOR_' + self._tag) as scope:
      # just to get the shape right
      height = tf.to_int32(tf.ceil(self._im_info[0, 0] / np.float32(self._feat_stride[0])))
      width = tf.to_int32(tf.ceil(self._im_info[0, 1] / np.float32(self._feat_stride[0])))
      anchors, anchor_length = tf.py_func(generate_anchors_pre,
                                          [height, width,
                                           self._feat_stride, self._anchor_scales, self._anchor_ratios],
                                          [tf.float32, tf.int32], name="generate_anchors")
      anchors.set_shape([None, 4])
      anchor_length.set_shape([])
      self._anchors = anchors
      self._anchor_length = anchor_length

  def build_network(self, sess, is_training=True):
    raise NotImplementedError

  def _smooth_l1_loss(self, bbox_pred, bbox_targets, bbox_inside_weights, bbox_outside_weights, sigma=1.0, dim=[1]):
    sigma_2 = sigma ** 2
    box_diff = bbox_pred - bbox_targets
    in_box_diff = bbox_inside_weights * box_diff
    abs_in_box_diff = tf.abs(in_box_diff)
    smoothL1_sign = tf.stop_gradient(tf.to_float(tf.less(abs_in_box_diff, 1. / sigma_2)))
    in_loss_box = tf.pow(in_box_diff, 2) * (sigma_2 / 2.) * smoothL1_sign \
                  + (abs_in_box_diff - (0.5 / sigma_2)) * (1. - smoothL1_sign)
    out_loss_box = bbox_outside_weights * in_loss_box
    loss_box = tf.reduce_mean(tf.reduce_sum(
      out_loss_box,
      axis=dim
    ))
    return loss_box


   ###this function is for repeating
  def repeat(self, score, label, batch, cls = False):

    # print('before ', score)
    # print('bfore', label)

    pos_len = tf.cast(tf.count_nonzero(label), tf.int32)
    neg_len = tf.cast(batch, tf.int32) - pos_len
    select = tf.where(tf.not_equal(label, 0))
    neg_select = tf.where(tf.equal(label, 0))

    if cls == False:
      size, fraction = 2, cfg.TRAIN.RPN_FG_FRACTION
    else:
      size, fraction = self._num_classes, cfg.TRAIN.FG_FRACTION

    pos_score = tf.reshape(tf.gather(score, select), [-1, size])
    pos_label = tf.reshape(tf.gather(label, select), [-1])

    neg_score = tf.reshape(tf.gather(score, neg_select), [-1, size])
    neg_label = tf.reshape(tf.gather(label, neg_select), [-1])

    #process negtive score first
    bg_num = tf.cast(batch - batch*fraction, tf.int32)

    neg_score = tf.cond(tf.less(bg_num, neg_len), lambda: neg_score[:bg_num], lambda: neg_score)
    neg_label = tf.cond(tf.less(bg_num, neg_len), lambda: neg_label[:bg_num], lambda: neg_label)


    fg_num = tf.cast(batch*fraction, tf.int32)

    mul = tf.cast(tf.cond(tf.equal(pos_len, 0), lambda: tf.constant(0, tf.float64), lambda: (fg_num/pos_len)), tf.int32)
    reminder = tf.cast(tf.cond(tf.equal(pos_len, 0), lambda: tf.constant(0, tf.int32), lambda: fg_num%pos_len), tf.int32)


    #process postive score
    score_tile = tf.tile(pos_score, tf.convert_to_tensor([mul, 1],tf.int32))
    label_tile = tf.tile(pos_label, tf.convert_to_tensor([mul],tf.int32))

    score_reminder = pos_score[:reminder]
    label_reminder = pos_label[:reminder]


    score = tf.concat([neg_score, score_tile, score_reminder],0)
    label = tf.concat([neg_label, label_tile, label_reminder],0)

    return score, label

  #DEBUG SESSION
  def DEBUG(self, sess, blobs):
    feed_dict = {self._image: blobs['data'], self._im_info: blobs['im_info'],
                 self._gt_boxes: blobs['gt_boxes']}


    b1,b2,b3,b4 = sess.run([self._predictions['cls3_score'],
                    self._predictions['size'],
                    self._predictions['factor'],
                    self._predictions['add']],
                      feed_dict=feed_dict)

    return b1,b2,b3,b4


  def _add_losses(self, sigma_rpn=3.0):
    with tf.variable_scope('loss_' + self._tag) as scope:
      

      #################################################RPN###################################################
      #------------------------------------rpn3------------------------------------#
      rpn3_cls_score = tf.reshape(self._predictions['rpn3_cls_score_reshape'], [-1, 2])

      rpn3_label = tf.reshape(self._anchor_targets['anchor3_rpn_labels'], [-1])

      rpn3_select = tf.where(tf.not_equal(rpn3_label, -1))
      rpn3_cls_score = tf.reshape(tf.gather(rpn3_cls_score, rpn3_select), [-1, 2])
      rpn3_label = tf.reshape(tf.gather(rpn3_label, rpn3_select), [-1])

      #repeat   
      if repeat:
        rpn3_cls_score, rpn3_label = self.repeat(rpn3_cls_score, rpn3_label, rpn_batch3) 

      #initialize rpn cls loss
      rpn3_cross_entropy = None

      if cfg.TRAIN.FOCAL_LOSS3 == True:
        #use Focal Loss
        length = tf.size(rpn3_label)
        rpn3_label = tf.one_hot(indices = rpn3_label, depth=2, on_value=1, off_value=0, axis=-1)
        rpn3_label = tf.cast(rpn3_label, tf.float32)
        rpn3_weights = tf.ones([1, length], tf.float32)
        rpn3_cross_entropy = self.fl.compute_loss(prediction_tensor = rpn3_cls_score, target_tensor = rpn3_label, weights = rpn3_weights)
      else:
        #use Original Loss
        rpn3_cross_entropy = tf.reduce_mean(
          tf.nn.sparse_softmax_cross_entropy_with_logits(logits=rpn3_cls_score, labels=rpn3_label))


      #------------------------------------rpn2------------------------------------#
      rpn2_cls_score = tf.reshape(self._predictions['rpn2_cls_score_reshape'], [-1, 2])

      rpn2_label = tf.reshape(self._anchor_targets['anchor2_rpn_labels'], [-1])

      rpn2_select = tf.where(tf.not_equal(rpn2_label, -1))
      rpn2_cls_score = tf.reshape(tf.gather(rpn2_cls_score, rpn2_select), [-1, 2])
      rpn2_label = tf.reshape(tf.gather(rpn2_label, rpn2_select), [-1])

      # rpn reject here using label -2
      rpn2_select = tf.where(tf.not_equal(rpn2_label, -2))
      rpn2_cls_score = tf.reshape(tf.gather(rpn2_cls_score, rpn2_select), [-1, 2])
      rpn2_label = tf.reshape(tf.gather(rpn2_label, rpn2_select), [-1])
      
      #repeat   
      if repeat:
        rpn2_cls_score, rpn2_label = self.repeat(rpn2_cls_score, rpn2_label, rpn_batch2) 

      #initialize rpn cls loss
      rpn2_cross_entropy = None

      if cfg.TRAIN.FOCAL_LOSS2 == True:
        #use Focal Loss
        length = tf.size(rpn2_label)
        rpn2_label = tf.one_hot(indices = rpn2_label, depth=2, on_value=1, off_value=0, axis=-1)
        rpn2_label = tf.cast(rpn2_label, tf.float32)
        rpn2_weights = tf.ones([1, length], tf.float32)
        rpn2_cross_entropy = self.fl.compute_loss(prediction_tensor = rpn2_cls_score, target_tensor = rpn2_label, weights = rpn2_weights)
      else:
        #use Original Loss
        rpn2_cross_entropy = tf.reduce_mean(
          tf.nn.sparse_softmax_cross_entropy_with_logits(logits=rpn2_cls_score, labels=rpn2_label))


      #------------------------------------rpn1------------------------------------#
      rpn1_cls_score = tf.reshape(self._predictions['rpn1_cls_score_reshape'], [-1, 2])

      rpn1_label = tf.reshape(self._anchor_targets['anchor1_rpn_labels'], [-1])

      rpn1_select = tf.where(tf.not_equal(rpn1_label, -1))
      rpn1_cls_score = tf.reshape(tf.gather(rpn1_cls_score, rpn1_select), [-1, 2])
      rpn1_label = tf.reshape(tf.gather(rpn1_label, rpn1_select), [-1])

      # rpn reject here using label -2
      rpn1_select = tf.where(tf.not_equal(rpn1_label, -2))
      rpn1_cls_score = tf.reshape(tf.gather(rpn1_cls_score, rpn1_select), [-1, 2])
      rpn1_label = tf.reshape(tf.gather(rpn1_label, rpn1_select), [-1])
      
      #repeat   
      if repeat:
        rpn1_cls_score, rpn1_label = self.repeat(rpn1_cls_score, rpn1_label, rpn_batch1) 

      #initialize rpn cls loss
      rpn1_cross_entropy = None

      if cfg.TRAIN.FOCAL_LOSS1 == True:
        #use Focal Loss
        length = tf.size(rpn1_label)
        rpn1_label = tf.one_hot(indices = rpn1_label, depth=2, on_value=1, off_value=0, axis=-1)
        rpn1_label = tf.cast(rpn1_label, tf.float32)
        rpn1_weights = tf.ones([1, length], tf.float32)
        rpn1_cross_entropy = self.fl.compute_loss(prediction_tensor = rpn1_cls_score, target_tensor = rpn1_label, weights = rpn1_weights)
      else:
        #use Original Loss
        rpn1_cross_entropy = tf.reduce_mean(
          tf.nn.sparse_softmax_cross_entropy_with_logits(logits=rpn1_cls_score, labels=rpn1_label))

      
      #------------------------------------rpn0------------------------------------#
      rpn0_cls_score = tf.reshape(self._predictions['rpn0_cls_score_reshape'], [-1, 2])

      rpn0_label = tf.reshape(self._anchor_targets['anchor_rpn_labels'], [-1])

      rpn0_select = tf.where(tf.not_equal(rpn0_label, -1))
      rpn0_cls_score = tf.reshape(tf.gather(rpn0_cls_score, rpn0_select), [-1, 2])
      rpn0_label = tf.reshape(tf.gather(rpn0_label, rpn0_select), [-1])

      # rpn reject here using label -2
      rpn0_select = tf.where(tf.not_equal(rpn0_label, -2))
      rpn0_cls_score = tf.reshape(tf.gather(rpn0_cls_score, rpn0_select), [-1, 2])
      rpn0_label = tf.reshape(tf.gather(rpn0_label, rpn0_select), [-1])

      
      #repeat   
      if repeat:
        rpn0_cls_score, rpn0_label = self.repeat(rpn0_cls_score, rpn0_label, rpn_batch) 

      #initialize rpn cls loss
      rpn0_cross_entropy = None

      if cfg.TRAIN.FOCAL_LOSS == True:
        #use Focal Loss
        length = tf.size(rpn_label)
        rpn0_label = tf.one_hot(indices = rpn0_label, depth=2, on_value=1, off_value=0, axis=-1)
        rpn0_label = tf.cast(rpn0_label, tf.float32)
        rpn0_weights = tf.ones([1, length], tf.float32)
        rpn0_cross_entropy = self.fl.compute_loss(prediction_tensor = rpn0_cls_score, target_tensor = rpn0_label, weights = rpn0_weights)
      else:
        #use Original Loss
        rpn0_cross_entropy = tf.reduce_mean(
          tf.nn.sparse_softmax_cross_entropy_with_logits(logits=rpn0_cls_score, labels=rpn0_label))
      
      #------------------------------------rpn------------------------------------#
      rpn_cls_score = tf.reshape(self._predictions['rpn_cls_score_reshape'], [-1, 2])

      rpn_label = tf.reshape(self._anchor_targets['anchor_rpn_labels'], [-1])

      rpn_select = tf.where(tf.not_equal(rpn_label, -1))
      rpn_cls_score = tf.reshape(tf.gather(rpn_cls_score, rpn_select), [-1, 2])
      rpn_label = tf.reshape(tf.gather(rpn_label, rpn_select), [-1])

      # rpn reject here using label -2
      rpn_select = tf.where(tf.not_equal(rpn_label, -2))
      rpn_cls_score = tf.reshape(tf.gather(rpn_cls_score, rpn_select), [-1, 2])
      rpn_label = tf.reshape(tf.gather(rpn_label, rpn_select), [-1])

      
      #repeat   
      if repeat:
        rpn_cls_score, rpn_label = self.repeat(rpn_cls_score, rpn_label, rpn_batch) 

      #initialize rpn cls loss
      rpn_cross_entropy = None

      if cfg.TRAIN.FOCAL_LOSS == True:
        #use Focal Loss
        length = tf.size(rpn_label)
        rpn_label = tf.one_hot(indices = rpn_label, depth=2, on_value=1, off_value=0, axis=-1)
        rpn_label = tf.cast(rpn_label, tf.float32)
        rpn_weights = tf.ones([1, length], tf.float32)
        rpn_cross_entropy = self.fl.compute_loss(prediction_tensor = rpn_cls_score, target_tensor = rpn_label, weights = rpn_weights)
      else:
        #use Original Loss
        rpn_cross_entropy = tf.reduce_mean(
          tf.nn.sparse_softmax_cross_entropy_with_logits(logits=rpn_cls_score, labels=rpn_label))
      


      # RPN, bbox loss
      rpn_bbox_pred = self._predictions['rpn_bbox_pred']
      rpn_bbox_targets = self._anchor_targets['anchor_rpn_bbox_targets']
      rpn_bbox_inside_weights = self._anchor_targets['anchor_rpn_bbox_inside_weights']
      rpn_bbox_outside_weights = self._anchor_targets['anchor_rpn_bbox_outside_weights']

      rpn_loss_box = self._smooth_l1_loss(rpn_bbox_pred, rpn_bbox_targets, rpn_bbox_inside_weights,
                                          rpn_bbox_outside_weights, sigma=sigma_rpn, dim=[1, 2, 3])


      ########################################################################################################


      #################################################RCNN####################################################

      #-----------------------------RCNN3, class loss-----------------------------------#
      cls3_score = self._predictions["cls3_score"]
      label3 = tf.reshape(self._proposal_targets["rpn3_rois_labels"], [-1])

      if repeat:
        cls3_score, label3 = self.repeat(cls3_score, label3, batch3, True)

      cross_entropy3 = tf.reduce_mean(
        tf.nn.sparse_softmax_cross_entropy_with_logits(
          logits=tf.reshape(cls3_score, [-1, self._num_classes]), labels=label3))


      #-----------------------------RCNN2, class loss-----------------------------------#
      cls2_score = self._predictions["cls2_score"]
      label2 = tf.reshape(self._proposal_targets["rpn2_rois_labels"], [-1])

      if repeat:
        cls2_score, label2 = self.repeat(cls2_score, label2, batch2, True)

      cross_entropy2 = tf.reduce_mean(
        tf.nn.sparse_softmax_cross_entropy_with_logits(
          logits=tf.reshape(cls2_score, [-1, self._num_classes]), labels=label2))


      #-----------------------------RCNN1, class loss-----------------------------------#
      cls1_score = self._predictions["cls1_score"]
      label1 = tf.reshape(self._proposal_targets["rpn1_rois_labels"], [-1])

      if repeat:
        cls1_score, label1 = self.repeat(cls1_score, label1, batch1, True)

      cross_entropy1 = tf.reduce_mean(
        tf.nn.sparse_softmax_cross_entropy_with_logits(
          logits=tf.reshape(cls1_score, [-1, self._num_classes]), labels=label1))


      #-----------------------------RCNN1_1, class loss-----------------------------------#
      cls_score_0 = self._predictions["cls0_score"]
      label = tf.reshape(self._proposal_targets["rpn_rois_labels"], [-1])

      if repeat:
        cls_score_0, label = self.repeat(cls_score_0, label, batch, True)

      cross_entropy0 = tf.reduce_mean(
        tf.nn.sparse_softmax_cross_entropy_with_logits(
          logits=tf.reshape(cls_score_0, [-1, self._num_classes]), labels=label))


      #-----------------------------RCNN, class loss-----------------------------------#
      cls_score = self._predictions["cls_score"]
      label = tf.reshape(self._proposal_targets["rpn_rois_labels"], [-1])

      if repeat:
        cls_score, label = self.repeat(cls_score, label, batch, True)

      cross_entropy = tf.reduce_mean(
        tf.nn.sparse_softmax_cross_entropy_with_logits(
          logits=tf.reshape(cls_score, [-1, self._num_classes]), labels=label))

      # RCNN, bbox loss
      bbox_pred = self._predictions['bbox_pred']
      bbox_targets = self._proposal_targets['rpn_rois_bbox_targets']
      bbox_inside_weights = self._proposal_targets['rpn_rois_bbox_inside_weights']
      bbox_outside_weights = self._proposal_targets['rpn_rois_bbox_outside_weights']

      loss_box = self._smooth_l1_loss(bbox_pred, bbox_targets, bbox_inside_weights, bbox_outside_weights)
      ########################################################################################################



      # RCNN Loss
      self._losses['cross_entropy'] = cross_entropy
      self._losses['loss_box'] = loss_box
      self._losses['cross_entropy0'] = cross_entropy0
      self._losses['cross_entropy1'] = cross_entropy1
      self._losses['cross_entropy2'] = cross_entropy2
      self._losses['cross_entropy3'] = cross_entropy3

      # RPN Loss
      self._losses['rpn_cross_entropy'] = rpn_cross_entropy
      self._losses['rpn_loss_box'] = rpn_loss_box
      self._losses['rpn0_cross_entropy'] = rpn0_cross_entropy
      self._losses['rpn1_cross_entropy'] = rpn1_cross_entropy
      self._losses['rpn2_cross_entropy'] = rpn2_cross_entropy
      self._losses['rpn3_cross_entropy'] = rpn3_cross_entropy


      #total loss
      loss = cross_entropy + cross_entropy0*0.1 + loss_box + rpn_cross_entropy + rpn0_cross_entropy*0.1 + rpn_loss_box + cross_entropy1*0.05 + cross_entropy2*0.025 + cross_entropy3*0.0125 + rpn1_cross_entropy*0.05 + rpn2_cross_entropy*0.025 + rpn3_cross_entropy*0.0125


      self._losses['total_loss'] = loss

      self._event_summaries.update(self._losses)

    return loss

  def create_architecture(self, sess, mode, num_classes, tag=None,
                          anchor_scales=(8, 16, 32), anchor_ratios=(0.5, 1, 2)):
    self._image = tf.placeholder(tf.float32, shape=[self._batch_size, None, None, 3])
    self._im_info = tf.placeholder(tf.float32, shape=[self._batch_size, 3])
    self._gt_boxes = tf.placeholder(tf.float32, shape=[None, 5])
    self._tag = tag

    self._num_classes = num_classes
    self._mode = mode
    self._anchor_scales = anchor_scales
    self._num_scales = len(anchor_scales)

    self._anchor_ratios = anchor_ratios
    self._num_ratios = len(anchor_ratios)

    self._num_anchors = self._num_scales * self._num_ratios

    training = mode == 'TRAIN'
    testing = mode == 'TEST'

    assert tag != None

    # handle most of the regularizers here
    weights_regularizer = tf.contrib.layers.l2_regularizer(cfg.TRAIN.WEIGHT_DECAY)
    if cfg.TRAIN.BIAS_DECAY:
      biases_regularizer = weights_regularizer
    else:
      biases_regularizer = tf.no_regularizer

    # list as many types of layers as possible, even if they are not used now
    with arg_scope([slim.conv2d, slim.conv2d_in_plane, \
                    slim.conv2d_transpose, slim.separable_conv2d, slim.fully_connected], 
                    weights_regularizer=weights_regularizer,
                    biases_regularizer=biases_regularizer, 
                    biases_initializer=tf.constant_initializer(0.0)): 
      rois, cls_prob, bbox_pred = self.build_network(sess, training)

    layers_to_output = {'rois': rois}
    layers_to_output.update(self._predictions)

    for var in tf.trainable_variables():
      self._train_summaries.append(var)

    if mode == 'TEST':
      stds = np.tile(np.array(cfg.TRAIN.BBOX_NORMALIZE_STDS), (self._num_classes))
      means = np.tile(np.array(cfg.TRAIN.BBOX_NORMALIZE_MEANS), (self._num_classes))
      self._predictions["bbox_pred"] *= stds
      self._predictions["bbox_pred"] += means
    else:
      self._add_losses()
      layers_to_output.update(self._losses)

    val_summaries = []
    with tf.device("/cpu:0"):
      val_summaries.append(self._add_image_summary(self._image, self._gt_boxes))
      for key, var in self._event_summaries.items():
        val_summaries.append(tf.summary.scalar(key, var))
      for key, var in self._score_summaries.items():
        # print(key)
        # print(var)
        self._add_score_summary(key, var)
      for var in self._act_summaries:
        self._add_act_summary(var)
      for var in self._train_summaries:
        self._add_train_summary(var)

    self._summary_op = tf.summary.merge_all()
    if not testing:
      self._summary_op_val = tf.summary.merge(val_summaries)

    return layers_to_output

  def get_variables_to_restore(self, variables, var_keep_dic):
    raise NotImplementedError

  def fix_variables(self, sess, pretrained_model):
    raise NotImplementedError

  # Extract the head feature maps, for example for vgg16 it is conv5_3
  # only useful during testing mode
  def extract_head(self, sess, image):
    feed_dict = {self._image: image}
    feat = sess.run(self._layers["head"], feed_dict=feed_dict)
    return feat

  # only useful during testing mode
  def test_image(self, sess, image, im_info):
    feed_dict = {self._image: image,
                 self._im_info: im_info}
    cls_score, cls_prob, bbox_pred, rois = sess.run([self._predictions["cls_score"],
                                                     self._predictions['cls_prob'],
                                                     self._predictions['bbox_pred'],
                                                     self._predictions['rois']],
                                                    feed_dict=feed_dict)
    return cls_score, cls_prob, bbox_pred, rois

  def get_summary(self, sess, blobs):
    feed_dict = {self._image: blobs['data'], self._im_info: blobs['im_info'],
                 self._gt_boxes: blobs['gt_boxes']}
    summary = sess.run(self._summary_op_val, feed_dict=feed_dict)

    return summary

  def train_step(self, sess, blobs, train_op):
    feed_dict = {self._image: blobs['data'], self._im_info: blobs['im_info'],
                 self._gt_boxes: blobs['gt_boxes']}
    rpn3_loss_cls, rpn2_loss_cls, rpn1_loss_cls, rpn0_loss_cls, loss_cls3, loss_cls2, loss_cls1, loss_cls0, rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss, _ = sess.run([self._losses["rpn3_cross_entropy"],
                                                                                                                                                      self._losses["rpn2_cross_entropy"],
                                                                                                                                                      self._losses["rpn1_cross_entropy"],
                                                                                                                                                      self._losses["rpn0_cross_entropy"],
                                                                                                                                                      self._losses["cross_entropy3"],
                                                                                                                                                      self._losses["cross_entropy2"],
                                                                                                                                                      self._losses["cross_entropy1"],
                                                                                                                                                      self._losses['cross_entropy0'],
                                                                                                                                                      self._losses["rpn_cross_entropy"],
                                                                                                                                                      self._losses['rpn_loss_box'],
                                                                                                                                                      self._losses['cross_entropy'],
                                                                                                                                                      self._losses['loss_box'],
                                                                                                                                                      self._losses['total_loss'],
                                                                                                                                                      train_op],
                                                                                                                                                      feed_dict=feed_dict)
    return rpn3_loss_cls, rpn2_loss_cls, rpn1_loss_cls, rpn0_loss_cls, loss_cls3, loss_cls2, loss_cls1, loss_cls0, rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss


  def train_step_with_summary(self, sess, blobs, train_op):
    feed_dict = {self._image: blobs['data'], self._im_info: blobs['im_info'],
                 self._gt_boxes: blobs['gt_boxes']}

    rpn3_loss_cls, rpn2_loss_cls, rpn1_loss_cls, rpn0_loss_cls, loss_cls3, loss_cls2, loss_cls1, loss_cls0, rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss, summary, _ = sess.run([self._losses["rpn3_cross_entropy"],
                                                                                                                                                               self._losses["rpn2_cross_entropy"],
                                                                                                                                                               self._losses["rpn1_cross_entropy"],
                                                                                                                                                               self._losses["rpn0_cross_entropy"],
                                                                                                                                                               self._losses["cross_entropy3"],
                                                                                                                                                               self._losses["cross_entropy2"],
                                                                                                                                                               self._losses["cross_entropy1"],
                                                                                                                                                               self._losses["cross_entropy0"],
                                                                                                                                                               self._losses["rpn_cross_entropy"],
                                                                                                                                                               self._losses['rpn_loss_box'],
                                                                                                                                                               self._losses['cross_entropy'],
                                                                                                                                                               self._losses['loss_box'],
                                                                                                                                                               self._losses['total_loss'],
                                                                                                                                                               self._summary_op,
                                                                                                                                                               train_op],
                                                                                                                                                               feed_dict=feed_dict)
    return rpn3_loss_cls, rpn2_loss_cls, rpn1_loss_cls, rpn0_loss_cls, loss_cls3, loss_cls2, loss_cls1, loss_cls0, rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss, summary


  def train_step_no_return(self, sess, blobs, train_op):
    feed_dict = {self._image: blobs['data'], self._im_info: blobs['im_info'],
                 self._gt_boxes: blobs['gt_boxes']}
    sess.run([train_op], feed_dict=feed_dict)







